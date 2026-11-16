"""``cave bootstrap``: first-run setup of Home Assistant (concept sections 4.5 and 8).

Waits until the stack is healthy, then runs ``tools/bootstrap.py`` in a one-off Home Assistant
container against the running Home Assistant. That tool creates the owner account, sets Home
Assistant's time zone, units, country and currency, finishes onboarding, sets up MQTT (and the
FRITZ! integrations when ``.env`` names a FRITZ!Box), creates the rooms' schedule helpers and the
script token. Everything it does is skipped when it is already done, so a second run changes
nothing.

The settings, passwords included, go to the tool as JSON on its standard input: never as
arguments or environment options, which a process listing would show. Afterwards this command
checks that the script token can be read by the user running ``./cave``, has mode 0600, and is
accepted by Home Assistant.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from ..container import run_in_homeassistant
from ..context import Context
from ..errors import CaveError
from ..health import check_health, wait_healthy
from ..paths import CONTAINER_TOOLS
from ._common import require_initialised

HELP = "First-run setup of Home Assistant: owner, settings, integrations, schedules, script token"

TOOL = f"{CONTAINER_TOOLS}/bootstrap.py"
# Home Assistant runs on the host network and reaches the broker there (concept section 4.1).
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
REQUIRED = (
    "CAVE_HA_OWNER_NAME",
    "CAVE_HA_OWNER_USER",
    "CAVE_HA_OWNER_PASSWORD",
    "CAVE_MQTT_USER",
    "CAVE_MQTT_PASSWORD",
)
FRITZ = ("CAVE_FRITZ_HOST", "CAVE_FRITZ_USER", "CAVE_FRITZ_PASSWORD")
# Beyond the tool's own wait for Home Assistant: the container's start and the config flows.
RUN_MARGIN = 300.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long to wait for the stack to be healthy and Home Assistant to answer "
        "(default 300)",
    )


def settings_from_env(env: dict[str, str], timeout: float) -> dict[str, Any]:
    """The tool's settings from ``.env``. Fails, naming the variables, when one is missing."""
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise CaveError(
            f"{', '.join(missing)} not set in .env; run `./cave init`, which adds missing variables"
        )
    settings: dict[str, Any] = {
        "owner_name": env["CAVE_HA_OWNER_NAME"],
        "owner_user": env["CAVE_HA_OWNER_USER"],
        "owner_password": env["CAVE_HA_OWNER_PASSWORD"],
        "time_zone": env.get("TZ") or "Europe/Berlin",
        "mqtt_host": MQTT_HOST,
        "mqtt_port": MQTT_PORT,
        "mqtt_user": env["CAVE_MQTT_USER"],
        "mqtt_password": env["CAVE_MQTT_PASSWORD"],
        "timeout": timeout,
    }
    if env.get("CAVE_FRITZ_HOST"):
        missing = [name for name in FRITZ if not env.get(name)]
        if missing:
            raise CaveError(
                f"CAVE_FRITZ_HOST is set, but {', '.join(missing)} is not: the FRITZ! "
                "integrations need the FRITZ!Box's address, user and password. Set them in .env, "
                "or empty CAVE_FRITZ_HOST to leave the FRITZ! integrations out."
            )
        settings.update(
            fritz_host=env["CAVE_FRITZ_HOST"],
            fritz_user=env["CAVE_FRITZ_USER"],
            fritz_password=env["CAVE_FRITZ_PASSWORD"],
        )
    return settings


def check_token_file(ctx: Context) -> None:
    """The script token is there, mode 0600, the invoking user's, and Home Assistant takes it."""
    path = ctx.paths.token
    try:
        info = path.stat()
    except FileNotFoundError:
        raise CaveError(f"the bootstrap did not leave the script token at {path}") from None
    problems = []
    if info.st_mode & 0o777 != 0o600:
        problems.append(f"its mode is {info.st_mode & 0o777:04o}, not 0600")
    euid = os.geteuid()
    if euid != 0 and info.st_uid != euid:
        problems.append(f"it belongs to user ID {info.st_uid}, not to you ({euid})")
    if problems:
        raise CaveError(
            f"the script token {path} cannot be used: {'; '.join(problems)}. The bootstrap gives "
            f"it to the owner of {path.parent}, which `./cave init` creates as the user who runs "
            f"./cave. Fix it with `sudo chown {euid}: {path.parent} {path}` and "
            f"`chmod 600 {path}`, then run `./cave bootstrap` again."
        )
    client = ctx.ha()  # raises when the file cannot be read
    if client is None:
        raise CaveError(f"the script token {path} is empty")
    client.config()  # raises when Home Assistant does not accept it


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    return set_up_home_assistant(ctx, settings_from_env(ctx.env, args.timeout), args.timeout)


def set_up_home_assistant(
    ctx: Context,
    settings: dict[str, Any],
    timeout: float,
    *,
    again: str = "./cave bootstrap",
    start: str = "./cave up",
) -> int:
    """Wait for the stack, run the tool with ``settings``, check the token and the health.

    ``cave demo`` calls this with its own context and settings; ``again`` is the command that
    continues an interrupted run, ``start`` the one that starts the stack.
    """
    # init creates it; if it went missing, recreate it as the user who runs ./cave, because the
    # tool gives the token file to the directory's owner.
    ctx.paths.token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    health = check_health(ctx, api=False)
    if not health.healthy:
        ctx.say(f"Waiting until the stack is healthy (now {health.describe()}) ...")
        health = wait_healthy(ctx, timeout=timeout, api=False)
    if not health.healthy:
        raise CaveError(
            f"the stack is {health.describe()} after {timeout:.0f} s; "
            f"start it with `{start}` and look at Home Assistant's log"
        )

    result = run_in_homeassistant(
        ctx,
        [TOOL],
        capture=False,
        input_text=json.dumps(settings),
        timeout=timeout + RUN_MARGIN,
    )
    if not result.ok:
        raise CaveError(
            f"the first-run setup failed (exit status {result.returncode}); "
            f"its messages are above. Running `{again}` again continues where it stopped."
        )

    check_token_file(ctx)
    health = wait_healthy(ctx, timeout=timeout)
    ctx.say(f"Stack {health.describe()}.")
    if not health.healthy:
        return 1
    ctx.say(f"Script token: {ctx.paths.token}. Home Assistant: {ctx.ha_url}")
    return 0
