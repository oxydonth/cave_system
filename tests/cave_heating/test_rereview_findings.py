"""Second review of pull request 9: what the concept asks for; every test fails on cb65f57."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.typing import MqttMockHAClient

from custom_components.cave_heating.const import DOMAIN
from tests.cave_heating.fakes import FakeHome, FakeNumber, FakeSelect, config, room

SEC, MIN, HOUR = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
LIVING = "climate.cave_living_room"
PROBLEM = "binary_sensor.cave_living_room_problem"
RESUME = "button.cave_living_room_resume_schedule"
COMFORT = "number.cave_living_room_comfort_temperature"
OFF = ("set_hvac_mode", {"hvac_mode": "off"})


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    return ("set_temperature", {"temperature": target} | ({"hvac_mode": mode} if mode else {}))


async def call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


# --- Must fix -------------------------------------------------------------------------------------


async def test_n1_a_lost_command_that_switches_the_radiator_back_on_is_resent_whole(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """After a window pause the radio loses the heat command; the resends must carry the mode."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.window", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.window"])))
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 5 * MIN)
    assert trv.commands == [OFF]
    home.set("binary_sensor.window", "off")
    trv.deaf = True  # the next command does not reach the device
    await home.advance(freezer, 3 * MIN)
    assert trv.commands == [OFF, heat(21.0, "heat")]
    trv.deaf = False
    await home.advance(freezer, 6 * HOUR)
    assert trv.commands[2] == heat(21.0, "heat"), "every resend and retry lacks the mode"
    assert trv.hvac_mode == "heat", "the radiator is still off six hours later"


async def test_n1_a_thermostat_switched_off_at_the_device_while_a_target_is_pending_is_put_back(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Concept 5.6: a FRITZ! dial turned all the way down reports off, which is undone."""
    trv = await home.thermostat("climate.a", target=21.0, delay=2 * MIN)
    await home.start(config(room("living_room", "climate.a")))
    await call(hass, "number", "set_value", entity_id=COMFORT, value=22)
    await home.advance(freezer, 30 * SEC)  # 22 is sent and on its way
    trv.report(mode="off")  # the dial is turned all the way down
    await home.advance(freezer, 3 * HOUR)
    assert trv.hvac_mode == "heat", "every resend and retry is a target without the mode"


async def test_n1_a_thermostat_that_stops_disagreeing_becomes_ok_again(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Budget used up three hours in a row; then the owner switches the device's programme off."""
    trv = await home.thermostat("climate.a", modes=("heat", "auto", "off"), target=21.0)
    await home.start(config(room("living_room", "climate.a")))
    for _ in range(5 * 60):  # the device falls back to auto a minute after every command
        if trv.hvac_mode != "auto":
            trv.report(mode="auto")
        await home.advance(freezer, 1 * MIN)
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.a": "unresponsive"}
    await home.advance(freezer, 3 * HOUR)  # six retries, none of them undone by the device
    assert trv.hvac_mode == "heat", "the retries are targets without the mode"
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.a": "ok"}


@pytest.mark.parametrize(("delay", "slow"), [(5 * SEC, False), (5 * MIN, True)])
async def test_n2_a_thermostat_that_reports_a_superseded_target_is_corrected_at_once(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    delay: timedelta,
    slow: bool,
) -> None:
    """A boost is cancelled before the thermostat has reported it: 25 arrives, 21 is wanted."""
    trv = await home.thermostat("climate.a", target=21.0, delay=delay)
    await home.start(config(room("living_room", {"entity": "climate.a", "slow": slow})))
    await call(hass, "climate", "set_preset_mode", entity_id=LIVING, preset_mode="boost")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(25.0)]
    await call(hass, "button", "press", entity_id=RESUME)  # the thermostat still reports 21
    await home.advance(freezer, delay)
    assert trv.target_temperature == 25.0  # the report of 25: nothing is waiting any more
    await home.advance(freezer, 10 * SEC)
    sent = list(trv.commands)
    await home.advance(freezer, delay)  # let the thermostat finish
    assert sent == [heat(25.0), heat(21.0)], "sent only when the confirm timeout passes"


# --- Should fix -----------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["thermostat", "started"])
async def test_n3_a_restored_window_pause_waits_for_a_contact_that_appears_after_the_start(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
    first: str,
) -> None:
    """As test_s2, but the contact's entity is created after Home Assistant has started."""
    hass_storage[f"{DOMAIN}.state"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.state",
        "data": {
            "home": {"mode": "auto", "away_temperature": 16.0},
            "rooms": {"living_room": {"mode": "heat", "window_paused": True}},
        },
    }
    hass.set_state(CoreState.starting)
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.window"])))
    if first == "thermostat":
        trv = await home.thermostat("climate.a", mode="off", target=21.0)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    if first == "started":
        trv = await home.thermostat("climate.a", mode="off", target=21.0)
    await home.advance(freezer, 20 * SEC)
    home.set("binary_sensor.window", "on")  # the window is still open
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == [], "heat, and off again a minute later"


async def test_n4_an_owed_setting_taken_out_of_the_file_is_forgotten(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The setting's entity is unavailable at load; the owner removes the entry; it returns."""
    thermostat = await home.thermostat("climate.a", target=21.0)
    select = FakeSelect("select.a_sensor", ["internal", "external"], "internal")
    await home.add(select)
    select._attr_available = False
    select.async_write_ha_state()
    trv = {"entity": "climate.a", "settings": {"select.a_sensor": "external"}}
    await home.start(config(room("living_room", trv)))
    await home.reload(config(room("living_room", "climate.a")))
    select._attr_available = True
    select.async_write_ha_state()
    thermostat.report(current=20.6)  # the next evaluation of the room
    await hass.async_block_till_done()
    assert "Evaluating room living_room failed" not in caplog.text, "KeyError: 'select.a_sensor'"
    assert home.state(PROBLEM).state == "off", "the controller failed on this room"


async def test_n5_a_room_sensor_named_before_it_is_paired_gets_its_whole_device(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, mqtt_mock: MqttMockHAClient
) -> None:
    """As test_m3, but heating.yaml names the sensor first and its entities appear one by one."""
    sensor = "sensor.wohnzimmer_sensor_temperature"
    await home.thermostat("climate.a", target=21.0, current=23.0)
    await home.add(FakeNumber("number.a_external", 20.4))
    trv = {"entity": "climate.a", "external_temperature": "number.a_external"}
    await home.start(config(room("living_room", trv, temperature_sensor=sensor)))
    device = {"identifiers": ["zigbee2mqtt_0x00124b0029f1a2b3"], "name": "Wohnzimmer Sensor"}
    for kind, unit in (("temperature", "°C"), ("humidity", "%")):
        discovery = {
            "name": kind.capitalize(),
            "unique_id": f"snzb02p_{kind}",
            "state_topic": "zigbee2mqtt/wohnzimmer",
            "value_template": "{{ value_json." + kind + " }}",
            "device_class": kind,
            "unit_of_measurement": unit,
            "device": device,
        }
        topic = f"homeassistant/sensor/snzb02p/{kind}/config"
        async_fire_mqtt_message(hass, topic, json.dumps(discovery))
        await hass.async_block_till_done()
        async_fire_mqtt_message(hass, "zigbee2mqtt/wohnzimmer", '{"temperature": 20.4}')
        await hass.async_block_till_done()
    assert hass.states.get(sensor) is not None
    for humidity in range(8):  # four hours; the temperature is steady, the humidity is not
        await home.advance(freezer, 30 * MIN)
        payload = json.dumps({"temperature": 20.4, "humidity": humidity})
        async_fire_mqtt_message(hass, "zigbee2mqtt/wohnzimmer", payload)
        await hass.async_block_till_done()
    assert home.state(LIVING).attributes["current_temperature"] == 20.4, "thermostats' average"
