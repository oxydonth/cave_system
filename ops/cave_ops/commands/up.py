"""``cave up``: start the stack and wait until it is healthy."""

from __future__ import annotations

import argparse

from ..context import Context
from ..health import wait_healthy
from ._common import require_initialised

HELP = "Create and start the containers of the active profiles, then wait until healthy"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-wait", action="store_true", help="return as soon as the containers are started"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long to wait for healthy (default 300)",
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    # Always recreate: the code is bind-mounted, so Compose cannot see that a checkout changed
    # (concept section 4.2: "a changed checkout takes effect when the containers are recreated").
    started = ctx.compose("up", "--detach", "--force-recreate", capture=False)
    if not started.ok:
        return started.returncode
    if args.no_wait:
        return 0
    ctx.say("Waiting until healthy ...")
    health = wait_healthy(ctx, timeout=args.timeout)
    ctx.say(health.describe())
    return 0 if health.healthy else 1
