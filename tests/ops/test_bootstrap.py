"""``cave bootstrap``: the command around ``tools/bootstrap.py``, Docker and Home Assistant faked.

What the tool does inside Home Assistant is tested against a real, in-process Home Assistant in
``tests/bootstrap``; here: what the command hands it, how, when it runs it, and what it checks
afterwards.
"""

from __future__ import annotations

import json
import os

import pytest

SERVICES = "homeassistant\nmosquitto\n"
HEALTHY = "\n".join(
    json.dumps({"Service": name, "State": "running", "Health": "healthy"})
    for name in ("homeassistant", "mosquitto")
)
TOOL = ("docker", "compose", "run", "--rm", "--no-deps", "-T")


@pytest.fixture
def initialised(run_cave, ctx):
    assert run_cave("init").code == 0
    return ctx.env


@pytest.fixture
def stack_healthy(fake_runner):
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    fake_runner.on(["docker", "compose", "ps"], out=HEALTHY)


@pytest.fixture
def tool_writes_token(fake_runner, ctx, fake_ha):
    """The tool succeeds and leaves the token the way it does: mode 0600, the directory's owner."""

    def write(call):
        path = ctx.paths.token
        path.write_text(fake_ha.token + "\n", encoding="utf-8")
        path.chmod(0o600)

    fake_runner.on(list(TOOL), side_effect=write)


def tool_settings(fake_runner) -> dict:
    (call,) = fake_runner.matching(*TOOL)
    return json.loads(call.input_text)


def test_bootstrap_needs_init_first(run_cave, fake_runner):
    result = run_cave("bootstrap")

    assert result.code == 1
    assert "./cave init" in result.err
    assert fake_runner.calls == []


def test_bootstrap_runs_the_tool_in_the_home_assistant_image_with_the_settings_on_stdin(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token
):
    result = run_cave("bootstrap")

    assert result.code == 0, result.err
    assert "Waiting until" not in result.out  # healthy at once: nothing to wait for
    (call,) = fake_runner.matching(*TOOL)
    assert call.args[call.args.index("--entrypoint") + 1] == "python3"
    assert call.args[-2:] == ("homeassistant", "/cave/tools/bootstrap.py")
    assert not call.capture  # its progress reaches the terminal
    assert call.timeout is not None
    settings = tool_settings(fake_runner)
    assert settings == {
        "owner_name": initialised["CAVE_HA_OWNER_NAME"],
        "owner_user": initialised["CAVE_HA_OWNER_USER"],
        "owner_password": initialised["CAVE_HA_OWNER_PASSWORD"],
        "time_zone": "Europe/Berlin",
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 1883,
        "mqtt_user": initialised["CAVE_MQTT_USER"],
        "mqtt_password": initialised["CAVE_MQTT_PASSWORD"],
        "timeout": 300.0,
    }
    assert "healthy" in result.out


def test_no_secret_is_in_any_command_line_or_the_output(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token
):
    initialised.update(
        CAVE_FRITZ_HOST="fritz.box", CAVE_FRITZ_USER="cave", CAVE_FRITZ_PASSWORD="fritz-secret"
    )
    secrets = [
        initialised["CAVE_HA_OWNER_PASSWORD"],
        initialised["CAVE_MQTT_PASSWORD"],
        "fritz-secret",
    ]

    result = run_cave("bootstrap")

    assert result.code == 0, result.err
    for call in fake_runner.calls:
        for secret in secrets:
            assert all(secret not in arg for arg in call.args)
            assert all(secret not in value for value in (call.env or {}).values())
    for secret in secrets:
        assert secret not in result.out + result.err


def test_the_fritz_box_is_passed_on_when_env_names_one(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token
):
    initialised.update(
        CAVE_FRITZ_HOST="192.168.178.1", CAVE_FRITZ_USER="cave", CAVE_FRITZ_PASSWORD="pw"
    )

    assert run_cave("bootstrap").code == 0

    settings = tool_settings(fake_runner)
    assert (settings["fritz_host"], settings["fritz_user"], settings["fritz_password"]) == (
        "192.168.178.1",
        "cave",
        "pw",
    )


def test_without_a_fritz_box_nothing_about_one_is_passed_on(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token
):
    assert run_cave("bootstrap").code == 0

    assert not any(key.startswith("fritz") for key in tool_settings(fake_runner))


@pytest.mark.parametrize("missing", ["CAVE_FRITZ_USER", "CAVE_FRITZ_PASSWORD"])
def test_a_fritz_box_without_its_login_stops_before_anything_runs(
    initialised, run_cave, fake_runner, missing
):
    initialised.update(CAVE_FRITZ_HOST="fritz.box", CAVE_FRITZ_USER="u", CAVE_FRITZ_PASSWORD="p")
    initialised[missing] = ""

    result = run_cave("bootstrap")

    assert result.code == 1
    assert missing in result.err
    assert fake_runner.calls == []


def test_a_missing_owner_password_stops_before_anything_runs(initialised, run_cave, fake_runner):
    initialised["CAVE_HA_OWNER_PASSWORD"] = ""

    result = run_cave("bootstrap")

    assert result.code == 1
    assert "CAVE_HA_OWNER_PASSWORD" in result.err
    assert fake_runner.calls == []


def test_the_time_zone_comes_from_env(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token
):
    initialised["TZ"] = "Europe/Vienna"

    assert run_cave("bootstrap").code == 0

    assert tool_settings(fake_runner)["time_zone"] == "Europe/Vienna"


def test_it_waits_for_a_starting_stack_without_asking_the_api(
    initialised, run_cave, fake_runner, fake_ha, clock, write_token, ctx
):
    # A token that Home Assistant no longer accepts must not keep the bootstrap from repairing it.
    write_token("revoked-token")
    starting = HEALTHY.replace('"Health": "healthy"', '"Health": "starting"', 1)
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    fake_runner.on(
        ["docker", "compose", "ps"],
        sequence=[(0, starting, ""), (0, starting, ""), (0, HEALTHY, "")],
    )
    requests_before_the_tool = []

    def tool(call):
        requests_before_the_tool.extend(fake_ha.requests)
        ctx.paths.token.write_text(fake_ha.token + "\n")

    fake_runner.on(list(TOOL), side_effect=tool)

    result = run_cave("bootstrap")

    assert result.code == 0, result.err
    assert clock.sleeps  # it waited
    assert "Waiting until the stack is healthy (now not healthy: homeassistant" in result.out
    assert requests_before_the_tool == []
    assert ("GET", "/api/config", None) in fake_ha.requests  # asked afterwards, with the new token


def test_an_unhealthy_stack_stops_it_and_says_how_to_start_it(
    initialised, run_cave, fake_runner, clock
):
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    fake_runner.on(["docker", "compose", "ps"], out="")

    result = run_cave("bootstrap", "--timeout", "20")

    assert result.code == 1
    assert "./cave up" in result.err
    assert "homeassistant: no container" in result.err
    assert not fake_runner.ran(*TOOL)
    assert clock.now >= 20


def test_a_failing_tool_fails_the_command_and_says_a_rerun_continues(
    initialised, run_cave, fake_runner, stack_healthy
):
    fake_runner.on(list(TOOL), rc=1)

    result = run_cave("bootstrap")

    assert result.code == 1
    assert "exit status 1" in result.err
    assert "again continues where it stopped" in result.err


def test_a_token_that_others_can_read_is_reported(
    initialised, run_cave, fake_runner, stack_healthy, ctx, fake_ha
):
    def write(call):
        ctx.paths.token.write_text(fake_ha.token + "\n")
        ctx.paths.token.chmod(0o644)

    fake_runner.on(list(TOOL), side_effect=write)

    result = run_cave("bootstrap")

    assert result.code == 1
    assert "its mode is 0644, not 0600" in result.err


def test_a_token_owned_by_someone_else_is_reported(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token, monkeypatch
):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    result = run_cave("bootstrap")

    assert result.code == 1
    assert f"it belongs to user ID {os.getuid()}" in result.err
    assert "sudo chown" in result.err


def test_a_missing_token_after_the_tool_is_reported(
    initialised, run_cave, fake_runner, stack_healthy
):
    fake_runner.on(list(TOOL))

    result = run_cave("bootstrap")

    assert result.code == 1
    assert "did not leave the script token" in result.err


def test_a_token_home_assistant_does_not_accept_is_reported(
    initialised, run_cave, fake_runner, stack_healthy, ctx
):
    def write(call):
        ctx.paths.token.write_text("not-the-token\n")
        ctx.paths.token.chmod(0o600)

    fake_runner.on(list(TOOL), side_effect=write)

    result = run_cave("bootstrap")

    assert result.code == 1
    assert "401" in result.err


def test_a_lost_token_directory_is_recreated_private_before_the_tool_runs(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token, ctx
):
    ctx.paths.token.parent.rmdir()

    assert run_cave("bootstrap").code == 0

    assert ctx.paths.token.parent.stat().st_mode & 0o777 == 0o700


def test_a_second_run_runs_the_tool_again_and_leaves_the_state_as_it_was(
    initialised, run_cave, fake_runner, stack_healthy, tool_writes_token, ctx, snapshot
):
    # The tool decides inside Home Assistant that there is nothing to do (tests/bootstrap and
    # the e2e job prove that); the command itself must not change anything on the host either.
    assert run_cave("bootstrap").code == 0
    fake_runner.on(list(TOOL))  # this time the tool finds everything in place
    before = snapshot(ctx.paths.data)

    result = run_cave("bootstrap")

    assert result.code == 0, result.err
    assert snapshot(ctx.paths.data) == before
    assert len(fake_runner.matching(*TOOL)) == 2
