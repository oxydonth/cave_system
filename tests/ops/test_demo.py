"""``cave demo``: its own project, paths and secrets; the applied copy; the bootstrap settings.

Docker and Home Assistant are faked. The demo running for real is the ``e2e`` job's business.
"""

from __future__ import annotations

import json
import shutil
import stat

import pytest

from cave_ops.envfile import load_env

FILES = ("-f", "compose.yaml", "-f", "compose.sim.yaml")
COMPOSE = ("docker", "compose", *FILES)
VALIDATE = (*COMPOSE, "run", "--rm", "--no-deps", "-T", "--workdir")
ONE_OFF = (*COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python3", "homeassistant")
TOOL = (*ONE_OFF, "/cave/tools/bootstrap.py")
GENERATE = (
    *ONE_OFF,
    "/cave/tools/render_dashboard.py",
    "/config/cave/.applied/heating.yaml",
    "/config/dashboards/heating.yaml",
)
SERVICES = "homeassistant\nmosquitto\nsim\n"
HEALTHY = "\n".join(
    json.dumps({"Service": name, "State": "running", "Health": "healthy"})
    for name in ("homeassistant", "mosquitto", "sim")
)


@pytest.fixture
def checkout_with_demo(checkout, repo_root):
    target = checkout / "sim" / "demo" / "heating.yaml"
    target.parent.mkdir(parents=True)
    shutil.copyfile(repo_root / "sim" / "demo" / "heating.yaml", target)
    return checkout


@pytest.fixture
def demo_data(checkout_with_demo, tmp_path, monkeypatch):
    """The default state directory, under a scratch $XDG_STATE_HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return (tmp_path / "state" / "cave" / "demo").resolve()


@pytest.fixture
def docker_works(fake_runner, fake_ha, demo_data):
    """Nothing of the home runs; the demo's stack builds, validates, starts and is healthy."""
    fake_runner.on(["docker", "ps"], out="")
    fake_runner.on([*COMPOSE, "build"])
    fake_runner.on(list(VALIDATE), out="cave/.validating.yaml is valid: 3 rooms\n")
    fake_runner.on(list(GENERATE))
    fake_runner.on([*COMPOSE, "up"])
    fake_runner.on([*COMPOSE, "config", "--services"], out=SERVICES)
    fake_runner.on([*COMPOSE, "ps"], out=HEALTHY)

    def tool_writes_token(call):
        token = demo_data / "homeassistant" / ".cave" / "token"
        token.write_text(fake_ha.token + "\n")
        token.chmod(0o600)

    fake_runner.on(list(TOOL), side_effect=tool_writes_token)


def test_the_demo_runs_as_a_project_of_its_own_with_its_own_state(
    run_cave, fake_runner, docker_works, demo_data, checkout_with_demo
):
    result = run_cave("demo")

    assert result.code == 0, result.err
    compose_calls = [call for call in fake_runner.calls if call.args[:2] == ("docker", "compose")]
    assert compose_calls
    for call in compose_calls:
        assert call.args[:6] == COMPOSE
        assert call.env["COMPOSE_PROJECT_NAME"] == "cave-demo"
        assert call.env["CAVE_DATA"] == str(demo_data)
        assert call.env["COMPOSE_PROFILES"] == ""
    # The home's checkout is untouched: no .env, no home state.
    assert not (checkout_with_demo / ".env").exists()
    assert not (checkout_with_demo / "data").exists()
    assert (demo_data / "demo.env").is_file()
    assert "The demo is running" in result.out


def test_secrets_written_into_demo_env_beforehand_are_kept_and_the_rest_added(
    run_cave, docker_works, demo_data
):
    # As the e2e job does, so that it can mask them before the demo starts.
    demo_data.mkdir(parents=True)
    given = {
        "CAVE_MQTT_PASSWORD": "given-mqtt-secret-for-tests",
        "CAVE_HA_OWNER_PASSWORD": "given-owner-secret-for-tests",
        "CAVE_FRITZ_PASSWORD": "given-fritz-secret-for-tests",
    }
    (demo_data / "demo.env").write_text("".join(f"{k}={v}\n" for k, v in given.items()))

    assert run_cave("demo").code == 0

    env = load_env(demo_data / "demo.env")
    assert {name: env[name] for name in given} == given
    assert env["CAVE_DATA"] == str(demo_data)
    assert env["CAVE_FRITZ_HOST"] == "http://127.0.0.1:8480"
    assert stat.S_IMODE((demo_data / "demo.env").stat().st_mode) == 0o600


def test_the_demo_keeps_its_secrets_in_its_own_file(run_cave, docker_works, demo_data):
    assert run_cave("demo").code == 0
    env_file = demo_data / "demo.env"
    first = load_env(env_file)

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert first["CAVE_DATA"] == str(demo_data)
    assert first["CAVE_FRITZ_HOST"] == "http://127.0.0.1:8480"
    assert first["CAVE_FRITZ_USER"] == "cave"
    assert len(first["CAVE_FRITZ_PASSWORD"]) >= 20
    assert len(first["CAVE_HA_OWNER_PASSWORD"]) >= 20
    # A second start keeps them, and the broker's password file matches them.
    assert run_cave("demo").code == 0
    assert load_env(env_file) == first
    passwd = (demo_data / "mosquitto" / "passwd").read_text()
    assert passwd.startswith(first["CAVE_MQTT_USER"] + ":")


def test_the_demo_home_is_validated_and_stored_before_the_stack_starts(
    run_cave, fake_runner, docker_works, demo_data, checkout_with_demo
):
    assert run_cave("demo").code == 0

    demo_heating = (checkout_with_demo / "sim" / "demo" / "heating.yaml").read_bytes()
    applied = demo_data / "homeassistant" / "cave" / ".applied" / "heating.yaml"
    assert applied.read_bytes() == demo_heating
    assert (demo_data / "homeassistant" / "cave" / "heating.yaml").read_bytes() == demo_heating
    order = [call.args[6] for call in fake_runner.calls if call.args[:6] == COMPOSE]
    assert order.index("run") < order.index("up")  # validated before Home Assistant starts
    (validation,) = fake_runner.matching(*VALIDATE)
    assert "custom_components.cave_heating.validate" in validation.args


def test_the_dashboard_is_generated_for_the_demo_home_in_the_demos_context(
    run_cave, fake_runner, docker_works, demo_data
):
    result = run_cave("demo")

    assert result.code == 0, result.err
    (generate,) = fake_runner.matching(*GENERATE)
    assert generate.env["COMPOSE_PROJECT_NAME"] == "cave-demo"
    assert generate.env["CAVE_DATA"] == str(demo_data)  # the demo's /config, not the home's
    order = [call.args for call in fake_runner.calls]
    validate = next(i for i, args in enumerate(order) if args[: len(VALIDATE)] == VALIDATE)
    up = order.index(next(a for a in order if a[:7] == (*COMPOSE, "up")))
    assert validate < order.index(GENERATE) < up  # from the stored copy, before the start
    # Nothing tells the visitor to run ./cave apply, which works on the home only.
    assert "cave apply" not in result.out + result.err


def test_a_dashboard_that_cannot_be_generated_stops_the_demo(run_cave, fake_runner, docker_works):
    fake_runner.on(list(GENERATE), rc=1, err="render_dashboard.py: cannot write the file\n")

    result = run_cave("demo")

    assert result.code == 1
    assert "cannot write the file" in result.err
    assert "generating the demo's dashboard failed" in result.err
    assert not fake_runner.ran(*COMPOSE, "up")


def test_an_invalid_demo_home_stops_before_anything_starts(
    run_cave, fake_runner, docker_works, demo_data
):
    fake_runner.on(list(VALIDATE), rc=1, err='Room "bathroom" (rooms[2]), field "x": bad.\n')

    result = run_cave("demo")

    assert result.code == 1
    assert 'Room "bathroom"' in result.err
    assert not fake_runner.ran(*COMPOSE, "up")
    assert not (demo_data / "homeassistant" / "cave" / ".applied" / "heating.yaml").exists()


def test_the_bootstrap_points_at_the_simulators(run_cave, fake_runner, docker_works, demo_data):
    assert run_cave("demo").code == 0

    (call,) = fake_runner.matching(*TOOL)
    settings = json.loads(call.input_text)
    env = load_env(demo_data / "demo.env")
    assert settings["fritz_host"] == "http://127.0.0.1:8480"
    assert settings["fritz_password"] == env["CAVE_FRITZ_PASSWORD"]
    assert settings["fritz_tools"] is False
    assert settings["switches"] == [
        {"entity_id": "input_boolean.demo_someone_home", "name": "Demo: someone is home"}
    ]
    assert settings["owner_password"] == env["CAVE_HA_OWNER_PASSWORD"]
    # The secrets travel on standard input only.
    for secret in (env["CAVE_HA_OWNER_PASSWORD"], env["CAVE_FRITZ_PASSWORD"]):
        assert all(secret not in arg for c in fake_runner.calls for arg in c.args)


def test_the_demo_refuses_while_the_homes_stack_runs(run_cave, fake_runner, demo_data):
    fake_runner.on(["docker", "ps"], out="3f2a9c1d\n")

    result = run_cave("demo")

    assert result.code == 1
    assert "./cave down" in result.err
    (call,) = fake_runner.calls
    assert "label=com.docker.compose.project=cave" in call.args
    assert not demo_data.exists()


@pytest.mark.parametrize(
    "where",
    ["the home's state", "inside the home's state", "the checkout holding the home's state"],
)
def test_the_demo_refuses_the_homes_state_directory(
    run_cave, fake_runner, ctx, checkout_with_demo, snapshot, where
):
    home = ctx.paths.data
    (home / "homeassistant" / "cave").mkdir(parents=True)
    (home / "homeassistant" / "cave" / "heating.yaml").write_text("the home's own description\n")
    target = {
        "the home's state": home,
        "inside the home's state": home / "demo",
        "the checkout holding the home's state": checkout_with_demo,
    }[where]
    before = snapshot(checkout_with_demo)

    result = run_cave("demo", "--data", str(target))

    assert result.code == 1
    assert "home's state directory" in result.err
    assert fake_runner.calls == []
    assert snapshot(checkout_with_demo) == before  # nothing was written anywhere


def test_the_demo_refuses_a_directory_that_is_not_a_demos(
    run_cave, fake_runner, tmp_path, snapshot
):
    other = tmp_path / "photos"
    other.mkdir()
    (other / "holiday.jpg").write_bytes(b"not really a photo")
    before = snapshot(other)

    result = run_cave("demo", "--data", str(other))

    assert result.code == 1
    assert "not a demo's state directory" in result.err
    assert fake_runner.calls == []
    assert snapshot(other) == before


def test_a_demo_whose_secrets_are_lost_is_not_started_with_new_ones(
    run_cave, fake_runner, docker_works, demo_data
):
    assert run_cave("demo").code == 0
    (demo_data / "demo.env").unlink()
    calls = len(fake_runner.calls)

    result = run_cave("demo")

    assert result.code == 1
    assert "secrets were lost" in result.err
    assert f"sudo rm -rf {demo_data}" in result.err
    assert len(fake_runner.calls) == calls
    assert not (demo_data / "demo.env").exists()


def test_demo_down_stops_the_demos_project(run_cave, fake_runner, docker_works, demo_data):
    assert run_cave("demo").code == 0
    fake_runner.on([*COMPOSE, "down"])

    assert run_cave("demo", "down").code == 0

    (call,) = fake_runner.matching(*COMPOSE, "down")
    assert call.env["COMPOSE_PROJECT_NAME"] == "cave-demo"


def test_demo_down_stops_the_demos_project_even_without_its_demo_env(
    run_cave, fake_runner, demo_data
):
    fake_runner.on([*COMPOSE, "down"])

    result = run_cave("demo", "down")

    assert result.code == 0
    assert "stopping project cave-demo anyway" in result.out
    (call,) = fake_runner.matching(*COMPOSE, "down")
    assert call.env["COMPOSE_PROJECT_NAME"] == "cave-demo"
    assert call.env["CAVE_FRITZ_PASSWORD"]  # compose.sim.yaml requires one, even to stop
    assert not demo_data.exists()  # nothing written


def test_another_state_directory_can_be_chosen(run_cave, fake_runner, docker_works, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    fake_runner.on(
        list(TOOL),
        side_effect=lambda call: _token(elsewhere),
    )

    assert run_cave("demo", "--data", str(elsewhere)).code == 0

    assert load_env(elsewhere / "demo.env")["CAVE_DATA"] == str(elsewhere.resolve())


def _token(data):
    token = data / "homeassistant" / ".cave" / "token"
    token.write_text("fake-token-for-tests\n")
    token.chmod(0o600)
