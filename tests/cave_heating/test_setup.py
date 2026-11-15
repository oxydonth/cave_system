"""Setting up, loading the applied file, reloading, restoring, and the contracts of section 5.2."""

from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

import custom_components.cave_heating as integration
from custom_components.cave_heating import runtime as runtime_module
from custom_components.cave_heating.const import DOMAIN, STORAGE_KEY
from custom_components.cave_heating.diagnostics import async_get_config_entry_diagnostics
from homeassistant import block_async_io
from tests.cave_heating.fakes import FakeHome, config, room

TRANSLATIONS = Path(integration.__file__).parent / "translations"
SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)


def two_rooms() -> dict[str, Any]:
    """A home with a living room and a bathroom."""
    return config(
        room("living_room", "climate.living_trv", name="Wohnzimmer"),
        room("bathroom", "climate.bath_trv", name="Bad"),
    )


async def two_thermostats(home: FakeHome) -> None:
    """The thermostats of two_rooms(), already at comfort."""
    await home.thermostat("climate.living_trv", target=21.0)
    await home.thermostat("climate.bath_trv", target=21.0)


# --- Section 5.2: entities, unique IDs, devices and areas -----------------------------------


async def test_entity_ids_and_unique_ids_are_exactly_those_of_section_5_2(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Every entity ID is set from the room id; unique IDs are <room id>_<key>."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    registry = er.async_get(hass)
    entities = {
        item.entity_id: item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    expected: dict[str, str] = {
        "select.cave_home_mode": "home_mode",
        "number.cave_away_temperature": "home_away_temperature",
        "binary_sensor.cave_home_occupied": "home_occupied",
    }
    for room_id in ("living_room", "bathroom"):
        expected |= {
            f"climate.cave_{room_id}": f"{room_id}_climate",
            f"number.cave_{room_id}_comfort_temperature": f"{room_id}_comfort_temperature",
            f"number.cave_{room_id}_eco_temperature": f"{room_id}_eco_temperature",
            f"button.cave_{room_id}_resume_schedule": f"{room_id}_resume_schedule",
            f"binary_sensor.cave_{room_id}_window_open": f"{room_id}_window_open",
            f"binary_sensor.cave_{room_id}_problem": f"{room_id}_problem",
            f"sensor.cave_{room_id}_reason": f"{room_id}_reason",
        }
    assert entities == expected
    for entity_id in expected:
        assert hass.states.get(entity_id) is not None, entity_id


async def test_room_thermostat_contract(hass: HomeAssistant, home: FakeHome) -> None:
    """Modes heat and off; 0.5 K steps from 5 to 30 °C; the presets; the three attributes."""
    await two_thermostats(home)
    await home.start(two_rooms())
    state = home.state("climate.cave_living_room")
    assert state.state == "heat"
    assert state.attributes["hvac_modes"] == ["heat", "off"]
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (5.0, 30.0)
    assert state.attributes["target_temp_step"] == 0.5
    assert state.attributes["preset_modes"] == ["comfort", "eco", "away", "boost", "none"]
    assert state.attributes["temperature"] == 21.0
    assert state.attributes["preset_mode"] == "comfort"
    assert state.attributes["reason"] == "comfort"
    assert state.attributes["override_until"] is None
    assert state.attributes["boost_until"] is None
    assert home.state("sensor.cave_living_room_reason").attributes["options"] == [
        "summer",
        "room_off",
        "window",
        "boost",
        "override",
        "away",
        "comfort",
        "eco",
    ]
    assert home.state("select.cave_home_mode").attributes["options"] == ["auto", "away", "off"]
    for entity_id in (
        "number.cave_living_room_comfort_temperature",
        "number.cave_living_room_eco_temperature",
        "number.cave_away_temperature",
    ):
        attributes = home.state(entity_id).attributes
        assert (attributes["min"], attributes["max"], attributes["step"]) == (5.0, 30.0, 0.5)
    assert home.state("binary_sensor.cave_home_occupied").state == "on"


async def test_config_entry_is_created_on_first_start_and_rooms_are_devices_in_their_areas(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """One entry, created by the integration; each room a device in the area of its name."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    assert entry.state is ConfigEntryState.LOADED
    assert entry.source == "import"
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    by_identifier = {
        next(iter(device.identifiers))[1]: device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
    }
    assert set(by_identifier) == {"home", "room_living_room", "room_bathroom"}
    for identifier, name in (("room_living_room", "Wohnzimmer"), ("room_bathroom", "Bad")):
        device = by_identifier[identifier]
        assert device.name == name
        area = areas.async_get_area(device.area_id) if device.area_id else None
        assert area is not None
        assert area.name == name
    assert by_identifier["home"].area_id is None
    registry = er.async_get(hass)
    assert registry.async_get("climate.cave_living_room").device_id == (
        by_identifier["room_living_room"].id
    )


async def test_second_start_keeps_the_single_entry(hass: HomeAssistant, home: FakeHome) -> None:
    """The import flow does not create a second entry."""
    entry = await home.start(config())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


async def test_flow_started_in_the_ui_says_the_integration_is_configured_through_the_file(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Also before the entry exists: there is nothing to set up in the UI."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configured_by_file"
    for language in ("en", "de"):
        strings = json.loads((TRANSLATIONS / f"{language}.json").read_text())
        assert "heating.yaml" in strings["config"]["abort"]["configured_by_file"]


def test_english_and_german_translations_have_the_same_keys() -> None:
    """Entity names, states, services, issues and exceptions ship in both languages."""

    def keys(data: Any, prefix: str = "") -> set[str]:
        if not isinstance(data, dict):
            return {prefix}
        return {key for name, value in data.items() for key in keys(value, f"{prefix}.{name}")}

    english = json.loads((TRANSLATIONS / "en.json").read_text())
    german = json.loads((TRANSLATIONS / "de.json").read_text())
    assert keys(english) == keys(german)


async def test_configuration_yaml_content_is_reported_and_ignored(
    hass: HomeAssistant, home: FakeHome, caplog: pytest.LogCaptureFixture
) -> None:
    """configuration.yaml holds an empty entry; anything else never stops Home Assistant."""
    await hass.async_add_executor_job(home.write_applied, config())
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {"rooms": []}})
    await hass.async_block_till_done()
    assert "does not support any configuration parameters" in caplog.text
    assert hass.config_entries.async_entries(DOMAIN)[0].state is ConfigEntryState.LOADED


# --- Loading the applied file -----------------------------------------------------------------


async def test_reads_the_applied_copy_not_the_edited_file(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """cave/heating.yaml is never read; cave/.applied/heating.yaml is."""
    await two_thermostats(home)

    def write_edited() -> None:
        edited = Path(hass.config.path("cave", "heating.yaml"))
        edited.parent.mkdir(parents=True, exist_ok=True)
        edited.write_text("version: 1\nrooms:\n  - this is not valid\n")

    await hass.async_add_executor_job(write_edited)
    await home.start(two_rooms())
    assert hass.states.get("climate.cave_bathroom") is not None
    assert not ir.async_get(hass).issues


@pytest.mark.parametrize(
    ("content", "issue_id", "text"),
    [
        (None, "applied_file_missing", "does not exist"),
        ("version: 1\nrooms: 3\n", "applied_file_invalid", 'Field "rooms": must be a list'),
        ("version: [\n", "applied_file_invalid", "The file is not valid YAML"),
    ],
)
async def test_missing_or_invalid_file_at_start_means_no_rooms_and_a_repair_notice(
    hass: HomeAssistant, home: FakeHome, content: str | None, issue_id: str, text: str
) -> None:
    """Home Assistant starts; the home's entities exist; the repair notice explains."""
    entry = await home.start(content)
    assert entry.state is ConfigEntryState.LOADED
    assert home.runtime().rooms == {}
    assert hass.states.get("select.cave_home_mode") is not None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == issue_id
    assert text in issue.translation_placeholders["errors"]
    assert issue.translation_placeholders["path"].endswith("cave/.applied/heating.yaml")


async def test_file_with_no_rooms_loads_without_problems(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """The starter configuration: only the home's entities, no repair notice."""
    entry = await home.start(config())
    registry = er.async_get(hass)
    assert sorted(
        item.entity_id for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    ) == [
        "binary_sensor.cave_home_occupied",
        "number.cave_away_temperature",
        "select.cave_home_mode",
    ]
    assert not ir.async_get(hass).issues


async def test_file_naming_entities_that_do_not_exist_loads_with_the_problem_sensor_on(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Section 5.4: as unavailable, with the problem sensor on and a repair notice naming it."""
    await home.thermostat("climate.present", target=21.0)
    entry = await home.start(
        config(
            room(
                "living_room",
                "climate.present",
                "climate.not_paired",
                temperature_sensor="sensor.not_paired",
                windows=["binary_sensor.not_paired"],
                name="Wohnzimmer",
            ),
            room("guest", "climate.ghost"),
        )
    )
    assert entry.state is ConfigEntryState.LOADED
    problem = home.state("binary_sensor.cave_living_room_problem")
    assert problem.state == "on"
    assert problem.attributes["thermostats"] == {
        "climate.present": "ok",
        "climate.not_paired": "missing",
    }
    assert problem.attributes["messages"] == [
        "climate.not_paired does not exist",
        "binary_sensor.not_paired (windows[0]) does not exist",
        "sensor.not_paired (temperature_sensor) does not exist",
    ]
    assert home.state("climate.cave_living_room").state == "heat"
    assert home.state("climate.cave_guest").state == "unavailable", "no thermostat available"
    assert home.state("binary_sensor.cave_guest_problem").state == "on"
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, "missing_living_room_climate.not_paired")
    assert issue is not None
    assert issue.translation_placeholders == {
        "room": "Wohnzimmer",
        "entity": "climate.not_paired",
        "field": "trvs[1].entity",
    }
    assert issues.async_get_issue(DOMAIN, "missing_living_room_sensor.not_paired") is not None

    paired = await home.thermostat("climate.not_paired", target=18.0)
    await home.advance(freezer, 3 * SEC)
    assert issues.async_get_issue(DOMAIN, "missing_living_room_climate.not_paired") is None
    assert paired.commands == [("set_temperature", {"temperature": 21.0})]


async def test_no_blocking_io_in_the_event_loop(
    hass: HomeAssistant, home: FakeHome, disable_block_async_io: None
) -> None:
    """The applied file is read in the executor, at start and on reload."""
    block_async_io.enable()
    threads: list[int] = []
    original = runtime_module.load_file

    def recording_load(path: Path) -> Any:
        threads.append(threading.get_ident())
        return original(path)

    runtime_module.load_file = recording_load
    try:
        await home.start(config())
        await home.reload(config(room("living_room", "climate.x")))
    finally:
        runtime_module.load_file = original
    loop_thread = hass.loop._thread_id  # type: ignore[attr-defined]
    assert len(threads) == 2
    assert loop_thread not in threads


# --- Reload -----------------------------------------------------------------------------------


async def test_reload_with_a_valid_file_adds_changes_and_removes_rooms(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Rooms appear and disappear with their devices; a kept room keeps its state."""
    await two_thermostats(home)
    kitchen = await home.thermostat("climate.kitchen_trv", target=18.0)
    entry = await home.start(two_rooms())
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.cave_living_room", "temperature": 23},
        blocking=True,
    )
    await home.reload(
        config(
            room("living_room", "climate.living_trv", name="Stube"),
            room("kitchen", "climate.kitchen_trv", name="Küche"),
        )
    )
    assert hass.states.get("climate.cave_bathroom") is None
    assert er.async_get(hass).async_get("climate.cave_bathroom") is None
    assert home.state("climate.cave_kitchen").state == "heat"
    living = home.state("climate.cave_living_room")
    assert living.attributes["reason"] == "override", "the room's state is kept"
    assert living.attributes["temperature"] == 23.0
    assert living.attributes["friendly_name"] == "Stube"
    devices = {
        next(iter(device.identifiers))[1]: device.name
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }
    assert devices == {"home": "Home", "room_living_room": "Stube", "room_kitchen": "Küche"}
    await home.advance(freezer, 3 * SEC)
    assert kitchen.commands == [("set_temperature", {"temperature": 21.0})]


async def test_reload_with_an_invalid_file_keeps_the_running_configuration(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """The call fails with a readable message; a repair notice is raised; nothing changes."""
    await two_thermostats(home)
    await home.start(two_rooms())
    with pytest.raises(ServiceValidationError) as caught:
        await home.reload("version: 1\nrooms:\n  - id: Bad Room\n")
    message = str(caught.value)
    assert message.startswith(
        "The applied heating.yaml cannot be used, so the previous configuration stays in effect:"
    )
    assert 'Room "Bad Room" (rooms[0]), field "id"' in message
    assert set(home.runtime().rooms) == {"living_room", "bathroom"}
    assert hass.states.get("climate.cave_bathroom") is not None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "reload_invalid")
    assert issue is not None
    await home.reload(two_rooms())
    assert ir.async_get(hass).async_get_issue(DOMAIN, "reload_invalid") is None


async def test_reload_over_the_rest_api_reports_an_invalid_file_as_an_error(
    hass: HomeAssistant,
    home: FakeHome,
    hass_client: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What `cave apply` sees: 400 with the message; 200 for a valid file; no error logged."""
    await home.start(config())
    assert await async_setup_component(hass, "api", {})
    client = await hass_client()
    await hass.async_add_executor_job(home.write_applied, "version: 7\n")
    response = await client.post(f"/api/services/{DOMAIN}/reload")
    assert response.status == HTTPStatus.BAD_REQUEST
    body = await response.json()
    assert "version 7 is not supported" in body["message"]
    await hass.async_add_executor_job(home.write_applied, config())
    response = await client.post(f"/api/services/{DOMAIN}/reload")
    assert response.status == HTTPStatus.OK
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_reload_after_a_missing_file_at_start_brings_the_rooms(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """The repair notice goes away once a valid file has been applied."""
    await two_thermostats(home)
    await home.start(None)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "applied_file_missing") is not None
    await home.reload(two_rooms())
    assert hass.states.get("climate.cave_living_room") is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "applied_file_missing") is None


async def test_rooms_removed_while_home_assistant_was_down_are_removed_at_start(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Devices of rooms no longer in a valid file go; an invalid file removes nothing."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_add_executor_job(home.write_applied, "not: [valid")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 3
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_add_executor_job(
        home.write_applied, config(room("living_room", "climate.living_trv"))
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert {
        next(iter(device.identifiers))[1]
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    } == {"home", "room_living_room"}


# --- Start-up: state restored after a restart -------------------------------------------------


async def test_room_and_home_state_is_restored_after_a_restart(
    hass: HomeAssistant, home: FakeHome, hass_storage: dict[str, Any]
) -> None:
    """Mode, override, boost end time and temperatures; an ended override is dropped."""
    await two_thermostats(home)
    await home.thermostat("climate.kitchen_trv", target=21.0)
    now = dt_util.utcnow()
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "home": {"mode": "away", "away_temperature": 15.0},
            "rooms": {
                "living_room": {
                    "mode": "heat",
                    "comfort_temperature": 22.0,
                    "eco_temperature": 18.0,
                    "hand": {
                        "kind": "override",
                        "until": (now + timedelta(hours=2)).isoformat(),
                        "target": 23.5,
                        "preset": None,
                        "waiting_for_home": False,
                    },
                },
                "bathroom": {
                    "mode": "off",
                    "comfort_temperature": 21.0,
                    "eco_temperature": 17.5,
                    "hand": None,
                },
                "kitchen": {
                    "mode": "heat",
                    "comfort_temperature": 21.0,
                    "eco_temperature": 17.5,
                    "hand": {
                        "kind": "boost",
                        "temperature": 25.0,
                        "until": (now - timedelta(minutes=1)).isoformat(),
                    },
                },
            },
        },
    }
    await home.start(
        config(
            room("living_room", "climate.living_trv"),
            room("bathroom", "climate.bath_trv"),
            room("kitchen", "climate.kitchen_trv"),
        )
    )
    assert home.state("select.cave_home_mode").state == "away"
    assert home.state("number.cave_away_temperature").state == "15.0"
    assert home.state("number.cave_living_room_comfort_temperature").state == "22.0"
    assert home.state("number.cave_living_room_eco_temperature").state == "18.0"
    living = home.state("climate.cave_living_room")
    assert (living.attributes["reason"], living.attributes["temperature"]) == ("override", 23.5)
    assert living.attributes["override_until"] == (now + timedelta(hours=2)).isoformat()
    assert home.state("climate.cave_bathroom").state == "off"
    assert home.state("climate.cave_kitchen").attributes["reason"] == "away", "boost has ended"


async def test_state_is_saved_and_survives_unload_and_setup(
    hass: HomeAssistant, home: FakeHome, hass_storage: dict[str, Any]
) -> None:
    """What is set in the UI is written to the state file and read back."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": "climate.cave_living_room", "preset_mode": "boost"},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.cave_bathroom_eco_temperature", "value": 16.5},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.cave_home_mode", "option": "away"},
        blocking=True,
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    saved = hass_storage[STORAGE_KEY]["data"]
    assert saved["home"]["mode"] == "away"
    assert saved["rooms"]["bathroom"]["eco_temperature"] == 16.5
    assert saved["rooms"]["living_room"]["hand"] is None, "away by hand ended the boost"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert home.state("number.cave_bathroom_eco_temperature").state == "16.5"
    assert home.state("select.cave_home_mode").state == "away"


async def test_file_temperatures_are_used_only_the_first_time(
    hass: HomeAssistant,
    home: FakeHome,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After that the value set in the UI wins, and the difference is logged on load."""
    await two_thermostats(home)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "home": {"mode": "auto", "away_temperature": 15.0},
            "rooms": {"living_room": {"mode": "heat", "comfort_temperature": 22.5}},
        },
    }
    caplog.set_level(logging.INFO, "custom_components.cave_heating")
    await home.start(two_rooms())
    assert home.state("number.cave_living_room_comfort_temperature").state == "22.5"
    assert home.state("number.cave_living_room_eco_temperature").state == "17.5"
    assert home.state("number.cave_bathroom_comfort_temperature").state == "21.0", "first time"
    assert home.state("number.cave_away_temperature").state == "15.0"
    assert "comfort_temperature is 21.0 in heating.yaml but 22.5 as set in the UI" in caplog.text
    assert "away_temperature is 16.0 in heating.yaml but 15.0 as set in the UI" in caplog.text


async def test_a_hold_is_not_restored_after_a_restart(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Holds live in memory only."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    await hass.services.async_call(
        DOMAIN, "hold", {"entity_id": "climate.living_trv"}, blocking=True
    )
    problem = "binary_sensor.cave_living_room_problem"
    assert home.state(problem).attributes["thermostats"] == {"climate.living_trv": "held"}
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert home.state(problem).attributes["thermostats"] == {"climate.living_trv": "ok"}


async def test_diagnostics_show_the_controller_state(hass: HomeAssistant, home: FakeHome) -> None:
    """Rooms, decisions, thermostats and feeds."""
    await two_thermostats(home)
    entry = await home.start(two_rooms())
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["loaded"] is True
    assert data["timing"]["confirm_timeout"] == "0:05:00"
    living = data["rooms"]["living_room"]
    assert living["decision"] == {
        "target": 21.0,
        "preset": "comfort",
        "reason": "comfort",
        "heating": True,
    }
    assert living["thermostats"]["climate.living_trv"]["status"] == "ok"
    json.dumps(data)


async def test_entities_not_loaded_while_home_assistant_starts_are_not_reported_missing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Device integrations may load after cave_heating; only after the start is absent missing."""
    hass.set_state(CoreState.not_running)
    await home.start(
        config(room("living_room", "climate.later", temperature_sensor="sensor.never"))
    )
    problem = "binary_sensor.cave_living_room_problem"
    assert home.state(problem).state == "off"
    assert not ir.async_get(hass).issues
    trv = await home.thermostat("climate.later", target=18.0)
    await home.advance(freezer, 1 * MIN)
    assert trv.commands == [], "nothing is sent while Home Assistant is still starting"
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert home.state(problem).attributes["messages"] == [
        "sensor.never (temperature_sensor) does not exist"
    ]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_living_room_sensor.never")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [("set_temperature", {"temperature": 21.0})]


async def test_a_restart_while_everybody_is_away_keeps_the_away_state(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """P4 and P7: since when nobody is home, and the trackers' readings, are restored.

    The trackers come back unavailable first, as Home Assistant restores them, then not_home.
    """
    before = dt_util.utcnow() - timedelta(hours=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "home": {
                "mode": "auto",
                "away_temperature": 16.0,
                "nobody_home_since": before.isoformat(),
                "presence": {"device_tracker.phone": "not_home"},
            },
            "rooms": {},
        },
    }
    hass.set_state(CoreState.starting)
    await home.start(config(room("living_room", "climate.a"), presence="device_tracker.phone"))
    trv = await home.thermostat("climate.a", target=16.0)
    home.set("device_tracker.phone", "unavailable")
    await home.advance(freezer, 1 * MIN)
    home.set("device_tracker.phone", "not_home")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await home.advance(freezer, 2 * timedelta(hours=1), step=10 * MIN)
    assert home.state("binary_sensor.cave_home_occupied").state == "off"
    assert trv.commands == []


async def test_a_reload_does_not_restart_the_away_delay(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """O2: "nobody home since" is kept, not re-dated from the tracker's last change."""
    await home.thermostat("climate.a", target=21.0)
    home.set("person.max", "home")
    file = config(room("living_room", "climate.a"), presence="person.max", away_delay="00:30:00")
    await home.start(file)
    home.set("person.max", "not_home")
    await home.advance(freezer, 15 * MIN)
    home.set("person.max", "work")  # still not home, but its last change is now
    await home.advance(freezer, 5 * MIN)
    await home.reload(file)
    await home.advance(freezer, 10 * MIN)
    assert home.state("binary_sensor.cave_home_occupied").state == "off"


async def test_boost_and_override_are_written_to_the_state_file(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """S8: what restore reads is what the running integration writes, window pause included."""
    await two_thermostats(home)
    home.set("binary_sensor.bath_window", "off")
    home.set("person.max", "not_home")
    entry = await home.start(
        config(
            room("living_room", "climate.living_trv"),
            room("bathroom", "climate.bath_trv", windows=["binary_sensor.bath_window"]),
            presence="person.max",
        )
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.cave_living_room", "temperature": 23},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "boost", {"entity_id": "climate.cave_bathroom"}, blocking=True
    )
    home.set("binary_sensor.bath_window", "on")
    await home.advance(freezer, 2 * MIN)
    assert await hass.config_entries.async_unload(entry.entry_id)
    saved = hass_storage[STORAGE_KEY]["data"]
    override = saved["rooms"]["living_room"]["hand"]
    assert (override["kind"], override["target"]) == ("override", 23.0)
    boost = saved["rooms"]["bathroom"]["hand"]
    assert (boost["kind"], boost["temperature"]) == ("boost", 25.0)
    assert saved["rooms"]["bathroom"]["window_paused"] is True
    assert saved["rooms"]["living_room"]["window_paused"] is False
    assert saved["home"]["nobody_home_since"] is not None
    assert saved["home"]["presence"] == {"person.max": "not_home"}


async def test_a_reload_keeps_pending_commands_holds_and_the_window_pause(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """S8: a reload is not a restart; nothing in flight or in effect is forgotten."""
    slow = await home.thermostat("climate.slow", target=18.0, delay=10 * MIN)
    held = await home.thermostat("climate.held", target=21.0)
    paused = await home.thermostat("climate.paused", target=21.0)
    home.set("binary_sensor.window", "off")
    file = config(
        room("living_room", {"entity": "climate.slow", "slow": True}, "climate.held"),
        room("bathroom", "climate.paused", windows=["binary_sensor.window"]),
    )
    await home.start(file)
    await hass.services.async_call(
        DOMAIN, "hold", {"entity_id": "climate.held", "duration": "01:00:00"}, blocking=True
    )
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 2 * MIN)
    assert slow.commands == [("set_temperature", {"temperature": 21.0})]
    assert paused.commands == [("set_hvac_mode", {"hvac_mode": "off"})]
    await home.reload(file)
    await home.advance(freezer, 3 * SEC)
    problem = "binary_sensor.cave_living_room_problem"
    assert home.state(problem).attributes["thermostats"] == {
        "climate.slow": "pending",
        "climate.held": "held",
    }
    assert home.state("binary_sensor.cave_bathroom_window_open").state == "on"
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.cave_living_room_comfort_temperature", "value": 22},
        blocking=True,
    )
    await home.advance(freezer, 30 * MIN, step=MIN)
    assert slow.commands == [
        ("set_temperature", {"temperature": 21.0}),
        ("set_temperature", {"temperature": 22.0}),
    ], "the command in flight was neither forgotten nor sent again"
    assert held.commands == [], "still held"
    assert paused.commands == [("set_hvac_mode", {"hvac_mode": "off"})], "still paused"
