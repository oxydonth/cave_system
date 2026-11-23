"""``scripts/provision-host.sh --dry-run`` must describe the work and do none of it."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

# Every program the script could use to change the system. In the test they are stubs that
# only record that they were called.
CHANGING_PROGRAMS = [
    "apt-get",
    "curl",
    "install",
    "chmod",
    "useradd",
    "usermod",
    "systemctl",
    "tee",
    "mkdir",
]


@pytest.fixture
def stubs(tmp_path):
    """A directory of recording stubs to put first on PATH, and the file they record to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "calls.txt"
    for name in CHANGING_PROGRAMS:
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{record}"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bin_dir, record


def run_script(repo_root, args, path_prefix=None):
    env = dict(os.environ)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(repo_root / "scripts/provision-host.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_dry_run_lists_every_step_and_runs_nothing_that_changes_the_system(repo_root, stubs):
    bin_dir, record = stubs

    done = run_script(repo_root, ["--dry-run"], bin_dir)

    assert done.returncode == 0, done.stderr
    assert "Dry run finished: nothing was changed." in done.stdout
    for step in (
        "docker-ce",
        "docker-compose-plugin",
        "download.docker.com/linux/debian",
        "git",
        "python3",
        "useradd",
        "--home-dir /var/lib/cave-home",  # not the checkout: a clone needs an empty directory
        "9DC858229FC7DD38854AE2D88D81803C0EBFCD88",  # Docker's published key fingerprint
        "/opt/cave",
        "/var/lib/cave",
    ):
        assert step in done.stdout, step
    assert not record.exists(), f"the dry run called: {record.read_text()}"


def test_dry_run_needs_no_root(repo_root):
    done = run_script(repo_root, ["--dry-run"])

    assert done.returncode == 0, done.stderr


def test_dry_run_respects_the_service_user_option(repo_root):
    done = run_script(repo_root, ["--dry-run", "--user", "heating"])

    assert done.returncode == 0
    assert "useradd" in done.stdout
    assert "heating" in done.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="the refusal is for non-root users")
def test_a_real_run_without_root_is_refused_before_changing_anything(repo_root, stubs):
    bin_dir, record = stubs

    done = run_script(repo_root, [], bin_dir)

    assert done.returncode == 1
    assert "root" in done.stderr
    assert not record.exists()


def test_unknown_options_are_refused(repo_root):
    done = run_script(repo_root, ["--bogus"])

    assert done.returncode == 1
    assert "unknown option" in done.stderr


def _apt_get_of_the_script(repo_root, tmp_path, fail_times, message):
    """Run the script's own ``apt_get`` with an apt-get that fails ``fail_times`` times."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    count = tmp_path / "count"
    count.write_text("0")
    stub = bin_dir / "apt-get"
    stub.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{count}"); echo $((n + 1)) > "{count}"; echo "apt-get $*"\n'
        f'if [ "$n" -lt {fail_times} ]; then echo "{message}" >&2; exit 100; fi\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    text = (repo_root / "scripts/provision-host.sh").read_text()
    function = text[text.index("apt_get() {") :]
    function = function[: function.index("\n}\n") + 3]
    code = function + "say() { printf '%s\\n' \"$*\"; }\nsleep() { :; }\napt_get update\n"
    done = subprocess.run(
        ["bash", "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )
    return done, int(count.read_text())


def test_apt_get_waits_for_a_package_lock_that_debians_daily_run_holds(repo_root, tmp_path):
    """Right after the first boot, apt-daily.timer may hold the lock of the package lists: the
    script waits for it instead of stopping (found in the review of the server rehearsal)."""
    done, calls = _apt_get_of_the_script(
        repo_root, tmp_path, 2, "E: Could not get lock /var/lib/apt/lists/lock."
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert calls == 3
    assert "apt-get -o DPkg::Lock::Timeout=600 update" in done.stdout
    assert done.stdout.count("the package lock is held") == 2


def test_apt_get_does_not_try_again_after_another_failure(repo_root, tmp_path):
    done, calls = _apt_get_of_the_script(repo_root, tmp_path, 5, "E: Unable to locate package x")

    assert done.returncode == 100
    assert calls == 1
