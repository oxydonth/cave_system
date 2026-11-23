"""``cave watch``: tell the owner through ``CAVE_ALERT_URL`` when the stack is down, also while Home
Assistant cannot.

``cave-watch.timer`` runs it every five minutes as the service user, who may ask Docker. It does
nothing at all while ``CAVE_ALERT_URL`` is unset. Otherwise, each run:

1. Reads who holds the lock of concept section 4.5, without taking it (``held_by``): while a
   command is at work (an update switching, a restore, a backup), the stack may be stopped on
   purpose, so the watch says nothing and starts counting afresh afterwards. It never takes the
   lock, so a nightly update that starts at the same moment is not refused because of it.
2. Decides whether there is an incident: an update that was interrupted, a restore that stopped
   half-way, or the stack not healthy or stopped. A stack that is stopped on purpose is none:
   after ``cave down``, after a restore or ``--undo`` with ``--no-start``, and before the first
   ``cave up`` (``records.py``).
3. Alerts once when an incident has lasted ``ALERT_AFTER`` (15 minutes), and once when the stack
   is healthy again. What it has alerted is remembered in ``CAVE_DATA/.cave/watch.json``
   (``alert.Memory``), which the service user may write and which no snapshot or backup
   contains. An alert that could not be delivered is tried again at the next run.

When the memory cannot be written (a full disk), what was loaded from it is not trusted for staying
silent: each run tries the write first, and if it fails, an incident is alerted at once and at
every run, saying so, whatever the memory says was alerted, and no all-clear is sent (it would
repeat every five minutes for as long as the disk is full). An unwritable memory causes more
incident alerts, never fewer. A memory that cannot be read counts as empty, which can only cause
an alert, never silence.

A record of a stack stopped on purpose that containers still outlive by more than ``INTERVAL`` is
removed: the stop did not happen, or the stack was started without ``cave``.

Two runs more than ``GAP`` apart (the server was off, the clock jumped) start the count afresh,
so that a stack that is still starting after a power cut is not reported for the hours it was off.
"""

from __future__ import annotations

import argparse
import datetime
from dataclasses import dataclass

from .. import alert
from ..context import Context
from ..health import Health, check_health
from ..lock import held_by
from ..records import clear_stopped, stopped
from . import _server
from . import _update_state as state
from ._common import require_initialised

HELP = "Alert through CAVE_ALERT_URL when the stack has been down for 15 minutes (the timer's job)"

INTERVAL = 5 * 60
"""How often ``cave-watch.timer`` runs it."""
ALERT_AFTER = 15 * 60
"""How long an incident lasts before it is alerted."""
GAP = 2 * INTERVAL + 60
"""Two runs further apart than this did not watch the time in between."""
COMMAND = "watch"


@dataclass(frozen=True)
class Finding:
    """What the watch found: ``kind`` is ``healthy``, ``on-purpose`` or ``incident``."""

    kind: str
    health: Health
    headline: str = ""
    happened: str = ""


def assess(ctx: Context) -> Finding:
    """Whether there is an incident, as described in the module's documentation."""
    record = state.load(ctx)
    health = check_health(ctx)
    if record.switch is not None:
        return Finding(
            "incident",
            health,
            "an update was interrupted",
            "An update was interrupted, and its way back has not finished. Until it has, no "
            "backup is made. `sudo ./cave update --recover` finishes it.",
        )
    if _server.restore_in_progress(ctx) is not None:
        return Finding(
            "incident",
            health,
            "a restore stopped half-way",
            "A restore stopped half-way: the state is half restored and the stack is stopped. "
            "Run the same restore again, restore another archive, or `sudo ./cave restore "
            "--undo`.",
        )
    if health.healthy:
        return Finding("healthy", health)
    if health.stopped and stopped(ctx.paths) is not None:
        return Finding("on-purpose", health)
    if health.stopped:
        return Finding(
            "incident",
            health,
            "the stack is stopped",
            "Nothing runs, and it was not stopped on purpose: Home Assistant does not control "
            "the heating. The thermostats keep their last setpoint.",
        )
    if health.running is None:
        return Finding(
            "incident",
            health,
            "Docker does not answer",
            "Docker does not answer, so whether the stack runs is unknown.",
        )
    return Finding(
        "incident", health, "the stack is not healthy", "The stack runs, but it is not healthy."
    )


def _clock(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).strftime("%H:%M UTC")


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    if not alert.configured(ctx):
        ctx.say(f"{alert.VARIABLE} is not set: nothing is watched, and nothing leaves the server.")
        return 0
    now = ctx.wall()
    memory = alert.load_memory(ctx.paths)
    watched = memory.last is not None and 0 <= now - memory.last <= GAP
    memory.last = now
    # The one rule for a memory that cannot be written: what was loaded from it is not trusted for
    # staying silent. Whether it can be written is found out first, before any decision.
    unwritable = _try_save(ctx, memory)

    holder = held_by(ctx.paths.lock_file)
    if holder is not None:
        memory.since = None
        ctx.say(f"{holder} is at work: nothing to say until it has finished.")
        return _done(ctx, memory, unwritable)

    finding = assess(ctx)
    _drop_a_stale_record(ctx, finding, now)
    if finding.kind == "healthy":
        memory.since = None
        if unwritable is not None:
            # No all-clear while the memory cannot be written: without it, an all-clear would go
            # out again every five minutes for as long as the disk is full, which teaches the
            # owner to ignore the channel. The incident's alerts, which said that they repeat,
            # stop instead.
            ctx.say("The stack is healthy (no all-clear while the memory cannot be written).")
            return _done(ctx, memory, unwritable)
        if memory.alerted is None:
            ctx.say("The stack is healthy.")
            return _done(ctx, memory, unwritable)
        return _alert(
            ctx, memory, finding, "healthy again", "The stack is running and healthy again.", True
        )
    if finding.kind == "on-purpose":
        # Not an incident. An incident alerted before stays open until the stack is healthy.
        memory.since = None
        ctx.say("The stack is stopped on purpose: nothing to report.")
        return _done(ctx, memory, unwritable)

    if unwritable is not None:
        # Neither "alerted already" nor the count of fifteen minutes can be trusted or kept: alert
        # at every run, saying so. An unwritable memory causes more alerts, never fewer.
        return _alert(ctx, memory, finding, finding.headline, finding.happened, False, unwritable)
    if memory.since is None or not watched:
        memory.since = now
    if memory.alerted is not None:
        ctx.say(f"{finding.headline[:1].upper()}{finding.headline[1:]}: alerted already.")
        return _save(ctx, memory)
    lasted = now - memory.since
    if lasted < ALERT_AFTER:
        ctx.say(
            f"{finding.headline[:1].upper()}{finding.headline[1:]} (since "
            f"{_clock(memory.since)}): alerted once it has lasted {ALERT_AFTER // 60} minutes."
        )
        return _save(ctx, memory)
    happened = (
        f"{finding.happened} This has lasted {int(lasted // 60)} minutes, since "
        f"{_clock(memory.since)}."
    )
    return _alert(ctx, memory, finding, finding.headline, happened, False)


REPEATS = (
    "cave watch cannot write what it has sent (a full disk, or CAVE_DATA/.cave not writable by "
    "the service user), so this repeats every five minutes while the incident lasts, and no "
    "all-clear follows: the repeats stop when the stack is healthy again."
)


def _alert(
    ctx: Context,
    memory: alert.Memory,
    finding: Finding,
    headline: str,
    happened: str,
    closes: bool,
    unwritable: str | None = None,
) -> int:
    """Send an alert (``closes``: the all-clear) and remember it; with ``unwritable`` (the memory
    cannot be written, which ``run`` found out first) the alert says that it repeats."""
    if unwritable is not None:
        happened = f"{happened} {REPEATS}"
    sent = _send(ctx, headline, happened, finding)
    if sent.delivered:
        memory.alerted, memory.what = (None, None) if closes else (ctx.wall(), headline)
    if unwritable is not None:
        _say_unwritable(ctx, unwritable)
        return 1
    return _save(ctx, memory, sent.delivered)


def _done(ctx: Context, memory: alert.Memory, unwritable: str | None) -> int:
    """The end of a run that sent nothing: save what changed, or say that it cannot be."""
    if unwritable is not None:
        _say_unwritable(ctx, unwritable)
        return 1
    return _save(ctx, memory)


def _drop_a_stale_record(ctx: Context, finding: Finding, now: float) -> None:
    """Containers run an interval after the stack was stopped on purpose: the stop did not happen
    (Docker failed under `cave down`), or the stack was started without `cave`."""
    record = stopped(ctx.paths)
    if record is None or not finding.health.running or now - (record.at or 0.0) <= INTERVAL:
        return
    try:
        clear_stopped(ctx.paths, now)
    except OSError as error:
        ctx.warn(f"The record of a stack stopped on purpose could not be removed: {error}.")


def _send(ctx: Context, headline: str, happened: str, finding: Finding) -> alert.Sent:
    title, body = alert.compose(f"cave {COMMAND}", headline, happened, finding.health.state())
    sent = alert.send(ctx, title, body)
    if sent.delivered:
        ctx.say(f"Alert sent: {headline}.")
    else:
        ctx.warn(
            f"The alert ({headline}) could not be sent: {sent.error}. The next run tries again."
        )
    return sent


def _try_save(ctx: Context, memory: alert.Memory, alert_changed: bool = False) -> str | None:
    """Write the memory; what went wrong, or ``None``. Unless this run changed what was alerted,
    an alert a command marked meanwhile is kept (``alert.save_memory``)."""
    try:
        alert.save_memory(ctx.paths, memory, keep_marked=not alert_changed)
    except OSError as error:
        return str(error)
    return None


def _say_unwritable(ctx: Context, error: str) -> None:
    message = (
        f"cave watch cannot remember what it has alerted ({error}): until the memory can be "
        "written it alerts an incident at once and at every run, and sends no all-clear."
    )
    ctx.warn(message)
    ctx.journal(message)


def _save(ctx: Context, memory: alert.Memory, alert_changed: bool = False) -> int:
    error = _try_save(ctx, memory, alert_changed)
    if error is not None:
        _say_unwritable(ctx, error)
        return 1
    return 0
