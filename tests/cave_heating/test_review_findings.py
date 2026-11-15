"""Review of pull request 9: each test states what the concept asks for and fails on 494641f."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.typing import MqttMockHAClient

from custom_components.cave_heating.const import DOMAIN
from tests.cave_heating.fakes import FakeHome, FakeNumber, FakeSelect, config, room

SEC, MIN, HOUR = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
LIVING = "climate.cave_living_room"
PROBLEM = "binary_sensor.cave_living_room_problem"
OFF = ("set_hvac_mode", {"hvac_mode": "off"})
EXTERNAL = ("select_option", {"option": "external"})


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    return ("set_temperature", {"temperature": target} | ({"hvac_mode": mode} if mode else {}))


async def one_room(home: FakeHome, *trvs: str | dict[str, Any], **fields: Any) -> None:
    top = fields.pop("top", {})
    await home.start(config(room("living_room", *trvs, **fields), **top))


async def call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


def reason(home: FakeHome) -> str:
    return str(home.state(LIVING).attributes["reason"])


async def ha_started(hass: HomeAssistant) -> None:
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


# --- Must fix -------------------------------------------------------------------------------------


async def test_m1_feed_starts_although_the_input_has_no_value_yet(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A freshly paired thermostat's external temperature input is `unknown` until written."""
    await home.thermostat("climate.a", target=21.0)
    feed = FakeNumber("number.a_external", None)
    select = FakeSelect("select.a_sensor", ["internal", "external"], None)  # type: ignore[arg-type]
    await home.add(feed, select)
    assert home.state("number.a_external").state == "unknown"
    home.set("sensor.room", "20.4")
    trv = {
        "entity": "climate.a",
        "external_temperature": "number.a_external",
        "settings": {"select.a_sensor": "external"},
    }
    await one_room(home, trv, temperature_sensor="sensor.room")
    await home.advance(freezer, 2 * HOUR, step=5 * MIN)
    assert feed.writes, "the room temperature never reaches the thermostat"
    assert select.commands == [EXTERNAL], "nor is the sensor selection ever set"


async def test_m2_settings_are_written_when_their_entity_appears_after_the_thermostat(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Home Assistant starts: cave_heating first, then the device's entities, climate first."""
    await one_room(home, {"entity": "climate.a", "settings": {"select.a_sensor": "external"}})
    await home.thermostat("climate.a", target=21.0)
    select = FakeSelect("select.a_sensor", ["internal", "external"], "internal")
    await home.add(select)
    await home.advance(freezer, 2 * HOUR, step=5 * MIN)
    assert select.commands == [EXTERNAL]


async def test_m2_settings_are_written_when_the_device_returns_climate_entity_first(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The device drops off the network and comes back having reset its sensor selection."""
    trv = await home.thermostat("climate.a", target=21.0)
    select = FakeSelect("select.a_sensor", ["internal", "external"], "external")
    await home.add(select)
    await one_room(home, {"entity": "climate.a", "settings": {"select.a_sensor": "external"}})
    trv.set_available(False)
    select._attr_available = False
    select.async_write_ha_state()
    await home.advance(freezer, 3 * HOUR, step=30 * MIN)
    select._attr_current_option = "internal"
    trv.set_available(True)  # the climate entity is updated before the select entity
    select._attr_available = True
    select.async_write_ha_state()
    await home.advance(freezer, 2 * HOUR, step=5 * MIN)
    assert select.commands == [EXTERNAL]


async def test_m3_mqtt_room_sensor_that_repeats_its_value_is_not_stale(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, mqtt_mock: MqttMockHAClient
) -> None:
    """Home Assistant's MQTT sensor does not advance last_reported for an unchanged value."""
    discovery = {
        "name": "Temperature",
        "unique_id": "snzb02p_temperature",
        "state_topic": "zigbee2mqtt/wohnzimmer",
        "value_template": "{{ value_json.temperature }}",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
    }
    async_fire_mqtt_message(hass, "homeassistant/sensor/snzb02p/t/config", json.dumps(discovery))
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "zigbee2mqtt/wohnzimmer", '{"temperature": 20.4}')
    await hass.async_block_till_done()
    sensor = next(state.entity_id for state in hass.states.async_all("sensor"))
    await home.thermostat("climate.a", target=21.0, current=23.0)
    feed = FakeNumber("number.a_external", 20.4)
    await home.add(feed)
    trv = {"entity": "climate.a", "external_temperature": "number.a_external"}
    await one_room(home, trv, temperature_sensor=sensor)
    for humidity in range(8):  # four hours of half-hourly reports from a room that is steady
        await home.advance(freezer, 30 * MIN)
        async_fire_mqtt_message(
            hass, "zigbee2mqtt/wohnzimmer", json.dumps({"temperature": 20.4, "humidity": humidity})
        )
        await hass.async_block_till_done()
    assert home.state(LIVING).attributes["current_temperature"] == 20.4, "thermostats' average"
    assert home.runtime().rooms["living_room"].feeds["climate.a"].active, "the feed has stopped"


async def test_m4_late_reports_of_21_25_21_are_not_hand_changes(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """21, 25 and 21 again are sent a minute apart; each report arrives five minutes late."""
    slow = await home.thermostat("climate.slow", target=18.0, delay=5 * MIN)
    fast = await home.thermostat("climate.fast", target=18.0)
    await one_room(home, {"entity": "climate.slow", "slow": True}, "climate.fast")
    await home.advance(freezer, 1 * MIN)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=25.0)
    await home.advance(freezer, 1 * MIN)
    await call(hass, "button", "press", entity_id="button.cave_living_room_resume_schedule")
    await home.advance(freezer, 30 * MIN)
    assert slow.commands == [heat(21.0), heat(25.0), heat(21.0)]
    assert fast.commands == [heat(21.0), heat(25.0), heat(21.0)], "two more: 25 and 21 again"
    assert reason(home) == "comfort", "the room is left in an override nobody set"


async def test_m5_nothing_is_adopted_from_a_thermostat_held_while_its_dial_settles(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Someone turns the dial, then starts `cave device-check` within the settle time."""
    checked = await home.thermostat("climate.a", target=21.0)
    other = await home.thermostat("climate.b", target=21.0)
    await one_room(home, "climate.a", "climate.b")
    checked.report(current=20.3)  # the first report after the start
    checked.turn_dial(22.0)
    await home.advance(freezer, 10 * SEC)
    await call(hass, DOMAIN, "hold", entity_id="climate.a")
    checked.turn_dial(27.5)  # the device check tries a target
    await home.advance(freezer, 2 * MIN)
    assert home.state(LIVING).attributes["temperature"] != 27.5
    assert heat(27.5) not in other.commands


@pytest.mark.parametrize("value", ["nan", "inf"])
async def test_m6_a_bad_reading_in_one_room_does_not_stop_the_others(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, value: str
) -> None:
    """The living room's sensor reports a non-finite value; the bedroom window is aired."""
    await home.thermostat("climate.a", target=21.0)
    bedroom = await home.thermostat("climate.b", target=21.0)
    await home.add(FakeNumber("number.a_external", 20.0))
    home.set("sensor.t", "20.0")
    home.set("binary_sensor.bed_window", "off")
    trv = {"entity": "climate.a", "external_temperature": "number.a_external"}
    await home.start(
        config(
            room("living_room", trv, temperature_sensor="sensor.t"),
            room("bedroom", "climate.b", windows=["binary_sensor.bed_window"]),
        )
    )
    home.set("sensor.t", "unavailable")
    await home.advance(freezer, 1 * MIN)
    home.set("sensor.t", value)
    await home.advance(freezer, 1 * MIN)
    home.set("binary_sensor.bed_window", "on")
    await home.advance(freezer, 30 * MIN)
    assert bedroom.commands == [OFF]
    home.set("binary_sensor.bed_window", "off")
    await home.advance(freezer, 2 * HOUR, step=10 * MIN)
    assert bedroom.commands == [OFF, heat(21.0, "heat")], "the bedroom stays off"


# --- Should fix -----------------------------------------------------------------------------------


async def test_s1_reload_while_home_assistant_starts_keeps_missing_entities_reported(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    hass.set_state(CoreState.starting)
    file = config(room("living_room", "climate.nowhere"))
    await home.start(file)
    await home.reload(file)  # `cave apply` before Home Assistant has finished starting
    await ha_started(hass)
    assert home.state(PROBLEM).state == "on"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_living_room_climate.nowhere")


async def test_s2_restart_during_a_window_pause_sends_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart the entities appear after cave_heating: radiator off, window open."""
    hass.set_state(CoreState.starting)
    await one_room(home, "climate.a", windows=["binary_sensor.window"])
    trv = await home.thermostat("climate.a", mode="off", target=21.0)
    home.set("binary_sensor.window", "on")
    await ha_started(hass)
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == [], "heat, and off again a minute later"


async def test_s2_restart_while_everybody_is_away_sends_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    hass.set_state(CoreState.starting)
    await one_room(home, "climate.a", top={"presence": ["device_tracker.phone"]})
    trv = await home.thermostat("climate.a", target=16.0)  # at the away temperature
    home.set("device_tracker.phone", "not_home")
    await ha_started(hass)
    await home.advance(freezer, 2 * HOUR)
    assert trv.commands == [], "comfort for the away delay, then away again"


async def test_s3_a_deadline_that_passes_while_an_evaluation_runs_is_not_lost(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The evaluation and the arming of the timer read the clock separately."""
    trv = await home.thermostat("climate.a", target=21.0)
    await one_room(home, "climate.a", top={"timing": {"debounce": 0.001}})
    # Each evaluation takes two milliseconds, as writing a room's seven entities does.
    remove = home.runtime().async_add_listener(lambda: freezer.tick(timedelta(milliseconds=2)))
    comfort = "number.cave_living_room_comfort_temperature"
    await call(hass, "number", "set_value", entity_id=comfort, value=22)
    remove()
    await home.advance(freezer, 6 * HOUR, step=30 * MIN)
    assert trv.commands == [heat(22.0)], "never sent: no timer is armed"


async def test_s4_dial_turned_back_on_a_thermostat_that_clamps_adopts_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    low = await home.thermostat("climate.low", target=20.0, max_temp=20.0)
    other = await home.thermostat("climate.other", target=21.0)
    await one_room(home, "climate.low", "climate.other")
    low.report(current=20.3)  # the first report after the start
    low.turn_dial(19.5)
    await home.advance(freezer, 5 * SEC)
    low.turn_dial(20.0)  # back to what the controller wants from it
    await home.advance(freezer, 2 * MIN)
    assert reason(home) == "comfort"
    assert other.commands == []


async def test_s5_home_mode_away_by_hand_also_cancels_a_dial_that_is_still_settling(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    trv = await home.thermostat("climate.a", target=21.0)
    await one_room(home, "climate.a")
    trv.report(current=20.3)  # the first report after the start
    trv.turn_dial(24.0)
    await home.advance(freezer, 10 * SEC)
    await call(hass, "select", "select_option", entity_id="select.cave_home_mode", option="away")
    await home.advance(freezer, 5 * MIN)
    assert reason(home) == "away"


@pytest.mark.parametrize(
    "text",
    ["version: 1\naway_delay: 99999999999999999999\n", "version: 1\nrooms: " + "[" * 3000 + "\n"],
    ids=["huge_number", "deep_nesting"],
)
async def test_s6_a_file_the_loader_cannot_digest_still_gives_a_repair_notice(
    hass: HomeAssistant, home: FakeHome, text: str
) -> None:
    await hass.async_add_executor_job(home.write_applied, text)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN)[0].state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "applied_file_invalid")
