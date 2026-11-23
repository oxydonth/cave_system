"""The core modules other commands build on: runner, Home Assistant client, health, notify, lock."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cave_ops.container import run_in_homeassistant
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


def test_client_error_carries_the_json_message_of_the_answer(ctx, write_token):
    write_token()
    problems = 'Room "office" (rooms[0]), field "name": is required. ' * 20

    def refuse(method, url, headers, body, timeout):
        return 400, json.dumps({"message": problems}).encode()

    ctx.transport = refuse

    with pytest.raises(HAError) as raised:
        ctx.ha().call_service("cave_heating", "reload")
    assert raised.value.status == 400
    assert str(raised.value) == f"Home Assistant answered 400: {problems.strip()}"


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


# ---------------------------------------------------------------------------------------- output
def test_what_a_command_says_reaches_a_pipe_at_once(tmp_path, repo_root):
    """Under systemd the output goes to the journal through a pipe, where Python buffers standard
    output. A line said before a power cut or a kill must already be there (found by the server
    rehearsal: the journal of a running update showed none of its progress). Standard error needs
    no test of its own: Python writes it line by line to a pipe anyway, so its flush changes
    nothing."""
    code = (
        "import os, sys; from pathlib import Path; from cave_ops.context import Context\n"
        "ctx = Context.create(Path(sys.argv[1]), environ={})\n"
        "ctx.say('Stopping the stack ...')\n"
        "os._exit(9)  # as a kill or a power cut: nothing is flushed on the way out\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(repo_root / "ops")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 9
    assert "Stopping the stack ..." in done.stdout


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


# ------------------------------------------------------------------------------------ container


def test_one_off_container_command_uses_the_service_mounts_and_returns_the_result(ctx, fake_runner):
    fake_runner.on(["docker", "compose", "run"], out="done\n")

    result = run_in_homeassistant(ctx, ["/cave/tools/bootstrap.py", "--once"])

    assert result.stdout == "done\n"
    (call,) = fake_runner.matching("docker", "compose", "run")
    assert call.args == (
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python3",
        "homeassistant",
        "/cave/tools/bootstrap.py",
        "--once",
    )
    assert call.cwd == ctx.paths.root


def test_one_off_container_command_with_working_directory_and_entrypoint(ctx, fake_runner):
    fake_runner.on(["docker", "compose", "run"])

    run_in_homeassistant(ctx, ["--version"], entrypoint="hass", workdir="/config", capture=False)

    (call,) = fake_runner.matching("docker", "compose", "run")
    assert call.args[5:10] == ("-T", "--workdir", "/config", "--entrypoint", "hass")
    assert not call.capture


def test_compose_with_overlays_names_compose_yaml_first(ctx, fake_runner):
    """Any -f stops Compose from reading compose.yaml by itself, so the context adds it."""
    fake_runner.on(["docker", "compose"])
    ctx.compose_files = ("compose.sim.yaml",)

    ctx.compose("ps")

    assert fake_runner.calls[0].args == (
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.sim.yaml",
        "ps",
    )


def test_compose_with_overlays_keeps_the_local_override_between_them(ctx, fake_runner):
    fake_runner.on(["docker", "compose"])
    (ctx.paths.root / "compose.override.yaml").write_text("services: {}\n")
    ctx.compose_files = ("compose.sim.yaml",)

    ctx.compose("ps")

    args = fake_runner.calls[0].args
    assert args[2:8] == (
        "-f",
        "compose.yaml",
        "-f",
        "compose.override.yaml",
        "-f",
        "compose.sim.yaml",
    )


def test_compose_without_overlays_leaves_the_choice_of_files_to_compose(ctx, fake_runner):
    fake_runner.on(["docker", "compose"])

    ctx.compose("ps")

    assert fake_runner.calls[0].args == ("docker", "compose", "ps")


def test_compose_passes_the_environment_of_the_context_and_of_the_call(ctx, fake_runner):
    fake_runner.on(["docker", "compose"])
    ctx.compose_env = {"CAVE_DATA": "/srv/demo-state", "TZ": "UTC"}

    ctx.compose("up", env={"TZ": "Europe/Berlin"})
    ctx.compose("ps")

    assert fake_runner.calls[0].env == {"CAVE_DATA": "/srv/demo-state", "TZ": "Europe/Berlin"}
    assert fake_runner.calls[1].env == {"CAVE_DATA": "/srv/demo-state", "TZ": "UTC"}


def test_compose_without_environment_passes_none(ctx, fake_runner):
    fake_runner.on(["docker", "compose"])

    ctx.compose("ps")

    assert fake_runner.calls[0].env is None


def test_nothing_runs_without_a_time_limit_unless_asked_to(ctx, fake_runner, write_token):
    fake_runner.on(["git"], out="v1\n")
    fake_runner.on(["docker", "compose", "config", "--services"], out="a\n")
    fake_runner.on(["docker", "compose", "ps"], out=ps(a=("running", "healthy")))
    fake_runner.on(["docker", "compose", "run"])

    ctx.git("describe")
    check_health(ctx)
    run_in_homeassistant(ctx, ["-V"])
    run_in_homeassistant(ctx, ["-V"], timeout=None)

    git_call = fake_runner.matching("git")[0]
    health_calls = fake_runner.matching("docker", "compose", "config") + fake_runner.matching(
        "docker", "compose", "ps"
    )
    run_calls = fake_runner.matching("docker", "compose", "run")
    assert git_call.timeout == 120.0
    assert [call.timeout for call in health_calls] == [60.0, 60.0]
    assert [call.timeout for call in run_calls] == [600.0, None]


def test_a_hung_docker_is_reported_as_not_healthy_instead_of_hanging(ctx, fake_runner):
    fake_runner.on(
        ["docker", "compose", "config"], rc=124, err="docker: timed out after 60 seconds\n"
    )

    health = check_health(ctx)

    assert not health.healthy
    assert "timed out" in health.problems[0]


def test_home_assistant_that_is_still_starting_is_not_healthy(
    ctx, fake_runner, fake_ha, write_token
):
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))
    write_token()
    fake_ha.state = "NOT_RUNNING"

    health = check_health(ctx)

    assert not health.healthy
    assert "NOT_RUNNING" in health.problems[0]
    fake_ha.state = "RUNNING"
    assert check_health(ctx).healthy


@pytest.mark.parametrize("mode", ["recovery_mode", "safe_mode"])
def test_home_assistant_in_recovery_or_safe_mode_is_not_healthy(
    ctx, fake_runner, fake_ha, write_token, mode
):
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))
    write_token()
    setattr(fake_ha, mode, True)

    health = check_health(ctx)

    assert not health.healthy
    assert "recovery or safe mode" in health.problems[0]


def test_a_dropped_connection_is_unreachable_not_a_crash(ctx, write_token):
    write_token()

    def dropped(method, url, headers, body, timeout):
        raise http.client.IncompleteRead(b"par")

    ctx.transport = dropped

    with pytest.raises(HAUnreachable):
        ctx.ha().config()


def test_malformed_answers_are_ha_errors(ctx, fake_ha, write_token):
    write_token()
    answers = {"/api/services": b'{"not": "a list"}', "/api/config": b"[1, 2]"}

    def odd(method, url, headers, body, timeout):
        return 200, answers[url.removeprefix("http://127.0.0.1:8123")]

    ctx.transport = odd
    client = ctx.ha()

    with pytest.raises(HAError):
        client.services()
    with pytest.raises(HAError):
        client.config()


def test_notify_never_raises_whatever_home_assistant_does(ctx, write_token, journal):
    write_token()

    def broken(method, url, headers, body, timeout):
        raise http.client.BadStatusLine("garbage")

    ctx.transport = broken

    result = notify(ctx, "Update", "failed")

    assert not result.delivered
    assert journal == ["Update: failed"]

    ctx.transport = lambda *args: (200, b'{"x": 1}')
    assert not notify(ctx, "Update", "failed").delivered


def test_the_service_hook_lets_a_fake_react_to_a_call(ctx, fake_ha, write_token):
    write_token()
    fake_ha.states["climate.x"] = {"state": "heat", "attributes": {"temperature": 20.0}}

    def confirm(data):
        fake_ha.states["climate.x"]["attributes"]["temperature"] = data["temperature"]

    fake_ha.on_service("climate", "set_temperature", confirm)

    ctx.ha().call_service("climate", "set_temperature", {"temperature": 21.5})

    assert ctx.ha().get_state("climate.x")["attributes"]["temperature"] == 21.5


# ------------------------------------------------------------------------ malformed tokens


@pytest.fixture(params=["two_lines", "directory", "not_utf8", "non_ascii", "inner_space"])
def damaged_token(request, ctx):
    path = ctx.paths.token
    path.parent.mkdir(parents=True, exist_ok=True)
    if request.param == "two_lines":
        path.write_text("abc\ndef\n")
    elif request.param == "directory":
        path.mkdir()
    elif request.param == "not_utf8":
        path.write_bytes(b"\xff\xfe\xfd")
    elif request.param == "non_ascii":
        path.write_text("tökën\n", encoding="utf-8")
    else:
        path.write_text("abc def\n")
    return request.param


def test_a_damaged_token_is_a_clear_error_where_one_is_required(ctx, damaged_token):
    with pytest.raises(CaveError) as raised:
        read_token(ctx.paths)

    assert str(ctx.paths.token) in str(raised.value)
    with pytest.raises(CaveError):
        ctx.ha()


def test_a_damaged_token_does_not_break_notify(ctx, damaged_token, journal):
    result = notify(ctx, "Update", "rolled back")

    assert not result.delivered
    assert str(ctx.paths.token) in (result.error or "")
    assert journal == ["Update: rolled back"]


def test_a_damaged_token_makes_the_health_check_say_so_instead_of_crashing(
    ctx, fake_runner, damaged_token, clock
):
    stack(fake_runner, ["a"], ps(a=("running", "healthy")))

    health = check_health(ctx)
    waited = wait_healthy(ctx, timeout=10, interval=5)

    assert not health.healthy
    assert "Home Assistant API" in health.problems[0]
    assert not waited.healthy
    assert clock.now == 10


def test_a_transport_that_rejects_the_request_is_unreachable_not_a_crash(ctx, write_token):
    write_token()

    def rejects(method, url, headers, body, timeout):
        raise ValueError("Invalid header value")

    ctx.transport = rejects

    with pytest.raises(HAUnreachable):
        ctx.ha().config()


def test_notify_survives_any_exception_from_home_assistant_code(ctx, write_token, journal):
    write_token()

    def broken(method, url, headers, body, timeout):
        raise RuntimeError("something nobody expected")

    ctx.transport = broken

    result = notify(ctx, "Update", "ok")

    assert not result.delivered
    assert "RuntimeError" in (result.error or "")
    assert journal == ["Update: ok"]
