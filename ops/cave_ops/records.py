"""Small records in ``CAVE_DATA/.cave`` that several commands share, and how they are written.

**A stack stopped on purpose** (concept section 4.5), ``.cave/stopped.json``: ``cave down``, a
restore or ``restore --undo`` with ``--no-start``, and ``cave init`` for a state directory it has
just created write it; every start of the stack by ``cave`` removes it (``cave up`` before it
starts, a restore and the recovery when they start). ``cave status`` shows it while the stack does
not run, and ``cave watch`` raises no alert for a stopped stack while it exists. A record that
containers still outlive by more than the watch's interval is stale (a ``down`` whose Docker
failed): ``cave watch`` removes it. It does not cover
a stack that runs but is unhealthy, nor an interrupted update or a restore that stopped half-way.

It lies in ``.cave`` itself, which belongs to the service user, not in ``.cave/ops``, which is
root's: the service user's ``up`` must be able to remove what root's restore wrote. Neither a
snapshot nor a backup contains it (``_server.state_entries``), so no restore or rollback brings an
old one back. Version v2026.11.22.1 kept the note of a ``--no-start`` in
``.cave/ops/not-started.json``; it is still read, and removed by whoever may. The service user
cannot (the directory is root's): a start by it writes ``{"started": true}`` into the new record,
which then takes precedence over the old note.

A command that fails or refuses before it has changed anything leaves the record as it was
(``read_stopped`` and ``restore_stopped``).
"""

from __future__ import annotations

import datetime
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import Paths

LEGACY_NOT_STARTED = Path("ops") / "not-started.json"
"""Where v2026.11.22.1 noted a ``--no-start``, relative to ``CAVE_DATA/.cave``."""


def write_atomic(path: Path, data: bytes, mode: int = 0o644, writer: str = "") -> None:
    """Replace ``path`` with ``data`` so that a reader, or a power cut, sees old or new only.

    The temporary file is created fresh and never through a symbolic link, because this runs as
    root in directories that other users can write. ``writer`` goes into its name
    (``.<name>.<writer>.tmp``) where two processes may write the same file at once, so that
    neither removes the other's temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{writer}.tmp" if writer else f".{path.name}.tmp")
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


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class Stopped:
    """Why the stack does not run, in words that complete "… so the stack was not started"."""

    what: str
    since: str
    at: float | None = None
    """When it was written, in seconds since the epoch (``None`` for the note of v2026.11.22.1)."""


def note_stopped(paths: Paths, what: str, at: float | None = None) -> None:
    """Record that the stack is stopped on purpose; ``what`` says by what, ``at`` when (the
    caller's ``ctx.wall()``; now if not given)."""
    when = time.time() if at is None else at
    text = json.dumps({"what": what, "since": utc_now(), "at": when}) + "\n"
    write_atomic(paths.stopped_record, text.encode(), 0o644)


def stopped(paths: Paths) -> Stopped | None:
    """The record of a stack stopped on purpose, or ``None``.

    A record that says the stack was started since (``clear_stopped`` as the service user, who
    cannot remove the note of v2026.11.22.1) means none, whatever that old note says.
    """
    for path in (paths.stopped_record, paths.lock_file.parent / LEGACY_NOT_STARTED):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if raw.get("started"):
            return None
        what = str(raw.get("what") or "a restore")
        if path != paths.stopped_record:
            what += " finished with --no-start"  # the wording v2026.11.22.1 had in status
        at = raw.get("at")
        return Stopped(
            what,
            str(raw.get("since") or "an unknown time"),
            float(at) if isinstance(at, int | float) else None,
        )
    return None


def clear_stopped(paths: Paths, at: float | None = None) -> None:
    """The stack is being started: forget that it was stopped on purpose.

    The note of v2026.11.22.1 lies in root's ``.cave/ops``, which the service user cannot change.
    When it cannot be removed, the record says instead that the stack was started (``at``), which
    ``stopped`` reads first; the next root command that starts the stack removes both.
    """
    legacy = paths.lock_file.parent / LEGACY_NOT_STARTED
    try:
        legacy.unlink(missing_ok=True)
    except PermissionError:
        when = time.time() if at is None else at
        text = json.dumps({"started": True, "since": utc_now(), "at": when}) + "\n"
        write_atomic(paths.stopped_record, text.encode(), 0o644)
        return
    paths.stopped_record.unlink(missing_ok=True)


def restore_stopped(paths: Paths, before: bytes | None) -> None:
    """Put the record back as it was (``read_stopped``): a command that failed before it changed
    anything leaves it so."""
    if before is None:
        paths.stopped_record.unlink(missing_ok=True)
    else:
        write_atomic(paths.stopped_record, before, 0o644)


def read_stopped(paths: Paths) -> bytes | None:
    """The record as it is, to be put back with ``restore_stopped``."""
    try:
        return paths.stopped_record.read_bytes()
    except FileNotFoundError:
        return None
