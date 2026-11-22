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
from ..records import stopped
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

    holder = held_by(ctx.paths.lock_file)
    if holder is not None:
        memory.since = None
        ctx.say(f"{holder} is at work: nothing to say until it has finished.")
        return _save(ctx, memory)

    finding = assess(ctx)
    if finding.kind == "healthy":
        memory.since = None
        if memory.alerted is not None:
            sent = _send(ctx, "healthy again", "The stack is running and healthy again.", finding)
            if sent.delivered:
                memory.alerted, memory.what = None, None
        else:
            ctx.say("The stack is healthy.")
        return _save(ctx, memory)
    if finding.kind == "on-purpose":
        # Not an incident. An incident alerted before stays open until the stack is healthy.
        memory.since = None
        ctx.say("The stack is stopped on purpose: nothing to report.")
        return _save(ctx, memory)

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
    sent = _send(ctx, finding.headline, happened, finding)
    if sent.delivered:
        memory.alerted, memory.what = now, finding.headline
    return _save(ctx, memory)


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


def _save(ctx: Context, memory: alert.Memory) -> int:
    try:
        alert.save_memory(ctx.paths, memory)
    except OSError as error:
        message = (
            f"cave watch cannot remember what it has alerted ({error}); it may alert the same "
            "incident again at its next run."
        )
        ctx.warn(message)
        ctx.journal(message)
        return 1
    return 0
