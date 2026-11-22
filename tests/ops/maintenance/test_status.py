"""``cave status``: installed release, health, last update and backup, unapplied heating.yaml."""

from __future__ import annotations

import pytest

from cave_ops.lock import Lock

NEW = "v2026.11.15.1"


def field(output, label):
    for line in output.splitlines():
        if line.startswith(f"{label}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no {label!r} line in:\n{output}")


def test_a_healthy_server_after_an_update(server, run_cave):
    server.release(NEW)
    assert run_cave("update").code == 0
    server.stack.ops.clear()
    server.runner.fake.calls.clear()

    result = run_cave("status")

    assert result.code == 0
    assert field(result.out, "Release") == NEW
    assert field(result.out, "Stack") == "healthy"
    assert field(result.out, "Last update").endswith(
        f"Installed {NEW} (was v2026.11.14.1); "
        "the stack is healthy. The snapshot of the state from before the update is kept as "
        "0001-v2026.11.14.1-to-v2026.11.15.1."
    )
    assert field(result.out, "Updates") == "nightly, automatic (CAVE_AUTO_UPDATE=1)"
    assert field(result.out, "heating.yaml") == "applied, no changes since"
    # Read-only: nothing was stopped, started or fetched.
    assert server.stack.ops == []
    assert not any(call.args[1:2] == ("fetch",) for call in server.runner.fake.calls)


def test_a_failed_release_and_a_newer_one_waiting(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    run_cave("update")

    result = run_cave("status")

    assert result.code == 0
    assert field(result.out, "Release") == "v2026.11.14.1"
    assert field(result.out, "Newer") == f"{NEW} is available and not installed"
    assert field(result.out, "Failed").startswith(f"{NEW} on ")
    assert "Rolled back" in field(result.out, "Last update")


def test_an_unhealthy_stack_and_unapplied_changes(server, run_cave, ctx):
    server.stack.unhealthy.add("v2026.11.14.1")
    ctx.paths.heating.write_text("version: 1\nrooms: [{name: Bad}]\n")
    ctx.env["CAVE_AUTO_UPDATE"] = "0"

    result = run_cave("status")

    assert result.code == 1
    assert field(result.out, "Stack").startswith("not healthy: homeassistant: health check")
    assert field(result.out, "heating.yaml").startswith("CHANGED since the last `./cave apply`")
    assert field(result.out, "Updates") == "manual only (CAVE_AUTO_UPDATE=0)"


def test_an_interrupted_update_is_shown_with_what_to_do(server, run_cave, killed):
    server.release(NEW)

    def kill(release):
        if release == NEW:
            raise killed

    server.stack.before["up"] = kill
    with pytest.raises(killed):
        run_cave("update")

    result = run_cave("status")

    assert result.code == 1
    assert "`sudo ./cave update --recover` finishes it" in field(result.out, "INTERRUPTED")


def test_a_running_update_is_named_and_not_called_interrupted(server, run_cave, ctx):
    with Lock(ctx.paths.lock_file, "update"):
        result = run_cave("status")

    assert field(result.out, "Running now").startswith("`cave update` (pid ")
    assert "INTERRUPTED" not in result.out


def test_the_last_backup(server, run_cave, ctx, tmp_path):
    backups = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(backups)
    assert field(run_cave("status").out, "Last backup") == f"none in {backups}"

    assert run_cave("backup").code == 0
    assert run_cave("backup").code == 0

    line = field(run_cave("status").out, "Last backup")
    assert line.startswith(str(backups / "cave-backup-"))
    assert line.endswith("; 2 kept")
