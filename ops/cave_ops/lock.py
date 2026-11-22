"""The one lock (``CAVE_DATA/.cave/lock``) of ``apply``, ``bootstrap``, ``update``, ``backup`` and
``restore``.

It is an advisory ``flock``, so the operating system releases it when the holder dies, however
it dies. A second holder does not wait for the first: it tries for ``PATIENCE`` seconds and then
fails. That short patience is for ``held_by``, which ``status`` and ``cave watch`` use to see who
holds the lock: it takes a shared lock for an instant, and a command that starts in exactly that
instant must not be refused because of it.

Root (``update``, ``backup``, ``restore``) and the service user (``apply``) both take it, in either
order. So the lock needs no write access (``flock`` works on a read-only descriptor; only the
holder's name is then not written), and when root takes it, it gives ``CAVE_DATA/.cave`` and the
lock file to the owner of ``CAVE_DATA``: the file root created, or found as its own, becomes the
service user's again. That also repairs a server where an earlier version left them root's.

The name in the file is the last one written. A holder that could not write it leaves its
predecessor's there, so a name is believed only while the process it names is alive.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from .errors import CaveError

PATIENCE = 2.0
"""Seconds a command keeps trying to take a lock that is held, before it gives up. Covers an
instant probe by ``held_by``; a command that really holds the lock takes far longer."""
RETRY = 0.05
UNKNOWN_HOLDER = "another cave command"
_PID = re.compile(r"\(pid (\d+)[,)]")


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
    info = os.fstat(fd) if fd is not None else path.lstat()
    if (info.st_uid, info.st_gid) == owner:
        return
    if fd is not None:
        os.fchown(fd, *owner)
    else:
        os.lchown(path, *owner)  # never through a link


def _alive(pid: int) -> bool:
    """Whether a process ``pid`` exists (of any user)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is only someone else's
    except OSError:
        return False
    return True


def _holder(fd: int) -> str:
    """The holder named in the lock file, if that process still lives; else ``UNKNOWN_HOLDER``.

    A holder that could not write the file (the service user, while an earlier version's lock
    file is still root's) leaves the previous holder's name in it: a process that has ended.
    """
    text = os.pread(fd, 200, 0).decode(errors="replace").strip()
    found = _PID.search(text)
    if not text or found is None or not _alive(int(found.group(1))):
        return UNKNOWN_HOLDER
    return text


def held_by(path: Path) -> str | None:
    """Who holds the lock at ``path`` right now, or ``None``. Needs only read access.

    For ``status`` and ``cave watch``, which must not take the lock. It probes with a shared lock
    for an instant; a command that tries to take the lock in exactly that instant keeps trying
    for ``PATIENCE`` seconds, so it is not refused because of the probe.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _holder(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class Lock:
    """``with Lock(path, "update"): ...``. Raises ``LockBusy`` naming the holder if it is taken.

    ``sleep`` and ``monotonic`` are the clock of the short patience (``ctx.lock`` passes the
    context's, so that tests do not wait).
    """

    def __init__(
        self,
        path: Path,
        command: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._command = command
        self._sleep = sleep
        self._monotonic = monotonic
        self._fd: int | None = None

    def acquire(self) -> None:
        directory = self._path.parent
        root = _euid() == 0
        directory.mkdir(parents=True, exist_ok=True)
        if root:
            if directory.is_symlink():
                # Root would create the lock and hand files over wherever the link points.
                raise CaveError(
                    f"{directory} is a symbolic link; it must be a directory. Nothing was changed."
                )
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
        deadline = self._monotonic() + PATIENCE
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if self._monotonic() >= deadline:
                    holder = _holder(fd)
                    os.close(fd)
                    raise LockBusy(f"{holder} is running; try again when it has finished") from None
                self._sleep(RETRY)
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
