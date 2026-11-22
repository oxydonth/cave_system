"""The systemd units in ``scripts/systemd`` and their installation by ``provision-host.sh``."""

from __future__ import annotations

import configparser
import os
import shlex
import shutil
import stat
import subprocess

import pytest

from cave_ops import cli

UNITS = (
    "cave-update.service",
    "cave-update.timer",
    "cave-backup.service",
    "cave-backup.timer",
    "cave-recover.service",
)
CHECKOUT = "/opt/cave"


def unit(repo_root, name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read(repo_root / "scripts/systemd" / name)
    return parser


def cave_arguments(command_line):
    """The arguments of a ``/opt/cave/cave ...`` command line, parsed by the real parser."""
    words = shlex.split(command_line)
    assert words[0] == f"{CHECKOUT}/cave"
    return cli.build_parser(cli.discover()).parse_args(words[1:])


def test_every_unit_exists_and_nothing_else(repo_root):
    assert sorted(p.name for p in (repo_root / "scripts/systemd").iterdir()) == sorted(UNITS)


@pytest.mark.parametrize(
    ("name", "command", "flags"),
    [
        ("cave-update.service", "update", {"auto": True, "recover": False, "force": False}),
        ("cave-recover.service", "update", {"recover": True, "auto": False}),
        ("cave-backup.service", "backup", {}),
    ],
)
def test_services_run_real_commands_as_root_once_initialised(repo_root, name, command, flags):
    service = unit(repo_root, name)

    args = cave_arguments(service["Service"]["ExecStart"])
    assert args.command == command
    for flag, value in flags.items():
        assert getattr(args, flag) is value
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["User"] == "root"
    assert service["Service"]["WorkingDirectory"] == CHECKOUT
    assert service["Unit"]["ConditionPathExists"] == f"{CHECKOUT}/.env"
    # Root writes no bytecode into the service user's checkout.
    assert service["Service"]["Environment"] == "PYTHONDONTWRITEBYTECODE=1"


def test_the_update_service_recovers_however_it_ends(repo_root):
    service = unit(repo_root, "cave-update.service")

    args = cave_arguments(service["Service"]["ExecStopPost"])
    assert (args.command, args.recover) == ("update", True)
    assert "docker.service" in service["Unit"]["Requires"]


def test_the_recovery_runs_at_boot_after_docker(repo_root):
    service = unit(repo_root, "cave-recover.service")

    assert service["Install"]["WantedBy"] == "multi-user.target"
    assert "docker.service" in service["Unit"]["After"]


def test_backup_runs_before_the_update(repo_root):
    backup = unit(repo_root, "cave-backup.timer")["Timer"]
    update = unit(repo_root, "cave-update.timer")["Timer"]

    assert backup["OnCalendar"] == "*-*-* 02:30"
    assert update["OnCalendar"] == "*-*-* 03:30"
    assert update["Persistent"] == "false"  # no update in the daytime after a night switched off


# ------------------------------------------------------------------------------ provisioning
@pytest.fixture
def stubs(tmp_path):
    """Recording stubs for every program that could change the system, first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "calls.txt"
    for name in ("apt-get", "curl", "install", "chmod", "useradd", "usermod", "systemctl"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{record}"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bin_dir, record


def dry_run(script, bin_dir):
    return subprocess.run(
        ["bash", str(script), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )


def test_provisioning_installs_and_enables_the_units(repo_root, stubs):
    bin_dir, record = stubs

    done = dry_run(repo_root / "scripts/provision-host.sh", bin_dir)

    assert done.returncode == 0, done.stderr
    for name in UNITS:
        assert (
            f"would run: install -m 0644 {repo_root}/scripts/systemd/{name} "
            f"/etc/systemd/system/{name}" in done.stdout
        )
    assert "would run: systemctl daemon-reload" in done.stdout
    assert "would run: systemctl enable --now cave-update.timer" in done.stdout
    assert "would run: systemctl enable --now cave-backup.timer" in done.stdout
    assert "would run: systemctl enable cave-recover.service" in done.stdout
    assert not record.exists(), record.read_text()


def test_a_script_copied_alone_says_to_run_it_again_from_the_checkout(repo_root, stubs, tmp_path):
    bin_dir, _record = stubs
    alone = tmp_path / "copied" / "provision-host.sh"
    alone.parent.mkdir()
    shutil.copy(repo_root / "scripts/provision-host.sh", alone)

    done = dry_run(alone, bin_dir)

    assert done.returncode == 0, done.stderr
    assert "skipped" in done.stdout
    assert "Run /opt/cave/scripts/provision-host.sh again" in done.stdout
