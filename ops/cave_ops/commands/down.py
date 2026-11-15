"""``cave down``: stop and remove the containers. State is kept."""

from __future__ import annotations

import argparse

from ..context import Context
from ._common import require_initialised

HELP = "Stop and remove the containers (the state directory is kept)"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    return ctx.compose("down", capture=False).returncode
