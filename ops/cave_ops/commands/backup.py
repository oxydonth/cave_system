"""``cave backup``: archive the state directory and ``.env`` without stopping anything.

Concept section 11. Runs as root (see ``_server``), holding the lock of section 4.5, so it never
overlaps ``update`` or ``restore``. Home Assistant's database, and any other SQLite database in
the state, is copied with SQLite's online backup, so the copy is consistent although Home
Assistant keeps writing. The archive (``_archives``) goes to ``CAVE_BACKUP_DIR``; the newest
``CAVE_BACKUP_KEEP`` are kept there and, if ``CAVE_BACKUP_COPY_TO`` names a directory, a copy
goes there with the same retention. Every outcome is reported through the notification
contract. ``cave restore`` reads the archive back, also on a new server.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

from ..context import Context
from ..errors import CaveError
from ..lock import LockBusy
from ..notify import notify
from . import _archives, _releases, _server
from ._common import require_initialised
from ._update_state import load as load_update_record

HELP = "Archive the state and .env without stopping anything (SQLite online backup)"

TITLE = "cave backup"
DEFAULT_DIR = "/var/backups/cave"
DEFAULT_KEEP = 14


def settings(ctx: Context) -> tuple[Path, int, Path | None]:
    """``CAVE_BACKUP_DIR``, ``CAVE_BACKUP_KEEP`` and ``CAVE_BACKUP_COPY_TO``, checked."""
    directory = Path(ctx.env.get("CAVE_BACKUP_DIR") or DEFAULT_DIR).expanduser()
    raw_keep = ctx.env.get("CAVE_BACKUP_KEEP") or str(DEFAULT_KEEP)
    try:
        keep = int(raw_keep)
    except ValueError:
        keep = 0
    if keep < 1:
        raise CaveError(f"CAVE_BACKUP_KEEP={raw_keep} in .env must be a whole number of at least 1")
    copy_to = ctx.env.get("CAVE_BACKUP_COPY_TO") or None
    return directory, keep, Path(copy_to).expanduser() if copy_to else None


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    directory, keep, copy_to = settings(ctx)
    _server.require_state_access(ctx, "backup")
    try:
        with ctx.lock("backup"):
            switch = load_update_record(ctx).switch
            if switch is not None:
                # The state may be half switched or half put back: archiving it now would keep a
                # broken state and, through the retention, push out a good one.
                message = (
                    f"No backup was made: the update to {switch.to_release} was interrupted "
                    f"({switch.phase}) and the state may be incomplete. `sudo ./cave update "
                    "--recover` finishes it; the next backup then runs as usual."
                )
                ctx.warn(message)
                notify(ctx, TITLE, message)
                return 1
            restoring = _server.restore_in_progress(ctx)
            if restoring is not None:
                message = (
                    f"No backup was made: a restore of {restoring} was interrupted and the state "
                    "may be half restored. " + _server.RESTORE_WAY_OUT
                )
                ctx.warn(message)
                notify(ctx, TITLE, message)
                return 1
            return _backup(ctx, directory, keep, copy_to)
    except LockBusy as busy:
        message = f"No backup was made: {busy}."
        ctx.warn(message)
        notify(ctx, TITLE, message)
        return 1


def _release(ctx: Context) -> tuple[str, str | None]:
    described = _server.git(ctx, "describe", "--tags", "--always", "--dirty")
    merged = _server.git(ctx, "tag", "--list", _releases.TAG_PATTERN, "--merged", "HEAD")
    tag = _releases.newest(merged.stdout.split()) if merged.ok else None
    return (described.stdout.strip() if described.ok else "unknown"), (tag.tag if tag else None)


def write_archive(ctx: Context, target: Path) -> dict[str, Any]:
    """Write the archive to ``target``; return its manifest."""
    data = ctx.paths.data
    release, release_tag = _release(ctx)
    databases: list[str] = []
    unreadable: list[str] = []
    left_out: list[str] = []
    env_info = ctx.paths.env_file.stat()
    owner = ctx.paths.root.stat()
    with (
        _archives.spool_dir(target.parent) as spool_name,
        tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as archive,
    ):
        spool = Path(spool_name)
        _archives.add_file(archive, "env", ctx.paths.env_file, env_info)
        entries = _server.state_entries(data)
        count = _archives.add_tree(
            archive,
            data,
            _server.walk_entries(data, entries),
            spool,
            databases,
            unreadable,
            left_out,
        )
        manifest: dict[str, Any] = {
            "format": _archives.FORMAT,
            "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "release": release,
            "release_tag": release_tag,
            "data": str(data),
            # The service user's ids here; restore maps them to the new server's.
            "owner": {"uid": owner.st_uid, "gid": owner.st_gid},
            "databases": databases,
            "unreadable_databases": unreadable,
            "links_left_out": left_out,
            "entries": count,
        }
        manifest_file = spool / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        _archives.add_file(archive, "manifest.json", manifest_file, manifest_file.stat())
    return manifest


def _backup(ctx: Context, directory: Path, keep: int, copy_to: Path | None) -> int:
    partial: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = _archives.archive_name(directory)
        partial = directory / f".{name}{_archives.PARTIAL}"
        ctx.say(f"Writing {directory / name} ...")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        manifest = write_archive(ctx, partial)
        with open(partial, "rb") as handle:
            os.fsync(handle.fileno())
        archive = directory / name
        os.replace(partial, archive)
        partial = None
        removed = _archives.prune(directory, keep, protect=name)
    except (OSError, sqlite3.Error, tarfile.TarError, CaveError) as error:
        if partial is not None:
            partial.unlink(missing_ok=True)
        message = f"The backup failed: {error}. No new archive was written; older ones are kept."
        ctx.warn(message)
        notify(ctx, TITLE, message)
        return 1

    size = archive.stat().st_size
    message = (
        f"Backup {name} written to {directory} ({size / 2**20:.1f} MiB, "
        f"{len(manifest['databases'])} database(s) copied online)"
    )
    if manifest["unreadable_databases"]:
        message += (
            f"; WARNING: {', '.join(manifest['unreadable_databases'])} could not be read as a "
            "SQLite database (damaged?) and was archived as it is"
        )
    if manifest["links_left_out"]:
        message += (
            f"; {len(manifest['links_left_out'])} symbolic link(s) pointing outside their "
            f"directory were left out ({', '.join(manifest['links_left_out'][:3])})"
        )
    if removed:
        message += f"; {len(removed)} older archive(s) removed, {keep} kept"
    status = 0
    if copy_to is not None:
        if not copy_to.is_dir():
            status = 1
            message += (
                f". The copy to CAVE_BACKUP_COPY_TO={copy_to} FAILED: it is not a directory (not"
                " mounted?); nothing was created there"
            )
        else:
            try:
                _archives.copy_archive(archive, copy_to)
                _archives.prune(copy_to, keep, protect=name)
                message += f"; copied to {copy_to}"
            except OSError as error:
                status = 1
                message += f". The copy to {copy_to} FAILED: {error}"
    message += "."
    (ctx.say if status == 0 else ctx.warn)(message)
    notify(ctx, TITLE, message)
    return status
