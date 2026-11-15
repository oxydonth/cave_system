"""``cave logs``: show service logs."""

from __future__ import annotations

import argparse

from ..context import Context
from ._common import require_initialised

HELP = "Show the logs of the services (all by default)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("services", nargs="*", help="services to show (default: all)")
    parser.add_argument("-f", "--follow", action="store_true", help="keep showing new lines")
    parser.add_argument(
        "--tail", metavar="N", default="200", help="lines per service, or `all` (default 200)"
    )
    parser.add_argument(
        "--since", metavar="TIME", help="only lines since TIME, for example 10m or an ISO timestamp"
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    options = ["--tail", args.tail]
    if args.follow:
        options.append("--follow")
    if args.since:
        options += ["--since", args.since]
    return ctx.compose("logs", *options, *args.services, capture=False).returncode
