"""``cave demo``: the whole system with simulated devices, started with one command.

The demo is a stack of its own next to the home's: Compose project ``cave-demo``, the overlay
``compose.sim.yaml`` (the ``sim`` service: the Zigbee2MQTT stand-in, the fake FRITZ!Box and the
control API), and a state directory of its own (``data/demo`` in the checkout unless ``--data``
says otherwise) with its own generated secrets in ``demo.env``. It needs no ``./cave init``, and it
never touches the home's ``.env`` or state. Because Home Assistant runs on the host network, the
demo and the home's stack use the same ports and cannot run at the same time.

``cave demo`` (or ``cave demo up``):

1. prepares the demo's state directory and ``demo.env``;
2. validates ``sim/demo/heating.yaml`` and stores it as the demo's applied copy through the shared
   ``validate_and_store``;
3. builds the simulator image and starts the stack;
4. runs the first-run setup against the simulators, as ``cave bootstrap`` does: FRITZ!SmartHome
   at the fake FRITZ!Box (FRITZ!Box Tools needs TR-064, which it does not have), and the switch
   that stands in for presence.

Run again, it starts the demo afresh with the same state. ``cave demo down`` stops it and keeps
the state.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any

from ..context import Context
from ..envfile import NAMES, fresh_values, generate_secret, load_env, render, write_private
from ..errors import CaveError
from ..heating import validate_and_store
from ..mosquitto import password_line
from ..paths import Paths
from .bootstrap import set_up_home_assistant, settings_from_env
from .init import HA_FILES, MOUNT_DIRS, PLACEHOLDER_DASHBOARD

HELP = "Start (or `down`: stop) a demo with simulated devices in a stack of its own"

PROJECT = "cave-demo"
HOME_PROJECT = "cave"  # the name in compose.yaml
OVERLAY = "compose.sim.yaml"
HEATING = Path("sim") / "demo" / "heating.yaml"
FRITZ_URL = "http://127.0.0.1:8480"  # the fake FRITZ!Box (concept section 4.1)
FRITZ_USER = "cave"
CONTROL_API = "http://127.0.0.1:8481"
PRESENCE_SWITCH = ("input_boolean.demo_someone_home", "Demo: someone is home")
ENV_HEADER = (
    "# The demo's settings and generated secrets, written by `./cave demo` (mode 0600). Only the\n"
    "# demo reads this file; the home's stack uses .env in the checkout.\n"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action", nargs="?", choices=("up", "down"), default="up", help="start (default) or stop"
    )
    parser.add_argument(
        "--data", metavar="DIR", help="the demo's state directory (default: data/demo)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long to wait for the stack and Home Assistant (default 300)",
    )


def demo_data(ctx: Context, args: argparse.Namespace) -> Path:
    if args.data:
        return Path(args.data).expanduser().resolve()
    return (ctx.paths.root / "data" / "demo").resolve()


def demo_env(data: Path) -> dict[str, str]:
    """The demo's variables from ``demo.env``, which is created or completed as needed."""
    path = data / "demo.env"
    values = load_env(path)
    wanted = fresh_values(data, existing=values)
    for name in ("CAVE_FRITZ_HOST", "CAVE_FRITZ_USER", "CAVE_FRITZ_PASSWORD"):
        if not values.get(name):
            wanted[name] = {
                "CAVE_FRITZ_HOST": FRITZ_URL,
                "CAVE_FRITZ_USER": FRITZ_USER,
                "CAVE_FRITZ_PASSWORD": generate_secret(),
            }[name]
    if wanted or not path.exists():
        values.update(wanted)
        values["CAVE_DATA"] = str(data)
        values["COMPOSE_PROFILES"] = ""  # no Zigbee2MQTT: the stand-in replaces it
        data.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        write_private(path, ENV_HEADER + "\n" + render(values))
    return values


def demo_context(ctx: Context, data: Path, env: dict[str, str]) -> Context:
    """A context for the demo: its paths, its variables, its Compose project and the overlay."""
    compose_env = {name: env[name] for name in NAMES if name in env}
    compose_env.update(COMPOSE_PROJECT_NAME=PROJECT, CAVE_DATA=str(data), COMPOSE_PROFILES="")
    return dataclasses.replace(
        ctx,
        paths=Paths(root=ctx.paths.root, data=data),
        env=dict(env),
        compose_files=(OVERLAY,),
        compose_env=compose_env,
    )


def _write_if_missing(path: Path, content: str | bytes, mode: int = 0o644) -> None:
    if path.exists():
        return
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)


def prepare_state(ctx: Context) -> None:
    """The demo's state directory: what ``init`` creates for the home, without Zigbee2MQTT."""
    paths = ctx.paths
    for directory in (
        paths.ha_config / "cave" / ".applied",
        paths.ha_config / "dashboards",
        paths.mosquitto_data,
        paths.lock_file.parent,
        *(paths.ha_config / relative for relative in MOUNT_DIRS),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    paths.token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, content in HA_FILES.items():
        _write_if_missing(
            paths.ha_config / name, content, 0o600 if name == "secrets.yaml" else 0o644
        )
    _write_if_missing(paths.ha_config / "configuration.yaml", "")  # single-file mount point
    _write_if_missing(paths.dashboard, PLACEHOLDER_DASHBOARD)
    # The broker's container takes the directory over at its first start; the demo's login
    # never changes, so a file that exists is the right one.
    _write_if_missing(
        paths.mosquitto_passwd,
        password_line(ctx.env["CAVE_MQTT_USER"], ctx.env["CAVE_MQTT_PASSWORD"]),
        0o600,
    )
    # The edited copy is the demo's description too, so that it agrees with the applied one.
    heating = (paths.root / HEATING).read_bytes()
    if not paths.heating.is_file() or paths.heating.read_bytes() != heating:
        paths.heating.write_bytes(heating)


def home_stack_running(ctx: Context) -> bool:
    found = ctx.docker(
        "ps", "--quiet", "--filter", f"label=com.docker.compose.project={HOME_PROJECT}"
    )
    return found.ok and bool(found.stdout.strip())


def bootstrap_settings(env: dict[str, str], timeout: float) -> dict[str, Any]:
    settings = settings_from_env(env, timeout)
    settings["fritz_tools"] = False  # the fake FRITZ!Box has no TR-064
    settings["switches"] = [{"entity_id": PRESENCE_SWITCH[0], "name": PRESENCE_SWITCH[1]}]
    return settings


def up(ctx: Context, args: argparse.Namespace) -> int:
    if home_stack_running(ctx):
        raise CaveError(
            "the home's stack is running; the demo uses the same ports (8123, 1883). "
            "Stop it with `./cave down` first."
        )
    data = demo_data(ctx, args)
    env = demo_env(data)
    demo = demo_context(ctx, data, env)
    prepare_state(demo)
    demo.say(f"Demo state: {data}")

    demo.say("Building the simulator image ...")
    if not demo.compose("build", "sim", capture=False, timeout=args.timeout + 300).ok:
        raise CaveError("building the simulator image failed; its output is above")

    demo.say(f"Validating {HEATING} and storing it as the demo's applied copy ...")
    applied = validate_and_store(demo, demo.paths.root / HEATING)
    for line in applied.messages:
        (demo.say if applied.ok else demo.warn)(line)
    if not applied.ok:
        return 1

    started = demo.compose("up", "--detach", "--force-recreate", capture=False)
    if not started.ok:
        return started.returncode
    code = set_up_home_assistant(
        demo,
        bootstrap_settings(env, args.timeout),
        args.timeout,
        again="./cave demo",
        start="./cave demo",
    )
    if code != 0:
        return code
    demo.say(
        "\nThe demo is running.\n"
        f"  Home Assistant: http://<this machine>:8123, login {env['CAVE_HA_OWNER_USER']!r}; the "
        f"password is CAVE_HA_OWNER_PASSWORD in {data / 'demo.env'}\n"
        f"  The simulators' control API: {CONTROL_API} (see sim/README.md)\n"
        "  Stop it with `./cave demo down`. See docs/demo.md."
    )
    return 0


def down(ctx: Context, args: argparse.Namespace) -> int:
    data = demo_data(ctx, args)
    env = load_env(data / "demo.env")
    if not env:
        ctx.say(f"No demo in {data}: nothing to stop.")
        return 0
    return demo_context(ctx, data, env).compose("down", capture=False).returncode


def run(ctx: Context, args: argparse.Namespace) -> int:
    return up(ctx, args) if args.action == "up" else down(ctx, args)
