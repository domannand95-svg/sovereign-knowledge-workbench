"""Regression tests for owned session-file publication (R3 diagnostics A/B).

POSIX tests exercise the real filesystem. Windows tests use a SIMULATED native
layer (FakeNative): they check ordering and verification logic only and do not
qualify the ctypes binding, which must be qualified on Windows.
"""
import json
import os
import stat
from pathlib import Path

import pytest

from sovereign_workbench import session_publication as sp

SESSION = {"token": "synthetic-token-not-a-secret", "authority": "none", "url": "http://127.0.0.1:1"}
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem mechanism")


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def track_session_writes(monkeypatch, name="session.json", swallow=False):
    """Record writes made to descriptors opened for ``name`` only (other writers,
    e.g. logging or test instrumentation, are ignored)."""
    record = {"fds": set(), "writes": [], "modes": []}
    real_open, real_write = os.open, os.write
    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.path.basename(os.fsdecode(path)) == name:
            record["fds"].add(fd)
        return fd
    def tracking_write(fd, data):
        if fd in record["fds"]:
            record["writes"].append(bytes(data))
            record["modes"].append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)
    monkeypatch.setattr(sp.os, "open", tracking_open)
    monkeypatch.setattr(sp.os, "write", tracking_write)
    return record


# ---------------------------------------------------------------- POSIX, real

@posix_only
def test_mode_is_0600_before_first_token_byte_under_permissive_umask(tmp_path, monkeypatch):
    parent = tmp_path / "pub"; parent.mkdir(); parent.chmod(0o755)
    record = track_session_writes(monkeypatch)
    old = os.umask(0o022)
    try:
        sp.publish(parent / "session.json", SESSION)
    finally:
        os.umask(old)
    assert record["writes"] and all(mode == 0o600 for mode in record["modes"])
    assert stat.S_IMODE(os.stat(parent / "session.json").st_mode) == 0o600


@posix_only
def test_published_bytes_match_previous_format_and_same_user_can_read(tmp_path):
    target = private_dir(tmp_path / "pub") / "session.json"
    sp.publish(target, SESSION)
    assert target.read_bytes() == json.dumps(SESSION, sort_keys=True).encode("utf-8")
    assert json.loads(target.read_text(encoding="utf-8")) == SESSION


@posix_only
def test_existing_file_refused_unchanged_and_no_bytes_written(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    target.write_bytes(b"existing-record")
    record = track_session_writes(monkeypatch)
    with pytest.raises(FileExistsError):
        sp.publish(target, SESSION)
    assert target.read_bytes() == b"existing-record" and record["writes"] == []


@posix_only
def test_link_at_target_is_not_followed_and_link_target_unchanged(tmp_path):
    parent = private_dir(tmp_path / "pub")
    victim = parent / "victim.txt"; victim.write_bytes(b"victim")
    (parent / "session.json").symlink_to(victim)
    with pytest.raises(FileExistsError):
        sp.publish(parent / "session.json", SESSION)
    assert victim.read_bytes() == b"victim" and (parent / "session.json").is_symlink()


@posix_only
@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_parent_writable_by_others_is_refused_without_creating(tmp_path, mode):
    parent = tmp_path / "pub"; parent.mkdir(); parent.chmod(mode)
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(parent / "session.json", SESSION)
    assert not (parent / "session.json").exists()


@posix_only
def test_parent_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    parent = private_dir(tmp_path / "pub")
    real = os.geteuid()
    monkeypatch.setattr(sp.os, "geteuid", lambda: real + 1)
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(parent / "session.json", SESSION)
    assert not (parent / "session.json").exists()


@posix_only
def test_ancestor_writable_by_others_without_sticky_is_refused(tmp_path):
    open_ancestor = tmp_path / "shared"; open_ancestor.mkdir(); open_ancestor.chmod(0o777)
    parent = private_dir(open_ancestor / "pub")
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(parent / "session.json", SESSION)
    assert not (parent / "session.json").exists()


@posix_only
def test_symlinked_parent_or_ancestor_is_refused(tmp_path):
    real = private_dir(tmp_path / "real")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(tmp_path / "alias" / "session.json", SESSION)
    private_dir(real / "inner")
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(tmp_path / "alias" / "inner" / "session.json", SESSION)
    assert not list(real.rglob("session.json"))


@posix_only
def test_failed_verification_writes_nothing_and_removes_own_file(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    record = track_session_writes(monkeypatch)
    def refuse(fd, euid): raise sp.SessionPublicationError("synthetic verification failure")
    monkeypatch.setattr(sp, "_verify_posix_file", refuse)
    with pytest.raises(sp.SessionPublicationError):
        sp.publish(target, SESSION)
    assert record["fds"] and record["writes"] == [] and not target.exists()


@posix_only
def test_verification_rejects_group_or_other_bits(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    real_open = os.open
    def widening_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        if name == "session.json":
            os.fchmod(fd, 0o640)  # simulate a creation that did not restrict
        return fd
    monkeypatch.setattr(sp.os, "open", widening_open)
    with pytest.raises(sp.SessionPublicationError, match="group/other"):
        sp.publish(target, SESSION)
    assert not target.exists()


@posix_only
def test_cleanup_never_removes_a_replacement_and_reports_it(tmp_path, monkeypatch):
    parent = private_dir(tmp_path / "pub"); target = parent / "session.json"
    def replace_then_fail(fd, euid):
        target.rename(parent / "ours-moved.json")
        target.write_bytes(b"replacement")
        raise sp.SessionPublicationError("synthetic failure after replacement")
    monkeypatch.setattr(sp, "_verify_posix_file", replace_then_fail)
    with pytest.raises(sp.SessionPublicationError) as caught:
        sp.publish(target, SESSION)
    assert target.read_bytes() == b"replacement"
    assert any("replaced" in note for note in getattr(caught.value, "__notes__", []))


@posix_only
def test_cleanup_failure_is_reported_not_hidden(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    def refuse(fd, euid): raise sp.SessionPublicationError("synthetic failure")
    def unlink_fails(*a, **k): raise PermissionError("synthetic unlink failure")
    monkeypatch.setattr(sp, "_verify_posix_file", refuse)
    monkeypatch.setattr(sp.os, "unlink", unlink_fails)
    with pytest.raises(sp.SessionPublicationError) as caught:
        sp.publish(target, SESSION)
    assert any("cleanup" in note and "failed" in note for note in getattr(caught.value, "__notes__", []))


@posix_only
def test_write_failure_removes_partial_file(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    def fsync_fails(fd): raise OSError("synthetic write failure")
    monkeypatch.setattr(sp.os, "fsync", fsync_fails)
    with pytest.raises(OSError, match="synthetic write failure"):
        sp.publish(target, SESSION)
    assert not target.exists()


# ------------------------------------------------------ Windows, SIMULATED

USER = "S-1-5-21-1111-2222-3333-1001"
OTHER = "S-1-5-21-9999-8888-7777-1234"
USERS = "S-1-5-32-545"
GOOD = {"owner": USER, "dacl_present": True, "dacl_null": False, "dacl_protected": True,
        "aces": [(0, 0, sp.SESSION_FILE_MASK, USER)]}


def directory(path, aces, owner=USER, root=False, null=False, reparse=False):
    return {"path": path, "owner": owner, "dacl_present": True, "dacl_null": null, "aces": aces, "is_root": root,
            "reparse": reparse}


TRUSTED_CHAIN = [
    directory("C:\\Users\\u\\AppData\\Local\\Temp\\pub", [(0, 0, 0x1F01FF, USER), (0, 0, 0x1F01FF, sp.SYSTEM_SID)]),
    directory("C:\\Users\\u", [(0, 0, 0x1F01FF, USER), (0, 0, 0x1F01FF, sp.ADMINISTRATORS_SID)]),
    directory("C:\\", [(0, 0, 0x1200A9, USERS), (0, sp.INHERIT_ONLY_ACE, 0x1301BF, "S-1-5-11"),
                       (0, 0, 0x0004, "S-1-5-11")], owner=sp.SYSTEM_SID, root=True),
]


class FakeNative:
    """SIMULATED Windows native layer recording call order."""

    def __init__(self, security=None, chain=None, create_error=None, delete_error=None):
        self.events, self.written = [], b""
        self.security = GOOD if security is None else security
        self.chain = TRUSTED_CHAIN if chain is None else chain
        self.create_error, self.delete_error = create_error, delete_error
        self.sddl = None

    drive = {"type": 3, "target": "\\Device\\HarddiskVolume3"}
    def token_user_sid(self): self.events.append("sid"); return USER
    def drive_info(self, drive): self.events.append("drive"); return self.drive
    def directory_chain(self, parent): self.events.append("chain"); return self.chain
    def file_identity(self, handle): self.events.append("identity"); return (1, handle)
    def create_new(self, path, sddl):
        self.events.append("create"); self.sddl = sddl
        if self.create_error: raise self.create_error
        return 7
    def handle_security(self, handle): self.events.append("verify"); return self.security
    def write_all(self, handle, data): self.events.append("write"); self.written += data
    def flush(self, handle): self.events.append("flush")
    def delete_by_handle(self, handle):
        self.events.append("delete")
        if self.delete_error: raise self.delete_error
    def close(self, handle): self.events.append("close")


def test_windows_simulated_order_restricts_and_verifies_before_any_write(monkeypatch):
    monkeypatch.setenv("USERNAME", "not-used"); monkeypatch.setenv("USERDOMAIN", "not-used")
    native = FakeNative()
    receipt = sp.publish_windows(Path("C:/x/session.json"), sp.encode_session(SESSION), native)
    assert native.events == ["sid", "drive", "chain", "create", "verify", "write", "flush", "identity", "close"]
    assert receipt == (1, 7)
    assert native.sddl == f"O:{USER}D:P(A;;FRFW;;;{USER})"
    assert native.written == json.dumps(SESSION, sort_keys=True).encode("utf-8")


def test_session_file_mask_equals_previous_icacls_read_write():
    assert sp.SESSION_FILE_MASK == 0x0012019F


@pytest.mark.parametrize("change", [
    {"dacl_protected": False},
    {"dacl_present": False},
    {"dacl_null": True},
    {"owner": OTHER},
    {"aces": [(0, 0, sp.SESSION_FILE_MASK, USER), (0, 0x10, 0x1200A9, USERS)]},   # inherited extra grant
    {"aces": [(0, 0x10, sp.SESSION_FILE_MASK, USER)]},                           # inherited flag
    {"aces": [(0, 0, 0x1F01FF, USER)]},                                          # broader mask
    {"aces": [(0, 0, sp.SESSION_FILE_MASK, OTHER)]},                             # wrong SID
    {"aces": []},
])
def test_windows_simulated_verification_mismatch_publishes_nothing(change):
    native = FakeNative(security={**GOOD, **change})
    with pytest.raises(sp.SessionPublicationError):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert "write" not in native.events and native.written == b""
    assert native.events[-2:] == ["delete", "close"]


@pytest.mark.parametrize("error", [FileExistsError(80, "exists"), OSError(5, "synthetic native creation failure"),
                                   OSError(1337, "synthetic security descriptor construction failure")])
def test_windows_simulated_creation_failure_touches_nothing(error):
    native = FakeNative(create_error=error)
    with pytest.raises(type(error)):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert native.events == ["sid", "drive", "chain", "create"]


def test_windows_simulated_cleanup_failure_is_reported():
    native = FakeNative(security={**GOOD, "dacl_protected": False}, delete_error=OSError(5, "synthetic delete failure"))
    with pytest.raises(sp.SessionPublicationError) as caught:
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert any("cleanup" in note for note in getattr(caught.value, "__notes__", []))
    assert native.events[-1] == "close" and native.written == b""


@pytest.mark.parametrize("chain", [
    [directory("P", [(0, 0, 0x1301BF, OTHER)])] + TRUSTED_CHAIN[1:],                       # Modify on parent
    [directory("P", [(0, 0, 0x0002, USERS)])] + TRUSTED_CHAIN[1:],                         # add-file on parent
    [directory("P", [], owner=OTHER)] + TRUSTED_CHAIN[1:],                                 # untrusted owner
    [directory("P", [], null=True)] + TRUSTED_CHAIN[1:],                                   # NULL DACL
    [TRUSTED_CHAIN[0], directory("A", [(0, 0, 0x1301BF, OTHER)])] + TRUSTED_CHAIN[2:],     # Modify (DELETE) on ancestor
    [TRUSTED_CHAIN[0], directory("A", [(0, 0, sp.FILE_DELETE_CHILD, OTHER)])] + TRUSTED_CHAIN[2:],
])
def test_windows_simulated_untrusted_location_refused_before_creation(chain):
    native = FakeNative(chain=chain)
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert "create" not in native.events and native.written == b""


def test_windows_simulated_read_only_and_inherit_only_grants_do_not_block():
    sp.check_windows_location(TRUSTED_CHAIN, USER)  # Users RX, inherit-only Modify, root add-subdirectory


OWNER_RIGHTS = "S-1-3-4"


def test_windows_simulated_owner_rights_accepted_only_with_verified_trusted_owner():
    # Python's os.mkdir(..., 0o700) on Windows produces an explicit DACL including OWNER RIGHTS.
    parent = directory("bt", [(0, 0, 0x1F01FF, OWNER_RIGHTS), (0, 0, 0x1F01FF, sp.SYSTEM_SID),
                              (0, 0, 0x1F01FF, sp.ADMINISTRATORS_SID)], owner=USER)
    sp.check_windows_location([parent] + TRUSTED_CHAIN[1:], USER)
    for owner in (OTHER, USERS):
        with pytest.raises(sp.UntrustedLocationError):
            sp.check_windows_location([{**parent, "owner": owner}] + TRUSTED_CHAIN[1:], USER)


def test_windows_simulated_owner_rights_on_ancestor_follows_that_ancestors_owner():
    ancestor = directory("A", [(0, 0, 0x1F01FF, OWNER_RIGHTS)], owner=USER)
    sp.check_windows_location([TRUSTED_CHAIN[0], ancestor] + TRUSTED_CHAIN[2:], USER)
    with pytest.raises(sp.UntrustedLocationError):
        sp.check_windows_location([TRUSTED_CHAIN[0], {**ancestor, "owner": OTHER}] + TRUSTED_CHAIN[2:], USER)


def test_windows_simulated_session_file_dacl_still_exact_with_owner_rights_present():
    native = FakeNative(security={**GOOD, "aces": GOOD["aces"] + [(0, 0, sp.SESSION_FILE_MASK, OWNER_RIGHTS)]})
    with pytest.raises(sp.SessionPublicationError):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert "write" not in native.events


TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
CODEX_SANDBOX_OFFLINE = "S-1-5-21-1274397259-2285404289-3631297971-1006"
CODEX_SANDBOX_USERS = "S-1-5-21-1274397259-2285404289-3631297971-1005"
# Shape recorded on the owner's laptop (Windows run v3): C:\ owned by TrustedInstaller.
LAPTOP_ROOT = directory("C:\\", [(0, 3, 0x1F01FF, sp.ADMINISTRATORS_SID), (0, 3, 0x1F01FF, sp.SYSTEM_SID),
                                   (0, 3, 0x1200A9, USERS), (0, 11, 0xE0010000, "S-1-5-11"), (0, 0, 0x4, "S-1-5-11")],
                        owner=TRUSTED_INSTALLER, root=True)


def test_windows_simulated_trusted_installer_owned_root_is_accepted():
    profile = directory("C:\\Users\\u", [(0, 3, 0x1F01FF, USER)], owner=sp.ADMINISTRATORS_SID)
    local = directory("C:\\Users\\u\\AppData\\Local", [(0, 19, 0x1200A9, CODEX_SANDBOX_USERS), (0, 19, 0x1F01FF, USER)], owner=USER)
    sp.check_windows_location([local, profile, LAPTOP_ROOT], USER)


def test_windows_simulated_trusted_installer_receiving_rights_on_a_folder_is_accepted():
    parent = directory("P", [(0, 0, 0x1F01FF, TRUSTED_INSTALLER)], owner=USER)
    sp.check_windows_location([parent, LAPTOP_ROOT], USER)


@pytest.mark.parametrize("parent", [
    directory("repo", [(0, 19, 0x1F01FF, USER)], owner=CODEX_SANDBOX_OFFLINE),                  # sandbox-owned
    directory("Temp", [(0, 3, 0x1301BF, CODEX_SANDBOX_USERS), (0, 3, 0x1F01FF, USER)], owner=USER),  # outsider-writable
    directory("svc", [(0, 0, 0x1301BF, "S-1-5-80-1111111111-2222222222-3333333333-4444444444-5555555555")], owner=USER),  # other service SID
])
def test_windows_simulated_trusted_installer_does_not_admit_other_principals(parent):
    with pytest.raises(sp.UntrustedLocationError):
        sp.check_windows_location([parent, LAPTOP_ROOT], USER)


def test_windows_simulated_session_file_dacl_rejects_a_trusted_installer_entry():
    for security in ({**GOOD, "aces": GOOD["aces"] + [(0, 0, sp.SESSION_FILE_MASK, TRUSTED_INSTALLER)]},
                     {**GOOD, "owner": TRUSTED_INSTALLER}):
        native = FakeNative(security=security)
        with pytest.raises(sp.SessionPublicationError):
            sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
        assert "write" not in native.events


# ------------------------------------------------- Windows, REAL native calls
# These exercise WindowsNative (ctypes) on Windows. They need a pytest basetemp in
# a location the trust rule accepts; otherwise they are SKIPPED with the refusing
# principal in the reason ("not exercised"), never counted as passes.

windows_only = pytest.mark.skipif(os.name != "nt", reason="real Windows native calls")


@pytest.fixture
def windows_native():
    return sp.WindowsNative()


@pytest.fixture
def trusted_windows_dir(tmp_path, windows_native):
    try:
        sp.check_windows_location(windows_native.directory_chain(tmp_path), windows_native.token_user_sid())
    except sp.UntrustedLocationError as error:
        pytest.skip(f"not exercised: basetemp location refused by the trust rule ({error})")
    return tmp_path


def path_security(native, path):
    import ctypes as c
    owner, dacl, psd = c.c_void_p(), c.c_void_p(), c.c_void_p()
    error = native.a.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, c.byref(owner), None, c.byref(dacl), None, c.byref(psd))
    if error:
        raise c.WinError(error)
    try:
        return native._parse(owner.value, dacl.value, psd.value)
    finally:
        native.k.LocalFree(psd)


@windows_only
def test_windows_native_token_sid_is_the_current_user(windows_native):
    import subprocess
    line = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout.strip()
    assert windows_native.token_user_sid() == line.rsplit(",", 1)[1].strip('"')


@windows_only
def test_windows_native_publish_has_exact_protected_user_only_dacl(trusted_windows_dir, windows_native):
    target = trusted_windows_dir / "session.json"
    sp.publish(target, SESSION)
    assert target.read_bytes() == json.dumps(SESSION, sort_keys=True).encode("utf-8")
    security = path_security(windows_native, target)
    sp.verify_windows_file_security(security, windows_native.token_user_sid())
    assert security["aces"] == [(0, 0, 0x0012019F, windows_native.token_user_sid())]


@windows_only
def test_windows_native_existing_file_refused_and_unchanged(trusted_windows_dir):
    target = trusted_windows_dir / "session.json"
    target.write_bytes(b"existing-record")
    with pytest.raises(FileExistsError):
        sp.publish(target, SESSION)
    assert target.read_bytes() == b"existing-record"


@windows_only
def test_windows_native_verification_failure_deletes_by_handle_without_writing(trusted_windows_dir):
    steps = []

    class Injecting(sp.WindowsNative):
        def create_new(self, path, sddl):
            handle = super().create_new(path, sddl); steps.append("created"); return handle
        def handle_security(self, handle):
            security = super().handle_security(handle); steps.append("failure_injected")
            return {**security, "dacl_protected": False}
        def write_all(self, handle, data):
            steps.append("write"); return super().write_all(handle, data)
        def delete_by_handle(self, handle):
            super().delete_by_handle(handle); steps.append("deleted")

    target = trusted_windows_dir / "verify-fail.json"
    with pytest.raises(sp.SessionPublicationError, match="not protected") as caught:
        sp.publish_windows(target, sp.encode_session(SESSION), Injecting())
    assert not isinstance(caught.value, sp.UntrustedLocationError)
    assert steps == ["created", "failure_injected", "deleted"]
    assert not target.exists()


# ---------------------------------------- review corrections (28 Oct, 23:11)

@pytest.mark.parametrize("ace_type", [9, 11, 5, 4, 0x13])   # callback (conditional), callback-object, object, compound, scoped policy
@pytest.mark.parametrize("position", ["parent", "ancestor"])
def test_windows_simulated_unsupported_granting_ace_types_are_refused(ace_type, position):
    entry = [(ace_type, 0, 0x1301BF, OTHER)]
    chain = ([directory("P", entry)] + TRUSTED_CHAIN[1:] if position == "parent"
             else [TRUSTED_CHAIN[0], directory("A", entry)] + TRUSTED_CHAIN[2:])
    with pytest.raises(sp.UntrustedLocationError, match="unsupported ACE type"):
        sp.check_windows_location(chain, USER)


def test_windows_simulated_conditional_outsider_grant_refused_before_creation():
    native = FakeNative(chain=[directory("P", [(9, 0, 0x1301BF, OTHER)])] + TRUSTED_CHAIN[1:])
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert "create" not in native.events and native.written == b""


@pytest.mark.parametrize("deny_type", sorted(sp.ACCESS_DENIED_ACE_TYPES))
def test_windows_simulated_deny_entries_never_grant_and_are_ignored(deny_type):
    sp.check_windows_location([directory("P", [(deny_type, 0, 0x1F01FF, OTHER)])] + TRUSTED_CHAIN[1:], USER)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_windows_simulated_reparse_point_anywhere_in_chain_is_refused(index):
    chain = [dict(entry) for entry in TRUSTED_CHAIN]
    chain[index]["reparse"] = True
    with pytest.raises(sp.UntrustedLocationError, match="reparse"):
        sp.check_windows_location(chain, USER)
    unchecked = [dict(entry) for entry in TRUSTED_CHAIN]
    del unchecked[index]["reparse"]
    with pytest.raises(sp.UntrustedLocationError, match="reparse"):
        sp.check_windows_location(unchecked, USER)


@pytest.mark.parametrize("target", ["C:/x/link/../session.json", "//server/share/session.json", "relative/../session.json"])
def test_windows_simulated_traversal_and_non_drive_paths_refused_before_creation(target):
    native = FakeNative()
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish_windows(Path(target), b"token-bytes", native)
    assert "create" not in native.events and native.written == b""


def test_windows_simulated_creates_inside_the_checked_parent():
    seen = []
    class Recording(FakeNative):
        def create_new(self, path, sddl): seen.append(path); return super().create_new(path, sddl)
    sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", Recording())
    assert seen == ["C:\\x\\session.json"]


def test_windows_simulated_close_failure_after_write_is_reported_without_retry():
    class CloseFails(FakeNative):
        def close(self, handle): self.events.append("close"); raise OSError(6, "synthetic close failure")
    native = CloseFails()
    with pytest.raises(sp.SessionPublicationError, match="may remain"):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert native.events == ["sid", "drive", "chain", "create", "verify", "write", "flush", "identity", "close"]


def test_windows_simulated_close_failure_on_error_path_keeps_original_error():
    class CloseFails(FakeNative):
        def close(self, handle): self.events.append("close"); raise OSError(6, "synthetic close failure")
    native = CloseFails(security={**GOOD, "dacl_protected": False})
    with pytest.raises(sp.SessionPublicationError, match="not protected") as caught:
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert any("closing" in note for note in getattr(caught.value, "__notes__", []))
    assert native.events.count("close") == 1 and native.written == b""


@posix_only
def test_parent_traversal_through_a_link_is_refused_and_neither_destination_receives_a_token(tmp_path):
    base = private_dir(tmp_path / "base")
    deep = private_dir(base / "deep"); private_dir(deep / "x")
    (base / "link").symlink_to(deep / "x", target_is_directory=True)
    with pytest.raises(sp.UntrustedLocationError, match="traversal"):
        sp.publish(base / "link" / ".." / "session.json", SESSION)
    assert not (base / "session.json").exists() and not (deep / "session.json").exists()
    with pytest.raises(sp.UntrustedLocationError, match="traversal"):
        sp.publish_posix(base / "link" / ".." / "session.json", sp.encode_session(SESSION))
    assert not list(tmp_path.rglob("session.json"))


def _failing_close_for_session(monkeypatch, record):
    real_close = os.close
    def close(fd):
        real_close(fd)                      # the descriptor really is closed (never retried)
        if fd in record["fds"]:
            raise OSError(5, "synthetic close failure")
    monkeypatch.setattr(sp.os, "close", close)


@posix_only
def test_close_failure_does_not_mask_the_original_error(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    record = track_session_writes(monkeypatch)
    _failing_close_for_session(monkeypatch, record)
    def refuse(fd, euid): raise sp.SessionPublicationError("synthetic verification failure")
    monkeypatch.setattr(sp, "_verify_posix_file", refuse)
    with pytest.raises(sp.SessionPublicationError, match="synthetic verification failure") as caught:
        sp.publish(target, SESSION)
    assert any("closing" in note and "also failed" in note for note in getattr(caught.value, "__notes__", []))
    assert record["writes"] == [] and not target.exists()


@posix_only
def test_close_failure_after_write_fails_publication_and_removes_own_file(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    record = track_session_writes(monkeypatch)
    _failing_close_for_session(monkeypatch, record)
    with pytest.raises(sp.SessionPublicationError, match="closing the session file failed after writing"):
        sp.publish(target, SESSION)
    assert record["writes"] and not target.exists()


@posix_only
def test_close_failure_after_write_with_failed_cleanup_reports_that_the_file_may_remain(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    record = track_session_writes(monkeypatch)
    _failing_close_for_session(monkeypatch, record)
    def unlink_fails(*a, **k): raise PermissionError("synthetic unlink failure")
    monkeypatch.setattr(sp.os, "unlink", unlink_fails)
    with pytest.raises(sp.SessionPublicationError) as caught:
        sp.publish(target, SESSION)
    assert any("may remain" in note for note in getattr(caught.value, "__notes__", []))


@windows_only
def test_windows_native_junction_in_path_is_refused(trusted_windows_dir):
    import subprocess
    real = trusted_windows_dir / "real"; real.mkdir()
    junction = trusted_windows_dir / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)], check=True, capture_output=True)
    with pytest.raises(sp.UntrustedLocationError, match="reparse"):
        sp.publish(junction / "session.json", SESSION)
    assert not (real / "session.json").exists()


# ------------------------------------------------ fix batch (29 Oct 2026, owner-approved 09:15)

@pytest.mark.parametrize("drive", [
    {"type": 3, "target": "\\??\\C:\\Users\\u\\elsewhere"},           # SUBST mapping
    {"type": 4, "target": "\\Device\\LanmanRedirector\\;Z:\\server"},  # mapped network drive
    {"type": 2, "target": "\\Device\\HarddiskVolume9"},               # removable
    {"type": 3, "target": ""},                                         # unknown target
])
def test_windows_simulated_redirected_or_non_fixed_drive_refused_before_any_check_or_creation(drive):
    native = FakeNative()
    native.drive = drive
    with pytest.raises(sp.UntrustedLocationError, match="drive"):
        sp.publish_windows(Path("C:/x/session.json"), b"token-bytes", native)
    assert "chain" not in native.events and "create" not in native.events and native.written == b""


@posix_only
def test_missing_parent_folders_are_created_0700_after_the_existing_ancestor_passed(tmp_path):
    base = private_dir(tmp_path / "pub")
    target = base / "a" / "b" / "session.json"
    receipt = sp.publish(target, SESSION)
    assert target.read_bytes() == json.dumps(SESSION, sort_keys=True).encode("utf-8")
    for folder in (base / "a", base / "a" / "b"):
        assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    assert receipt == sp.identity_of(target)


@posix_only
def test_untrusted_existing_ancestor_refuses_before_any_folder_is_created(tmp_path):
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    target = open_dir / "a" / "b" / "session.json"
    with pytest.raises(sp.UntrustedLocationError):
        sp.publish(target, SESSION)
    assert not (open_dir / "a").exists()


@posix_only
def test_first_fstat_failure_closes_the_descriptor_and_reports_the_empty_file(tmp_path, monkeypatch):
    target = private_dir(tmp_path / "pub") / "session.json"
    real_fstat, real_close, calls, closed = os.fstat, os.close, [], []
    def fstat(fd):
        calls.append(fd)
        if len(calls) == 2:  # 1st: the parent directory check; 2nd: the new file's identity
            raise OSError(5, "synthetic fstat failure")
        return real_fstat(fd)
    def close(fd):
        closed.append(fd); return real_close(fd)
    monkeypatch.setattr(sp.os, "fstat", fstat)
    monkeypatch.setattr(sp.os, "close", close)
    with pytest.raises(OSError, match="synthetic fstat failure") as caught:
        sp.publish(target, SESSION)
    monkeypatch.undo()
    assert any("may remain" in note for note in getattr(caught.value, "__notes__", []))
    assert closed.count(calls[1]) == 1            # the new file's descriptor was closed exactly once
    assert target.exists() and target.stat().st_size == 0 and stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_remove_published_removes_only_the_file_it_names(tmp_path):
    base = private_dir(tmp_path / "pub")
    target = base / "session.json"
    receipt = sp.publish(target, SESSION)
    assert sp.remove_published(target, receipt) == "removed" and not target.exists()
    assert sp.remove_published(target, receipt) == "absent"
    second = sp.publish(target, SESSION)
    target.unlink()
    target.write_bytes(b"replacement")              # a different file now sits at the path
    assert sp.remove_published(target, second) == "kept-replaced"
    assert target.read_bytes() == b"replacement"
    assert sp.remove_published(target, None) == "kept-no-receipt" and target.exists()


@posix_only
def test_remove_published_does_not_follow_a_link_at_the_path(tmp_path):
    base = private_dir(tmp_path / "pub")
    target = base / "session.json"
    receipt = sp.publish(target, SESSION)
    target.rename(base / "moved.json")
    target.symlink_to(base / "moved.json")         # a link to the original file
    assert sp.remove_published(target, receipt) == "kept-replaced"
    assert (base / "moved.json").exists() and target.is_symlink()


@windows_only
def test_windows_native_fixed_system_drive_is_a_real_device():
    info = sp.WindowsNative().drive_info(os.environ["SystemDrive"])
    assert info["type"] == sp.DRIVE_FIXED and info["target"].startswith("\\Device\\")


@windows_only
def test_windows_native_missing_parents_created_inside_a_trusted_location(trusted_windows_dir):
    target = trusted_windows_dir / "a" / "b" / "session.json"
    receipt = sp.publish(target, SESSION)
    assert target.read_bytes() == json.dumps(SESSION, sort_keys=True).encode("utf-8") and receipt


@windows_only
def test_windows_native_remove_published_removes_own_file_and_keeps_a_replacement(trusted_windows_dir):
    target = trusted_windows_dir / "session.json"
    receipt = sp.publish(target, SESSION)
    assert sp.remove_published(target, receipt) == "removed" and not target.exists()
    assert sp.remove_published(target, receipt) == "absent"
    second = sp.publish(target, SESSION)
    os.remove(target)
    target.write_bytes(b"replacement")
    assert sp.remove_published(target, second) == "kept-replaced"
    assert target.read_bytes() == b"replacement"
