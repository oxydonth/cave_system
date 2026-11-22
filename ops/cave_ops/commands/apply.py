"""``cave apply``: make an edited ``cave/heating.yaml`` take effect.

It holds the lock of concept section 4.5, the same one as ``update``, ``backup`` and ``restore``,
for its whole run. So two ``apply`` runs cannot interleave (one rendering the dashboard of a copy
the other has just replaced), and ``apply`` cannot change the applied copy between an update's
check of it against the new release and the restart that follows. When the lock is taken it says
who holds it and changes nothing.

Three steps, in this order (concept sections 4.4 and 5.1):

1. Validate the edited file and store it as the applied copy, through the shared function
   ``validate_and_store``. An invalid file stops here and changes nothing: the messages name the
   room and field, and the applied copy, the dashboard and Home Assistant stay as they were.
2. Regenerate ``dashboards/heating.yaml`` from the applied copy with ``tools/render_dashboard.py``,
   run in a one-off Home Assistant container (it needs PyYAML, which is there).
3. Tell Home Assistant to reload ``cave_heating``. Without a script token (before
   ``cave bootstrap``) it says that Home Assistant has to be restarted instead.

From step 2 on, the applied copy is already stored, so a failure there cannot undo it. The command
carries on as far as it can and ends with a summary of exactly what state each of the three
things is in, and a non-zero status when something still needs the person's attention:

* The dashboard could not be generated: the old dashboard file is untouched (the generator
  replaces it in one step or not at all), the heating still follows the new applied copy, and the
  message of the generator is shown. Fix the cause and run ``./cave apply`` again.
* Home Assistant could not be told (not running, or ``cave_heating`` is not loaded in it): the
  applied copy and the dashboard are new, and Home Assistant loads the applied copy when it
  starts, so the message says to start or restart it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from ..container import run_in_homeassistant
from ..context import Context
from ..errors import CaveError
from ..ha import HAError, HAUnreachable
from ..heating import validate_and_store
from ..lock import LockBusy
from ..paths import APPLIED_IN_CONTAINER, DASHBOARD_IN_CONTAINER, GENERATOR
from ._common import require_initialised
from ._update_state import maintenance_pending

HELP = "Validate cave/heating.yaml, then use it: regenerate the dashboard and reload cave_heating"

RESTART_COMMAND = "./cave restart homeassistant"


@dataclass
class _Outcome:
    """What each of the three things ended up as, for the summary."""

    applied: str = ""
    dashboard: str = ""
    home_assistant: str = ""
    needs_attention: bool = False


def _regenerate_dashboard(ctx: Context, outcome: _Outcome) -> None:
    """Run the generator; on failure show its message and say that the old dashboard stands."""
    result = run_in_homeassistant(ctx, [GENERATOR, APPLIED_IN_CONTAINER, DASHBOARD_IN_CONTAINER])
    if result.ok:
        outcome.dashboard = "regenerated (dashboards/heating.yaml, shown at /cave-heating)"
        return
    for line in result.output.splitlines():
        if line.strip():
            ctx.warn(line.rstrip())
    outcome.dashboard = (
        f"NOT regenerated (exit status {result.returncode}); the dashboard is still the previous "
        "one and does not match the new applied copy. Fix the cause above and run "
        "`./cave apply` again"
    )
    outcome.needs_attention = True


def _reload(ctx: Context, outcome: _Outcome) -> None:
    """Call ``cave_heating.reload``, or say what to do instead."""
    try:
        client = ctx.ha()
    except CaveError as error:
        outcome.home_assistant = (
            f"not told: {error}. Home Assistant loads the applied copy when it is "
            f"restarted: `{RESTART_COMMAND}`"
        )
        outcome.needs_attention = True
        return
    if client is None:
        outcome.home_assistant = (
            "not told, because there is no script token yet (`./cave bootstrap` creates it). "
            f"Home Assistant has to be restarted instead: `{RESTART_COMMAND}`"
        )
        return
    try:
        if not client.has_service("cave_heating", "reload"):
            outcome.home_assistant = (
                "not told: the running Home Assistant has no cave_heating.reload, so the "
                f"integration is not loaded in it. Restart it: `{RESTART_COMMAND}`"
            )
            outcome.needs_attention = True
            return
        client.call_service("cave_heating", "reload")
    except HAUnreachable as error:
        outcome.home_assistant = (
            f"not told: {error}. It loads the applied copy when it starts; "
            "start it with `./cave up`"
        )
        outcome.needs_attention = True
    except HAError as error:
        if error.status == 401:
            outcome.home_assistant = (
                "not told: Home Assistant did not accept the script token "
                f"({ctx.paths.token}). `./cave bootstrap` creates a new one. The applied copy "
                f"is stored and is loaded when Home Assistant restarts: `{RESTART_COMMAND}`"
            )
            outcome.needs_attention = True
            return
        # Home Assistant's own words, in full: for an invalid applied copy they are the problems.
        ctx.warn(str(error))
        outcome.home_assistant = (
            f"reload refused (answer {error.status}, shown above); the configuration that was "
            "running stays in effect. The applied copy is stored and is loaded when Home "
            f"Assistant restarts: `{RESTART_COMMAND}`"
        )
        outcome.needs_attention = True
    else:
        outcome.home_assistant = "cave_heating reloaded"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    if not ctx.paths.heating.is_file():
        raise CaveError(f"{ctx.paths.heating} does not exist; run `./cave init` first")
    try:
        with ctx.lock("apply"):
            return _apply(ctx)
    except LockBusy as error:
        ctx.warn(str(error))
        ctx.warn("Nothing was changed.")
        return 1
    except OSError as error:
        # The state directory is read-only or owned by someone else, or the file cannot be read.
        # Both happen before the applied copy is replaced, and that is done in one step.
        where = f" ({error.filename})" if error.filename else ""
        ctx.warn(f"cannot read or write the state directory{where}: {error.strerror or error}")
        ctx.warn("Nothing was changed.")
        return 1


def _apply(ctx: Context) -> int:
    pending = maintenance_pending(ctx)
    if pending is not None:
        ctx.warn(f"Not applied: {pending}.")
        ctx.warn("Nothing was changed.")
        return 1
    result = validate_and_store(ctx)
    if not result.ok:
        ctx.warn(f"{ctx.paths.heating} is not valid:")
        for line in result.messages:
            ctx.warn(f"  {line}")
        ctx.warn(
            "Nothing was changed: the applied copy, the dashboard and Home Assistant "
            "are as they were."
        )
        return 1

    ctx.say(f"{ctx.paths.heating} is valid.")
    for line in result.messages:
        ctx.say(f"  {line}")
    outcome = _Outcome(applied="updated" if result.changed else "unchanged (already up to date)")

    _regenerate_dashboard(ctx, outcome)
    _reload(ctx, outcome)

    ctx.say(f"Applied copy:    {outcome.applied}")
    ctx.say(f"Dashboard:       {outcome.dashboard}")
    ctx.say(f"Home Assistant:  {outcome.home_assistant}")
    return 1 if outcome.needs_attention else 0
