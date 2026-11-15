"""Helpers for ``update``, ``status``, ``backup`` and ``restore``: who may run them, Git as the
owner of the checkout, and copying the state directory with its owners.

**Which user.** ``update``, ``backup`` and ``restore`` run as **root** (the systemd units do).
The state directory holds files that the containers created, owned by root or by a container's
user and often mode 0600: Home Assistant's ``.storage``, Mosquitto's data. Only root can read all
of them, and only root can give a restored file back its owner. Run as another user, the
commands refuse before they touch anything, unless every file in the state directory belongs to
that user (a development checkout, where nothing needs privileges).

**Git as the owner.** The checkout belongs to the service user, whose home holds the deploy key.
Run as root, Git is started as that user (``runuser -u <owner> -- git ...``), so the fetch uses
the deploy key, no file in the checkout becomes root's, and Git does not refuse a repository
owned by someone else.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from ..context import Context
from ..errors import CaveError
from ..runner import Result

OPS_DIR = "ops"
"""``CAVE_DATA/.cave/ops``: the commands' own working files (update record, snapshots, staging).
Neither a snapshot nor a backup contains it, nor the lock file next to it."""


def current_uid() -> int:
    """The effective user; a function so that tests can pretend to be someone else."""
    return os.geteuid()


def is_root() -> bool:
    return current_uid() == 0


def cave_dir(ctx: Context) -> Path:
    """``CAVE_DATA/.cave``: the lock and the commands' bookkeeping."""
    return ctx.paths.lock_file.parent


def ops_dir(ctx: Context) -> Path:
    return cave_dir(ctx) / OPS_DIR


# ---------------------------------------------------------------------------------- privileges
def _foreign_entry(top: Path, uid: int) -> Path | None:
    """The first file or directory below ``top`` (included) not owned by ``uid``."""
    if not top.exists():
        return None
    if top.lstat().st_uid != uid:
        return top
    for current, dirs, files in os.walk(top):
        for name in (*dirs, *files):
            path = Path(current) / name
            try:
                if path.lstat().st_uid != uid:
                    return path
            except FileNotFoundError:
                continue
    return None


def require_state_access(ctx: Context, command: str, data: Path | None = None) -> None:
    """Refuse, before anything is touched, unless this user can read and restore every file.

    Root always can. Another user only if every entry of the state directory is theirs.
    """
    if is_root():
        return
    foreign = _foreign_entry(data or ctx.paths.data, current_uid())
    if foreign is None:
        return
    try:
        owner = pwd.getpwuid(foreign.lstat().st_uid).pw_name
    except KeyError:
        owner = f"uid {foreign.lstat().st_uid}"
    raise CaveError(
        f"`cave {command}` must run as root: {foreign} belongs to {owner}, and only root can read "
        f"and restore the files the containers own. Run `sudo ./cave {command}` (the systemd "
        "units do). Nothing was changed."
    )


# ------------------------------------------------------------------------------------------ git
def git(ctx: Context, *args: str, timeout: float | None = 120.0) -> Result:
    """``git <args>`` in the checkout, as the checkout's owner when running as root."""
    prefix: list[str] = []
    owner = ctx.paths.root.stat().st_uid
    if is_root() and owner != 0:
        prefix = ["runuser", "-u", pwd.getpwuid(owner).pw_name, "--"]
    return ctx.runner.run([*prefix, "git", *args], cwd=ctx.paths.root, timeout=timeout)


def give_to_checkout_owner(ctx: Context, path: Path) -> None:
    """Make ``path`` the checkout owner's, so that Git running as that user can write there."""
    if is_root():
        info = ctx.paths.root.stat()
        os.chown(path, info.st_uid, info.st_gid)


# ---------------------------------------------------------------------------- files with owners
def state_entries(data: Path) -> list[str]:
    """What a snapshot or backup holds, relative to ``CAVE_DATA``: every top-level entry, and
    inside ``.cave`` everything except the lock and the commands' own ``ops`` directory."""
    if not data.is_dir():
        return []
    entries = sorted(name for name in os.listdir(data) if name != ".cave")
    meta = data / ".cave"
    if meta.is_dir():
        entries += sorted(
            f".cave/{name}" for name in os.listdir(meta) if name not in ("lock", OPS_DIR)
        )
    return entries


def _keep_owner(path: Path, info: os.stat_result) -> None:
    if is_root():
        os.lchown(path, info.st_uid, info.st_gid)


def copy_entry(source: Path, target: Path) -> None:
    """Copy a file, symbolic link or directory tree with its modes, times and, as root, owners.

    Sockets, pipes and devices are skipped: none of the services keeps one in its state.
    """
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), target)
        _keep_owner(target, info)
    elif stat.S_ISDIR(info.st_mode):
        target.mkdir(mode=0o700)
        for name in sorted(os.listdir(source)):
            copy_entry(source / name, target / name)
        _keep_owner(target, info)
        shutil.copystat(source, target, follow_symlinks=False)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(source, target, follow_symlinks=False)
        _keep_owner(target, info)
        os.chmod(target, stat.S_IMODE(info.st_mode))


def tree_size(path: Path) -> int:
    """Bytes in the regular files below ``path``."""
    total = 0
    for current, _dirs, files in os.walk(path):
        for name in files:
            try:
                info = (Path(current) / name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def free_bytes(path: Path) -> int:
    """Free space on the file system that holds ``path``."""
    return shutil.disk_usage(path).free


def remove(path: Path) -> None:
    """Remove a file or a tree; nothing if it does not exist."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def sync() -> None:
    """Flush everything to disk, so that a power cut after this point finds the files complete."""
    os.sync()


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace ``path`` with ``data`` so that a reader, or a power cut, sees old or new only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# ------------------------------------------------------------------------------------- archives
class UnsafeArchive(CaveError):
    """An archive member that would land outside the target or is of an unexpected kind."""

    def __init__(self, member: str, why: str) -> None:
        super().__init__(f"refusing archive member {member!r}: {why}")


def check_member(member: tarfile.TarInfo, prefix: str = "") -> PurePosixPath:
    """The path of ``member`` below ``prefix``, refusing anything that could escape the target.

    Only directories, regular files and symbolic links that stay inside are accepted.
    """
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise UnsafeArchive(member.name, "the path leaves the target directory")
    if prefix:
        try:
            path = path.relative_to(prefix)
        except ValueError as error:
            raise UnsafeArchive(member.name, f"not below {prefix}/") from error
    if not (member.isdir() or member.isreg() or member.issym()):
        raise UnsafeArchive(member.name, "only directories, files and symbolic links are allowed")
    if member.issym():
        link = PurePosixPath(member.linkname)
        depth = len(path.parts) - 1
        if link.is_absolute() or link.parts.count("..") > depth:
            raise UnsafeArchive(member.name, f"the link to {member.linkname} leaves the target")
    return path


def extract(archive: tarfile.TarFile, members: list[tarfile.TarInfo], target: Path) -> None:
    """Extract checked ``members`` (whose names are relative to ``target``) with their modes,
    times and, as root, owners. Directories get their times after their contents."""
    directories: list[tuple[Path, tarfile.TarInfo]] = []
    for member in members:
        relative = check_member(member)
        if relative == PurePosixPath("."):
            continue
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if member.isdir():
            path.mkdir(mode=0o700, exist_ok=True)
            directories.append((path, member))
            continue
        if member.issym():
            os.symlink(member.linkname, path)
        else:
            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchive(member.name, "no content")
            with source, open(path, "wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(path, member.mode & 0o7777)
            os.utime(path, (member.mtime, member.mtime))
        if is_root():
            os.lchown(path, member.uid, member.gid)
    for path, member in reversed(directories):
        if is_root():
            os.chown(path, member.uid, member.gid)
        os.chmod(path, member.mode & 0o7777)
        os.utime(path, (member.mtime, member.mtime))


def walk_entries(data: Path, entries: list[str]) -> Iterator[Path]:
    """Every path below the given state entries, parents before children, sorted."""
    for entry in entries:
        top = data / entry
        yield top
        if top.is_dir() and not top.is_symlink():
            for current, dirs, files in os.walk(top):
                dirs.sort()
                for name in sorted([*dirs, *files]):
                    yield Path(current) / name
