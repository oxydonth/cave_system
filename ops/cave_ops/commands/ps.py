"""``cave ps``: show the containers."""

from __future__ import annotations

import argparse

from ..context import Context
from ._common import require_initialised

HELP = "Show the containers and their health"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    return ctx.compose("ps", "--all", capture=False).returncode
