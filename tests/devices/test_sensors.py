"""The room sensor (SNZB-02P) and the window contact (SNZB-04P) through Home Assistant's MQTT
integration, with cave_heating on top. A TRVZB is the room's thermostat throughout."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from cave_sim.testing import HOME_ASSISTANT_CLIENT
from cave_sim.zigbee.discovery import project_device_options
from tests.devices.capabilities import devices
from tests.devices.rig import (
    CONTACT,
    CONTACT_ID,
    HOUR,
    HUMIDITY,
    MIN,
    OFF,
    ROOM,
    ROOM_CLIMATE,
    SEC,
    SENSOR_ID,
    SENSOR_NAME,
    TEMPERATURE,
    TRVZB,
    WINDOW_OPEN,
    Rig,
    heat,
)

pytestmark = pytest.mark.usefixtures("hass")

EXTERNAL = "external_temperature_input"
LAST_SEEN = f"sensor.{SENSOR_ID}_last_seen"


@pytest.mark.parametrize("device", devices("room_temperature_and_humidity"))
async def test_room_temperature_and_humidity(rig: Rig, device: str) -> None:
    """Concept 5.4 and 6: the room thermostat shows the room sensor's temperature and humidity,
    not the thermostats' own readings, and follows their changes."""
    await rig.start(TRVZB, contact=False)
    attributes = rig.state(ROOM_CLIMATE).attributes
    assert attributes["current_temperature"] == float(rig.state(TEMPERATURE).state) == 19.0
    assert attributes["current_humidity"] == float(rig.state(HUMIDITY).state) == 50.0
    rig.sim.home.set_room_temperature(ROOM, 20.5)
    rig.sim.home.set_room_humidity(ROOM, 65)
    await rig.advance(15 * SEC)
    attributes = rig.state(ROOM_CLIMATE).attributes
    assert attributes["current_temperature"] == 20.5
    assert attributes["current_humidity"] == 65.0


@pytest.mark.parametrize("device", devices("sensor_going_stale"))
async def test_sensor_going_stale(rig: Rig, device: str) -> None:
    """Concept 5.4: a room sensor whose device has not reported (temperature, humidity or
    battery) for sensor_stale (3 h) counts as unavailable. The room then shows the average of its
    thermostats' readings and the temperature feed stops; when the sensor reports again, the
    feed writes at once.

    The SNZB-02P falls silent (a dead battery): Zigbee2MQTT keeps it available for 25 hours and
    Home Assistant keeps showing its last value, so only the staleness rule notices.
    """
    await rig.start(TRVZB, contact=False)
    sensor_entities = (TEMPERATURE, HUMIDITY, f"sensor.{SENSOR_ID}_battery")
    rig.sim.home.set_silent(SENSOR_ID, True)
    await rig.advance(1 * SEC)
    last = max(rig.state(entity).last_reported for entity in sensor_entities)
    stale_at = last.timestamp() + 3 * HOUR
    await rig.advance(stale_at - rig.now().timestamp() - 1 * MIN)
    assert rig.state(TEMPERATURE).state != "unavailable"
    assert rig.state(ROOM_CLIMATE).attributes["current_temperature"] == float(
        rig.state(TEMPERATURE).state
    ), "still fresh"
    await rig.advance(2 * MIN)
    thermostat = rig.state(TRVZB.climate).attributes["current_temperature"]
    assert rig.state(ROOM_CLIMATE).attributes["current_temperature"] == round(thermostat, 1)
    rig.mark()
    await rig.advance(1 * HOUR)
    assert rig.writes(TRVZB.name, EXTERNAL) == [], "the feed stopped"

    rig.sim.home.set_room_temperature(ROOM, 22.0)
    rig.sim.home.set_silent(SENSOR_ID, False)
    await rig.advance(3 * SEC)
    assert rig.writes(TRVZB.name, EXTERNAL) == [22.0], "written at once when the feed resumes"
    assert rig.state(ROOM_CLIMATE).attributes["current_temperature"] == 22.0


@pytest.mark.parametrize(("device", "case"), devices("sensor_stays_fresh_while_it_reports"))
async def test_sensor_stays_fresh_while_it_reports(rig: Rig, device: str, case: str) -> None:
    """Concept 5.4: the room sensor counts as stale only when neither it nor any other entity of
    the same device has reported for sensor_stale (3 h), because Home Assistant does not record a
    repeated identical value as a new report. In a steady room (19 °C inside and out, comfort
    19 °C) the temperature never changes for 4.5 hours; the sensor stays fresh, so the room keeps
    showing its reading and the feed keeps writing it every 25 minutes past the three hours.

    ``humidity-changing``: the humidity changes every half hour, so the device's humidity entity
    reports. ``everything-steady``: nothing changes for twelve hours, so Home Assistant records
    no report of the temperature, humidity or battery entity; the SNZB-02P still sends its values
    every hour (its maximum reporting interval). Its ``last_seen`` sensor, which this project's
    Zigbee2MQTT enables (``device_options`` in compose.yaml), changes with every message, and the
    room stays on the sensor (issue #21, F4).
    """

    async def steady(rig: Rig) -> None:
        rig.sim.home.set_outdoor_temperature(19.0)

    await rig.start(TRVZB, contact=False, room={"comfort_temperature": 19.0}, before_heating=steady)
    reported = rig.state(TEMPERATURE).last_reported
    start = len(rig.sim.broker.log)
    humidity = 50
    for half_hour in range(1, 25):
        if case == "humidity-changing":
            humidity = 52 if humidity == 50 else 50
            rig.sim.home.set_room_humidity(ROOM, humidity)
        await rig.advance(30 * MIN)
        if half_hour == 6:
            rig.mark()  # three hours in: what follows is past sensor_stale
        assert rig.room_runtime().sensor_fresh, f"fresh after {half_hour / 2} h"
        assert rig.state(ROOM_CLIMATE).attributes["current_temperature"] == 19.0
    assert float(rig.state(TEMPERATURE).state) == 19.0, "the temperature never changed"
    sent = [
        message
        for message in rig.sim.broker.log[start:]
        if message.topic == f"zigbee2mqtt/{SENSOR_NAME}"
    ]
    assert len(sent) >= 12, "the sensor sent its values at least every hour"
    if case == "everything-steady":
        assert rig.state(TEMPERATURE).last_reported == reported, "no report was recorded"
        assert rig.state(HUMIDITY).last_reported <= reported
    writes = rig.writes(TRVZB.name, EXTERNAL)
    assert len(writes) >= 20, f"the feed goes on past sensor_stale, got {writes}"
    assert set(writes) == {19.0}


@pytest.mark.parametrize("device", devices("sensor_last_seen_enabled_after_an_upgrade"))
async def test_sensor_last_seen_enabled_after_an_upgrade(rig: Rig, device: str) -> None:
    """Issue #21: a home paired before this project's Zigbee2MQTT enabled ``last_seen``.

    Home Assistant registered every device's ``last_seen`` sensor disabled, and keeps it so
    when Zigbee2MQTT, started again with the new ``device_options``, rediscovers it as enabled
    by default: ``enabled_by_default`` counts only at registration. cave_heating enables the
    room sensor's at its start; Home Assistant reloads the MQTT integration 30 s later, which
    makes every MQTT entity unavailable for a moment and then unknown until Zigbee2MQTT
    publishes the states again, 30 s after Home Assistant's birth message. A window pause in
    effect holds through it, nothing is sent to the thermostat but what the rules ask for, and
    nothing reaches Zigbee2MQTT but those commands. Afterwards the steady room stays on its
    sensor for twelve hours.
    """
    registry = er.async_get(rig.hass)

    async def before_upgrade(rig: Rig) -> None:
        assert registry.async_get(LAST_SEEN).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        rig.sim.home.zigbee.set_device_options(project_device_options())
        await rig.sim.async_flush_subscriptions()
        await rig.advance(1 * SEC)
        entry = registry.async_get(LAST_SEEN)
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, "still disabled"
        rig.sim.home.set_outdoor_temperature(19.0)
        rig.sim.home.set_window(CONTACT_ID, True)
        await rig.advance(2 * MIN)

    await rig.start(
        TRVZB,
        zigbee2mqtt={"device_options": {}},
        # A close delay far shorter than the reload, so that only window_unavailable_grace can
        # hold the pause through it.
        room={"comfort_temperature": 19.0, "window_close_delay": "00:00:05"},
        before_heating=before_upgrade,
    )
    assert registry.async_get(LAST_SEEN).disabled_by is None, "enabled by cave_heating"
    others = [
        entry.entity_id
        for entry in registry.entities.values()
        if entry.unique_id.endswith("_last_seen_zigbee2mqtt") and entry.entity_id != LAST_SEEN
    ]
    assert others, "the other devices have one too"
    assert all(registry.async_get(entity_id).disabled_by for entity_id in others), "left alone"
    assert rig.state(WINDOW_OPEN).state == "on"
    assert rig.commands(TRVZB) == OFF
    rig.mark()
    log_start = len(rig.sim.broker.log)
    watched = (CONTACT, TRVZB.climate, TEMPERATURE)
    seen: dict[str, list[str]] = {entity_id: [] for entity_id in (*watched, WINDOW_OPEN)}

    async def second() -> None:
        await rig.advance(1 * SEC)
        for entity_id, states in seen.items():
            state = rig.hass.states.get(entity_id)
            if state is not None and (not states or states[-1] != state.state):
                states.append(state.state)

    for _ in range(40):
        await second()
        if rig.state(CONTACT).state == "unavailable":
            break
    assert all(seen[entity_id][-1] == "unavailable" for entity_id in watched), "the reload"
    await rig.mqtt_reconnected()
    for _ in range(40):
        await second()
    assert seen[CONTACT] == ["on", "unavailable", "unknown", "on"]
    assert seen[TRVZB.climate] == ["off", "unavailable", "unknown", "off"]
    assert seen[WINDOW_OPEN] == ["on"], "the grace held the pause through the reload"
    assert rig.commands(TRVZB) == [], "no mode or target command"
    to_zigbee2mqtt = [
        (message.topic, message.payload.decode())
        for message in rig.sim.broker.log[log_start:]
        if message.sender == HOME_ASSISTANT_CLIENT and message.topic.startswith("zigbee2mqtt/")
    ]
    # The thermostat came back while its settings entities were still unknown (concept 5.5,
    # device settings): those are written once more, with the configured values. Nothing else.
    settings = {
        f"zigbee2mqtt/{TRVZB.name}/set/temperature_sensor_select": "remote_temperature",
        f"zigbee2mqtt/{TRVZB.name}/set/open_window": "OFF",
    }
    assert all(settings.get(topic) == payload for topic, payload in to_zigbee2mqtt), to_zigbee2mqtt
    # Not restored by Home Assistant (an MQTT sensor without expire_after is not), and not
    # recorded: the value comes from Zigbee2MQTT's republish, and it is the time the sensor was
    # last heard, not the time of the republish.
    last_seen = dt_util.parse_datetime(rig.state(LAST_SEEN).state)
    assert last_seen is not None
    heard = rig.sim.home.zigbee.last_heard(rig.sim.home.zigbee.devices[SENSOR_NAME])
    assert abs(last_seen.timestamp() - heard) < 1
    assert last_seen < rig.state(LAST_SEEN).last_reported - timedelta(seconds=30)
    assert not rig.problem()

    rig.sim.home.set_window(CONTACT_ID, False)
    await rig.advance(3 * MIN)
    rig.mark()
    for half_hour in range(1, 25):
        await rig.advance(30 * MIN)
        assert rig.room_runtime().sensor_fresh, f"fresh after {half_hour / 2} h"
    assert len(rig.writes(TRVZB.name, EXTERNAL)) >= 25, "the feed went on for twelve hours"


@pytest.mark.parametrize("device", devices("contact_starts_and_ends_window_pause"))
async def test_contact_starts_and_ends_window_pause(rig: Rig, device: str) -> None:
    """Concept 5.3, window pause: it starts when the contact has been open for window_open_delay
    (1 min) and ends when it has been closed for window_close_delay (2 min). Airing for less than
    a minute changes nothing and sends nothing."""
    await rig.start(TRVZB, sensor=False)
    rig.mark()
    rig.sim.home.set_window(CONTACT_ID, True)
    await rig.advance(30 * SEC)
    assert rig.state(CONTACT).state == "on"
    rig.sim.home.set_window(CONTACT_ID, False)
    await rig.advance(5 * MIN)
    assert rig.state(WINDOW_OPEN).state == "off"
    assert rig.commands(TRVZB) == []

    rig.sim.home.set_window(CONTACT_ID, True)
    await rig.advance(59 * SEC)
    assert rig.state(WINDOW_OPEN).state == "off"
    await rig.advance(5 * SEC)
    assert rig.state(WINDOW_OPEN).state == "on"
    assert rig.room() == (21.0, "comfort", "window")
    assert rig.commands(TRVZB) == OFF
    rig.sim.home.set_window(CONTACT_ID, False)
    await rig.advance(1 * MIN + 55 * SEC)
    assert rig.state(WINDOW_OPEN).state == "on"
    await rig.advance(10 * SEC)
    assert rig.state(WINDOW_OPEN).state == "off"
    assert rig.room() == (21.0, "comfort", "comfort")
    assert rig.commands(TRVZB) == OFF + heat(TRVZB, 21.0, with_mode=True)


@pytest.mark.parametrize("device", devices("contact_silent_while_open"))
async def test_contact_silent_while_open(rig: Rig, device: str) -> None:
    """Concept 5.4: a contact that goes silent while open keeps reading open until Zigbee2MQTT
    declares it unavailable (25 hours by default; 2 hours here, set through Zigbee2MQTT's
    passive timeout to keep the test short). From then the pause continues for
    window_unavailable_grace (30 min) and ends, and the problem sensor turns on. When the contact
    answers again (closed), the problem clears."""
    await _contact_silent_while_open_until_the_grace_ends(rig)
    rig.sim.home.set_silent(CONTACT_ID, False)
    await rig.advance(3 * SEC)
    assert rig.state(CONTACT).state == "off"
    assert not rig.problem()


async def _contact_silent_while_open_until_the_grace_ends(rig: Rig) -> None:
    await rig.start(TRVZB, sensor=False, zigbee2mqtt={"passive_timeout_minutes": 120})
    rig.sim.home.set_window(CONTACT_ID, True)
    await rig.advance(2 * MIN)
    assert rig.state(WINDOW_OPEN).state == "on"
    rig.mark()
    rig.sim.home.set_silent(CONTACT_ID, True)
    rig.sim.home.set_window(CONTACT_ID, False)  # closed, but nobody hears it
    await rig.advance(2 * HOUR - 1 * MIN)
    assert rig.state(CONTACT).state == "on", "still available, still open"
    assert rig.state(WINDOW_OPEN).state == "on"
    await rig.advance(1 * MIN + 5 * SEC)
    assert rig.state(CONTACT).state == "unavailable"
    assert rig.state(WINDOW_OPEN).state == "on", "the grace runs"
    await rig.advance(29 * MIN)
    assert rig.state(WINDOW_OPEN).state == "on"
    assert rig.commands(TRVZB) == []
    await rig.advance(1 * MIN + 5 * SEC)
    assert rig.state(WINDOW_OPEN).state == "off"
    assert rig.problem()
    assert rig.commands(TRVZB) == heat(TRVZB, 21.0, with_mode=True)


@pytest.mark.parametrize("device", devices("contact_returns_closed_after_grace"))
async def test_contact_returns_closed_after_grace(rig: Rig, device: str) -> None:
    """Concept 5.3 and 5.4: after the grace has ended the pause, the contact answers again and
    reports the window closed. The window pause starts when a contact has been open for
    window_open_delay; a closed contact starts nothing, so nothing changes and nothing is sent.

    Zigbee2MQTT publishes the device's availability before its new state, so Home Assistant
    first shows the contact with its last known state (open) and at the same instant the new one
    (closed); a real Zigbee2MQTT does the same (``onLastSeenChanged`` comes before the publish).
    """
    await _contact_silent_while_open_until_the_grace_ends(rig)
    rig.mark()
    rig.sim.home.set_silent(CONTACT_ID, False)
    await rig.advance(3 * MIN)
    assert rig.state(CONTACT).state == "off"
    assert rig.state(WINDOW_OPEN).state == "off"
    assert rig.commands(TRVZB) == []
