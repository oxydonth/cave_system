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
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from .. import alert
from ..context import Context
from ..envfile import HEADER, NAMES, load_env, parse_env, quote_value, render
from ..errors import CaveError
from ..health import check_health, wait_healthy
from ..notify import notify
from ..paths import Paths, default_data_dir
from ..records import Stopped, clear_stopped, note_stopped, stopped
from . import _archives, _releases, _server
from ._update_state import BEFORE_SWITCH, Switch, complete_undo, now
from ._update_state import load as load_update_record
from ._update_state import save as save_update_record

HELP = "Restore the state and .env from a backup archive (also onto a new server)"

TITLE = "cave restore"
OPERATIONS = "docs/operations.md"
FREE_SPACE = (
    "old archives in CAVE_BACKUP_DIR, but not the one being restored nor the one you would "
    "fall back to; unused images with `sudo docker image prune`, without -a, which with the "
    "stack stopped would also remove its own images, which `up` would then have to download "
    "again"
)
"""What a restore may suggest deleting while the stack is stopped, and what not."""
SPACE_MARGIN = 256 * 1024 * 1024


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "archive", nargs="?", help="a cave-backup-*.tar.gz written by `cave backup`"
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="put back the state from before a restore that stopped half-way (no archive)",
    )
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
    if args.undo:
        if args.archive:
            raise CaveError("give either an archive or --undo, not both")
        return undo(ctx, args)
    if not args.archive:
        raise CaveError("name the archive to restore, or give --undo")
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
    complete_undo(ctx)  # an --undo cut off after it had put the state back
    earlier = _read_marker(_server.ops_dir(ctx) / _server.RESTORE_MARKER)
    if earlier is not None and not earlier["aside"]:
        raise CaveError(_OLD_MARKER.format(marker=_server.ops_dir(ctx) / _server.RESTORE_MARKER))
    before = load_update_record(ctx).switch
    _check_release(ctx, archive.manifest, args.force, before.from_commit if before else "HEAD")
    if not data.exists():
        # A new state directory belongs to the service user, as `init` would make it; the lock
        # then gives `.cave` and the lock file to that owner too.
        data.mkdir(parents=True, mode=0o750)
        _server.give_to_checkout_owner(ctx, data)
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
RESERVED = _server.RESERVED


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
    # A stack stopped on purpose (`./cave down`) stays so until this restore changes something:
    # the record is cleared where its marker is first written. Whether it was stopped is decided
    # below, by what runs, right before the restore stops the stack.
    stopped_before: Stopped | None = None
    staging = ops / "restore-staging"
    _server.remove(staging)  # left by an interrupted restore

    existing = data
    while not existing.exists():
        existing = existing.parent
    needed = archive.size + SPACE_MARGIN
    free = _server.free_bytes(existing)
    if free < needed:
        pending = _half_way(_read_marker(ops / _server.RESTORE_MARKER))
        raise CaveError(
            f"unpacking needs about {needed // 2**20} MiB, but only {free // 2**20} MiB are free "
            f"on {existing}. "
            + (
                "The stack is stopped and the state half restored from an earlier attempt. Free "
                f"space ({FREE_SPACE}) and run it again, or restore another archive. "
                + _keep(ops, pending)
                + f" See {OPERATIONS}, 'A restore that cannot finish'."
                if pending is not None
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
        # The record of a stop is a statement of intent, and it can be stale (a start without
        # `cave`, an `up` or `down` cut off between its steps). What runs is the fact: only a
        # stack of which nothing runs counts as stopped on purpose, so that a restore that then
        # cannot unpack leaves it as it was; one that runs is started again.
        if check_health(ctx, api=False).running == 0:
            stopped_before = stopped(ctx.paths)
        ctx.say("Stopping the stack ...")
        down = ctx.compose("down", timeout=300.0)
        if not down.ok:
            raise CaveError(
                f"the stack could not be stopped ({down.output.strip()[-300:]}); nothing was "
                "restored"
            )

    info = ctx.paths.root.stat()
    marker = ops / _server.RESTORE_MARKER
    recorded = _read_marker(marker)
    earlier = _half_way(recorded)  # a restore that stopped half-way: this is a repeat
    # A restore before this one whose state is in place and whose start is still owed. This
    # restore is a new one: it must not inherit "only the start is owed".
    owed = recorded if earlier is None else None
    try:
        ctx.say(f"Unpacking into {staging} ...")
        staging.mkdir(parents=True, mode=0o700)
        archive.extract(staging, archive.owners(info.st_uid, info.st_gid))
        _server.sync()
        # Only now, with everything unpacked: the release from before an interrupted update, and
        # the marker that makes an interrupted move visible (`status`) and stops `update` and
        # `backup` until the restore is run again or undone.
        if switch is not None:
            ctx.say(f"Checking out {switch.from_label}, the release before the interrupted update")
            failed, kept, removed = _server.check_out_exactly(
                ctx,
                switch.from_commit,
                switch.to_commit,
                remove_added=switch.phase != BEFORE_SWITCH,
            )
            if failed is not None:
                raise CaveError(f"{switch.from_label} could not be checked out: {failed}")
            if kept:
                ctx.warn("Left in place, not the update's files: " + ", ".join(kept))
            if removed:
                ctx.warn(
                    f"Removed, because they are {switch.to_release}'s own files, byte for byte "
                    f"(Git gives them back with {switch.to_release}): " + ", ".join(removed)
                )
        if earlier is None:
            aside_name = f"before-restore-{stamp}"
            counter = 1
            while (ops / aside_name).exists():
                counter += 1
                aside_name = f"before-restore-{stamp}-{counter}"
            series: dict[str, Any] = {
                "archive": str(archive.path),
                "aside": aside_name,
                "original_entries": _server.state_entries(data),
                "env_backup": None,
                # The update this restore supersedes: --undo puts it back in flight, so that the
                # state from before never runs under the release the restore checked out.
                "superseded_update": asdict(switch) if switch is not None else None,
            }
        else:
            series = {**earlier, "archive": str(archive.path)}
            if switch is not None and not series.get("superseded_update"):
                series["superseded_update"] = asdict(switch)
        _write_marker(marker, series)
        # From here on the state changes: whatever stopped the stack before, the restore decides
        # now (its start, its --no-start, or its marker if it is cut off).
        clear_stopped(ctx.paths, ctx.wall())
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
        if earlier is not None:
            return _repeat_failed(ctx, error, ops, earlier)
        if owed is None:
            marker.unlink(missing_ok=True)
            return _unpack_failed(
                ctx,
                args,
                error,
                was_running,
                archive=archive.path.name,
                stopped_before=stopped_before,
            )
        # Nothing of the state was touched, so the restore before this one is still complete
        # and its start still owed: that stays on record until the stack has been started.
        _write_marker(marker, owed)
        return _unpack_failed(
            ctx, args, error, was_running, owed_start=marker, archive=archive.path.name
        )

    # The whole state from before the first attempt goes into one directory, `aside`, over all
    # repeats: an original entry is moved there; whatever an earlier attempt moved in (an entry
    # that was not there before, or one whose original is in `aside` already) is discarded.
    aside = ops / series["aside"]
    originals = set(series["original_entries"])
    restored = _server.state_entries(staging)
    current = _server.state_entries(data)
    try:
        if current:
            ctx.say(f"Moving the current state aside to {aside} ...")
            aside.mkdir(mode=0o700, exist_ok=True)
        for entry in current:
            if entry in originals and not os.path.lexists(aside / entry):
                (aside / entry).parent.mkdir(parents=True, exist_ok=True)
                os.rename(data / entry, aside / entry)
            else:
                _server.remove(data / entry)
        for entry in restored:
            (data / entry).parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging / entry, data / entry)
    except OSError as error:
        alert.warn_owner(
            ctx,
            "restore",
            "restore stopped half-way",
            "A restore stopped half-way while it moved the state into place: the state is half "
            "restored, and the stack is stopped.",
            about_the_stack=True,
        )
        raise CaveError(
            f"the restore stopped half-way while moving the state into place: {error}. The stack "
            "is stopped, and update and backup wait for the restore. Run the same restore again; "
            "if it stops at the same place again, restore another archive (an older one), or put "
            f"the state from before back with `sudo ./cave restore --undo`. It is in {aside}. See "
            f"{OPERATIONS}, 'A restore that cannot finish'."
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
    if env_file.exists() and series.get("env_backup") is None:
        # A copy, and .env itself is then replaced in one step: there is always a .env, which
        # names the state directory for a repeat or --undo after a kill.
        # Named like the `before-restore-*` directories: two restores within one second must
        # not overwrite the first one's copy of the owner's .env.
        backup = env_file.with_name(f".env.before-restore-{stamp}")
        counter = 1
        while os.path.lexists(backup):
            counter += 1
            backup = env_file.with_name(f".env.before-restore-{stamp}-{counter}")
        _server.write_atomic(backup, env_file.read_bytes(), 0o600)
        if _server.is_root():
            # The checkout is the service user's; a file of root's in it is one the service user
            # cannot read (found by the server rehearsal).
            # lchown: the directory is the service user's, so never through a link.
            info = ctx.paths.root.stat()
            os.lchown(backup, info.st_uid, info.st_gid)
        series["env_backup"] = backup.name
        _write_marker(marker, series)
    # Only the variables of concept section 4.2 (`render` writes exactly the table), so that the
    # archive cannot set anything else that Compose reads from .env (COMPOSE_FILE,
    # COMPOSE_PROJECT_NAME, ...); CAVE_DATA is the directory actually used.
    values = {**archived_env, "CAVE_DATA": str(data)}
    ignored = sorted(name for name in archived_env if name not in NAMES)
    for name in [name for name in NAMES if name in values]:
        try:
            quote_value(values[name])
        except ValueError:
            # Cannot be written so that Compose and cave read the same: the default is written,
            # and the owner is told to set it again.
            del values[name]
            ignored.append(f"{name} (its value cannot be written unchanged; set it again)")
    _server.write_atomic(env_file, (HEADER + "\n" + render(values)).encode(), 0o600)
    if _server.is_root():
        info = ctx.paths.root.stat()
        os.lchown(env_file, info.st_uid, info.st_gid)
    alert_url = ctx.env.get(alert.VARIABLE, "")
    ctx.env.update(load_env(env_file))
    # This restore's own alerts still go where alerts went when it started (if anywhere); the
    # restored .env decides for every later command. The URL may carry a token: never print it.
    url_changed = ctx.env.get(alert.VARIABLE, "") != alert_url
    if alert_url:
        ctx.env[alert.VARIABLE] = alert_url
    # The state is in place. The restore is finished only once the stack is started (or the
    # owner chose --no-start): until then the marker says so (`start_owed`), and the same command
    # or the boot's `cave update --recover` starts the stack.
    if args.no_start:
        note_stopped(
            ctx.paths, f"the restore of {archive.path.name} finished with --no-start", ctx.wall()
        )
        marker.unlink(missing_ok=True)
    else:
        series["start_owed"] = True
        _write_marker(marker, series)
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
    if url_changed:
        done += (
            f" {alert.VARIABLE} is now the archive's ("
            + ("set" if values.get(alert.VARIABLE) else "empty: no alerts")
            + "); check it in .env."
        )
    if current:
        done += f" The state before the restore is kept in {aside}; remove it when all is well."
    if args.no_start:
        ctx.say(f"{done} Not started (--no-start): run `sudo -u cave ./cave up` when ready.")
        return 0
    return _start(ctx, args, marker, done)


def _start(ctx: Context, args: argparse.Namespace, marker: Path, done: str) -> int:
    """Start the stack for a restore or an undo whose state is in place; remove the marker only
    once the start was issued, so that an interruption before it leaves `start_owed` for the
    same command or the boot's `cave update --recover` to finish."""
    ctx.say("Starting the stack ...")
    # The record of a stack stopped on purpose was cleared when this restore or undo first
    # changed the state, earlier in this same run.
    started = ctx.compose("up", "--detach", "--force-recreate", timeout=900.0)
    if not started.ok:
        message = (
            f"{done} But the stack could not be started: {started.output.strip()[-300:]}. The "
            "next `cave update` (at boot, or `sudo ./cave update --recover`) tries again."
        )
        ctx.warn(message)
        notify(ctx, TITLE, message)
        alert.warn_owner(
            ctx,
            "restore",
            "restored stack not started",
            "The state was restored, but the stack could not be started. The next start of the "
            "server tries again.",
            about_the_stack=True,
        )
        return 1
    marker.unlink(missing_ok=True)
    health = wait_healthy(ctx, timeout=args.timeout)
    message = f"{done} The stack is {health.describe()}."
    (ctx.say if health.healthy else ctx.warn)(message)
    notify(ctx, TITLE, message)
    if not health.healthy:
        alert.warn_owner(
            ctx,
            "restore",
            "restored stack not healthy",
            "The state was restored and the stack started, but it is not healthy.",
            health,
            about_the_stack=True,
        )
    return 0 if health.healthy else 1


def _unpack_failed(
    ctx: Context,
    args: argparse.Namespace,
    error: BaseException,
    was_running: bool,
    owed_start: Path | None = None,
    archive: str = "an archive",
    stopped_before: Stopped | None = None,
) -> int:
    """Nothing of the state was touched. Start what was running; if an update was interrupted,
    finish that update's way back instead, because its release and state belong together.

    ``owed_start`` is the marker of a restore before this one whose state is in place and whose
    start is still owed: it is removed only once the start was issued, as in ``_start``.

    With ``--no-start`` nothing is started: the stack stays stopped, on purpose
    (``records.py``). The state in place is complete (this restore changed nothing), so an owed
    start of the restore before is given up for it; an interrupted update stays in flight, and
    the next ``cave update`` (the boot's) finishes its way back.

    A stack of which nothing ran, stopped on purpose before the restore (``stopped_before``,
    decided by what ran, not by the record alone), is not started
    either: the restore changed nothing, so it stays as it was, record and all."""
    pending = load_update_record(ctx).switch
    head = f"The restore failed while unpacking: {error}. Nothing was restored."
    if pending is not None and args.no_start:
        message = (
            f"{head} The update to {pending.to_release} is still interrupted; with --no-start its "
            "way back, which starts the stack, was not run. `sudo ./cave update --recover` "
            "finishes it; the next start of the server does too."
        )
    elif pending is not None:
        from .update import _Update

        recover = argparse.Namespace(force=False, auto=False, recover=True, timeout=args.timeout)
        status = _Update(ctx, recover).finish_interrupted()
        message = (
            f"{head} The interrupted update to {pending.to_release} was finished instead (exit "
            f"status {status}, see its own message)."
        )
    elif (was_running or owed_start is not None) and args.no_start:
        note_stopped(
            ctx.paths,
            f"the restore of {archive} failed while unpacking, run with --no-start",
            ctx.wall(),
        )
        if owed_start is not None:
            owed_start.unlink(missing_ok=True)
        waiting = (
            " It holds the complete state of the restore before this one, which had not been "
            "started yet."
            if owed_start is not None
            else ""
        )
        message = (
            f"{head} The stack was not started again (--no-start).{waiting} `sudo -u cave ./cave "
            "up` starts it."
        )
    elif was_running and owed_start is None and stopped_before is not None:
        message = (
            f"{head} The stack was not started: it had been stopped on purpose before the restore "
            f"({stopped_before.what}), and stays so. `sudo -u cave ./cave up` starts it."
        )
    elif was_running:
        clear_stopped(ctx.paths, ctx.wall())
        started = ctx.compose("up", "--detach", "--force-recreate", timeout=900.0)
        message = (
            f"{head} The stack was started again unchanged."
            if started.ok
            else f"{head} The stack could NOT be started again: {started.output.strip()[-300:]}"
        )
        if owed_start is not None and started.ok:
            owed_start.unlink(missing_ok=True)
            message = (
                f"{head} The stack was started on the state of the restore before this one, "
                "which had not been started yet."
            )
        elif owed_start is not None:
            message += (
                ". The next `cave update` (at boot, or `sudo ./cave update --recover`) tries again."
            )
    else:
        message = head
    ctx.warn(message)
    notify(ctx, TITLE, message)
    alert.warn_owner(ctx, "restore", "restore failed", "A restore failed while unpacking.")
    return 1


def _read_marker(marker: Path) -> dict[str, Any] | None:
    """The record of a restore that stopped half-way, or ``None``.

    A marker written by an earlier version names only the archive; it is taken as "the state
    from before was not recorded", which ``--undo`` then refuses to guess.
    """
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = {}
    raw.setdefault("archive", "an archive")
    raw.setdefault("aside", None)
    raw.setdefault("original_entries", [])
    raw.setdefault("env_backup", None)
    return dict(raw)


def _half_way(series: dict[str, Any] | None) -> dict[str, Any] | None:
    """The record, if it is of a restore that stopped half-way, which the next restore repeats.

    A marker in its last phase, ``start_owed``, is of a restore whose state is complete. A
    restore after it is a new one with its own state from before; were it to take that marker
    for an earlier attempt of its own, a cut while it moves the state in would leave "only the
    start is owed" over a half-restored state, and the boot would start the stack on it.
    """
    return None if series is None or series.get("start_owed") else series


def _write_marker(marker: Path, series: dict[str, Any]) -> None:
    _server.write_atomic(marker, json.dumps(series, indent=2).encode(), 0o644)


_OLD_MARKER = (
    "a restore stopped half-way here, recorded by an earlier version of cave in {marker}, which "
    "does not say where the state from before is. Neither a repeat nor --undo can continue it. "
    "Look at the newest .cave/ops/before-restore-* directory (the state from before that "
    "restore), put back what you need by hand, then remove {marker} and restore again. Nothing "
    "was changed."
)


def _keep(ops: Path, series: dict[str, Any] | None) -> str:
    """The sentence that names the one directory that must not be deleted during a series."""
    if not series or not series.get("aside"):
        return ""
    return (
        f"Do not delete {ops / series['aside']}: it holds the only copy of the state from before, "
        "which `--undo` needs."
    )


def _repeat_failed(ctx: Context, error: BaseException, ops: Path, series: dict[str, Any]) -> int:
    """A repeat of a restore that stopped half-way failed before it moved anything. The state is
    still half restored: keep the marker, do not start the stack, say so."""
    message = (
        f"The restore failed while unpacking: {error}. The state is still half restored from the "
        "earlier attempt, so the stack was NOT started and update and backup still wait. Free "
        f"space if that was the cause ({FREE_SPACE}) and run it again, restore another archive, "
        "or put the state from before back with "
        f"`sudo ./cave restore --undo`. {_keep(ops, series)} See {OPERATIONS}, 'A restore that "
        "cannot finish'."
    )
    ctx.warn(message)
    notify(ctx, TITLE, message)
    alert.warn_owner(
        ctx,
        "restore",
        "restore failed again",
        "A repeated restore failed while unpacking: the state is still half restored from the "
        "attempt before, and the stack is stopped.",
        about_the_stack=True,
    )
    return 1


def undo(ctx: Context, args: argparse.Namespace) -> int:
    """``cave restore --undo``: put back the state from before a restore that stopped half-way.

    Everything an attempt moved in is discarded; every original entry comes back from the one
    directory all attempts moved them to (or stays, if no attempt reached it); the ``.env`` from
    before comes back if an attempt replaced it. Then the marker goes, and the stack starts.
    """
    data, why = _target(ctx, args)
    ctx.paths = Paths(root=ctx.paths.root, data=data)
    _server.require_state_access(ctx, "restore")
    ops = _server.ops_dir(ctx)
    marker = ops / _server.RESTORE_MARKER
    with ctx.lock("restore"):
        series = _read_marker(marker)
        if series is None:
            raise CaveError(
                f"there is no restore to undo in {data} ({why}): no restore stopped half-way. "
                "Nothing was changed."
            )
        if not series["aside"]:
            raise CaveError(
                f"the restore that stopped half-way in {data} was made by an earlier version, "
                "which did not record the state from before; it cannot be undone automatically. "
                f"See {OPERATIONS}, 'A restore that cannot finish'. Nothing was changed."
            )
        if ctx.paths.env_file.is_file():
            down = ctx.compose("down", timeout=300.0)
            if not down.ok:
                raise CaveError(
                    f"the stack could not be stopped ({down.output.strip()[-300:]}); nothing was "
                    "undone. Run `sudo ./cave restore --undo` again."
                )
        # From here on the undo changes the state: what stopped the stack before no longer
        # decides; the undo's start, its --no-start, or its marker does.
        clear_stopped(ctx.paths, ctx.wall())
        aside = ops / series["aside"]
        originals = set(series["original_entries"])
        ctx.say(f"Putting back the state from before, from {aside} ...")
        for entry in _server.state_entries(data):
            if not (entry in originals and not os.path.lexists(aside / entry)):
                _server.remove(data / entry)  # moved in by an attempt
        if aside.is_dir():
            for entry in _server.state_entries(aside):
                (data / entry).parent.mkdir(parents=True, exist_ok=True)
                os.rename(aside / entry, data / entry)
        _server.sync()
        env_file = ctx.paths.env_file
        backup = series.get("env_backup")
        if backup and (env_file.parent / backup).is_file():
            os.replace(env_file.parent / backup, env_file)
            if _server.is_root():
                # The .env put back is the checkout owner's, whoever wrote the copy (a copy made
                # by an earlier version is root's).
                info = ctx.paths.root.stat()
                os.lchown(env_file, info.st_uid, info.st_gid)
        series["undone"] = True
        series["no_start"] = bool(args.no_start) or not env_file.is_file()
        _write_marker(marker, series)
        superseded = series.get("superseded_update")
        # The superseded update in flight again (its way back starts the stack), or the marker
        # on to `start_owed`, or, with --no-start, the not-started note.
        complete_undo(ctx)
        _server.remove(aside)
        _server.remove(ops / "restore-staging")
        message = (
            f"The restore that stopped half-way was undone: {data} holds the state from before."
        )
        if isinstance(superseded, dict):
            # That state is the one the interrupted update left: finish the update's way back,
            # which puts its snapshot and the release from before it back together.
            from .update import _Update

            recover = argparse.Namespace(
                force=False, auto=False, recover=True, timeout=args.timeout
            )
            status = _Update(ctx, recover).finish_interrupted()
            message += (
                f" The restore had superseded the interrupted update to "
                f"{superseded.get('to_release')}; that update was put back in flight and its way "
                f"back finished (exit status {status}, see its own message)."
            )
            (ctx.say if status in (1, 2) else ctx.warn)(message)
            notify(ctx, TITLE, message)
            return status
    if series["no_start"]:
        ctx.say(f"{message} Not started: run `sudo -u cave ./cave up` when ready.")
        return 0
    return _start(ctx, args, marker, message)
