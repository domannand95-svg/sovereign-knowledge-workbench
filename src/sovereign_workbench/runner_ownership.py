"""One cooperating runner per canonical local database, without expiring leases.

Use only on local filesystems with reliable OS advisory locks. Do not unlink the
sidecar while any runner can be active. Stop old runner versions before upgrade.
This is ownership of queue dispatch, not proof of absent external effects.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import secrets
import stat
from threading import Lock


class RunnerBusyError(ValueError):
    pass


class OwnershipLostError(ValueError):
    pass


_guard = Lock()
_active = set()


@contextmanager
def exclusive_runner(database):
    if database.in_transaction:
        raise ValueError('Runner requires a committed database connection')
    paths = [row[2] for row in database.execute('PRAGMA database_list') if row[1] == 'main']
    if not paths or not paths[0]:
        raise ValueError('Runner ownership requires a file-backed local database')
    path = Path(paths[0]).resolve(strict=True)
    key = str(path)
    with _guard:
        if key in _active:
            raise RunnerBusyError('Another runner owns this database')
        _active.add(key)
    descriptor = None
    try:
        lock_path = Path(str(path) + '.runner.lock')
        if lock_path.is_symlink():
            raise ValueError('Runner lock must not be a symbolic link')
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.set_inheritable(descriptor, False)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Runner lock must be a regular file')
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'\0')
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RunnerBusyError('Another runner owns this database') from error
            raise
        yield secrets.token_hex(32)
    finally:
        # Closing releases the OS lock even after exceptions; kernel crash cleanup
        # releases it after process exit. Never unlink a possibly shared lock inode.
        if descriptor is not None:
            os.close(descriptor)
        with _guard:
            _active.discard(key)
