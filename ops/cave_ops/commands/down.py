"""``cave down``: stop and remove the containers. State is kept.

It records that the stack is stopped on purpose (``records.py``) before it stops it, so that
``cave watch`` raises no alert and ``cave status`` says why nothing runs; ``cave up`` removes the
record. If Compose fails, the record is put back as it was: a stack that ``down`` did not stop is
not one stopped on purpose.
"""

from __future__ import annotations

import argparse

from ..context import Context
from ..records import note_stopped, read_stopped, restore_stopped
from ._common import require_initialised

HELP = "Stop and remove the containers (the state directory is kept)"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    before = read_stopped(ctx.paths)
    try:
        note_stopped(ctx.paths, "`./cave down` stopped it", ctx.wall())
    except OSError as error:
        # Stopping matters more than the record: say what follows from its absence.
        ctx.warn(
            f"Could not record that the stack is stopped on purpose ({error}): `cave watch` will "
            "report it as stopped after 15 minutes."
        )
    stopped = ctx.compose("down", capture=False)
    if not stopped.ok:
        try:
            restore_stopped(ctx.paths, before)
        except OSError as error:
            ctx.warn(
                f"Could not take back the record that the stack is stopped on purpose ({error}); "
                "`cave watch` removes it once the containers have run five minutes."
            )
    return stopped.returncode
