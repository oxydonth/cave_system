"""``tools/bootstrap.py`` against a real, in-process Home Assistant 2026.10.4.

The acceptance criterion is idempotence: a second run changes nothing and does not fail, and a
run that finds any partial state (onboarding half done, the token file deleted, the token revoked,
the owner's password changed) finishes the job without duplicating anything.
"""

from __future__ import annotations

import io
import json
import os
import stat
from datetime import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tools.bootstrap import (
    DEFAULT_WEEK,
    TOKEN_NAME,
    Api,
    BootstrapError,
    Output,
    Reply,
    Settings,
    WebSocket,
    main,
    write_token_file,
)

# The values of the `home` fixture (tests/bootstrap/conftest.py), which test modules cannot import.
OWNER_PASSWORD = "owner-pass-for-tests"
MQTT_PASSWORD = "mqtt-pass-for-tests"
FRITZ_PASSWORD = "fritz-pass-for-tests"
SECRETS = (OWNER_PASSWORD, MQTT_PASSWORD, FRITZ_PASSWORD)
Home = Any  # tests/bootstrap/conftest.py: Home


# ------------------------------------------------------------------------------------- helpers
async def people(hass: HomeAssistant) -> list[Any]:
    return [user for user in await hass.auth.async_get_users() if not user.system_generated]


async def owner(hass: HomeAssistant) -> Any:
    (user,) = await people(hass)
    return user


async def script_tokens(hass: HomeAssistant) -> list[Any]:
    return [
        token
        for user in await people(hass)
        for token in user.refresh_tokens.values()
        if token.client_name == TOKEN_NAME
    ]


async def snapshot(home: Home) -> dict[str, Any]:
    """Everything the bootstrap could change, in a form that compares equal when nothing did."""
    hass = home.hass
    registry = er.async_get(hass)
    token_file = home.token_file
    info = token_file.stat()
    schedules = sorted(e.entity_id for e in registry.entities.values() if e.domain == "schedule")
    timetables = {}
    for entity_id in schedules:
        response = await hass.services.async_call(
            "schedule",
            "get_schedule",
            target={"entity_id": entity_id},
            blocking=True,
            return_response=True,
        )
        timetables[entity_id] = (hass.states.get(entity_id).name, response)
    return {
        "users": sorted((u.id, u.name, u.is_owner, u.is_admin) for u in await people(hass)),
        "refresh_tokens": sorted(
            (t.id, t.client_name, t.client_id, t.token_type)
            for u in await people(hass)
            for t in u.refresh_tokens.values()
        ),
        "credentials": sorted(
            (c.id, c.data.get("username")) for u in await people(hass) for c in u.credentials
        ),
        "onboarding": list(hass.data["onboarding"].steps["done"]),
        "core": (
            hass.config.time_zone,
            hass.config.units.temperature_unit,
            hass.config.country,
            hass.config.currency,
        ),
        "entries": sorted(
            (e.entry_id, e.domain, e.title, tuple(sorted(e.data.items())))
            for e in hass.config_entries.async_entries()
        ),
        "flows": hass.config_entries.flow.async_progress(),
        "schedules": timetables,
        "token_file": (token_file.read_text(), info.st_mode, info.st_uid, info.st_mtime_ns),
    }


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def stored_token(home: Home) -> str:
    return home.token_file.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------------- first run
async def test_a_fresh_home_assistant_is_set_up_completely(home: Home) -> None:
    hass = home.hass

    run = await home.run()

    assert run.ok, run.output
    assert_no_secret(run.output)
    # The owner, from .env, and nobody else.
    user = await owner(hass)
    assert user.is_owner
    assert user.name == "Max"
    assert [c.data["username"] for c in user.credentials] == ["max"]
    # Onboarding is complete, so the web UI opens normally.
    assert set(hass.data["onboarding"].steps["done"]) == {
        "user",
        "core_config",
        "analytics",
        "integration",
    }
    # Home Assistant's own settings.
    assert hass.config.time_zone == "Europe/Berlin"
    assert hass.config.country == "DE"
    assert hass.config.currency == "EUR"
    assert hass.config.units.temperature_unit == "°C"
    # The script token: one line, mode 0600, a long-lived token named cave-scripts that works.
    (token,) = await script_tokens(hass)
    assert token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    assert token.user.is_owner
    text = home.token_file.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert stat.S_IMODE(home.token_file.stat().st_mode) == 0o600
    assert home.token_file.stat().st_uid == home.token_file.parent.stat().st_uid
    refresh = hass.auth.async_validate_access_token(text.strip())
    assert refresh is not None
    assert refresh.id == token.id
    # The run's own login session was revoked: the owner keeps only the script token.
    assert [t.client_name for t in user.refresh_tokens.values()] == [TOKEN_NAME]
    # MQTT, against the broker from .env.
    (entry,) = hass.config_entries.async_entries("mqtt")
    assert entry.data["broker"] == "127.0.0.1"
    assert entry.data["port"] == 1883
    assert entry.data["username"] == "cave"
    assert entry.data["password"] == MQTT_PASSWORD
    # No FRITZ!Box in the settings: neither FRITZ! integration.
    assert hass.config_entries.async_entries("fritzbox") == []
    assert hass.config_entries.async_entries("fritz") == []
    assert "FRITZ!SmartHome and FRITZ!Box Tools are not set up" in run.output
    # A schedule with exactly the entity ID each room names; none for the room without one.
    registry = er.async_get(hass)
    schedules = sorted(e.entity_id for e in registry.entities.values() if e.domain == "schedule")
    assert schedules == ["schedule.bad_heizplan", "schedule.living_room_heating"]
    assert hass.states.get("schedule.living_room_heating").name == "Wohnzimmer heating schedule"
    response = await hass.services.async_call(
        "schedule",
        "get_schedule",
        target={"entity_id": "schedule.living_room_heating"},
        blocking=True,
        return_response=True,
    )
    week = response["schedule.living_room_heating"]
    assert {day: [(r["from"], r["to"]) for r in ranges] for day, ranges in week.items()} == {
        day: [(_time(r["from"]), _time(r["to"])) for r in ranges]
        for day, ranges in DEFAULT_WEEK.items()
    }
    # Nothing is left half-way.
    assert hass.config_entries.flow.async_progress() == []
    # What onboarding set up by itself is gone again, and the output says so.
    for domain in ("radio_browser", "google_translate", "shopping_list"):
        assert hass.config_entries.async_entries(domain) == []
        assert f"removed {domain} (" in run.output


async def test_the_same_integrations_added_by_hand_are_kept(home: Home) -> None:
    assert (await home.run()).ok
    by_hand = MockConfigEntry(domain="shopping_list", title="Einkaufsliste", source="user")
    by_hand.add_to_hass(home.hass)

    run = await home.run()

    assert run.ok, run.output
    assert home.hass.config_entries.async_entries("shopping_list") == [by_hand]
    assert "changed:" not in run.output


async def test_onboarding_integrations_that_never_appear_are_waited_for_only_so_long(
    home: Home,
) -> None:
    flows = home.hass.config_entries.flow
    original = flows.async_init

    async def nothing_from_onboarding(
        handler: str, *, context: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if (context or {}).get("source") == "onboarding":
            return {"type": "abort", "reason": "declined"}
        return await original(handler, context=context, **kwargs)

    with patch.object(flows, "async_init", nothing_from_onboarding):
        run = await home.run(onboarding_wait=0.5)

    assert run.ok, run.output
    assert "onboarding did not set up google_translate, radio_browser, shopping_list" in run.output
    assert "changed:" not in (await home.run()).output


def _time(value: str) -> Any:
    return time.fromisoformat(value)


async def test_a_second_run_changes_nothing(home: Home) -> None:
    first = await home.run()
    assert first.ok, first.output
    before = await snapshot(home)

    second = await home.run()

    assert second.ok, second.output
    assert await snapshot(home) == before
    assert "changed:" not in second.output
    assert_no_secret(second.output)


async def test_a_second_run_does_not_log_in(home: Home) -> None:
    assert (await home.run()).ok
    user = await owner(home.hass)

    with patch.object(home.hass.auth, "async_create_refresh_token") as create:
        second = await home.run()

    assert second.ok, second.output
    create.assert_not_called()
    assert [t.client_name for t in user.refresh_tokens.values()] == [TOKEN_NAME]


async def test_after_a_restart_without_onboarding_views_a_run_changes_nothing(home: Home) -> None:
    # Started with onboarding complete, Home Assistant does not register /api/onboarding at all.
    assert (await home.run()).ok
    before = await snapshot(home)
    original = Api.request

    async def without_onboarding(self: Api, method: str, path: str, **kwargs: Any) -> Reply:
        if path == "/api/onboarding":
            return Reply(404, "404: Not Found")
        return await original(self, method, path, **kwargs)

    with patch.object(Api, "request", without_onboarding):
        run = await home.run()

    assert run.ok, run.output
    assert "changed:" not in run.output
    assert await snapshot(home) == before


# ----------------------------------------------------------------------------- partial states
async def test_onboarding_left_half_done_is_completed(home: Home) -> None:
    # Someone created the owner (with the .env login) and stopped before the other steps.
    response = await _post(
        home,
        "/api/onboarding/users",
        {
            "client_id": f"{home.url}/",
            "name": "Max",
            "username": "max",
            "password": OWNER_PASSWORD,
            "language": "en",
        },
    )
    assert "auth_code" in response

    run = await home.run()

    assert run.ok, run.output
    assert len(await people(home.hass)) == 1
    assert set(home.hass.data["onboarding"].steps["done"]) == {
        "user",
        "core_config",
        "analytics",
        "integration",
    }
    assert len(await script_tokens(home.hass)) == 1
    assert (await home.run()).output.count("changed:") == 0


async def test_a_deleted_token_file_is_replaced_and_the_lost_token_deleted(home: Home) -> None:
    assert (await home.run()).ok
    (old,) = await script_tokens(home.hass)
    home.token_file.unlink()

    run = await home.run()

    assert run.ok, run.output
    (new,) = await script_tokens(home.hass)
    assert new.id != old.id
    assert home.hass.auth.async_get_refresh_token(old.id) is None
    assert home.hass.auth.async_validate_access_token(stored_token(home)).id == new.id
    assert stat.S_IMODE(home.token_file.stat().st_mode) == 0o600


async def test_a_token_revoked_in_the_ui_is_replaced(home: Home) -> None:
    assert (await home.run()).ok
    (old,) = await script_tokens(home.hass)
    home.hass.auth.async_remove_refresh_token(old)

    run = await home.run()

    assert run.ok, run.output
    assert "no longer accepts the stored script token" in run.output
    (new,) = await script_tokens(home.hass)
    assert home.hass.auth.async_validate_access_token(stored_token(home)).id == new.id


async def test_a_token_file_with_garbage_is_replaced(home: Home) -> None:
    assert (await home.run()).ok
    home.token_file.write_text("not a token at all\n", encoding="utf-8")

    run = await home.run()

    assert run.ok, run.output
    assert len(await script_tokens(home.hass)) == 1
    assert home.hass.auth.async_validate_access_token(stored_token(home)) is not None


async def test_a_token_file_that_others_can_read_is_made_private(home: Home) -> None:
    assert (await home.run()).ok
    token = stored_token(home)
    home.token_file.chmod(0o644)

    run = await home.run()

    assert run.ok, run.output
    assert stat.S_IMODE(home.token_file.stat().st_mode) == 0o600
    assert stored_token(home) == token  # the token itself is kept


async def test_a_changed_owner_password_does_not_matter_while_the_token_works(home: Home) -> None:
    assert (await home.run()).ok
    before = await snapshot(home)
    changed = Settings.from_json(
        {**_settings_json(home.settings), "owner_password": "a-different-password"}
    )

    run = await home.run(owner=changed.owner)

    assert run.ok, run.output
    assert await snapshot(home) == before
    assert "a-different-password" not in run.output


async def test_a_changed_owner_password_without_a_token_stops_with_a_clear_message(
    home: Home,
) -> None:
    assert (await home.run()).ok
    home.token_file.unlink()
    wrong = Settings.from_json({**_settings_json(home.settings), "owner_password": "wrong-pass"})

    run = await home.run(owner=wrong.owner)

    assert not run.ok
    assert "refused the login 'max' with the password from .env" in run.error
    assert "never changes the owner's password" in run.error
    assert "wrong-pass" not in run.output
    assert len(await people(home.hass)) == 1


async def test_home_assistant_that_does_not_answer_is_waited_for_and_named(
    home: Home, unused_tcp_port: int
) -> None:
    run = await home.run(url=f"http://127.0.0.1:{unused_tcp_port}", timeout=1.0)

    assert not run.ok
    assert "waiting for Home Assistant to answer at" in run.error
    assert "/api/onboarding" in run.error
    assert "gave up after 1 s" in run.error


# --------------------------------------------------------------------------------- integrations
async def test_the_mqtt_flow_that_opens_with_a_menu_is_answered(home: Home) -> None:
    # With the Supervisor, the MQTT flow opens with a menu (add-on or broker).
    addons = MagicMock(addon_name="Mosquitto broker")
    with (
        patch("homeassistant.components.mqtt.config_flow.is_hassio", return_value=True),
        patch("homeassistant.components.mqtt.config_flow.get_addon_manager", return_value=addons),
    ):
        run = await home.run()

    assert run.ok, run.output
    (entry,) = home.hass.config_entries.async_entries("mqtt")
    assert entry.data["broker"] == "127.0.0.1"
    assert entry.data["password"] == MQTT_PASSWORD
    addons.async_schedule_install_addon.assert_not_called()  # "broker" was chosen, not the add-on


async def test_a_broker_that_refuses_stops_with_the_error_and_leaves_no_flow(home: Home) -> None:
    home.broker_accepts = False

    run = await home.run()

    assert not run.ok
    assert "mqtt: step 'broker' failed: {'base': 'cannot_connect'}" in run.error
    assert_no_secret(run.output)
    assert home.hass.config_entries.async_entries("mqtt") == []
    assert home.hass.config_entries.flow.async_progress() == []
    # The next run, with the broker back, finishes the job.
    home.broker_accepts = True
    assert (await home.run()).ok
    assert len(home.hass.config_entries.async_entries("mqtt")) == 1


async def test_a_broker_login_changed_in_env_reconfigures_the_entry(home: Home) -> None:
    assert (await home.run()).ok
    (entry,) = home.hass.config_entries.async_entries("mqtt")
    _store_entries(home)
    new = Settings.from_json({**_settings_json(home.settings), "mqtt_password": "new-mqtt-pass"})

    run = await home.run(mqtt=new.mqtt, config_entries_file=home.settings.config_entries_file)

    assert run.ok, run.output
    (same,) = home.hass.config_entries.async_entries("mqtt")
    assert same.entry_id == entry.entry_id
    assert same.data["password"] == "new-mqtt-pass"
    assert "new-mqtt-pass" not in run.output
    # With the storage in step again, nothing more changes.
    _store_entries(home)
    again = await home.run(mqtt=new.mqtt)
    assert "changed:" not in again.output


async def test_fritz_box_tools_can_be_left_out(home: Home, fritz_box: Any) -> None:
    fritz = Settings.from_json(
        {
            **_settings_json(home.settings),
            "fritz_host": "http://127.0.0.1:8480",
            "fritz_user": "cave",
            "fritz_password": FRITZ_PASSWORD,
            "fritz_tools": False,
        }
    )

    run = await home.run(fritz_host=fritz.fritz_host, fritz=fritz.fritz, fritz_tools=False)

    assert run.ok, run.output
    (smarthome,) = home.hass.config_entries.async_entries("fritzbox")
    assert smarthome.data["host"] == "http://127.0.0.1:8480"
    assert home.hass.config_entries.async_entries("fritz") == []
    assert "FRITZ!Box Tools is left out by request" in run.output
    fritz_box.tools.assert_not_called()


async def test_a_switch_is_created_switched_on_and_left_alone_afterwards(home: Home) -> None:
    switches = (("input_boolean.demo_someone_home", "Demo: someone is home"),)

    run = await home.run(switches=switches)

    assert run.ok, run.output
    state = home.hass.states.get("input_boolean.demo_someone_home")
    assert state.state == "on"
    assert state.name == "Demo: someone is home"
    # Switched off by someone: a second run leaves it off and changes nothing.
    await home.hass.services.async_call(
        "input_boolean", "turn_off", {"entity_id": "input_boolean.demo_someone_home"}, blocking=True
    )
    before = await snapshot(home)
    again = await home.run(switches=switches)
    assert again.ok, again.output
    assert "changed:" not in again.output
    assert home.hass.states.get("input_boolean.demo_someone_home").state == "off"
    assert await snapshot(home) == before


async def test_a_switch_created_but_not_yet_named_is_finished(home: Home) -> None:
    assert (await home.run()).ok
    await _ws(home, "input_boolean/create", name="demo_someone_home")

    run = await home.run(switches=(("input_boolean.demo_someone_home", "Demo: someone is home"),))

    assert run.ok, run.output
    state = home.hass.states.get("input_boolean.demo_someone_home")
    assert (state.state, state.name) == ("on", "Demo: someone is home")


def test_switches_must_be_input_booleans() -> None:
    with pytest.raises(BootstrapError, match="switches"):
        Settings.from_json(
            {**_minimal_json(), "switches": [{"entity_id": "switch.x", "name": "X"}]}
        )


async def test_the_fritz_integrations_are_set_up_when_env_names_a_fritz_box(
    home: Home, fritz_box: Any
) -> None:
    fritz = Settings.from_json(
        {
            **_settings_json(home.settings),
            "fritz_host": "192.168.178.1",
            "fritz_user": "cave",
            "fritz_password": FRITZ_PASSWORD,
        }
    )

    run = await home.run(fritz_host=fritz.fritz_host, fritz=fritz.fritz)

    assert run.ok, run.output
    assert_no_secret(run.output)
    (smarthome,) = home.hass.config_entries.async_entries("fritzbox")
    assert smarthome.data["host"] == "http://192.168.178.1"
    assert smarthome.data["username"] == "cave"
    assert smarthome.data["password"] == FRITZ_PASSWORD
    (tools,) = home.hass.config_entries.async_entries("fritz")
    assert tools.data["host"] == "192.168.178.1"
    assert tools.data["username"] == "cave"
    before = await snapshot(home)
    again = await home.run(fritz_host=fritz.fritz_host, fritz=fritz.fritz)
    assert again.ok, again.output
    assert await snapshot(home) == before


@pytest.mark.parametrize(
    ("host", "url", "tools_host"),
    [
        ("fritz.box", "http://fritz.box", "fritz.box"),
        ("192.168.178.1", "http://192.168.178.1", "192.168.178.1"),
        ("127.0.0.1:8480", "http://127.0.0.1:8480", "127.0.0.1"),
        ("https://fritz.box", "https://fritz.box", "fritz.box"),
    ],
)
def test_the_fritz_address_is_given_to_each_integration_in_its_form(
    host: str, url: str, tools_host: str
) -> None:
    settings = Settings.from_json(
        {
            **_minimal_json(),
            "fritz_host": host,
            "fritz_user": "u",
            "fritz_password": "p",
        }
    )

    assert settings.fritz_url == url
    assert settings.fritz_tools_host == tools_host


# ------------------------------------------------------------------------------------ schedules
async def test_a_schedule_that_exists_is_left_alone(home: Home) -> None:
    assert (await home.run()).ok
    # The owner edits the timetable in the UI.
    hass = home.hass
    collection_items = await _ws(home, "schedule/list")
    item = next(i for i in collection_items if i["id"] == "living_room_heating")
    await _ws(
        home,
        "schedule/update",
        schedule_id=item["id"],
        name="Mein Plan",
        monday=[{"from": "05:00:00", "to": "07:00:00"}],
    )
    before = await snapshot(home)

    run = await home.run()

    assert run.ok, run.output
    assert await snapshot(home) == before
    assert hass.states.get("schedule.living_room_heating").name == "Mein Plan"


async def test_a_schedule_created_but_not_yet_named_is_named(home: Home) -> None:
    assert (await home.run()).ok
    # An earlier run stopped right after creating the helper under its object ID.
    await _ws(home, "schedule/delete", schedule_id="living_room_heating")
    await _ws(home, "schedule/create", name="living_room_heating", monday=[])
    assert home.hass.states.get("schedule.living_room_heating").name == "living_room_heating"

    run = await home.run()

    assert run.ok, run.output
    assert "named the schedule schedule.living_room_heating" in run.output
    registry = er.async_get(home.hass)
    living = [e for e in registry.entities.values() if e.entity_id.startswith("schedule.living")]
    assert [e.entity_id for e in living] == ["schedule.living_room_heating"]
    state = home.hass.states.get("schedule.living_room_heating")
    assert state.name == "Wohnzimmer heating schedule"


async def test_a_schedule_defined_outside_the_ui_is_left_alone(home: Home) -> None:
    # A schedule entity without a registry entry, as one from YAML without an ID would be.
    home.hass.states.async_set("schedule.living_room_heating", "on")

    run = await home.run()

    assert run.ok, run.output
    assert "schedule.living_room_heating exists (defined outside the UI)" in run.output
    items = {item["id"] for item in await _ws(home, "schedule/list")}
    assert items == {"bad_heizplan"}


async def test_a_room_that_names_something_other_than_a_schedule_is_reported(
    home: Home, tmp_path: Path
) -> None:
    home.settings.heating_file.write_text(
        "rooms:\n  - id: kitchen\n    name: Küche\n    schedule: input_boolean.kitchen\n",
        encoding="utf-8",
    )

    run = await home.run()

    assert not run.ok
    assert "room 'kitchen'" in run.error
    assert "input_boolean.kitchen" in run.error


async def test_no_applied_file_means_no_schedules(home: Home) -> None:
    home.settings.heating_file.unlink()

    run = await home.run()

    assert run.ok, run.output
    registry = er.async_get(home.hass)
    assert [e for e in registry.entities.values() if e.domain == "schedule"] == []


# ------------------------------------------------------------------------------- the entry point
def test_main_reads_its_settings_from_standard_input_and_names_a_missing_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _minimal_json()
    del settings["owner_password"]

    code = main(stdin=io.StringIO(json.dumps(settings)))

    assert code == 2
    assert "'owner_password' is missing" in capsys.readouterr().out


def test_main_rejects_input_that_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(stdin=io.StringIO("owner=max")) == 2
    assert "cannot read the settings" in capsys.readouterr().out


def test_output_masks_every_secret() -> None:
    stream = io.StringIO()
    Output(["s3cret", "longer-s3cret"], stream).say("a longer-s3cret and s3cret here")

    assert stream.getvalue() == "a ******** and ******** here\n"


def test_the_token_file_goes_to_the_owner_of_its_directory(tmp_path: Path) -> None:
    directory = tmp_path / ".cave"
    directory.mkdir(mode=0o700)
    path = directory / "token"
    path.write_text("old\n")
    path.chmod(0o644)

    write_token_file(path, "new-token")

    assert path.read_text() == "new-token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == directory.stat().st_uid == os.getuid()
    assert [p.name for p in directory.iterdir()] == ["token"]  # no temporary file is left


# ------------------------------------------------------------------------------------ utilities
def _minimal_json() -> dict[str, Any]:
    return {
        "owner_name": "Max",
        "owner_user": "max",
        "owner_password": "x",
        "time_zone": "Europe/Berlin",
        "mqtt_host": "127.0.0.1",
        "mqtt_user": "cave",
        "mqtt_password": "y",
    }


def _settings_json(settings: Settings) -> dict[str, Any]:
    return {
        "owner_name": settings.owner_name,
        "owner_user": settings.owner.username,
        "owner_password": settings.owner.password,
        "time_zone": settings.time_zone,
        "mqtt_host": settings.mqtt_host,
        "mqtt_port": settings.mqtt_port,
        "mqtt_user": settings.mqtt.username,
        "mqtt_password": settings.mqtt.password,
    }


def _store_entries(home: Home) -> None:
    """Write the config entries as Home Assistant's storage does, for the tool to compare."""
    path = home.settings.config_entries_file
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [e.as_storage_fragment for e in home.hass.config_entries.async_entries()]
    path.write_bytes(json_bytes({"version": 1, "data": {"entries": entries}}))


async def _post(home: Home, path: str, payload: dict[str, Any]) -> Any:
    async with aiohttp.ClientSession() as session, session.post(home.url + path, json=payload) as r:
        return await r.json()


async def _ws(home: Home, kind: str, **fields: Any) -> Any:
    """A WebSocket command with the stored script token."""
    async with (
        aiohttp.ClientSession() as session,
        WebSocket(session, home.url, stored_token(home)) as ws,
    ):
        return await ws.call(kind, **fields)
