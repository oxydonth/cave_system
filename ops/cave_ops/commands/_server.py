"""Helpers for ``update``, ``status``, ``backup`` and ``restore``: who may run them, Git as the
owner of the checkout, and copying the state directory with its owners.

**Which user.** ``update``, ``backup`` and ``restore`` run as **root** (the systemd units do).
The state directory holds files that the containers created, owned by root or by a container's
user and often mode 0600: Home Assistant's ``.storage``, Mosquitto's data. Only root can read all
of them, and only root can give a restored file back its owner. Run as another user, the
commands refuse before they touch anything, unless every file in the state directory belongs to
that user (a development checkout, where nothing needs privileges).

**Git as the owner.** The checkout belongs to the service user, whose home holds Git's state (and,
on a server cloned over SSH while the repository was private, the deploy key). Run as root, Git is
started as that user (``runuser -u <owner> -- git ...``), so the fetch uses that user's
configuration and key, no file in the checkout becomes root's, and Git does not refuse a
repository owned by someone else.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from .. import records
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


def check_out_exactly(
    ctx: Context, commit: str, other: str, *, remove_added: bool
) -> tuple[str | None, list[str], list[str]]:
    """Make the checkout exactly ``commit``, whatever an interrupted checkout of ``other`` left:
    Git's lock files, HEAD where it was, files of both in the working tree.

    With ``remove_added`` (only when a checkout of ``other`` may have run), a file that ``other``
    adds is removed if, and only if, its content is ``other``'s own: the owner's untracked files
    are never touched, even where ``other`` has a file of the same name. Returns what failed (or
    ``None``), the paths that were left in place and the paths that were removed (a file of the
    owner's that is byte for byte ``other``'s is among them: checking out ``other`` gives it back).
    """
    remove_stale_git_locks(ctx)
    done = git(ctx, "checkout", "--force", "--detach", commit)
    if not done.ok:
        done = git(ctx, "reset", "--hard", commit)
    if not done.ok:
        return done.output.strip()[-300:] or f"exit status {done.returncode}", [], []
    if not remove_added:
        return None, [], []
    added = git(ctx, "diff", "--name-only", "--diff-filter=A", "-z", commit, other)
    if not added.ok:
        listed = f"the files {other} added could not be listed: {added.output.strip()[-300:]}"
        return listed, [], []
    present = [
        name
        for name in filter(None, added.stdout.split("\0"))
        if os.path.lexists(ctx.paths.root / name)
    ]
    regular = [name for name in present if (ctx.paths.root / name).is_file()]
    kept = [name for name in present if name not in regular]
    removed: list[str] = []
    if regular:
        here = git(ctx, "hash-object", "--", *regular)
        theirs = git(ctx, "rev-parse", *(f"{other}:{name}" for name in regular))
        if not here.ok or not theirs.ok:
            return None, present, []
        for name, mine, released in zip(
            regular, here.stdout.split(), theirs.stdout.split(), strict=False
        ):
            if mine != released:
                kept.append(name)
                continue
            path = ctx.paths.root / name
            path.unlink()
            removed.append(name)
            parent = path.parent
            while parent != ctx.paths.root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    return None, sorted(kept), sorted(removed)


def entries_elsewhere(ctx: Context) -> list[str]:
    """State entries on another file system than ``CAVE_DATA`` itself: the snapshot could not be
    put back by renaming them."""
    data = ctx.paths.data
    home = device_of(data)
    return [entry for entry in state_entries(data) if device_of(data / entry) != home]


def device_of(path: Path) -> int:
    """The file system a path is on; a function so that tests can pretend."""
    return path.lstat().st_dev


RESTORE_MARKER = "restore.json"
RESTORE_WAY_OUT = (
    "Run the same `sudo ./cave restore` again, restore another archive, or put the state from "
    "before back with `sudo ./cave restore --undo` (docs/operations.md, 'A restore that cannot "
    "finish')."
)


def _restore_marker(ctx: Context) -> dict[str, Any] | None:
    """The restore marker's content; ``{}`` for one that cannot be read; ``None`` for none."""
    marker = ops_dir(ctx) / RESTORE_MARKER
    try:
        series = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return series if isinstance(series, dict) else {}


def restore_in_progress(ctx: Context) -> str | None:
    """The archive of a restore (or ``--undo``) whose state is not in place yet, or ``None``.

    A marker in the last phase, ``start_owed`` (the state is in place, only the start of the
    stack is owed), is not "in progress" for the guards: see ``restore_start_owed``.
    """
    series = _restore_marker(ctx)
    if series is None or series.get("start_owed"):
        return None
    return str(series.get("archive", "an archive"))


def restore_start_owed(ctx: Context) -> str | None:
    """What a restore or ``--undo`` that put the state in place but was cut off before it started
    the stack did ("the restore of …" / "the undo of …"), or ``None``. The same command run
    again and ``cave update`` (at boot) start the stack for it."""
    series = _restore_marker(ctx)
    if not series or not series.get("start_owed"):
        return None
    what = "the undo of the restore of" if series.get("undone") else "the restore of"
    return f"{what} {series.get('archive', 'an archive')}"


def give_to_checkout_owner(ctx: Context, path: Path) -> None:
    """Make ``path`` the checkout owner's, so that Git running as that user can write there."""
    if is_root():
        info = ctx.paths.root.stat()
        os.chown(path, info.st_uid, info.st_gid)


# ---------------------------------------------------------------------------- files with owners
NOT_STATE = ("lock", OPS_DIR, "stopped.json", "watch.json")
"""Entries of ``CAVE_DATA/.cave`` that are records of the commands, not state: the lock, the
``ops`` directory, the record of a stack stopped on purpose and what ``cave watch`` has said
(``records.py``, ``alert.py``). No snapshot, backup or restore moves them."""


def state_entries(data: Path) -> list[str]:
    """What a snapshot or backup holds, relative to ``CAVE_DATA``: every top-level entry, and
    inside ``.cave`` everything except the commands' own records (``NOT_STATE``) and their
    temporary files."""
    if not data.is_dir():
        return []
    entries = sorted(name for name in os.listdir(data) if name != ".cave")
    meta = data / ".cave"
    if meta.is_dir():
        entries += sorted(f".cave/{name}" for name in os.listdir(meta) if not _a_record(name))
    return entries


def _a_record(name: str) -> bool:
    """One of ``NOT_STATE``, or a temporary file of one (``.<name>.tmp``, ``.<name>.<writer>.tmp``,
    ``records.write_atomic``)."""
    if name in NOT_STATE:
        return True
    return name.endswith(".tmp") and any(
        name == f".{record}.tmp" or name.startswith(f".{record}.") for record in NOT_STATE
    )


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


write_atomic = records.write_atomic


# ------------------------------------------------------------------------------------- archives
class UnsafeArchive(CaveError):
    """An archive member that would land outside the target or is of an unexpected kind."""

    def __init__(self, member: str, why: str) -> None:
        super().__init__(f"refusing archive member {member!r}: {why}")


def safe_link_target(target: str) -> bool:
    """The one rule for symbolic links in a backup: relative, and no ``..`` anywhere.

    Such a link can only point at or below its own directory. Together with "nothing in the
    archive lies below a link" it cannot resolve outside the tree, whatever other links its path
    passes through and in whatever order they are created. The state of Home Assistant,
    Mosquitto and Zigbee2MQTT needs nothing else: none of them keeps links in its data as this
    system configures them (Zigbee2MQTT's ``log/current`` link exists only with
    ``advanced.log_symlink_current``, which the template leaves off, and points at a sibling
    log directory). ``backup`` leaves out any link that breaks the rule, so every archive it
    writes can be restored.
    """
    if not target or target.startswith("/"):
        return False
    return ".." not in PurePosixPath(target).parts


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
    if member.issym() and not safe_link_target(member.linkname):
        raise UnsafeArchive(
            member.name,
            f"the link to {member.linkname!r} is absolute or contains '..'; backups have none",
        )
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


RESERVED = (f".cave/{OPS_DIR}", ".cave/lock")
"""No state link may point at or below these, nor at a directory that contains them (``.cave``,
the state directory itself): the commands' own records, snapshots and lock."""


def touches_reserved(lands: PurePosixPath, reserved: tuple[str, ...] = RESERVED) -> str | None:
    """The reserved path that a link landing on ``lands`` (relative to the state) would expose."""
    for place in reserved:
        reserved_path = PurePosixPath(place)
        if (
            lands == reserved_path
            or reserved_path in lands.parents
            or lands in reserved_path.parents
        ):
            return place
    return None


Owners = Callable[[tarfile.TarInfo], tuple[int, int]]


def _inside(path: Path, root: Path) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(root)
    return real == top or real.startswith(top + os.sep)


def extract(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    target: Path,
    owners: Owners | None = None,
    reserved: tuple[str, ...] = (),
) -> None:
    """Extract ``members`` (whose names are relative to ``target``, an empty directory) with
    their modes, times and, as root, owners (``owners(member)`` maps them, for example from the
    old server's service user to this one's).

    Modes keep only the permission bits: an archive cannot plant a setuid or setgid file. No
    link may point at or below a path in ``reserved``.

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
        os.chmod(path, member.mode & 0o777)
        os.utime(path, (member.mtime, member.mtime))
    for path, member in links:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(path.parent, target):
            raise UnsafeArchive(member.name, "its directory resolves outside the target")
        os.symlink(member.linkname, path)
        if is_root():
            os.lchown(path, *owner_of(member))
    # Once the whole tree exists, every link in it must resolve inside it. (The rule above
    # already guarantees this; checking the finished tree does not depend on that reasoning.)
    for current, dirs, files in os.walk(target):
        for name in (*dirs, *files):
            path = Path(current) / name
            if not path.is_symlink():
                continue
            if not _inside(path, target):
                raise UnsafeArchive(
                    str(path.relative_to(target)),
                    f"it resolves outside, to {os.path.realpath(path)}",
                )
            real = Path(os.path.realpath(path)).relative_to(os.path.realpath(target))
            lands = PurePosixPath(real.as_posix())
            place = touches_reserved(lands, reserved)
            if place is not None:
                raise UnsafeArchive(
                    str(path.relative_to(target)),
                    f"it points into {place}, or at a directory that contains it, which is "
                    "reserved",
                )
    for path, member in reversed(directories):
        if is_root():
            os.chown(path, *owner_of(member))
        os.chmod(path, member.mode & 0o777)
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
