"""``cave init``: create the state, the secrets and ``.env``. Run again, it changes nothing.

Everything is create-if-missing, so a second run changes nothing and says so. The two things
that must stay in step are repaired when they are not: ``.env`` gets a value for every variable
that lacks a usable one (and mode 0600), and the broker's password file is made to accept the
login that ``.env`` names.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from ..context import Context
from ..envfile import (
    HEADER,
    apply_updates,
    fresh_values,
    load_env,
    missing_names,
    render,
    write_private,
)
from ..errors import CaveError
from ..mosquitto import accepts, password_line
from ..paths import Paths, default_data_dir
from ..state import HA_FILES, MOUNT_DIRS, PLACEHOLDER_DASHBOARD, home_assistant_directories

HELP = "Create the state directories, secrets and .env (changes nothing when run again)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data",
        metavar="DIR",
        help="state directory for a new .env (default: /var/lib/cave on a server, else ./data)",
    )


class _Report:
    """Counts what init changed, so that "nothing to do" is only said when it is true."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self.changes = 0

    def _shown(self, what: Path | str) -> Path | str:
        if isinstance(what, Path) and what.is_relative_to(self._ctx.paths.root):
            return what.relative_to(self._ctx.paths.root)
        return what

    def made(self, what: Path | str) -> None:
        self.changes += 1
        self._ctx.say(f"created  {self._shown(what)}")

    def updated(self, what: Path | str, why: str) -> None:
        self.changes += 1
        self._ctx.say(f"updated  {self._shown(what)} ({why})")


def _make_dir(report: _Report, path: Path, mode: int = 0o755) -> None:
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, mode=mode)
    except OSError as error:
        raise CaveError(
            f"cannot create {path}: {error.strerror}. Run scripts/provision-host.sh first, "
            "or choose a directory you can write: `./cave init --data DIR` for a new .env, "
            "or CAVE_DATA in an existing one."
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


def _replace_file(path: Path, text: str, mode: int) -> None:
    """Write ``text`` to ``path`` atomically (a temporary file in the same directory)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _ensure_env(ctx: Context, report: _Report, requested_data: Path | None) -> Paths:
    """Create or complete ``.env``; return the paths for the state directory it names.

    A variable that is missing, or empty where it cannot work (a generated secret, the state
    directory), gets a value. Everything else the owner set is kept.
    """
    env_file = ctx.paths.env_file
    default_data = requested_data or default_data_dir(ctx.paths.root, os.environ)
    if not env_file.exists():
        # Check that the state directory can be made before .env names it: a failed init must
        # not leave a .env that points at a directory that cannot exist.
        _make_dir(report, default_data)
        values = fresh_values(default_data)
        write_private(env_file, HEADER + "\n" + render(values))
        report.made(f"{env_file} (mode 0600)")
        ctx.env.update(values)
        return Paths(root=ctx.paths.root, data=Path(values["CAVE_DATA"]))

    existing = load_env(env_file)
    named = existing.get("CAVE_DATA")
    if requested_data is not None and named and Path(named).expanduser() != requested_data:
        raise CaveError(
            f"{env_file} already sets CAVE_DATA={named}, so --data {requested_data} would be "
            "ignored. Edit CAVE_DATA in .env, or remove .env to start again."
        )
    needed = missing_names(existing)
    if needed:
        added = fresh_values(default_data, names=needed)
        _replace_file(env_file, apply_updates(env_file.read_text(encoding="utf-8"), added), 0o600)
        report.updated(env_file, "set " + ", ".join(needed))
        existing.update(added)
    if stat.S_IMODE(env_file.stat().st_mode) != 0o600:
        env_file.chmod(0o600)
        report.updated(env_file, "mode set to 0600, it holds secrets")
    ctx.env.update(existing)
    return Paths(root=ctx.paths.root, data=Path(existing["CAVE_DATA"]).expanduser())


def _ensure_broker_password(
    ctx: Context, report: _Report, paths: Paths, values: dict[str, str]
) -> None:
    """Make the broker's password file accept exactly the login that ``.env`` names.

    Once the broker has run, its container owns the file and the user of ``./cave`` cannot read
    it; a stamp that records which login the file was written for stands in for the check then.
    When the file has to change but cannot be written, stop with the command that fixes it.
    """
    user, password = values["CAVE_MQTT_USER"], values["CAVE_MQTT_PASSWORD"]
    passwd, stamp = paths.mosquitto_passwd, paths.mqtt_stamp
    expected = hashlib.sha256(f"{user}\0{password}".encode()).hexdigest()

    consistent = False
    if passwd.exists():
        try:
            consistent = accepts(passwd.read_text(encoding="utf-8"), user, password)
        except PermissionError:
            consistent = stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == expected
    if not consistent:
        existed = passwd.exists()
        try:
            _replace_file(passwd, password_line(user, password), 0o600)
        except PermissionError as error:
            raise CaveError(
                f"the broker's password file {passwd} does not match the login in .env, and "
                f"cannot be replaced: {error.strerror}. The broker's container owns it. Run "
                f'`sudo chown -R "$(id -un)": {passwd.parent}`, then `./cave init` and `./cave up`.'
            ) from error
        if existed:
            report.updated(passwd, "now matches CAVE_MQTT_USER and CAVE_MQTT_PASSWORD in .env")
            ctx.say("The broker reads its password file at start: run `./cave up` to apply it.")
        else:
            report.made(passwd)
    if not stamp.is_file() or stamp.read_text(encoding="utf-8").strip() != expected:
        _replace_file(stamp, expected + "\n", 0o600)  # bookkeeping, not a change the user sees


def run(ctx: Context, args: argparse.Namespace) -> int:
    requested = Path(args.data).expanduser().resolve() if args.data else None
    for template in (ctx.paths.heating_example, ctx.paths.z2m_template):
        if not template.is_file():
            raise CaveError(f"{template} is missing; run ./cave from a complete checkout")

    report = _Report(ctx)
    paths = _ensure_env(ctx, report, requested)
    ctx.paths = paths
    values = load_env(paths.env_file)

    for directory in (
        paths.data,
        *home_assistant_directories(paths),
        paths.z2m_data,
        paths.mosquitto_data,
        paths.lock_file.parent,
        # compose.yaml mounts it into Home Assistant. If it were missing, the Docker daemon
        # would create it as root inside the checkout, and the next update of the checkout to
        # a release that tracks files in it would fail with "Permission denied".
        paths.root / "tools",
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

    _ensure_broker_password(ctx, report, paths, values)

    if report.changes == 0:
        ctx.say("Nothing to do: already initialised.")
    else:
        ctx.say("Done. Next: review .env, then `./cave up`.")
    return 0
