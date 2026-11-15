"""``cave init``: create the state, the secrets and ``.env``. Run again, it changes nothing.

Everything is create-if-missing. Existing files are never rewritten, so their modification
times stay, and a ``.env`` that lacks a variable gets only that variable appended.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from ..context import Context
from ..envfile import HEADER, NAMES, VARIABLES, fresh_values, load_env, render, write_private
from ..errors import CaveError
from ..mosquitto import password_line
from ..paths import Paths, default_data_dir

HELP = "Create the state directories, secrets and .env (changes nothing when run again)"

SECRETS_YAML = "# Home Assistant secrets, for `!secret` in the configuration. Empty on purpose.\n"
PLACEHOLDER_DASHBOARD = """\
# Placeholder until `./cave apply` generates the heating dashboard from cave/heating.yaml.
title: Heizung
views:
  - title: Heizung
    cards:
      - type: markdown
        content: >-
          No rooms yet. Describe them in `cave/heating.yaml` and run `./cave apply`.
"""

# Files Home Assistant includes but does not create (concept section 4.2).
HA_FILES = {
    "secrets.yaml": SECRETS_YAML,
    "automations.yaml": "[]\n",
    "scripts.yaml": "{}\n",
    "scenes.yaml": "[]\n",
}

# Mount points of the read-only code mounts (compose.yaml). Created here so that they belong
# to the service user rather than to root, which Docker would otherwise create them as.
MOUNT_DIRS = ("packages", "custom_components/cave_heating")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data",
        metavar="DIR",
        help="state directory for a new .env (default: /var/lib/cave on a server, else ./data)",
    )


class _Report:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self.created = 0

    def made(self, what: Path | str) -> None:
        self.created += 1
        shown = what
        if isinstance(what, Path) and what.is_relative_to(self._ctx.paths.root):
            shown = what.relative_to(self._ctx.paths.root)
        self._ctx.say(f"created  {shown}")


def _make_dir(report: _Report, path: Path, mode: int = 0o755) -> None:
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, mode=mode)
    except OSError as error:
        raise CaveError(
            f"cannot create {path}: {error.strerror}. Run scripts/provision-host.sh first, "
            "or choose another state directory with `./cave init --data DIR`."
        ) from error
    report.made(path)


def _write_new(report: _Report, path: Path, content: str | bytes, mode: int = 0o644) -> None:
    if path.exists():
        return
    data = content.encode() if isinstance(content, str) else content
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    report.made(path)


def _ensure_env(ctx: Context, report: _Report, requested_data: Path) -> Paths:
    """Create or complete ``.env``; return the paths for the state directory it names."""
    env_file = ctx.paths.env_file
    existing = load_env(env_file)
    if env_file.exists():
        missing = [name for name in NAMES if name not in existing]
        if missing:
            added = fresh_values(requested_data, existing=existing)
            text = render(added, [v for v in VARIABLES if v.name in missing])
            with env_file.open("a", encoding="utf-8") as handle:
                handle.write("\n" + text)
            env_file.chmod(0o600)
            ctx.say(f"updated  {env_file} (added {', '.join(missing)})")
            existing.update(added)
        data = existing["CAVE_DATA"]
    else:
        # Check that the state directory can be made before .env names it: a failed init must
        # not leave a .env that points at a directory that cannot exist.
        _make_dir(report, requested_data)
        values = fresh_values(requested_data)
        write_private(env_file, HEADER + "\n" + render(values))
        report.made(f"{env_file} (mode 0600)")
        existing = values
        data = values["CAVE_DATA"]
    ctx.env.update(existing)
    return Paths(root=ctx.paths.root, data=Path(data))


def run(ctx: Context, args: argparse.Namespace) -> int:
    requested = Path(args.data).expanduser().resolve() if args.data else None
    if requested is None:
        requested = (
            Path(ctx.env["CAVE_DATA"])
            if "CAVE_DATA" in ctx.env
            else default_data_dir(ctx.paths.root, os.environ)
        )
    for template in (ctx.paths.heating_example, ctx.paths.z2m_template):
        if not template.is_file():
            raise CaveError(f"{template} is missing; run ./cave from a complete checkout")

    report = _Report(ctx)
    paths = _ensure_env(ctx, report, requested)
    ctx.paths = paths
    values = load_env(paths.env_file)

    for directory in (
        paths.data,
        paths.ha_config,
        paths.ha_config / "cave",
        paths.ha_config / "cave" / ".applied",
        paths.ha_config / "dashboards",
        paths.z2m_data,
        paths.mosquitto_data,
        paths.lock_file.parent,
    ):
        _make_dir(report, directory)
    _make_dir(report, paths.token.parent, 0o700)
    for relative in MOUNT_DIRS:
        _make_dir(report, paths.ha_config / relative)

    for name, content in HA_FILES.items():
        _write_new(
            report, paths.ha_config / name, content, 0o600 if name == "secrets.yaml" else 0o644
        )
    _write_new(report, paths.ha_config / "configuration.yaml", "", 0o644)  # single-file mount point

    starter = paths.heating_example.read_bytes()
    _write_new(report, paths.heating, starter)
    # The starter is stored as the applied copy directly: it is tracked and known to be valid,
    # and init must not depend on the validator, which needs the Home Assistant image.
    _write_new(report, paths.heating_applied, starter)
    _write_new(report, paths.dashboard, PLACEHOLDER_DASHBOARD)

    if not paths.z2m_config.exists():
        shutil.copyfile(paths.z2m_template, paths.z2m_config)
        report.made(paths.z2m_config)

    _write_new(
        report,
        paths.mosquitto_passwd,
        password_line(values["CAVE_MQTT_USER"], values["CAVE_MQTT_PASSWORD"]),
        0o600,
    )

    if report.created == 0:
        ctx.say("Nothing to do: already initialised.")
    else:
        ctx.say("Done. Next: review .env, then `./cave up`.")
    return 0
