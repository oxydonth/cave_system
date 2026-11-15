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
import posixpath
import pwd
import shutil
import stat
import tarfile
from collections.abc import Callable, Iterator
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


def remove_stale_git_locks(ctx: Context) -> None:
    """Remove lock files that a killed Git left in the checkout. Safe only while holding the cave
    lock, because on the server Git runs only through ``update`` and ``restore``."""
    git_dir = ctx.paths.root / ".git"
    for name in ("index.lock", "HEAD.lock"):
        lock = git_dir / name
        if lock.exists():
            ctx.warn(f"Removing {lock}, left by an interrupted Git.")
            lock.unlink()


def check_out_exactly(ctx: Context, commit: str, other: str) -> str | None:
    """Make the checkout exactly ``commit``, whatever an interrupted checkout of ``other`` left:
    Git's lock files, HEAD where it was, files of both in the working tree. Returns ``None``, or
    what failed.
    """
    remove_stale_git_locks(ctx)
    done = git(ctx, "checkout", "--force", "--detach", commit)
    if not done.ok:
        done = git(ctx, "reset", "--hard", commit)
    if not done.ok:
        return done.output.strip()[-300:] or f"exit status {done.returncode}"
    # Files that only ``other`` has are untracked now; remove exactly those, nothing else.
    added = git(ctx, "diff", "--name-only", "--diff-filter=A", "-z", commit, other)
    if not added.ok:
        return f"the files {other} added could not be listed: {added.output.strip()[-300:]}"
    for name in filter(None, added.stdout.split("\0")):
        path = ctx.paths.root / name
        if os.path.lexists(path) and not path.is_dir():
            path.unlink()
            parent = path.parent
            while parent != ctx.paths.root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    return None


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
    """Replace ``path`` with ``data`` so that a reader, or a power cut, sees old or new only.

    The temporary file is created fresh and never through a symbolic link, because this runs as
    root in directories that other users can write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
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


def _escapes(relative: str) -> bool:
    """Whether a normalised relative path leaves its root."""
    return relative == ".." or relative.startswith("../") or relative.startswith("/")


def check_member(member: tarfile.TarInfo, prefix: str = "") -> PurePosixPath:
    """The path of ``member`` below ``prefix``, refusing what could escape the target by itself.

    Only directories, regular files and symbolic links are accepted. A link must be relative and
    its target, taken from the link's own directory, must stay inside. ``check_members`` adds
    the checks that need the whole archive.
    """
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise UnsafeArchive(member.name, "the path leaves the target directory")
    if prefix:
        try:
            path = path.relative_to(prefix)
        except ValueError as error:
            raise UnsafeArchive(member.name, f"not below {prefix}/") from error
    if not (member.isdir() or member.isreg() or member.issym()):
        raise UnsafeArchive(member.name, "only directories, files and symbolic links are allowed")
    if member.issym():
        if not member.linkname or member.linkname.startswith("/"):
            raise UnsafeArchive(member.name, f"the link to {member.linkname!r} leaves the target")
        resolved = posixpath.normpath(posixpath.join(str(path.parent), member.linkname))
        if _escapes(resolved):
            raise UnsafeArchive(member.name, f"the link to {member.linkname} leaves the target")
    return path


def check_members(
    members: list[tarfile.TarInfo], prefix: str = ""
) -> list[tuple[PurePosixPath, tarfile.TarInfo]]:
    """Check a whole archive: every member by itself, no path twice, and nothing below a path
    that the archive makes a symbolic link (a member written through a link could land
    anywhere)."""
    checked = [(check_member(member, prefix), member) for member in members]
    seen: set[PurePosixPath] = set()
    links: set[PurePosixPath] = set()
    for path, member in checked:
        if path in seen and path != PurePosixPath("."):
            raise UnsafeArchive(member.name, "the path occurs twice")
        seen.add(path)
        if member.issym():
            links.add(path)
    for path, member in checked:
        for parent in path.parents:
            if parent in links:
                raise UnsafeArchive(member.name, f"it lies below the symbolic link {parent}")
    return checked


Owners = Callable[[tarfile.TarInfo], tuple[int, int]]


def _inside(path: Path, root: Path) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(root)
    return real == top or real.startswith(top + os.sep)


def extract(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    target: Path,
    owners: Owners | None = None,
) -> None:
    """Extract ``members`` (whose names are relative to ``target``, an empty directory) with
    their modes, times and, as root, owners (``owners(member)`` maps them, for example from the
    old server's service user to this one's).

    Nothing can be written outside ``target``: the archive is checked as a whole first; files
    are created new and never through a link; every parent directory is checked to resolve
    inside ``target``; links are created last, so nothing is written through them, and each must
    resolve inside ``target`` as well.
    """
    checked = check_members(members)
    owner_of = owners or (lambda member: (member.uid, member.gid))
    directories: list[tuple[Path, tarfile.TarInfo]] = []
    links: list[tuple[Path, tarfile.TarInfo]] = []
    for relative, member in checked:
        if relative == PurePosixPath("."):
            continue
        path = target / relative
        if member.issym():
            links.append((path, member))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(path.parent, target):
            raise UnsafeArchive(member.name, "its directory resolves outside the target")
        if member.isdir():
            if path.is_symlink():
                raise UnsafeArchive(member.name, "a symbolic link is in the way")
            path.mkdir(mode=0o700, exist_ok=True)
            directories.append((path, member))
            continue
        source = archive.extractfile(member)
        if source is None:
            raise UnsafeArchive(member.name, "no content")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with source, os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
            shutil.copyfileobj(source, handle)
        if is_root():
            os.lchown(path, *owner_of(member))
        os.chmod(path, member.mode & 0o7777)
        os.utime(path, (member.mtime, member.mtime))
    for path, member in links:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(path.parent, target):
            raise UnsafeArchive(member.name, "its directory resolves outside the target")
        os.symlink(member.linkname, path)
        if not _inside(path, target):
            path.unlink()
            raise UnsafeArchive(member.name, f"the link to {member.linkname} resolves outside")
        if is_root():
            os.lchown(path, *owner_of(member))
    for path, member in reversed(directories):
        if is_root():
            os.chown(path, *owner_of(member))
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
