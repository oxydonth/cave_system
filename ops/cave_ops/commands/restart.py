"""``cave restart``: restart services without recreating them."""

from __future__ import annotations

import argparse

from ..context import Context
from ..health import wait_healthy
from ._common import require_initialised

HELP = "Restart services (all by default) and wait until healthy (`up` picks up a new checkout)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("services", nargs="*", help="services to restart (default: all)")
    parser.add_argument("--no-wait", action="store_true", help="return without waiting for healthy")


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    restarted = ctx.compose("restart", *args.services, capture=False)
    if not restarted.ok:
        return restarted.returncode
    if args.no_wait:
        return 0
    health = wait_healthy(ctx)
    ctx.say(health.describe())
    return 0 if health.healthy else 1
