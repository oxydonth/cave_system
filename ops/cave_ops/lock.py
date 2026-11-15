"""The one lock (``CAVE_DATA/.cave/lock``) of ``apply``, ``update``, ``backup`` and ``restore``.

It is an advisory ``flock``, so the operating system releases it when the holder dies, however
it dies. A second holder fails at once instead of waiting.
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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 200, 0).decode(errors="replace").strip() or "another cave command"
            os.close(fd)
            raise LockBusy(f"{holder} is running; try again when it has finished") from None
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"`cave {self._command}` (pid {os.getpid()}, since {stamp})\n".encode(), 0)
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
