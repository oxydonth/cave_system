"""First-run setup against the real stack: ``./cave bootstrap`` on a fresh Home Assistant.

The tests run in the order written. The first group looks at what the first run left behind; the
second proves that further runs change nothing, also after the partial states that the bootstrap
must repair: a deleted token file, a token revoked in the UI, a changed owner password, and a
Home Assistant that is restarting.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cave_ops.context import Context
from cave_ops.ha import HAError
from cave_ops.heating import validate_and_store
from tools.bootstrap import DEFAULT_WEEK, ONBOARDING_INTEGRATIONS, TOKEN_NAME, BootstrapError

HEATING = Path(__file__).with_name("first_run_heating.yaml")
ROOM_SCHEDULES = {
    "schedule.living_room_heating": "Wohnzimmer heating schedule",
    "schedule.bad_heizplan": "Bad heating schedule",
}


@pytest.fixture(scope="module", autouse=True)
def home_stack(e2e_stack: str) -> None:
    if e2e_stack != "home":
        pytest.skip("first-run tests need the home's fresh stack: CAVE_E2E=home")


@pytest.fixture(scope="module")
def first_run(stack: Any) -> subprocess.CompletedProcess[str]:
    """Apply the test home and run the bootstrap once, on the fresh stack the job started.

    The test home goes through the shared function that validates first (concept section 4.5),
    with the validator of cave_heating in a one-off Home Assistant container.
    """
    assert not stack.token_file.exists(), "the e2e job expects a stack that was never set up"
    applied = validate_and_store(Context.create(stack.root), HEATING)
    assert applied.ok, applied.messages
    return stack.cave("bootstrap")


# ------------------------------------------------------------------------------ the first run
def test_the_first_run_succeeds_and_says_what_it_did(first_run: Any, stack: Any) -> None:
    assert first_run.returncode == 0
    for line in (
        f"created the owner account {stack.env['CAVE_HA_OWNER_USER']!r}",
        "set the time zone",
        "completed the onboarding step integration",
        f"created the script token {TOKEN_NAME}",
        "set up MQTT with the broker 127.0.0.1:1883",
        "created the schedule schedule.living_room_heating",
        *(f"removed {domain} (" for domain in ONBOARDING_INTEGRATIONS),
        "Stack healthy",
    ):
        assert line in first_run.stdout
    for secret in stack.secrets:
        assert secret not in first_run.stdout + first_run.stderr


def test_nothing_that_reaches_the_internet_is_left_from_onboarding(
    first_run: Any, stack: Any
) -> None:
    entries = stack.api().request("GET", "/api/config/config_entries/entry")

    assert sorted({entry["domain"] for entry in entries} & set(ONBOARDING_INTEGRATIONS)) == []
    assert "met" not in {entry["domain"] for entry in entries}  # declines without a location


def test_the_token_file_is_the_runners_alone(first_run: Any, stack: Any) -> None:
    assert_token_file_is_the_runners_alone(stack)


def assert_token_file_is_the_runners_alone(stack: Any) -> None:
    """Owned by the user who runs ./cave (and the tests), mode 0600, unreadable for others."""
    info = stack.token_file.stat()
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert stat.S_IMODE(stack.token_file.parent.stat().st_mode) == 0o700
    assert stack.token_file.read_text().count("\n") == 1
    # Another user cannot read it.
    other = subprocess.run(
        ["sudo", "-n", "-u", "nobody", "cat", str(stack.token_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert other.returncode != 0
    assert stack.token() not in other.stdout


def test_home_assistant_accepts_the_token_and_runs_normally(first_run: Any, stack: Any) -> None:
    config = stack.api().config()

    assert config["state"] == "RUNNING"
    assert not config["recovery_mode"]
    assert not config["safe_mode"]
    for component in ("mqtt", "schedule", "cave_heating", "onboarding"):
        assert component in config["components"]


def test_onboarding_is_complete(first_run: Any, stack: Any) -> None:
    steps = stack.api().request("GET", "/api/onboarding")

    assert {step["step"]: step["done"] for step in steps} == {
        "user": True,
        "core_config": True,
        "analytics": True,
        "integration": True,
    }


def test_home_assistants_own_settings(first_run: Any, stack: Any) -> None:
    config = stack.api().config()

    assert config["time_zone"] == stack.env["TZ"]
    assert config["country"] == "DE"
    assert config["currency"] == "EUR"
    assert config["unit_system"]["temperature"] == "°C"


def test_there_is_one_owner_with_one_token(first_run: Any, stack: Any) -> None:
    users, tokens, me = stack.ws(
        ("config/auth/list", {}), ("auth/refresh_tokens", {}), ("auth/current_user", {})
    )

    people = [user for user in users if not user["system_generated"]]
    assert [(u["username"], u["name"], u["is_owner"]) for u in people] == [
        (stack.env["CAVE_HA_OWNER_USER"], stack.env["CAVE_HA_OWNER_NAME"], True)
    ]
    assert me["id"] == people[0]["id"]
    # The script token and nothing else: the run's own login session was revoked.
    assert [(t["client_name"], t["type"], t["is_current"]) for t in tokens] == [
        (TOKEN_NAME, "long_lived_access_token", True)
    ]


def test_mqtt_is_connected_with_the_login_from_env(
    first_run: Any, stack: Any, wait_until: Any
) -> None:
    api = stack.api()
    (entry,) = api.request("GET", "/api/config/config_entries/entry?domain=mqtt")
    assert entry["state"] == "loaded"
    # Home Assistant publishes through the broker; a client with the .env login receives it.
    env = stack.env
    listener = subprocess.Popen(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "mosquitto",
            "mosquitto_sub",
            "-h",
            "127.0.0.1",
            "-u",
            env["CAVE_MQTT_USER"],
            "-P",
            env["CAVE_MQTT_PASSWORD"],
            "-t",
            "cave/e2e/bootstrap",
            "-C",
            "1",
            "-W",
            "60",
        ],
        cwd=stack.root,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:

        def received() -> bool:
            api.call_service("mqtt", "publish", {"topic": "cave/e2e/bootstrap", "payload": "hi"})
            return listener.poll() is not None

        wait_until(received, "Home Assistant's MQTT message to reach a subscriber", 60)
        assert listener.stdout is not None
        assert listener.stdout.read().strip() == "hi"
        assert listener.returncode == 0
    finally:
        listener.kill()


def test_without_a_fritz_box_neither_fritz_integration_is_set_up(
    first_run: Any, stack: Any
) -> None:
    api = stack.api()

    assert api.request("GET", "/api/config/config_entries/entry?domain=fritzbox") == []
    assert api.request("GET", "/api/config/config_entries/entry?domain=fritz") == []
    assert "FRITZ!SmartHome and FRITZ!Box Tools are not set up" in first_run.stdout


def test_each_room_that_names_a_schedule_has_it_with_the_default_week(
    first_run: Any, stack: Any
) -> None:
    api = stack.api()
    (items,) = stack.ws(("schedule/list", {}))

    assert len(items) == len(ROOM_SCHEDULES)  # none for the room without a schedule
    for entity_id, name in ROOM_SCHEDULES.items():
        state = api.get_state(entity_id)
        assert state is not None, entity_id
        assert state["attributes"]["friendly_name"] == name
        answer = api.request(
            "POST",
            "/api/services/schedule/get_schedule?return_response",
            {"entity_id": entity_id},
        )
        week = answer["service_response"][entity_id]
        assert week == {
            day: [{"from": r["from"], "to": r["to"]} for r in ranges]
            for day, ranges in DEFAULT_WEEK.items()
        }


# ------------------------------------------------------------------------ and again: no changes
def test_a_second_run_changes_nothing(first_run: Any, stack: Any) -> None:
    before = stack.state()

    second = stack.cave("bootstrap")

    assert second.returncode == 0
    assert "bootstrap: done, nothing to change" in second.stdout
    assert "changed:" not in second.stdout
    assert stack.state() == before


def test_a_deleted_token_file_is_replaced_and_the_lost_token_deleted(
    first_run: Any, stack: Any
) -> None:
    (old,) = stack.ws(("auth/refresh_tokens", {}))[0]
    stack.token_file.unlink()

    run = stack.cave("bootstrap")

    assert run.returncode == 0
    assert f"deleted the old {TOKEN_NAME} token" in run.stdout
    (tokens,) = stack.ws(("auth/refresh_tokens", {}))
    assert [(t["client_name"], t["is_current"]) for t in tokens] == [(TOKEN_NAME, True)]
    assert tokens[0]["id"] != old["id"]
    assert_token_file_is_the_runners_alone(stack)


def test_a_token_revoked_in_the_ui_is_replaced(first_run: Any, stack: Any) -> None:
    (tokens,) = stack.ws(("auth/refresh_tokens", {}))
    # As the UI does it. Home Assistant closes the connection of the token it deletes, before
    # it answers.
    with contextlib.suppress(BootstrapError):
        stack.ws(("auth/delete_refresh_token", {"refresh_token_id": tokens[0]["id"]}))
    with pytest.raises(HAError) as rejected:
        stack.api().config()
    assert rejected.value.status == 401

    run = stack.cave("bootstrap")

    assert run.returncode == 0
    assert "no longer accepts the stored script token" in run.stdout
    assert stack.api().config()["state"] == "RUNNING"
    (tokens,) = stack.ws(("auth/refresh_tokens", {}))
    assert [t["client_name"] for t in tokens] == [TOKEN_NAME]


def test_a_changed_owner_password_in_env_is_harmless_while_the_token_works(
    first_run: Any, stack: Any
) -> None:
    env_file = stack.root / ".env"
    original = env_file.read_text(encoding="utf-8")
    password = stack.env["CAVE_HA_OWNER_PASSWORD"]
    before = stack.state()
    try:
        env_file.write_text(
            original.replace(
                f"CAVE_HA_OWNER_PASSWORD={password}", "CAVE_HA_OWNER_PASSWORD=changed-in-env"
            ),
            encoding="utf-8",
        )
        assert stack.env["CAVE_HA_OWNER_PASSWORD"] == "changed-in-env"

        run = stack.cave("bootstrap")
    finally:
        env_file.write_text(original, encoding="utf-8")

    assert run.returncode == 0
    assert "changed-in-env" not in run.stdout
    assert stack.state() == before


def test_a_restarting_home_assistant_is_waited_for(first_run: Any, stack: Any) -> None:
    before = stack.state()
    subprocess.run(
        ["docker", "compose", "restart", "homeassistant"],
        cwd=stack.root,
        check=True,
    )

    run = stack.cave("bootstrap")

    assert run.returncode == 0
    assert "Waiting until the stack is healthy" in run.stdout
    assert "bootstrap: done, nothing to change" in run.stdout
    assert stack.state() == before
