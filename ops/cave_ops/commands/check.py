"""``cave check``: validate the Compose file and the Home Assistant configuration."""

from __future__ import annotations

import argparse

from ..container import run_in_homeassistant
from ..context import Context
from ..paths import CONTAINER_CONFIG
from ._common import require_initialised

HELP = "Validate the Compose file and the Home Assistant configuration in the pinned image"


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    failed = False

    ctx.say("Compose file ...")
    compose = ctx.compose("config", "--quiet")
    if compose.ok:
        ctx.say("  ok")
    else:
        failed = True
        ctx.warn(compose.output.strip())

    # A one-off container of the pinned image with the same mounts as the service. It works
    # while Home Assistant is down, and does not touch a running one.
    ctx.say("Home Assistant configuration ...")
    config = run_in_homeassistant(
        ctx, ["-m", "homeassistant", "--script", "check_config", "--config", CONTAINER_CONFIG]
    )
    if config.ok:
        ctx.say("  ok")
    else:
        failed = True
        ctx.warn(config.output.strip())

    return 1 if failed else 0
