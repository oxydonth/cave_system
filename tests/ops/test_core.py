"""The core modules other commands build on: runner, Home Assistant client, health, notify, lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cave_ops.errors import CaveError
from cave_ops.ha import HAClient, HAError, HAUnreachable, read_token
from cave_ops.health import check_health, parse_compose_ps, wait_healthy
from cave_ops.lock import Lock, LockBusy
from cave_ops.notify import notify
from cave_ops.paths import CONTAINER_CONFIG, Paths, default_data_dir
from cave_ops.runner import CommandError, SubprocessRunner

# ----------------------------------------------------------------------------------------- runner


def test_subprocess_runner_captures_output_and_status():
    runner = SubprocessRunner()

    result = runner.run(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"]
    )

    assert (result.returncode, result.stdout.strip(), result.stderr) == (3, "out", "err")
    assert not result.ok
    assert result.output.startswith("out")


def test_subprocess_runner_check_raises_with_the_tail_of_the_output():
    runner = SubprocessRunner()

    with pytest.raises(CommandError) as raised:
        runner.run([sys.executable, "-c", "import sys; print('boom'); sys.exit(2)"], check=True)

    assert raised.value.result.returncode == 2
    assert "boom" in str(raised.value)
    assert isinstance(raised.value, CaveError)


def test_subprocess_runner_reports_a_missing_program_as_status_127():
    result = SubprocessRunner().run(["cave-no-such-program"])

    assert result.returncode == 127
    assert "not found" in result.stderr


def test_subprocess_runner_passes_cwd_environment_and_input(tmp_path):
    code = "import os, sys; print(os.getcwd(), os.environ['CAVE_TEST'], sys.stdin.read())"

    result = SubprocessRunner().run(
        [sys.executable, "-c", code], cwd=tmp_path, env={"CAVE_TEST": "yes"}, input_text="in"
    )

    assert result.stdout.split() == [str(tmp_path.resolve()), "yes", "in"]


def test_fake_runner_rejects_unexpected_commands(fake_runner):
    with pytest.raises(AssertionError, match="unexpected command: docker ps"):
        fake_runner.run(["docker", "ps"])


def test_fake_runner_later_rules_win_and_check_raises(fake_runner):
    fake_runner.on(["docker"], rc=0)
    fake_runner.on(["docker", "ps"], rc=1, err="no daemon")

    assert fake_runner.run(["docker", "info"]).ok
    with pytest.raises(CommandError):
        fake_runner.run(["docker", "ps"], check=True)


# ------------------------------------------------------------------------------------------- paths


def test_paths_follow_the_contract_of_concept_section_4_5(tmp_path):
    paths = Paths(root=tmp_path / "repo", data=tmp_path / "data")

    assert paths.token == tmp_path / "data/homeassistant/.cave/token"
    assert paths.heating == tmp_path / "data/homeassistant/cave/heating.yaml"
    assert paths.heating_applied == tmp_path / "data/homeassistant/cave/.applied/heating.yaml"
    assert paths.dashboard == tmp_path / "data/homeassistant/dashboards/heating.yaml"
    assert paths.lock_file == tmp_path / "data/.cave/lock"
    assert paths.container_path(paths.token) == f"{CONTAINER_CONFIG}/.cave/token"
    assert paths.container_path(tmp_path / "elsewhere.yaml") is None


def test_default_data_directory_prefers_the_environment_then_the_checkout(tmp_path):
    assert (
        default_data_dir(tmp_path, {"CAVE_DATA": str(tmp_path / "x")}) == (tmp_path / "x").resolve()
    )
    # On a machine without a writable /var/lib/cave (every development machine and CI runner).
    if not Path("/var/lib/cave").is_dir():
        assert default_data_dir(tmp_path, {}) == (tmp_path / "data").resolve()


# --------------------------------------------------------------------------------- Home Assistant


def test_read_token_missing_empty_and_present(ctx, write_token):
    assert read_token(ctx.paths) is None
    write_token("abc")
    assert read_token(ctx.paths) == "abc"
    ctx.paths.token.write_text("\n")
    assert read_token(ctx.paths) is None


def test_ctx_ha_needs_a_token(ctx, write_token):
    assert ctx.ha() is None
    write_token()
    assert isinstance(ctx.ha(), HAClient)


def test_client_sends_the_token_and_decodes_json(ctx, fake_ha, write_token):
    write_token()
    fake_ha.states["sensor.x"] = {"state": "21.5"}
    client = ctx.ha()

    assert client.get_state("sensor.x") == {"state": "21.5"}
    assert client.get_state("sensor.missing") is None
    assert client.config()["components"] == fake_ha.components
    assert client.services()["persistent_notification"] == {"create", "dismiss"}
    assert client.has_service("homeassistant", "reload_all")
    assert not client.has_service("script", "cave_notify")


def test_client_posts_service_calls_as_json(ctx, fake_ha, write_token):
    write_token()

    ctx.ha().call_service("persistent_notification", "create", {"message": "hi"})

    assert fake_ha.service_calls == [("persistent_notification", "create", {"message": "hi"})]


def test_client_reports_errors_with_status(ctx, fake_ha, write_token):
    write_token("the-wrong-token")

    with pytest.raises(HAError) as raised:
        ctx.ha().config()
    assert raised.value.status == 401


def test_client_reports_an_unreachable_home_assistant(ctx, fake_ha, write_token):
    write_token()
    fake_ha.down = True

    with pytest.raises(HAUnreachable, match="cannot reach"):
        ctx.ha().config()


def test_calling_an_unknown_service_is_an_error(ctx, write_token):
    write_token()

    with pytest.raises(HAError) as raised:
        ctx.ha().call_service("script", "nope")
    assert raised.value.status == 400


# ----------------------------------------------------------------------------------------- health


def ps(**states):
    return "\n".join(
        json.dumps({"Service": name, "State": state, "Health": health})
        for name, (state, health) in states.items()
    )


def stack(fake_runner, services, listing):
    fake_runner.on(["docker", "compose", "config", "--services"], out="\n".join(services) + "\n")
    fake_runner.on(["docker", "compose", "ps"], out=listing)


def test_parse_ps_accepts_lines_and_arrays():
    assert parse_compose_ps('{"Service": "a"}\n{"Service": "b"}\n') == [
        {"Service": "a"},
        {"Service": "b"},
    ]
    assert parse_compose_ps('[{"Service": "a"}]') == [{"Service": "a"}]
    assert parse_compose_ps("") == []


def test_healthy_when_every_service_runs_and_passes_its_check(ctx, fake_runner):
    stack(fake_runner, ["a", "b"], ps(a=("running", "healthy"), b=("running", "healthy")))

    assert check_health(ctx).healthy


@pytest.mark.parametrize(
    ("listing", "problem"),
    [
        (ps(a=("running", "healthy")), "b: no container"),
        (ps(a=("running", "healthy"), b=("exited", "")), "b: exited"),
        (ps(a=("running", "starting"), b=("running", "healthy")), "a: health check starting"),
        (ps(a=("running", "healthy"), b=("running", "unhealthy")), "b: health check unhealthy"),
    ],
)
def test_not_healthy_names_the_failing_service(ctx, fake_runner, listing, problem):
    stack(fake_runner, ["a", "b"], listing)

    health = check_health(ctx)

    assert not health.healthy
    assert problem in health.problems


def test_only_services_of_the_active_profiles_are_expected(ctx, fake_runner):
    # `config --services` omits zigbee2mqtt without the profile, so it is not required.
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))

    assert check_health(ctx).healthy


def test_with_a_token_home_assistant_must_have_set_up_cave_heating(
    ctx, fake_runner, fake_ha, write_token
):
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))
    write_token()
    assert check_health(ctx).healthy

    fake_ha.components = ["homeassistant"]
    health = check_health(ctx)
    assert not health.healthy
    assert "cave_heating" in health.problems[0]

    fake_ha.down = True
    assert "Home Assistant API" in check_health(ctx).problems[0]


def test_without_a_token_the_api_is_not_asked(ctx, fake_runner, fake_ha):
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))

    check_health(ctx)

    assert fake_ha.requests == []


def test_docker_failures_are_reported_not_raised(ctx, fake_runner):
    fake_runner.on(["docker", "compose", "config"], rc=1, err="no such file")

    health = check_health(ctx)

    assert not health.healthy
    assert "no such file" in health.problems[0]


def test_wait_healthy_returns_the_last_verdict_after_the_timeout(ctx, fake_runner, clock):
    stack(fake_runner, ["a"], ps(a=("running", "starting")))

    health = wait_healthy(ctx, timeout=20, interval=5)

    assert not health.healthy
    assert clock.now == 20


# ---------------------------------------------------------------------------------------- notify


def test_notify_uses_the_script_when_it_exists(ctx, fake_ha, write_token, journal):
    write_token()
    fake_ha.services["script"] = ["cave_notify"]

    result = notify(ctx, "Update", "Installed v1")

    assert result.via == "script"
    assert fake_ha.service_calls == [
        ("script", "cave_notify", {"title": "Update", "message": "Installed v1"})
    ]
    assert journal == ["Update: Installed v1"]


def test_notify_falls_back_to_a_persistent_notification(ctx, fake_ha, write_token, journal):
    write_token()

    result = notify(ctx, "Backup", "failed")

    assert result.via == "persistent_notification"
    assert fake_ha.service_calls == [
        ("persistent_notification", "create", {"title": "Backup", "message": "failed"})
    ]
    assert journal == ["Backup: failed"]


def test_notify_always_writes_the_journal_even_without_home_assistant(
    ctx, fake_ha, write_token, journal
):
    # No token yet: only the journal.
    assert not notify(ctx, "A", "b").delivered
    assert "no script token" in (notify(ctx, "A", "b").error or "")

    # A token, but Home Assistant is down: the caller is not disturbed.
    write_token()
    fake_ha.down = True
    result = notify(ctx, "C", "d")

    assert not result.delivered
    assert "cannot reach" in (result.error or "")
    assert journal[-1] == "C: d"


# ------------------------------------------------------------------------------------------ lock


def test_lock_is_exclusive_and_names_the_holder(tmp_path):
    path = tmp_path / ".cave" / "lock"
    with Lock(path, "update"), pytest.raises(LockBusy, match="cave update"):
        Lock(path, "backup").acquire()


def test_lock_is_released_after_the_block_and_after_an_error(tmp_path):
    path = tmp_path / "lock"
    with pytest.raises(RuntimeError), Lock(path, "update"):
        raise RuntimeError("boom")

    with Lock(path, "backup"):
        pass


def test_lock_is_exclusive_across_processes_and_freed_when_the_holder_dies(tmp_path, repo_root):
    path = tmp_path / "lock"
    code = (
        "import sys; from pathlib import Path; from cave_ops.lock import Lock\n"
        "lock = Lock(Path(sys.argv[1]), 'update'); lock.acquire()\n"
        "print('held', flush=True); sys.stdin.read()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        env={**os.environ, "PYTHONPATH": str(repo_root / "ops")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(LockBusy, match="cave update"):
            Lock(path, "backup").acquire()
    finally:
        holder.kill()  # no clean-up code runs: the operating system must drop the lock
        holder.wait()

    with Lock(path, "backup"):
        pass


def test_ctx_lock_uses_the_lock_file_of_the_contract(ctx):
    with ctx.lock("update"):
        assert ctx.paths.lock_file.read_text().startswith("`cave update`")
        with pytest.raises(LockBusy):
            ctx.lock("restore").acquire()
