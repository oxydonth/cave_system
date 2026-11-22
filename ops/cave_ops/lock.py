"""The one lock (``CAVE_DATA/.cave/lock``) of ``apply``, ``update``, ``backup`` and ``restore``.

It is an advisory ``flock``, so the operating system releases it when the holder dies, however
it dies. A second holder fails at once instead of waiting.

Root (``update``, ``backup``, ``restore``) and the service user (``apply``) both take it, in either
order. So the lock needs no write access (``flock`` works on a read-only descriptor; only the
holder's name is then not written), and when root takes it, it gives ``CAVE_DATA/.cave`` and the
lock file to the owner of ``CAVE_DATA``: the file root created, or found as its own, becomes the
service user's again. That also repairs a server where an earlier version left them root's.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from types import TracebackType

from .errors import CaveError


class LockBusy(CaveError):
    """Another ``cave`` command holds the lock."""


def _euid() -> int:
    """The effective user; a function so that tests can pretend to be root."""
    return os.geteuid()


def _owner_of(path: Path) -> tuple[int, int]:
    """Who owns ``path``; a function so that tests can pretend another owner."""
    info = path.stat()
    return info.st_uid, info.st_gid


def _hand_over(path: Path, owner: tuple[int, int], fd: int | None = None) -> None:
    """As root, make ``path`` (or the open ``fd``) belong to ``owner`` if it does not already."""
    info = os.fstat(fd) if fd is not None else path.stat()
    if (info.st_uid, info.st_gid) == owner:
        return
    if fd is not None:
        os.fchown(fd, *owner)
    else:
        os.chown(path, *owner)


def held_by(path: Path) -> str | None:
    """Who holds the lock at ``path`` right now, or ``None``. Needs only read access.

    For ``status``, which must not take the lock. It probes with a shared lock for an instant; a
    command that tries to take the lock in exactly that instant is refused as if it were busy.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return os.pread(fd, 200, 0).decode(errors="replace").strip() or "another cave command"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class Lock:
    """``with Lock(path, "update"): ...``. Raises ``LockBusy`` naming the holder if it is taken."""

    def __init__(self, path: Path, command: str) -> None:
        self._path = path
        self._command = command
        self._fd: int | None = None

    def acquire(self) -> None:
        directory = self._path.parent
        root = _euid() == 0
        directory.mkdir(parents=True, exist_ok=True)
        if root:
            _hand_over(directory, _owner_of(directory.parent))
        writable = True
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        except PermissionError:
            # A lock file that another user made (root's, from an earlier version): flock needs
            # no write access, only the holder's name cannot be written.
            writable = False
            try:
                fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as error:
                raise CaveError(
                    f"cannot take the lock {self._path}: {error.strerror}. {directory} must "
                    "belong to the service user: the next root command that takes the lock (the "
                    "nightly backup, or `sudo ./cave backup`) gives it back, or run "
                    f"`sudo chown -R <service user>: {directory}`."
                ) from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 200, 0).decode(errors="replace").strip() or "another cave command"
            os.close(fd)
            raise LockBusy(f"{holder} is running; try again when it has finished") from None
        if root:
            _hand_over(self._path, _owner_of(directory), fd)
            os.fchmod(fd, 0o644)
        if writable:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            os.ftruncate(fd, 0)
            text = f"`cave {self._command}` (pid {os.getpid()}, since {stamp})\n"
            os.pwrite(fd, text.encode(), 0)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None

    def __enter__(self) -> Lock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
