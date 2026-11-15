"""``up``, ``down``, ``restart``, ``ps``, ``logs``, ``check`` and ``version``, with Docker faked."""

from __future__ import annotations

import json

import pytest

SERVICES = "homeassistant\nmosquitto\n"


def ps_json(**states: tuple[str, str]) -> str:
    """Output of ``docker compose ps --format json``, from {service: (state, health)}."""
    return "\n".join(
        json.dumps({"Service": name, "State": state, "Health": health})
        for name, (state, health) in states.items()
    )


HEALTHY = ps_json(homeassistant=("running", "healthy"), mosquitto=("running", "healthy"))


@pytest.fixture
def initialised(run_cave):
    assert run_cave("init").code == 0


@pytest.fixture
def stack_healthy(fake_runner):
    fake_runner.on(["docker", "compose", "up"])
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    fake_runner.on(["docker", "compose", "ps"], out=HEALTHY)


@pytest.mark.parametrize("command", ["up", "down", "restart", "ps", "logs", "check"])
def test_compose_commands_need_init_first(run_cave, fake_runner, command):
    result = run_cave(command)

    assert result.code == 1
    assert "./cave init" in result.err
    assert fake_runner.calls == []


def test_up_recreates_the_containers_and_waits_until_healthy(
    initialised, run_cave, fake_runner, stack_healthy, checkout
):
    result = run_cave("up")

    assert result.code == 0, result.err
    (up,) = fake_runner.matching("docker", "compose", "up")
    assert up.args == ("docker", "compose", "up", "--detach", "--force-recreate")
    assert up.cwd == checkout
    assert not up.capture  # progress is shown to the user
    assert "healthy" in result.out


def test_up_waits_through_a_start_and_reports_when_it_became_healthy(
    initialised, run_cave, fake_runner, clock
):
    fake_runner.on(["docker", "compose", "up"])
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    starting = ps_json(homeassistant=("running", "starting"), mosquitto=("running", "healthy"))
    fake_runner.on(
        ["docker", "compose", "ps"],
        sequence=[(0, starting, ""), (0, starting, ""), (0, HEALTHY, "")],
    )

    result = run_cave("up")

    assert result.code == 0
    assert len(clock.sleeps) == 2


def test_up_fails_and_says_what_is_wrong_when_a_service_never_becomes_healthy(
    initialised, run_cave, fake_runner, clock
):
    fake_runner.on(["docker", "compose", "up"])
    fake_runner.on(["docker", "compose", "config", "--services"], out=SERVICES)
    unhealthy = ps_json(homeassistant=("running", "unhealthy"), mosquitto=("running", "healthy"))
    fake_runner.on(["docker", "compose", "ps"], out=unhealthy)

    result = run_cave("up", "--timeout", "30")

    assert result.code == 1
    assert "homeassistant: health check unhealthy" in result.out
    assert clock.now >= 30


def test_up_stops_at_once_when_compose_fails(initialised, run_cave, fake_runner):
    fake_runner.on(["docker", "compose", "up"], rc=1)

    result = run_cave("up")

    assert result.code == 1
    assert not fake_runner.ran("docker", "compose", "ps")


def test_up_without_waiting(initialised, run_cave, fake_runner):
    fake_runner.on(["docker", "compose", "up"])

    result = run_cave("up", "--no-wait")

    assert result.code == 0
    assert not fake_runner.ran("docker", "compose", "ps")


def test_down(initialised, run_cave, fake_runner):
    fake_runner.on(["docker", "compose", "down"])

    assert run_cave("down").code == 0
    assert fake_runner.ran("docker", "compose", "down")


def test_restart_names_the_services_and_waits(initialised, run_cave, fake_runner, stack_healthy):
    fake_runner.on(["docker", "compose", "restart"])

    result = run_cave("restart", "homeassistant")

    assert result.code == 0
    assert fake_runner.matching("docker", "compose", "restart")[0].args[-1] == "homeassistant"
    assert "healthy" in result.out


def test_ps_and_its_exit_status(initialised, run_cave, fake_runner):
    fake_runner.on(["docker", "compose", "ps"], rc=3)

    assert run_cave("ps").code == 3


def test_logs_options_are_passed_to_compose(initialised, run_cave, fake_runner):
    fake_runner.on(["docker", "compose", "logs"])

    run_cave("logs", "-f", "--tail", "50", "--since", "10m", "homeassistant")

    (call,) = fake_runner.matching("docker", "compose", "logs")
    assert call.args == (
        "docker",
        "compose",
        "logs",
        "--tail",
        "50",
        "--follow",
        "--since",
        "10m",
        "homeassistant",
    )


def test_check_validates_compose_and_the_home_assistant_configuration_in_the_image(
    initialised, run_cave, fake_runner
):
    fake_runner.on(["docker", "compose", "config"])
    fake_runner.on(["docker", "compose", "run"], out="Testing configuration at /config\n")

    result = run_cave("check")

    assert result.code == 0, result.err
    (run,) = fake_runner.matching("docker", "compose", "run")
    assert "--no-deps" in run.args
    assert run.args[-9:] == (
        "--entrypoint",
        "python3",
        "homeassistant",
        "-m",
        "homeassistant",
        "--script",
        "check_config",
        "--config",
        "/config",
    )
    assert fake_runner.matching("docker", "compose", "config")[0].args[-1] == "--quiet"


def test_check_fails_when_the_home_assistant_configuration_is_invalid(
    initialised, run_cave, fake_runner
):
    fake_runner.on(["docker", "compose", "config"])
    fake_runner.on(
        ["docker", "compose", "run"], rc=1, out="Invalid config for 'recorder': bad value\n"
    )

    result = run_cave("check")

    assert result.code == 1
    assert "Invalid config for 'recorder'" in result.err


def test_check_fails_when_the_compose_file_is_invalid_but_still_checks_the_rest(
    initialised, run_cave, fake_runner
):
    fake_runner.on(["docker", "compose", "config"], rc=1, err="services.x must be a mapping\n")
    fake_runner.on(["docker", "compose", "run"])

    result = run_cave("check")

    assert result.code == 1
    assert "services.x must be a mapping" in result.err
    assert fake_runner.ran("docker", "compose", "run")


def test_version_prints_the_release_and_images(run_cave, fake_runner):
    fake_runner.on(["git", "describe"], out="v2026.11.15.1\n")

    result = run_cave("version")

    assert result.code == 0
    assert "release: v2026.11.15.1" in result.out
    assert "image:   ghcr.io/home-assistant/home-assistant:2026.10.4" in result.out
    assert "image:   eclipse-mosquitto:2.0.22" in result.out
    assert "image:   ghcr.io/koenkk/zigbee2mqtt:2.14.3" in result.out


def test_version_outside_a_checkout_still_prints_images(run_cave, fake_runner):
    fake_runner.on(["git", "describe"], rc=128, err="fatal: not a repository\n")

    result = run_cave("version")

    assert result.code == 0
    assert "release: unknown" in result.out
