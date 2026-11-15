"""What ``update`` remembers, and its snapshots of the state directory.

Everything lives in ``CAVE_DATA/.cave/ops/``, which neither a snapshot nor a backup contains, so
restoring one never turns back this record:

``update.json``
    The update in flight (``switch``: written before the stack is stopped, so that a run after
    a kill or a power cut knows what to finish), the releases that failed, the last result and
    the last check. Readable by everyone (``status`` runs as the service user), written
    atomically by root.
``snapshots/NNNN-<from>-to-<to>/``
    ``data/`` (a copy of the state entries taken with the stack stopped) and ``snapshot.json``.
    A snapshot is written as ``<name>.partial`` and renamed when complete and on disk, so a
    directory without the suffix is always whole. The last ``KEEP_SNAPSHOTS`` are kept.
``discard/``
    Where the current state entries go while a snapshot is put back; removed afterwards.

A snapshot is put back by renaming, not by copying, so a rollback needs no free space: the
current entries are moved to ``discard/`` and the snapshot's entries into their place, one entry
at a time, each step repeatable after an interruption. The snapshot is used up by that;
``Switch.restored`` records that it was put back.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..context import Context
from . import _server

STATE_FILE = "update.json"
SNAPSHOTS = "snapshots"
KEEP_SNAPSHOTS = 2
PARTIAL = ".partial"

# The phases of an update in flight. See docs/operations.md, "Interrupted updates".
BEFORE_SWITCH = "before-switch"
"""Stopping the stack or taking the snapshot: the checkout and the state are as they were."""
SWITCHING = "switching"
"""The snapshot is complete; the checkout, the containers and the state may have changed."""
ROLLING_BACK = "rolling-back"
"""Putting back the snapshot and the previous release."""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class Switch:
    """An update between stopping the stack and its outcome."""

    phase: str
    from_commit: str
    from_label: str
    to_release: str
    to_commit: str
    snapshot: str | None = None
    started: str = field(default_factory=now)
    restored: bool = False
    """The snapshot has been put back (and so used up)."""


@dataclass
class UpdateRecord:
    switch: Switch | None = None
    failed: dict[str, dict[str, str]] = field(default_factory=dict)
    """Releases that failed, by tag: ``{"time": ..., "reason": ...}``."""
    last_result: dict[str, Any] | None = None
    """The last run that did something: ``time``, ``outcome``, ``release``, ``message`` and
    ``status``."""
    last_check: dict[str, Any] | None = None
    """The last run at all: ``time`` and ``message``."""
    problem: str | None = None
    """Set when the file could not be read; the record then starts empty."""


def state_file(ctx: Context) -> Path:
    return _server.ops_dir(ctx) / STATE_FILE


def load(ctx: Context) -> UpdateRecord:
    path = state_file(ctx)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UpdateRecord()
    except (OSError, ValueError) as error:
        return UpdateRecord(problem=f"{path} could not be read ({error}); starting afresh")
    switch = raw.get("switch")
    return UpdateRecord(
        switch=_switch(switch) if isinstance(switch, dict) else None,
        failed=dict(raw.get("failed") or {}),
        last_result=raw.get("last_result"),
        last_check=raw.get("last_check"),
    )


def _switch(raw: dict[str, Any]) -> Switch:
    known = {name: raw[name] for name in Switch.__dataclass_fields__ if name in raw}
    return Switch(**known)


def save(ctx: Context, record: UpdateRecord) -> None:
    data = {
        "switch": asdict(record.switch) if record.switch else None,
        "failed": record.failed,
        "last_result": record.last_result,
        "last_check": record.last_check,
    }
    ops = _server.ops_dir(ctx)
    ops.mkdir(parents=True, exist_ok=True)
    _server.write_atomic(state_file(ctx), (json.dumps(data, indent=2) + "\n").encode(), 0o644)


# ------------------------------------------------------------------------------------ snapshots
def snapshots_dir(ctx: Context) -> Path:
    return _server.ops_dir(ctx) / SNAPSHOTS


def snapshots(ctx: Context) -> list[str]:
    """Complete snapshots, oldest first."""
    folder = snapshots_dir(ctx)
    if not folder.is_dir():
        return []
    return sorted(
        name
        for name in os.listdir(folder)
        if re.match(r"^\d{4}-", name)
        and not name.endswith(PARTIAL)
        and (folder / name / "snapshot.json").is_file()
    )


def remove_partial_snapshots(ctx: Context) -> list[str]:
    """Remove snapshots that were never finished (a kill or a power cut while copying)."""
    folder = snapshots_dir(ctx)
    if not folder.is_dir():
        return []
    removed = sorted(name for name in os.listdir(folder) if name.endswith(PARTIAL))
    for name in removed:
        _server.remove(folder / name)
    return removed


def take_snapshot(ctx: Context, label: str) -> str:
    """Copy the state entries, with the stack stopped, into a new snapshot; return its name.

    The copy is written as ``<name>.partial``, flushed to disk and only then renamed. On any
    failure the partial copy is removed and the error raised.
    """
    folder = snapshots_dir(ctx)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    remove_partial_snapshots(ctx)
    existing = [int(name[:4]) for name in snapshots(ctx)]
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:80].rstrip(".")
    name = f"{max(existing, default=0) + 1:04d}-{safe_label}"
    partial = folder / f"{name}{PARTIAL}"
    entries = _server.state_entries(ctx.paths.data)
    try:
        (partial / "data").mkdir(parents=True, mode=0o700)
        for entry in entries:
            target = partial / "data" / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            _server.copy_entry(ctx.paths.data / entry, target)
        manifest = {"entries": entries, "label": label, "created": now()}
        (partial / "snapshot.json").write_text(json.dumps(manifest, indent=2) + "\n")
        _server.sync()
        os.rename(partial, folder / name)
        _server.sync()
    except BaseException:
        _server.remove(partial)
        raise
    return name


def restore_snapshot(ctx: Context, name: str) -> None:
    """Put the state entries of snapshot ``name`` back in place of the current ones, by renaming.

    Needs no free space. Entry by entry: the current one goes to ``discard/``, the snapshot's
    takes its place. An entry the snapshot no longer holds was put back by an earlier, interrupted
    run and stays. Entries that only the new release created go to ``discard/`` too. So this can
    be repeated after an interruption until it completes. The stack must be stopped.
    """
    source = snapshots_dir(ctx) / name
    entries = json.loads((source / "snapshot.json").read_text(encoding="utf-8"))["entries"]
    discard = _server.ops_dir(ctx) / "discard"
    discard.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = ctx.paths.data

    def set_aside(entry: str) -> None:
        target = discard / entry
        _server.remove(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(data / entry, target)

    for entry in entries:
        saved = source / "data" / entry
        if not os.path.lexists(saved):
            continue  # already put back
        if os.path.lexists(data / entry):
            set_aside(entry)
        (data / entry).parent.mkdir(parents=True, exist_ok=True)
        os.rename(saved, data / entry)
    for entry in _server.state_entries(data):
        if entry not in entries:
            set_aside(entry)
    _server.sync()


def discard_after_restore(ctx: Context, name: str) -> None:
    """Remove what ``restore_snapshot`` set aside, and the used-up snapshot."""
    _server.remove(_server.ops_dir(ctx) / "discard")
    _server.remove(snapshots_dir(ctx) / name)


def prune_snapshots(ctx: Context, keep: int = KEEP_SNAPSHOTS) -> list[str]:
    """Remove all but the newest ``keep`` complete snapshots; return the removed names."""
    names = snapshots(ctx)
    removed = names[:-keep] if keep > 0 else names
    for name in removed:
        _server.remove(snapshots_dir(ctx) / name)
    return removed
