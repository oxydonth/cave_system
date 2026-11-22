"""When a room sensor counts as stale (concept section 5.4), with Zigbee2MQTT's last_seen sensor.

The room sensor here is a device as Zigbee2MQTT's discovery registers it: a temperature entity
and, on the same device, an MQTT ``last_seen`` sensor with Zigbee2MQTT's unique ID. Its states
are written by hand, so the tests say exactly what Home Assistant would show (issue #21).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.cave_heating.fakes import FakeHome, FakeNumber, config, room

CONFIGURATION = Path(__file__).resolve().parents[2] / "homeassistant" / "configuration.yaml"

SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
LIVING = "climate.cave_living_room"
IEEE = "0x00158d0000000103"
LAST_SEEN_ID = f"{IEEE}_last_seen_zigbee2mqtt"


def _iso(moment: datetime) -> str:
    """A timestamp sensor's state."""
    return moment.isoformat(timespec="seconds")


def _sensor_device(
    hass: HomeAssistant,
    *,
    last_seen_disabled_by: er.RegistryEntryDisabler | None = None,
    last_seen: bool = True,
) -> tuple[str, str, str]:
    """A Zigbee2MQTT room sensor: temperature, humidity and (optionally) last_seen."""
    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("mqtt", f"zigbee2mqtt_{IEEE}")}
    )
    registry = er.async_get(hass)

    def create(unique_id: str, object_id: str, **extra: object) -> str:
        return registry.async_get_or_create(
            "sensor",
            "mqtt",
            unique_id,
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=entry,
            **extra,  # type: ignore[arg-type]
        ).entity_id

    temperature = create(f"{IEEE}_temperature_zigbee2mqtt", "wohnzimmer_temperature")
    humidity = create(f"{IEEE}_humidity_zigbee2mqtt", "wohnzimmer_humidity")
    seen = ""
    if last_seen:
        seen = create(LAST_SEEN_ID, "wohnzimmer_last_seen", disabled_by=last_seen_disabled_by)
    return temperature, humidity, seen


async def _room(home: FakeHome, temperature: str) -> None:
    await home.thermostat("climate.a", target=21.0, current=23.0)
    await home.start(config(room("living_room", "climate.a", temperature_sensor=temperature)))


def _shown(home: FakeHome) -> float:
    return float(home.state(LIVING).attributes["current_temperature"])


def test_the_recorder_leaves_out_every_last_seen_and_nothing_the_heating_shows() -> None:
    """homeassistant/configuration.yaml: ``last_seen`` changes with every message of every
    device, so it is not recorded; the controller reads only its current value. The room
    sensor's readings and the thermostats keep their history."""

    class Loader(yaml.SafeLoader):
        """Home Assistant's ``!include`` tags are read as nothing: only ``recorder`` matters."""

    Loader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)
    configuration = yaml.load(CONFIGURATION.read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506
    section = configuration["recorder"]
    conf = recorder.CONFIG_SCHEMA({"recorder": section})["recorder"]
    recorded = convert_include_exclude_filter(conf).get_filter()
    for entity_id in ("sensor.wohnzimmer_last_seen", "sensor.wohnzimmer_heizung_tuer_last_seen"):
        assert not recorded(entity_id), entity_id
    for entity_id in (
        "sensor.wohnzimmer_temperature",
        "sensor.wohnzimmer_humidity",
        "climate.wohnzimmer_heizung_tuer",
        "climate.cave_living_room",
        "binary_sensor.wohnzimmer_fenster_contact",
    ):
        assert recorded(entity_id), entity_id


async def test_a_sensor_whose_values_repeat_stays_fresh_while_last_seen_moves(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """F4: no entity of the device records a report, but last_seen changes every hour."""
    temperature, humidity, seen = _sensor_device(hass)
    home.set(temperature, "20.4")
    home.set(humidity, "50")
    home.set(seen, _iso(home.now()))
    await _room(home, temperature)
    for _ in range(12):
        await home.advance(freezer, HOUR, step=HOUR)
        home.set(seen, _iso(home.now()))
        await home.settle()
    assert _shown(home) == 20.4
    await home.advance(freezer, 3 * HOUR, step=HOUR)
    assert _shown(home) == 23.0, "stale three hours after the last message"


async def test_after_a_restart_the_value_of_last_seen_counts_not_the_time_of_the_write(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A restart writes every entity again; a sensor last heard from 4 h ago is stale at once."""
    temperature, humidity, seen = _sensor_device(hass)
    home.set(temperature, "20.4")
    home.set(humidity, "50")
    home.set(seen, _iso(home.now() - 4 * HOUR))
    await _room(home, temperature)
    assert _shown(home) == 23.0, "stale, although every entity was just written"
    home.set(seen, _iso(home.now()))
    await home.settle()
    assert _shown(home) == 20.4, "the device reported"


async def test_last_seen_is_capped_at_the_time_it_was_received(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A last_seen in the future (a clock that jumped) counts from when it arrived."""
    temperature, humidity, seen = _sensor_device(hass)
    home.set(temperature, "20.4")
    home.set(humidity, "50")
    home.set(seen, _iso(home.now() + 10 * HOUR))
    await _room(home, temperature)
    assert _shown(home) == 20.4
    await home.advance(freezer, 3 * HOUR, step=HOUR)
    assert _shown(home) == 23.0


@pytest.mark.parametrize("seen_state", ["unknown", "unavailable", "not a time"])
async def test_without_a_usable_last_seen_the_newest_report_of_the_device_counts(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, seen_state: str
) -> None:
    """Before Zigbee2MQTT has sent last_seen, the rule of the device's reports applies."""
    temperature, humidity, seen = _sensor_device(hass)
    home.set(temperature, "20.4")
    home.set(humidity, "50")
    home.set(seen, seen_state)
    await _room(home, temperature)
    for step in range(8):
        await home.advance(freezer, 30 * MIN)
        home.set(humidity, str(51 + step))
    assert _shown(home) == 20.4, "the humidity keeps the device fresh"
    await home.advance(freezer, 3 * HOUR, step=HOUR)
    assert _shown(home) == 23.0


@pytest.mark.parametrize(
    ("disabled_by", "enabled"),
    [
        (er.RegistryEntryDisabler.INTEGRATION, True),
        (er.RegistryEntryDisabler.USER, False),
        (None, True),
    ],
)
async def test_a_room_sensor_s_last_seen_disabled_by_its_integration_is_enabled(
    hass: HomeAssistant,
    home: FakeHome,
    disabled_by: er.RegistryEntryDisabler | None,
    enabled: bool,
) -> None:
    """Registered disabled before compose.yaml enabled it by default: enabled at the start.

    One the owner disabled stays disabled.
    """
    temperature, _humidity, seen = _sensor_device(hass, last_seen_disabled_by=disabled_by)
    home.set(temperature, "20.4")
    await _room(home, temperature)
    entry = er.async_get(hass).async_get(seen)
    assert entry is not None
    assert (entry.disabled_by is None) is enabled
    if not enabled:
        assert entry.disabled_by is er.RegistryEntryDisabler.USER


async def test_only_the_last_seen_of_a_room_sensor_is_enabled(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Another device's last_seen, and an entity that only looks like one, are left alone."""
    temperature, _humidity, _seen = _sensor_device(hass, last_seen=False)
    registry = er.async_get(hass)
    other_entry = MockConfigEntry(domain="mqtt")
    other_entry.add_to_hass(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id, identifiers={("mqtt", "zigbee2mqtt_other")}
    )
    elsewhere = registry.async_get_or_create(
        "sensor",
        "mqtt",
        "0x00158d0000000999_last_seen_zigbee2mqtt",
        device_id=other.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    sensor_device = registry.async_get(temperature)
    assert sensor_device is not None
    lookalike = registry.async_get_or_create(
        "sensor",
        "template",
        LAST_SEEN_ID,
        device_id=sensor_device.device_id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    # An MQTT entity whose object ID only starts with last_seen_: its unique ID does not end in
    # the room sensor's base topic after "_last_seen_".
    prefixed = registry.async_get_or_create(
        "sensor",
        "mqtt",
        f"{IEEE}_last_seen_extra_zigbee2mqtt",
        device_id=sensor_device.device_id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    home.set(temperature, "20.4")
    await _room(home, temperature)
    for entry in (elsewhere, lookalike, prefixed):
        assert registry.async_get(entry.entity_id).disabled_by is not None  # type: ignore[union-attr]


async def test_a_last_seen_registered_disabled_after_the_start_is_enabled(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A device paired after the file named its sensor gains its entities one by one."""
    temperature, _humidity, _seen = _sensor_device(hass, last_seen=False)
    home.set(temperature, "20.4")
    await _room(home, temperature)
    registry = er.async_get(hass)
    sensor_entry = registry.async_get(temperature)
    assert sensor_entry is not None
    late = registry.async_get_or_create(
        "sensor",
        "mqtt",
        LAST_SEEN_ID,
        device_id=sensor_entry.device_id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    home.set("climate.a", "heat", temperature=21.0, current_temperature=23.0)
    await home.advance(freezer, 5 * MIN)
    assert registry.async_get(late.entity_id).disabled_by is None  # type: ignore[union-attr]


async def test_the_subscriptions_follow_the_room_sensor_s_device_when_it_gains_entities(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Issue #13: a sibling the device gained after the start brings a stale sensor back at once.

    The sensor's device has only its temperature at the start; a humidity entity is added later.
    Once the sensor is stale, the new entity's report must make the controller look again at
    once: it is subscribed to, not only read at the next timer.
    """
    temperature, _humidity, _seen = _sensor_device(hass, last_seen=False)
    registry = er.async_get(hass)
    registry.async_remove(_humidity)
    home.set(temperature, "20.4")
    await _room(home, temperature)
    sensor_entry = registry.async_get(temperature)
    assert sensor_entry is not None
    added = registry.async_get_or_create(
        "sensor", "mqtt", f"{IEEE}_battery_zigbee2mqtt", device_id=sensor_entry.device_id
    ).entity_id
    home.set(added, "90")
    await home.advance(freezer, 3 * HOUR + MIN, step=HOUR)
    assert _shown(home) == 23.0, "stale"
    home.set(added, "89")
    await home.settle()
    assert _shown(home) == 20.4, "the new sibling's report counted at once"


async def test_a_message_whose_last_seen_is_written_first_feeds_its_new_temperature(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Home Assistant writes the entities of one message one after the other.

    A stale sensor reports again with a new temperature. When its last_seen is written before
    its temperature, the feed must write the new temperature, not the one from before.
    """
    temperature, humidity, seen = _sensor_device(hass)
    await home.thermostat("climate.a", target=21.0, current=23.0)
    feed_input = FakeNumber("number.a_external", 0.0)
    await home.add(feed_input)
    home.set(temperature, "20.4")
    home.set(humidity, "50")
    home.set(seen, _iso(home.now()))
    await home.start(
        config(
            room(
                "living_room",
                {"entity": "climate.a", "external_temperature": "number.a_external"},
                temperature_sensor=temperature,
            )
        )
    )
    await home.settle()
    assert feed_input.writes == [20.4]
    # Past sensor_stale, and past feed_min_interval since the feed's last write.
    await home.advance(freezer, 3 * HOUR + 15 * MIN, step=HOUR)
    assert _shown(home) == 23.0, "stale"
    before = len(feed_input.writes)
    home.set(seen, _iso(home.now()))
    home.set(temperature, "21.6")
    await home.settle()
    assert feed_input.writes[before:] == [21.6]
    assert _shown(home) == 21.6


@pytest.mark.parametrize(
    "order",
    [
        ("temperature", "humidity", "last_seen"),
        ("humidity", "temperature", "last_seen"),
        ("temperature", "last_seen", "humidity"),
        ("last_seen", "temperature", "humidity"),
    ],
    ids="-".join,
)
@pytest.mark.parametrize("humidity_in_the_room", [False, True], ids=["temperature-only", "both"])
async def test_a_sensor_dead_before_a_restart_stays_stale_through_the_republish(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    order: tuple[str, ...],
    humidity_in_the_room: bool,
) -> None:
    """After a restart every entity of the room sensor is unknown until Zigbee2MQTT sends its
    states again, 30 s after Home Assistant's birth message. The sensor died 4 h before.

    That one message carries its old temperature, its old humidity and its old last_seen, and
    Home Assistant writes them in an order it does not fix. Whichever comes first, and whether
    the room uses the humidity or not, the room stays on the thermostat's reading and the feed
    does not write the dead sensor's value. (From the second review of pull request #29.)
    """
    temperature, humidity, seen = _sensor_device(hass)
    await home.thermostat("climate.a", target=21.0, current=23.0)
    feed_input = FakeNumber("number.a_external", 0.0)
    await home.add(feed_input)
    for entity_id in (temperature, humidity, seen):
        home.set(entity_id, "unknown")
    fields = {"temperature_sensor": temperature}
    if humidity_in_the_room:
        fields["humidity_sensor"] = humidity
    await home.start(
        config(
            room(
                "living_room",
                {"entity": "climate.a", "external_temperature": "number.a_external"},
                **fields,
            )
        )
    )
    await home.advance(freezer, 30 * SEC)
    assert _shown(home) == 23.0
    values = {
        "temperature": (temperature, "19.0"),
        "humidity": (humidity, "50"),
        "last_seen": (seen, _iso(home.now() - 4 * HOUR)),
    }

    for name in order:  # the republish: one message, written without giving up control
        home.set(*values[name])
    await home.settle()
    await home.advance(freezer, 5 * SEC)

    assert _shown(home) == 23.0, "stale at once"
    assert feed_input.writes == [], "the dead sensor's value is not fed"
