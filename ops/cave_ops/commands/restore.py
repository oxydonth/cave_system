"""``cave restore ARCHIVE``: put back the state and ``.env`` from a ``cave backup`` archive.

Also the way onto a new server: clone the repository, then ``sudo ./cave restore <archive>``
instead of ``./cave init``. In this order:

1. Read and check the whole archive (every member below its target, its compressed data
   intact) and decide the state directory: ``--data``, else ``CAVE_DATA`` of the existing
   ``.env``, else the one the archive was taken from. Refuse an archive from a newer release
   than the checkout, whose state that release may not read (``--force`` overrides).
2. Holding the lock, stop the stack if there is one.
3. Unpack the state into ``CAVE_DATA/.cave/ops/restore-staging``, then move the current state
   entries aside into ``CAVE_DATA/.cave/ops/before-restore-<time>`` and the restored ones into
   place. Nothing is deleted: remove the set-aside copy once the restored system works.
4. Write the archived ``.env`` (the current one is kept as ``.env.before-restore-<time>``),
   with ``CAVE_DATA`` set to the state directory chosen in step 1.
5. Start the stack and wait for healthy, unless ``--no-start``.

Runs as root, so that every file gets its owner back. An interrupted restore is finished by
running the same command again.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..context import Context
from ..envfile import HEADER, NAMES, load_env, parse_env, render
from ..errors import CaveError
from ..health import wait_healthy
from ..notify import notify
from ..paths import Paths, default_data_dir
from . import _archives, _releases, _server
from ._update_state import BEFORE_SWITCH, Switch, now
from ._update_state import load as load_update_record
from ._update_state import save as save_update_record

HELP = "Restore the state and .env from a backup archive (also onto a new server)"

TITLE = "cave restore"
OPERATIONS = "docs/operations.md"
SPACE_MARGIN = 256 * 1024 * 1024


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("archive", help="a cave-backup-*.tar.gz written by `cave backup`")
    parser.add_argument(
        "--data",
        metavar="DIR",
        help="state directory to restore into (default: CAVE_DATA of the existing .env, else "
        "the default state directory, /var/lib/cave on a provisioned server); never taken from "
        "the archive",
    )
    parser.add_argument("--no-start", action="store_true", help="do not start the stack afterwards")
    parser.add_argument(
        "--force",
        action="store_true",
        help="restore an archive taken with a newer release than the one checked out, or into "
        "a directory that is neither empty nor a cave state directory",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        metavar="SECONDS",
        help="how long to wait for healthy after the start (default 900)",
    )


class _Archive:
    """A checked archive: its manifest, ``.env`` text and the members below ``data/``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            with tarfile.open(path, "r:gz") as archive:
                members = archive.getmembers()  # reads to the end: the gzip check runs
                names = {member.name: member for member in members}
                if "manifest.json" not in names or "env" not in names:
                    raise CaveError(f"{path} is not a cave backup: no manifest.json or env")
                self.manifest: dict[str, Any] = json.loads(_read(archive, names["manifest.json"]))
                self.env_text = _read(archive, names["env"])
        except (OSError, tarfile.TarError, EOFError, ValueError) as error:
            raise CaveError(f"{path} cannot be read as a cave backup: {error}") from error
        if self.manifest.get("format") != _archives.FORMAT:
            raise CaveError(f"{path} has backup format {self.manifest.get('format')}, not 1")
        self.data: list[tarfile.TarInfo] = []
        for member in members:
            if member.name in ("manifest.json", "env"):
                continue
            relative = _server.check_member(member, "data")
            if relative == PurePosixPath("."):
                continue
            self.data.append(_renamed(member, relative.as_posix()))
        for member in self.data:
            parts = PurePosixPath(member.name).parts
            if parts == (".cave",) and not member.isdir():
                raise _server.UnsafeArchive(f"data/{member.name}", ".cave must be a directory")
            if parts[:2] in ((".cave", _server.OPS_DIR), (".cave", "lock")):
                raise _server.UnsafeArchive(
                    f"data/{member.name}", "the commands' own records and lock are never backed up"
                )
        # The whole archive: no path twice, nothing below a link it creates. extract() checks
        # again where each member really lands.
        _server.check_members(self.data)
        self.size = sum(member.size for member in self.data if member.isreg())
        owner = self.manifest.get("owner") or {}
        self.owner = (owner.get("uid"), owner.get("gid"))

    def extract(self, target: Path, owners: _server.Owners) -> None:
        with tarfile.open(self.path, "r:gz") as archive:
            wanted = {f"data/{m.name}" for m in self.data}
            members = []
            for member in archive:
                if member.name in wanted:
                    members.append(_renamed(member, member.name[len("data/") :]))
            _server.extract(archive, members, target, owners, reserved=RESERVED)

    def owners(self, local_uid: int, local_gid: int) -> _server.Owners:
        """Files of the old server's service user become this server's service user's: its uid
        depends on the order in which users were created. The manifest says which ids those were;
        root's (0) are never mapped, whatever it says, so that root's files stay root's."""
        old_uid, old_gid = self.owner
        old_uid = old_uid if isinstance(old_uid, int) and old_uid != 0 else None
        old_gid = old_gid if isinstance(old_gid, int) and old_gid != 0 else None

        def owner_of(member: tarfile.TarInfo) -> tuple[int, int]:
            uid = local_uid if old_uid is not None and member.uid == old_uid else member.uid
            gid = local_gid if old_gid is not None and member.gid == old_gid else member.gid
            return uid, gid

        return owner_of


def _renamed(member: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    renamed = copy.copy(member)
    renamed.name = name
    return renamed


def _read(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    handle = archive.extractfile(member)
    if handle is None:
        raise CaveError(f"{member.name} in the archive has no content")
    with handle:
        return handle.read().decode("utf-8")


def run(ctx: Context, args: argparse.Namespace) -> int:
    source = Path(args.archive).expanduser().resolve()
    if not source.is_file():
        raise CaveError(f"{source} does not exist")
    ctx.say(f"Checking {source} ...")
    archive = _Archive(source)

    archived_env = parse_env(archive.env_text)
    data, why = _target(ctx, args)
    ctx.say(f"Restoring into {data} ({why}).")
    taken_from = archived_env.get("CAVE_DATA")
    if taken_from and Path(taken_from).expanduser() != data:
        ctx.say(f"The archive was taken from {taken_from}; the restored .env will name {data}.")
    if not _is_state_dir(data) and not args.force:
        raise CaveError(
            f"{data} is neither empty nor a cave state directory (it has none of .cave, "
            f"{', '.join(SERVICE_ENTRIES)}). Choose the state directory with --data DIR, or pass "
            "--force to restore into it anyway. Nothing was changed."
        )
    ctx.paths = Paths(root=ctx.paths.root, data=data)

    _server.require_state_access(ctx, "restore")
    if not _server.is_root():
        foreign = [m.name for m in archive.data if m.uid != _server.current_uid()]
        if foreign:
            raise CaveError(
                f"`cave restore` must run as root: {len(foreign)} file(s) in the archive belong "
                f"to other users (for example {foreign[0]}), and only root can give them back "
                "their owners. Run `sudo ./cave restore ...`. Nothing was changed."
            )
    # An interrupted update is superseded by the restore: the restore puts back the release from
    # before that update with the archive's state, and forgets the update, so that its recovery
    # can never put its snapshot back over the restored state later. So the archive is checked
    # against that release. Checked before anything is created, and again under the lock.
    before = load_update_record(ctx).switch
    _check_release(ctx, archive.manifest, args.force, before.from_commit if before else "HEAD")
    _server.cave_dir(ctx).mkdir(parents=True, exist_ok=True)
    with ctx.lock("restore"):
        switch = load_update_record(ctx).switch
        if switch != before:
            commit = switch.from_commit if switch else "HEAD"
            _check_release(ctx, archive.manifest, args.force, commit)
        return _restore(ctx, args, archive, archived_env, switch)


SERVICE_ENTRIES = ("homeassistant", "mosquitto", "zigbee2mqtt")
STEERING = ("CAVE_BACKUP_DIR", "CAVE_BACKUP_COPY_TO", "CAVE_Z2M_SERIAL_PORT", "COMPOSE_PROFILES")
"""Restored settings that decide where the nightly backup writes or what is passed to a
container; the restore names them so that the owner can check them."""
RESERVED = (f".cave/{_server.OPS_DIR}", ".cave/lock")
"""No restored link may point at or below these: the commands' own records, snapshots and lock."""


def _target(ctx: Context, args: argparse.Namespace) -> tuple[Path, str]:
    """Where to restore, and why there. Never from the archive: its ``.env`` could name any
    directory, and root would then move that directory's contents aside and write into it."""
    if args.data:
        return Path(args.data).expanduser().resolve(), "given with --data"
    named = load_env(ctx.paths.env_file).get("CAVE_DATA")
    if named:
        return Path(named).expanduser().resolve(), f"CAVE_DATA in {ctx.paths.env_file}"
    default = default_data_dir(ctx.paths.root, os.environ)
    return default, "no .env yet: the default state directory"


def _is_state_dir(path: Path) -> bool:
    """Missing, empty, or what ``init``, ``restore`` or the services made: safe to restore into."""
    if not path.exists():
        return True
    if not path.is_dir() or path.is_symlink():
        return False
    names = set(os.listdir(path))
    return not names or bool(names & {".cave", *SERVICE_ENTRIES})


def _check_release(ctx: Context, manifest: dict[str, Any], force: bool, commit: str) -> None:
    taken = _releases.parse_tag(manifest.get("release_tag") or "")
    merged = _server.git(ctx, "tag", "--list", _releases.TAG_PATTERN, "--merged", commit)
    installed = _releases.newest(merged.stdout.split()) if merged.ok else None
    if taken is not None and (installed is None or installed < taken) and not force:
        raise CaveError(
            f"the archive was taken with {taken}, but the checkout has "
            f"{installed or 'no release'}: an older release may not read newer state. Check out "
            f"{taken} first (`git checkout {taken}` as the owner of the checkout), or pass "
            "--force. Nothing was changed."
        )


def _restore(
    ctx: Context,
    args: argparse.Namespace,
    archive: _Archive,
    archived_env: dict[str, str],
    switch: Switch | None,
) -> int:
    data = ctx.paths.data
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    ops = _server.ops_dir(ctx)
    staging = ops / "restore-staging"
    _server.remove(staging)  # left by an interrupted restore

    existing = data
    while not existing.exists():
        existing = existing.parent
    needed = archive.size + SPACE_MARGIN
    free = _server.free_bytes(existing)
    if free < needed:
        repeat = _server.restore_in_progress(ctx) is not None
        raise CaveError(
            f"unpacking needs about {needed // 2**20} MiB, but only {free // 2**20} MiB are free "
            f"on {existing}. "
            + (
                "The stack is stopped and the state half restored from an earlier attempt: free "
                "space (older .cave/ops/before-restore-* directories, old archives in "
                "CAVE_BACKUP_DIR, `sudo docker image prune -a`) and run it again, or restore "
                f"another archive. See {OPERATIONS}, 'A restore that cannot finish'."
                if repeat
                else "Nothing was changed."
            )
        )

    elsewhere = _server.entries_elsewhere(ctx) if data.is_dir() else []
    if elsewhere:
        raise CaveError(
            f"{', '.join(elsewhere)} in {data} is on another file system than the rest of the "
            "state; the restore moves entries by renaming and cannot replace it. Nothing was "
            "changed."
        )

    was_running = ctx.paths.env_file.is_file()
    if was_running:
        ctx.say("Stopping the stack ...")
        down = ctx.compose("down", timeout=300.0)
        if not down.ok:
            raise CaveError(
                f"the stack could not be stopped ({down.output.strip()[-300:]}); nothing was "
                "restored"
            )

    info = ctx.paths.root.stat()
    marker = ops / _server.RESTORE_MARKER
    try:
        ctx.say(f"Unpacking into {staging} ...")
        staging.mkdir(parents=True, mode=0o700)
        archive.extract(staging, archive.owners(info.st_uid, info.st_gid))
        _server.sync()
        # Only now, with everything unpacked: the release from before an interrupted update, and
        # the marker that makes an interrupted move visible (`status`) and stops `update` and
        # `backup` until the restore is run again.
        if switch is not None:
            ctx.say(f"Checking out {switch.from_label}, the release before the interrupted update")
            failed, kept = _server.check_out_exactly(
                ctx,
                switch.from_commit,
                switch.to_commit,
                remove_added=switch.phase != BEFORE_SWITCH,
            )
            if failed is not None:
                raise CaveError(f"{switch.from_label} could not be checked out: {failed}")
            if kept:
                ctx.warn("Left in place, not the update's files: " + ", ".join(kept))
        _server.write_atomic(marker, json.dumps({"archive": str(archive.path)}).encode(), 0o644)
        if switch is not None:
            # From here on the restore, not the update's snapshot, decides the state: a recovery
            # must never put that snapshot back over the restored state.
            record = load_update_record(ctx)
            record.failed.setdefault(
                switch.to_release,
                {"time": now(), "reason": "interrupted, then superseded by a restore"},
            )
            record.switch = None
            save_update_record(ctx, record)
    except (OSError, tarfile.TarError, EOFError, CaveError) as error:
        _server.remove(staging)
        marker.unlink(missing_ok=True)
        return _unpack_failed(ctx, args, error, was_running)

    aside = ops / f"before-restore-{stamp}"
    counter = 1
    while aside.exists():  # a repeated restore in the same second keeps both
        counter += 1
        aside = ops / f"before-restore-{stamp}-{counter}"
    restored = _server.state_entries(staging)
    current = _server.state_entries(data)
    try:
        if current:
            ctx.say(f"Moving the current state aside to {aside} ...")
            aside.mkdir(mode=0o700)
        for entry in current:
            (aside / entry).parent.mkdir(parents=True, exist_ok=True)
            os.rename(data / entry, aside / entry)
        for entry in restored:
            (data / entry).parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging / entry, data / entry)
    except OSError as error:
        raise CaveError(
            f"the restore stopped half-way while moving the state into place: {error}. The stack "
            "is stopped, and update and backup wait for the restore. Run the same restore again; "
            "if it stops at the same place again, restore another archive (an older one). The "
            f"state from before is in {aside}. See {OPERATIONS}, 'A restore that cannot finish'."
        ) from error
    _server.remove(staging)
    _server.sync()
    abandoned = ""
    if switch is not None:
        abandoned = (
            f" The interrupted update to {switch.to_release} was abandoned: {switch.from_label} "
            "is checked out."
        )

    env_file = ctx.paths.env_file
    if env_file.exists():
        os.replace(env_file, env_file.with_name(f".env.before-restore-{stamp}"))
    # Only the variables of concept section 4.2, so that the archive cannot set anything else
    # that Compose reads from .env (COMPOSE_FILE, COMPOSE_PROJECT_NAME, ...); CAVE_DATA is the
    # directory actually used.
    values = {name: value for name, value in archived_env.items() if name in NAMES}
    values["CAVE_DATA"] = str(data)
    ignored = sorted(name for name in archived_env if name not in NAMES)
    _server.write_atomic(env_file, (HEADER + "\n" + render(values)).encode(), 0o600)
    if _server.is_root():
        info = ctx.paths.root.stat()
        os.chown(env_file, info.st_uid, info.st_gid)
    ctx.env.update(load_env(env_file))
    marker.unlink(missing_ok=True)
    # As `init` does: Docker would create a missing mount source as root inside the checkout.
    tools = ctx.paths.root / "tools"
    if not tools.exists():
        tools.mkdir()
        _server.give_to_checkout_owner(ctx, tools)

    done = (
        f"Restored {archive.path.name} (taken {archive.manifest.get('created')} with "
        f"{archive.manifest.get('release')}) into {data}."
    )
    done += abandoned
    if ignored:
        done += (
            " Variables in the archive's .env that cave does not know were left out: "
            + ", ".join(ignored)
            + "."
        )
    # Settings from the archive that decide where root writes later: say them, to be checked.
    steering = [f"{name}={values[name]}" for name in STEERING if values.get(name)]
    if steering:
        done += " From the archive's .env, check: " + ", ".join(steering) + "."
    if current:
        done += f" The state before the restore is kept in {aside}; remove it when all is well."
    if args.no_start:
        ctx.say(f"{done} Not started (--no-start): run `./cave up` when ready.")
        return 0
    ctx.say("Starting the stack ...")
    started = ctx.compose("up", "--detach", "--force-recreate", timeout=900.0)
    if not started.ok:
        message = f"{done} But the stack could not be started: {started.output.strip()[-300:]}"
        ctx.warn(message)
        notify(ctx, TITLE, message)
        return 1
    health = wait_healthy(ctx, timeout=args.timeout)
    message = f"{done} The stack is {health.describe()}."
    (ctx.say if health.healthy else ctx.warn)(message)
    notify(ctx, TITLE, message)
    return 0 if health.healthy else 1


def _unpack_failed(
    ctx: Context, args: argparse.Namespace, error: BaseException, was_running: bool
) -> int:
    """Nothing of the state was touched. Start what was running; if an update was interrupted,
    finish that update's way back instead, because its release and state belong together."""
    pending = load_update_record(ctx).switch
    head = f"The restore failed while unpacking: {error}. Nothing was restored."
    if pending is not None:
        from .update import _Update

        recover = argparse.Namespace(force=False, auto=False, recover=True, timeout=args.timeout)
        status = _Update(ctx, recover).finish_interrupted()
        message = (
            f"{head} The interrupted update to {pending.to_release} was finished instead (exit "
            f"status {status}, see its own message)."
        )
    elif was_running:
        started = ctx.compose("up", "--detach", "--force-recreate", timeout=900.0)
        message = (
            f"{head} The stack was started again unchanged."
            if started.ok
            else f"{head} The stack could NOT be started again: {started.output.strip()[-300:]}"
        )
    else:
        message = head
    ctx.warn(message)
    notify(ctx, TITLE, message)
    return 1
