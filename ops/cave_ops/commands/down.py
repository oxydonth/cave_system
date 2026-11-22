"""``cave down``: stop and remove the containers. State is kept.

It records that the stack is stopped on purpose (``records.py``) before it stops it, so that
``cave watch`` raises no alert and ``cave status`` says why nothing runs; ``cave up`` removes the
record.
"""

from __future__ import annotations

import argparse

from ..context import Context
from ..records import note_stopped
from ._common import require_initialised

HELP = "Stop and remove the containers (the state directory is kept)"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    note_stopped(ctx.paths, "`./cave down` stopped it")
    return ctx.compose("down", capture=False).returncode
