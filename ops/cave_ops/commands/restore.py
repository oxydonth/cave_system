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
from ..envfile import apply_updates, load_env, parse_env
from ..errors import CaveError
from ..health import wait_healthy
from ..notify import notify
from ..paths import Paths
from . import _archives, _releases, _server
from ._update_state import Switch, now
from ._update_state import load as load_update_record
from ._update_state import save as save_update_record

HELP = "Restore the state and .env from a backup archive (also onto a new server)"

TITLE = "cave restore"
SPACE_MARGIN = 256 * 1024 * 1024


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("archive", help="a cave-backup-*.tar.gz written by `cave backup`")
    parser.add_argument(
        "--data",
        metavar="DIR",
        help="state directory to restore into (default: CAVE_DATA of .env, else of the archive)",
    )
    parser.add_argument("--no-start", action="store_true", help="do not start the stack afterwards")
    parser.add_argument(
        "--force",
        action="store_true",
        help="restore an archive taken with a newer release than the one checked out",
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
            _server.extract(archive, members, target, owners)

    def owners(self, local_uid: int, local_gid: int) -> _server.Owners:
        """Files of the old server's service user become this server's service user's: its uid
        depends on the order in which users were created."""
        old_uid, old_gid = self.owner

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
    current_env = load_env(ctx.paths.env_file)
    chosen = args.data or current_env.get("CAVE_DATA") or archived_env.get("CAVE_DATA")
    if not chosen:
        raise CaveError("the archive's .env names no CAVE_DATA; give one with --data DIR")
    data = Path(chosen).expanduser().resolve()
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
        raise CaveError(
            f"unpacking needs about {needed // 2**20} MiB, but only {free // 2**20} MiB are free "
            f"on {existing}. Nothing was changed."
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
    try:
        if switch is not None:
            ctx.say(f"Checking out {switch.from_label}, the release before the interrupted update")
            failed = _server.check_out_exactly(ctx, switch.from_commit, switch.to_commit)
            if failed is not None:
                raise CaveError(f"{switch.from_label} could not be checked out: {failed}")
        ctx.say(f"Unpacking into {staging} ...")
        staging.mkdir(parents=True, mode=0o700)
        archive.extract(staging, archive.owners(info.st_uid, info.st_gid))
        _server.sync()
    except (OSError, tarfile.TarError, EOFError, CaveError) as error:
        # Nothing of the state was touched yet: clean up and start what was running.
        _server.remove(staging)
        again = ""
        if was_running:
            started = ctx.compose("up", "--detach", "--force-recreate", timeout=900.0)
            again = (
                " The stack was started again unchanged."
                if started.ok
                else f" The stack could NOT be started again: {started.output.strip()[-300:]}"
            )
        message = f"The restore failed while unpacking: {error}. Nothing was restored.{again}"
        notify(ctx, TITLE, message)
        raise CaveError(message) from error

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
            f"is stopped. Run the same restore again; the previous state is in {aside}."
        ) from error
    _server.remove(staging)
    _server.sync()
    abandoned = ""
    if switch is not None:
        record = load_update_record(ctx)
        record.failed.setdefault(
            switch.to_release,
            {"time": now(), "reason": "interrupted, then superseded by a restore"},
        )
        record.switch = None
        save_update_record(ctx, record)
        abandoned = (
            f" The interrupted update to {switch.to_release} was abandoned: {switch.from_label} "
            "is checked out."
        )

    env_file = ctx.paths.env_file
    if env_file.exists():
        os.replace(env_file, env_file.with_name(f".env.before-restore-{stamp}"))
    text = archive.env_text
    if archived_env.get("CAVE_DATA") != str(data):
        text = apply_updates(text, {"CAVE_DATA": str(data)})
    _server.write_atomic(env_file, text.encode(), 0o600)
    if _server.is_root():
        info = ctx.paths.root.stat()
        os.chown(env_file, info.st_uid, info.st_gid)
    ctx.env.update(load_env(env_file))
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
