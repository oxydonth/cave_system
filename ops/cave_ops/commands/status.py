"""``cave status``: what is installed, whether it is healthy, how the last update and backup went,
and whether ``heating.yaml`` has changes that were not applied.

Read-only and without the lock, so it also answers while an update runs, and as the service user
(``sudo -u cave ./cave status``) as well as root. It does not fetch: "newer release" means newer
than the installed one among the releases the last ``update`` fetched. Exit status 0 when the
stack is healthy and no update was interrupted, else 1.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..context import Context
from ..errors import CaveError
from ..health import check_health
from ..lock import held_by
from . import _archives, _releases, _server
from . import _update_state as state
from ._common import require_initialised
from .backup import settings
from .update import auto_updates_off

HELP = "Show the installed release, health, the last update and backup, unapplied heating.yaml"

WIDTH = 13


def _line(ctx: Context, label: str, text: str) -> None:
    ctx.say(f"{label + ':':<{WIDTH}} {text}")


def _git_lines(ctx: Context, *args: str) -> list[str] | None:
    result = _server.git(ctx, *args)
    return result.stdout.split() if result.ok else None


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    healthy = _release(ctx)

    health = check_health(ctx)
    _line(ctx, "Stack", health.describe())
    healthy = healthy and health.healthy

    record = state.load(ctx)
    if record.problem:
        _line(ctx, "Update record", record.problem)
    holder = held_by(ctx.paths.lock_file)
    if holder:
        _line(ctx, "Running now", holder)
    if record.switch is not None and not (holder and "update" in holder):
        healthy = False
        _line(
            ctx,
            "INTERRUPTED",
            f"an update from {record.switch.from_label} to {record.switch.to_release} "
            f"({record.switch.phase}, started {record.switch.started}) was interrupted. "
            "`sudo ./cave update --recover` finishes it; it also runs at boot.",
        )
    if record.last_result:
        last = record.last_result
        _line(ctx, "Last update", f"{last.get('time')}: {last.get('message')}")
    else:
        _line(ctx, "Last update", "none recorded")
    if record.last_check and record.last_check.get("message") != (record.last_result or {}).get(
        "message"
    ):
        _line(
            ctx,
            "Last check",
            f"{record.last_check.get('time')}: {record.last_check.get('message')}",
        )
    for tag, failure in sorted(record.failed.items()):
        _line(
            ctx,
            "Failed",
            f"{tag} on {failure.get('time')}; skipped until a newer release or "
            "`sudo ./cave update --force`",
        )
    _line(
        ctx,
        "Updates",
        "manual only (CAVE_AUTO_UPDATE=0)"
        if auto_updates_off(ctx.env)
        else "nightly, automatic (CAVE_AUTO_UPDATE=1)",
    )

    _line(ctx, "heating.yaml", _heating(ctx))
    _line(ctx, "Last backup", _last_backup(ctx))
    return 0 if healthy else 1


def _release(ctx: Context) -> bool:
    described = _server.git(ctx, "describe", "--tags", "--always", "--dirty")
    merged = _git_lines(ctx, "tag", "--list", _releases.TAG_PATTERN, "--merged", "HEAD")
    known = _git_lines(ctx, "tag", "--list", _releases.TAG_PATTERN)
    if not described.ok or merged is None or known is None:
        _line(ctx, "Release", f"unknown: {ctx.paths.root} is not a Git checkout")
        return False
    installed = _releases.newest(merged)
    newest = _releases.newest(known)
    checked_out = described.stdout.strip()
    text = str(installed or "no release")
    if checked_out != text:
        text += f" (checked out: {checked_out})"
    _line(ctx, "Release", text)
    if newest is not None and (installed is None or newest > installed):
        _line(ctx, "Newer", f"{newest} is available and not installed")
    return True


def _heating(ctx: Context) -> str:
    edited, applied = ctx.paths.heating, ctx.paths.heating_applied
    try:
        edited_bytes = edited.read_bytes()
    except FileNotFoundError:
        return f"{edited} is missing"
    except OSError as error:
        return f"cannot read {edited}: {error}"
    try:
        applied_bytes = applied.read_bytes()
    except FileNotFoundError:
        return "never applied: run `./cave apply`"
    except OSError as error:
        return f"cannot read {applied}: {error}"
    if edited_bytes == applied_bytes:
        return "applied, no changes since"
    return "CHANGED since the last `./cave apply`; the changes are not in effect until it runs"


def _last_backup(ctx: Context) -> str:
    try:
        directory, _keep, _copy = settings(ctx)
    except CaveError as error:
        return str(error)
    try:
        names = _archives.archives(directory)
    except OSError as error:
        return f"cannot read {directory}: {error.strerror}"
    if not names:
        return f"none in {directory}"
    newest = Path(directory) / names[-1]
    try:
        size = f", {newest.stat().st_size / 2**20:.1f} MiB"
    except OSError:
        size = ""
    return f"{newest}{size}; {len(names)} kept"
