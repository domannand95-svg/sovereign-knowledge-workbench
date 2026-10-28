"""Owned publication of the companion session file.

Required properties (R3 diagnostics, 28 Oct 2026):

* access is restricted to the owning user before the first token byte is
  written: POSIX creates the file with mode 0600 in the creating call;
  Windows creates it with a protected DACL in the creating ``CreateFileW``;
* that restriction is verified through the created descriptor/handle before
  writing; any mismatch publishes nothing;
* exclusive creation is kept, and a link at the target is never followed;
* cleanup removes only the object this call created, and cleanup failures
  are reported rather than hidden;
* the parent directory must be a trusted location, otherwise publication is
  refused (fail closed). Folder permissions are never changed here.

The Windows native layer (``WindowsNative``) has not been executed by the
author of this change; its behaviour must be qualified on Windows.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PureWindowsPath


class SessionPublicationError(OSError):
    """Publication refused or failed; no session file was published."""


class UntrustedLocationError(SessionPublicationError):
    """The parent directory, or one of its ancestors, is not trusted."""


def encode_session(session: dict) -> bytes:
    """Byte-for-byte the format previously written by ``companion``."""
    return json.dumps(session, sort_keys=True).encode("utf-8")


def _reject_traversal(path: Path) -> None:
    # ".." is refused before any normalization: lexical collapsing (abspath)
    # could otherwise hide a link on the real route ("link/../session.json").
    if ".." in path.parts:
        raise UntrustedLocationError(f"parent-traversal components are not supported: {path}")


def publish(path: Path, session: dict, *, native=None) -> None:
    path = Path(path)
    _reject_traversal(path)
    data = encode_session(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        publish_windows(path, data, native if native is not None else WindowsNative())
    else:
        publish_posix(path, data)


def _note_cleanup(error: BaseException, problem: str) -> None:
    try:
        error.add_note(problem)
    except AttributeError:  # Python < 3.11
        error.args = tuple(error.args) + (problem,)


# --------------------------------------------------------------------- POSIX

_POSIX_ANY_WRITE = stat.S_IWGRP | stat.S_IWOTH


def _check_posix_ancestors(parent: Path, euid: int) -> None:
    # Every ancestor must be unmodifiable by other users, except root-owned
    # sticky directories such as /tmp (entries there are protected by owner).
    for ancestor in parent.parents:
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode):
            raise UntrustedLocationError(f"ancestor is a symbolic link: {ancestor}")
        sticky_root = bool(st.st_mode & stat.S_ISVTX) and st.st_uid == 0
        if st.st_uid not in (0, euid) and not sticky_root:
            raise UntrustedLocationError(f"ancestor owned by another user: {ancestor}")
        if st.st_mode & _POSIX_ANY_WRITE and not sticky_root:
            raise UntrustedLocationError(f"ancestor writable by other users: {ancestor}")


def _check_posix_parent(dir_fd: int, euid: int, parent: Path) -> None:
    st = os.fstat(dir_fd)
    if not stat.S_ISDIR(st.st_mode):
        raise UntrustedLocationError(f"parent is not a directory: {parent}")
    if st.st_uid != euid:
        raise UntrustedLocationError(f"parent not owned by the publishing user: {parent}")
    if st.st_mode & _POSIX_ANY_WRITE:
        raise UntrustedLocationError(f"parent writable by other users: {parent}")


def _verify_posix_file(fd: int, euid: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise SessionPublicationError("created object is not a regular file")
    if st.st_uid != euid:
        raise SessionPublicationError("created file is not owned by the publishing user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SessionPublicationError(
            f"created file mode {oct(stat.S_IMODE(st.st_mode))} grants group/other access")
    if st.st_nlink != 1:
        raise SessionPublicationError("created file has unexpected hard links")


def _posix_cleanup(dir_fd: int, name: str, fd: int, error: BaseException) -> None:
    try:
        mine = os.fstat(fd)
        try:
            present = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (present.st_dev, present.st_ino) != (mine.st_dev, mine.st_ino):
            _note_cleanup(error, f"cleanup skipped: {name} was replaced; the replacement was not removed")
            return
        os.unlink(name, dir_fd=dir_fd)
    except OSError as cleanup_error:
        _note_cleanup(error, f"cleanup of {name} failed: {cleanup_error!r}")


def _close_reporting(fd: int, what: str, pending: BaseException | None) -> BaseException | None:
    """Close once (never retried). A close error never masks an earlier error;
    it is attached to it, or returned when there is no earlier error."""
    try:
        os.close(fd)
    except OSError as close_error:
        if pending is not None:
            _note_cleanup(pending, f"closing {what} also failed: {close_error!r}")
            return None
        return close_error
    return None


def publish_posix(path: Path, data: bytes) -> None:
    _reject_traversal(path)
    euid = os.geteuid()
    parent = Path(os.path.abspath(path.parent))
    name = path.name
    if name in ("", ".", "..") or "/" in name:
        raise SessionPublicationError(f"invalid session file name: {path}")
    _check_posix_ancestors(parent, euid)
    try:
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise UntrustedLocationError(f"parent directory cannot be opened without following links: {parent}") from error
    pending: BaseException | None = None
    try:
        _check_posix_parent(dir_fd, euid, parent)
        # Mode 0600 at creation: the umask can only remove bits. A failed
        # exclusive creation raises here with nothing to clean up.
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dir_fd)
        mine = os.fstat(fd)
        try:
            _verify_posix_file(fd, euid)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException as error:
            pending = error
            _posix_cleanup(dir_fd, name, fd, error)
        close_error = _close_reporting(fd, name, pending)
        if close_error is not None:
            # Bytes were written but the descriptor's close was reported as failed:
            # treat publication as failed and remove our file by identity (the
            # descriptor is not touched again). Removal is attempted, not promised.
            pending = SessionPublicationError(f"closing the session file failed after writing: {close_error!r}")
            pending.__cause__ = close_error
            _posix_cleanup_by_identity(dir_fd, name, mine, pending)
    except BaseException as error:
        if pending is None:
            pending = error
    finally:
        # A parent-directory close error is attached to any earlier error. After a
        # completed publication it is not raised: the file is in place and the
        # caller must be able to rely on (and later remove) it.
        _close_reporting(dir_fd, "the parent directory", pending)
    if pending is not None:
        raise pending


def _posix_cleanup_by_identity(dir_fd: int, name: str, mine: os.stat_result, error: BaseException) -> None:
    try:
        try:
            present = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (present.st_dev, present.st_ino) != (mine.st_dev, mine.st_ino):
            _note_cleanup(error, f"cleanup skipped: {name} was replaced; the replacement was not removed")
            return
        os.unlink(name, dir_fd=dir_fd)
    except OSError as cleanup_error:
        _note_cleanup(error, f"cleanup of {name} failed; the file may remain: {cleanup_error!r}")


# ------------------------------------------------------------------- Windows

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"  # represents the object's current owner
# NT SERVICE\TrustedInstaller (Windows servicing); exact service SID, not a name or all services.
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
ACCESS_ALLOWED_ACE_TYPE = 0
# Access-denied ACE types never grant rights; ignoring them only makes the check
# stricter. Every other ACE type (conditional/callback, object, compound, ...)
# may grant rights and is not interpreted here, so its presence refuses trust.
ACCESS_DENIED_ACE_TYPES = frozenset({1, 6, 10, 12})
INHERIT_ONLY_ACE = 0x08
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
SESSION_FILE_MASK = FILE_GENERIC_READ | FILE_GENERIC_WRITE  # icacls (R,W) == SDDL "FRFW"
DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000
FILE_WRITE_DATA = 0x0002      # FILE_ADD_FILE on directories
FILE_APPEND_DATA = 0x0004     # FILE_ADD_SUBDIRECTORY on directories
FILE_WRITE_EA = 0x0010
FILE_DELETE_CHILD = 0x0040
FILE_WRITE_ATTRIBUTES = 0x0100
PARENT_MODIFY_RIGHTS = (FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_DELETE_CHILD
                        | FILE_WRITE_ATTRIBUTES | DELETE | WRITE_DAC | WRITE_OWNER
                        | GENERIC_ALL | GENERIC_WRITE)
ANCESTOR_MODIFY_RIGHTS = FILE_DELETE_CHILD | DELETE | WRITE_DAC | WRITE_OWNER | GENERIC_ALL | GENERIC_WRITE


def session_file_sddl(user_sid: str) -> str:
    return f"O:{user_sid}D:P(A;;FRFW;;;{user_sid})"


def verify_windows_file_security(security: dict, user_sid: str) -> None:
    """``security`` as returned by ``WindowsNative.handle_security``."""
    if not security.get("dacl_present") or security.get("dacl_null"):
        raise SessionPublicationError("session file DACL missing or NULL")
    if not security.get("dacl_protected"):
        raise SessionPublicationError("session file DACL is not protected from inheritance")
    if security.get("owner") != user_sid:
        raise SessionPublicationError("session file owner is not the publishing user")
    aces = security.get("aces")
    expected = [(ACCESS_ALLOWED_ACE_TYPE, 0, SESSION_FILE_MASK, user_sid)]
    if aces != expected:
        raise SessionPublicationError(f"session file DACL differs from the single intended grant: {aces!r}")


def check_windows_location(chain: list[dict], user_sid: str) -> None:
    """``chain``: parent first, then each ancestor up to the volume root.

    Each entry: {"path", "owner", "dacl_present", "dacl_null", "aces", "is_root", "reparse"}.
    Any reparse point (junction, symlink, mount point) in the chain is refused.
    Conservative: deny ACEs and group membership are not credited.
    """
    # Directory trust only: owners, and principals that receive rights on directories.
    # The session file's own DACL (verify_windows_file_security) is unaffected.
    trusted = {user_sid, SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID}
    for index, entry in enumerate(chain):
        where = entry.get("path")
        if not entry.get("dacl_present") or entry.get("dacl_null"):
            raise UntrustedLocationError(f"directory has a missing or NULL DACL: {where}")
        if entry.get("reparse") is not False:
            raise UntrustedLocationError(f"redirected (reparse-point) or unchecked directory is not supported: {where}")
        if entry.get("owner") not in trusted:
            raise UntrustedLocationError(f"directory owned by an untrusted principal: {where}")
        rights = PARENT_MODIFY_RIGHTS if index == 0 else ANCESTOR_MODIFY_RIGHTS
        if index > 0 and entry.get("is_root"):
            rights &= ~DELETE  # a volume root cannot be renamed
        for ace_type, flags, mask, sid in entry.get("aces", []):
            if ace_type in ACCESS_DENIED_ACE_TYPES:
                continue
            if ace_type != ACCESS_ALLOWED_ACE_TYPE:
                raise UntrustedLocationError(f"unsupported ACE type {ace_type} on directory: {where}")
            if flags & INHERIT_ONLY_ACE or sid in trusted:
                continue
            # OWNER RIGHTS applies to whoever owns this same directory. It is credited
            # only because that owner was verified as trusted above; it is never a
            # blanket trusted SID and never applies to the session file's own DACL.
            if sid == OWNER_RIGHTS_SID and entry.get("owner") in trusted:
                continue
            if mask & rights:
                raise UntrustedLocationError(
                    f"{sid} may modify {'the parent directory' if index == 0 else 'an ancestor'}: {where}")


def publish_windows(path: Path, data: bytes, native) -> None:
    _reject_traversal(path)
    user_sid = native.token_user_sid()
    if os.name == "nt":
        parent = Path(os.path.abspath(path.parent))
    else:  # simulation with a fake native layer on a non-Windows host
        parent = PureWindowsPath(str(path)).parent
    drive = parent.drive
    if not (len(drive) == 2 and drive[1] == ":"):
        raise UntrustedLocationError(f"only local drive-letter paths are supported: {parent}")
    check_windows_location(native.directory_chain(parent), user_sid)
    # Create exactly inside the checked parent. Failed exclusive creation raises
    # here (FileExistsError) with no cleanup.
    handle = native.create_new(str(parent / path.name), session_file_sddl(user_sid))
    try:
        verify_windows_file_security(native.handle_security(handle), user_sid)
        native.write_all(handle, data)
        native.flush(handle)
    except BaseException as error:
        # Delete exactly the object behind our handle; a replacement at the
        # same path is never touched. Deletion completes when the handle closes.
        try:
            native.delete_by_handle(handle)
        except OSError as cleanup_error:
            _note_cleanup(error, f"cleanup of {path} failed: {cleanup_error!r}")
        try:
            native.close(handle)
        except OSError as close_error:
            _note_cleanup(error, f"closing {path} after failure failed: {close_error!r}")
        raise
    try:
        native.close(handle)
    except OSError as close_error:
        # The handle's state is unknown; it is not used again (no retry, no
        # delete by handle or by path). The written file may remain.
        error = SessionPublicationError(
            f"closing the session file failed after writing; the file may remain: {close_error!r}")
        raise error from close_error


class WindowsNative:
    """ctypes binding. NOT executed by the author; qualify on Windows."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        if os.name != "nt":
            raise RuntimeError("Windows required")
        self.c, self.w = c, w
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.a = c.WinDLL("advapi32", use_last_error=True)

        class SECURITY_ATTRIBUTES(c.Structure):
            _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p), ("bInheritHandle", w.BOOL)]

        class ACL_SIZE_INFORMATION(c.Structure):
            _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD)]

        class ACE_HEADER(c.Structure):
            _fields_ = [("AceType", c.c_ubyte), ("AceFlags", c.c_ubyte), ("AceSize", w.WORD)]

        class FILE_DISPOSITION_INFO(c.Structure):
            _fields_ = [("DeleteFile", w.BOOLEAN)]

        self.SA, self.ASI, self.AH, self.FDI = SECURITY_ATTRIBUTES, ACL_SIZE_INFORMATION, ACE_HEADER, FILE_DISPOSITION_INFO
        k, a = self.k, self.a
        k.GetCurrentProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]; k.CloseHandle.restype = w.BOOL
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]; k.WriteFile.restype = w.BOOL
        k.FlushFileBuffers.argtypes = [w.HANDLE]; k.FlushFileBuffers.restype = w.BOOL
        k.SetFileInformationByHandle.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.SetFileInformationByHandle.restype = w.BOOL
        k.LocalFree.argtypes = [c.c_void_p]; k.LocalFree.restype = c.c_void_p
        k.GetFileAttributesW.argtypes = [w.LPCWSTR]; k.GetFileAttributesW.restype = w.DWORD
        a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]; a.OpenProcessToken.restype = w.BOOL
        a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        a.GetTokenInformation.restype = w.BOOL
        a.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]; a.ConvertSidToStringSidW.restype = w.BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.ULONG)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
        a.GetSecurityInfo.argtypes = [w.HANDLE, c.c_int, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
                                      c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)]
        a.GetSecurityInfo.restype = w.DWORD
        a.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, c.c_int, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
                                            c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)]
        a.GetNamedSecurityInfoW.restype = w.DWORD
        a.GetSecurityDescriptorControl.argtypes = [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)]
        a.GetSecurityDescriptorControl.restype = w.BOOL
        a.GetAclInformation.argtypes = [c.c_void_p, c.c_void_p, w.DWORD, c.c_int]; a.GetAclInformation.restype = w.BOOL
        a.GetAce.argtypes = [c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]; a.GetAce.restype = w.BOOL

    def _ok(self, result):
        if not result:
            raise self.c.WinError(self.c.get_last_error())

    def _sid_string(self, psid) -> str:
        out = self.w.LPWSTR()
        self._ok(self.a.ConvertSidToStringSidW(psid, self.c.byref(out)))
        try:
            return out.value
        finally:
            self.k.LocalFree(out)

    def token_user_sid(self) -> str:
        c, w = self.c, self.w
        token = w.HANDLE()
        self._ok(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x0008, c.byref(token)))  # TOKEN_QUERY
        try:
            needed = w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, c.byref(needed))  # TokenUser
            buffer = c.create_string_buffer(needed.value)
            self._ok(self.a.GetTokenInformation(token, 1, buffer, needed, c.byref(needed)))
            psid = c.c_void_p.from_buffer(buffer).value  # TOKEN_USER.User.Sid
            return self._sid_string(psid)
        finally:
            self.k.CloseHandle(token)

    def create_new(self, path: str, sddl: str):
        c, w = self.c, self.w
        psd = c.c_void_p()
        self._ok(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(psd), None))
        try:
            sa = self.SA(c.sizeof(self.SA), psd, False)  # handle not inheritable
            handle = self.k.CreateFileW(path, GENERIC_WRITE | 0x00020000 | DELETE,  # + READ_CONTROL, DELETE
                                        0, c.byref(sa), 1, 0x80, None)  # no sharing, CREATE_NEW, NORMAL
            if handle is None or handle == w.HANDLE(-1).value:
                error = c.get_last_error()
                if error in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                    raise FileExistsError(error, "session file already exists", path)
                raise c.WinError(error)
            return handle
        finally:
            self.k.LocalFree(psd)

    def _parse(self, owner, dacl, psd) -> dict:
        c, w = self.c, self.w
        control, revision = w.WORD(), w.DWORD()
        self._ok(self.a.GetSecurityDescriptorControl(psd, c.byref(control), c.byref(revision)))
        result = {"owner": self._sid_string(owner) if owner else None,
                  "dacl_present": bool(control.value & 0x0004), "dacl_null": not dacl,
                  "dacl_protected": bool(control.value & 0x1000), "aces": []}
        if dacl:
            info = self.ASI()
            self._ok(self.a.GetAclInformation(dacl, c.byref(info), c.sizeof(info), 2))  # AclSizeInformation
            for index in range(info.AceCount):
                pace = c.c_void_p()
                self._ok(self.a.GetAce(dacl, index, c.byref(pace)))
                header = self.AH.from_address(pace.value)
                mask = w.DWORD.from_address(pace.value + 4).value
                sid = self._sid_string(pace.value + 8) if header.AceType in (0, 1) else None
                result["aces"].append((header.AceType, header.AceFlags, mask, sid))
        return result

    def handle_security(self, handle) -> dict:
        c = self.c
        owner, dacl, psd = c.c_void_p(), c.c_void_p(), c.c_void_p()
        error = self.a.GetSecurityInfo(handle, 1, 0x1 | 0x4, c.byref(owner), None, c.byref(dacl), None, c.byref(psd))
        if error:
            raise c.WinError(error)
        try:
            return self._parse(owner.value, dacl.value, psd.value)
        finally:
            self.k.LocalFree(psd)

    def directory_chain(self, parent: Path) -> list[dict]:
        c = self.c
        chain = []
        for directory in [parent, *parent.parents]:
            owner, dacl, psd = c.c_void_p(), c.c_void_p(), c.c_void_p()
            error = self.a.GetNamedSecurityInfoW(str(directory), 1, 0x1 | 0x4, c.byref(owner), None,
                                                 c.byref(dacl), None, c.byref(psd))
            if error:
                raise c.WinError(error)
            try:
                entry = self._parse(owner.value, dacl.value, psd.value)
            finally:
                self.k.LocalFree(psd)
            entry["path"] = str(directory)
            entry["is_root"] = directory.parent == directory
            attributes = self.k.GetFileAttributesW(str(directory))
            if attributes == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
                raise self.c.WinError(self.c.get_last_error())
            entry["reparse"] = bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
            chain.append(entry)
        return chain

    def write_all(self, handle, data: bytes) -> None:
        c, w = self.c, self.w
        view = memoryview(data)
        while view:
            chunk = bytes(view[:1 << 20])
            written = w.DWORD()
            self._ok(self.k.WriteFile(handle, chunk, len(chunk), c.byref(written), None))
            if written.value == 0:
                raise OSError("WriteFile wrote no bytes")
            view = view[written.value:]

    def flush(self, handle) -> None:
        self._ok(self.k.FlushFileBuffers(handle))

    def delete_by_handle(self, handle) -> None:
        info = self.FDI(True)
        self._ok(self.k.SetFileInformationByHandle(handle, 4, self.c.byref(info), self.c.sizeof(info)))  # FileDispositionInfo

    def close(self, handle) -> None:
        self._ok(self.k.CloseHandle(handle))
