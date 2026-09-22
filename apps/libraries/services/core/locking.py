"""Serialised, atomic writes to the MHVTL config files.

Moved from mhvtl_library_service.py:130 (_FileLock) and :164
(_atomic_write_text) - the only lock-and-fsync implementation in the tree.
mhvtl_script_service.py and the shadowed services.py write device.conf with
neither, and the tape operations service edits library_contents with no lock at
all, so two concurrent tape operations can lose each other's changes.

One change from the original: the lock is held with fcntl.flock rather than an
O_EXCL lockfile. An O_EXCL lock strands the file when its holder is killed - a
gunicorn worker reaped at the 120s timeout leaves the lockfile behind, and every
later write then waits its timeout and fails. flock is released by the kernel
when the process dies, whatever killed it.

The lock file lives in the configuration directory, which is root-owned and
only group-readable on a normal install, so the web user cannot create it. It
does not have to: flock works on a read-only file descriptor. When the file is
missing and cannot be created, it is created with `sudo touch` (the one command
needed for this) and then opened read-only. Root and the web user therefore
take the same lock, which is the point.

A second change, added in step 8: the lock is reentrant within one thread.
Creating a library takes the lock and then calls ConfigService.write_device_conf,
which takes it again; flock on a second file descriptor for the same file blocks
even in the same process, so the nested call waited out its timeout and the
create failed with "another process is writing the configuration" when the only
writer was itself. Counting the depth per thread lets a service compose out of
locked pieces, which is the whole point of the layer.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import errno
import fcntl
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 15.0

#: Locks this thread already holds, by resolved path, with their depth. Per
#: thread rather than per process: two request threads in the same gunicorn
#: worker are independent writers and must still exclude each other.
_held = threading.local()


class LockTimeout(TimeoutError):
    """Another process held the lock longer than we were willing to wait."""


class FileLock:
    """Cross-process lock around the configuration.

    One lock for the whole config directory rather than one per file: a library
    change touches device.conf and library_contents.N together, and a reader
    should not see one without the other.

        with FileLock(config_dir / '.mhvtl.lock'):
            atomic_write_text(device_conf, text)

    Reentrant within one thread, so a service that holds the lock may call
    another that takes it. Not reentrant across threads or processes, which is
    what it is for.
    """

    def __init__(self, lock_path: Union[str, Path],
                 timeout_s: float = DEFAULT_LOCK_TIMEOUT):
        self.lock_path = Path(lock_path)
        self.timeout_s = timeout_s
        self._fd = None
        self._key = None
        self._reentered = False
        self._writable = True

    def __enter__(self):
        if not self.lock_path.parent.is_dir():
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._key = str(self.lock_path.resolve())

        depths = getattr(_held, 'depths', None)
        if depths is None:
            depths = _held.depths = {}
        if self._key in depths:
            # Already ours: count the nesting and return without touching flock.
            depths[self._key] += 1
            self._reentered = True
            return self

        self._fd, self._writable = self._open()
        deadline = time.monotonic() + self.timeout_s

        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self._writable:
                    os.truncate(self._fd, 0)
                    os.write(self._fd, f'{os.getpid()}\n'.encode('ascii', 'ignore'))
                depths[self._key] = 1
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self._close()
                    raise
                if time.monotonic() > deadline:
                    self._close()
                    raise LockTimeout(
                        f'waited {self.timeout_s}s for {self.lock_path}; another '
                        f'process is writing the MHVTL configuration')
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        depths = getattr(_held, 'depths', {})
        if self._key in depths:
            depths[self._key] -= 1
            if depths[self._key] <= 0:
                del depths[self._key]

        if self._reentered:
            # An inner block: the outer one still holds the flock.
            self._reentered = False
            return False

        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._close()
        return False

    def _open(self):
        """(fd, writable) for the lock file, creating it if it is missing.

        The configuration directory is root-owned and group-readable, so an
        unprivileged web process can neither create the lock file nor open it
        for writing. flock needs neither: the fallback creates the file with
        sudo when it is absent and locks it read-only.
        """
        try:
            return os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644), True
        except PermissionError:
            pass

        if not self.lock_path.exists():
            from . import shell
            created = shell.sudo(['touch', str(self.lock_path)])
            if not created.ok:
                raise PermissionError(
                    f'cannot create the lock file {self.lock_path}: '
                    f'{created.stderr.strip() or "sudo touch failed"}')

        logger.debug('locking %s read-only: the directory is not writable',
                     self.lock_path)
        return os.open(str(self.lock_path), os.O_RDONLY), False

    def _close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def atomic_write_text(path: Union[str, Path], content: str, *,
                      mode: int = 0o644) -> None:
    """Replace a file's contents in one step.

    Write a temporary file in the same directory, fsync it, then rename over the
    target. A reader sees either the old file or the new one, never a half-written
    device.conf - the difference between a library that is briefly invisible and
    one the daemons refuse to start.

    Raises PermissionError on root-owned files; callers needing sudo use
    core.shell.sudo_tee, accepting that tee is not atomic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))

        # fsync the directory as well, so the rename itself survives a crash.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
