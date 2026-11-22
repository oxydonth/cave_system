"""The transcription of Zigbee2MQTT's discovery, the fixtures and the recorder."""

import json
import shutil
from pathlib import Path

from cave_sim.clock import ManualClock
from cave_sim.home import parse_home
from cave_sim.mqtt import InMemoryBroker
from cave_sim.runtime import SimulatedHome
from cave_sim.zigbee import fixtures
from cave_sim.zigbee.discovery import DeviceInfo, Settings, discover_device, serialize
from cave_sim.zigbee.models import BTH_RA, SNZB_04P, TRVZB
from cave_sim.zigbee.record import Recorder

IEEE = "0x00158d0000000102"
DEVICE = {
    "identifiers": [f"zigbee2mqtt_{IEEE}"],
    "manufacturer": "SONOFF",
    "model": "Zigbee thermostatic radiator valve",
    "model_id": "TRVZB",
    "name": "Wohnzimmer Heizung Tuer",
    "sw_version": "Zigbee2MQTT 2.14.3",
    "via_device": "zigbee2mqtt_bridge_0x00124b0000000001",
}
AVAILABILITY = [
    {"topic": "zigbee2mqtt/bridge/state", "value_template": "{{ value_json.state }}"},
    {
        "topic": "zigbee2mqtt/Wohnzimmer Heizung Tuer/availability",
        "value_template": "{{ value_json.state }}",
    },
]
ORIGIN = {"name": "Zigbee2MQTT", "sw": "2.14.3", "url": "https://www.zigbee2mqtt.io"}


def _trvzb() -> dict[str, dict[str, object]]:
    return discover_device(DeviceInfo("Wohnzimmer Heizung Tuer", IEEE, TRVZB), Settings())


def test_the_trvzb_climate_payload_is_what_zigbee2mqtt_publishes() -> None:
    """Worked out by hand from homeassistant.ts at 2.14.3 for the TRVZB definition."""
    base = "zigbee2mqtt/Wohnzimmer Heizung Tuer"
    assert _trvzb()[f"climate/{IEEE}/climate/config"] == {
        "name": None,
        "temperature_unit": "C",
        "temp_step": 0.5,
        "min_temp": "4",
        "max_temp": "35",
        "current_temperature_topic": base,
        "current_temperature_template": (
            '{% if "local_temperature" in value_json %}'
            '{{ value_json["local_temperature"] }}{% endif %}'
        ),
        "mode_state_topic": base,
        "mode_state_template": (
            '{% if "system_mode" in value_json %}{{ value_json["system_mode"] }}{% endif %}'
        ),
        "modes": ["off", "auto", "heat"],
        "mode_command_topic": f"{base}/set/system_mode",
        "action_topic": base,
        "action_template": (
            '{% if "running_state" in value_json %}'
            "{% set values = {None:None,'idle':'idle','heat':'heating','cool':'cooling',"
            "'fan_only':'fan'} %}{{ values[value_json[\"running_state\"]] }}{% endif %}"
        ),
        "temperature_command_topic": f"{base}/set/occupied_heating_setpoint",
        "temperature_state_template": (
            '{% if "occupied_heating_setpoint" in value_json %}'
            '{{ value_json["occupied_heating_setpoint"] }}{% endif %}'
        ),
        "temperature_state_topic": base,
        "object_id": "wohnzimmer_heizung_tuer",
        "default_entity_id": "climate.wohnzimmer_heizung_tuer",
        "unique_id": f"{IEEE}_climate_zigbee2mqtt",
        "device": DEVICE,
        "origin": ORIGIN,
        "availability_mode": "all",
        "availability": AVAILABILITY,
    }


def test_a_settable_binary_becomes_a_switch_named_after_the_property() -> None:
    switch = _trvzb()[f"switch/{IEEE}/switch_open_window/config"]
    assert switch["default_entity_id"] == "switch.wohnzimmer_heizung_tuer_open_window"
    assert switch["unique_id"] == f"{IEEE}_switch_open_window_zigbee2mqtt"
    assert switch["command_topic"] == "zigbee2mqtt/Wohnzimmer Heizung Tuer/set/open_window"
    assert (switch["payload_on"], switch["payload_off"]) == ("ON", "OFF")
    assert switch["entity_category"] == "config"


def test_the_external_temperature_keeps_its_device_class_and_drops_its_name() -> None:
    number = _trvzb()[f"number/{IEEE}/external_temperature_input/config"]
    assert number["device_class"] == "temperature"
    assert "name" not in number
    assert (number["min"], number["max"], number["step"]) == (0, 99.9, 0.1)
    assert number["entity_category"] == "config"


def test_the_bosch_mode_goes_through_operating_mode() -> None:
    messages = discover_device(DeviceInfo("Bosch", "0x0000000000000003", BTH_RA), Settings())
    climate = messages["climate/0x0000000000000003/climate/config"]
    assert climate["mode_command_topic"] == "zigbee2mqtt/Bosch/set"
    assert climate["modes"] == ["off", "heat", "auto"]
    assert "operating_mode" in climate["mode_command_template"]


def test_the_contact_is_a_door_binary_sensor_with_boolean_payloads() -> None:
    messages = discover_device(DeviceInfo("Fenster", "0x0000000000000005", SNZB_04P), Settings())
    contact = messages["binary_sensor/0x0000000000000005/contact/config"]
    assert contact["device_class"] == "door"
    assert (contact["payload_on"], contact["payload_off"]) == (False, True)


def test_serialisation_is_compact_sorted_and_prints_integers_without_a_fraction() -> None:
    assert serialize({"b": 4.0, "a": "°C", "c": [0.5, None]}) == '{"a":"°C","b":4,"c":[0.5,null]}'


def test_the_shipped_fixtures_are_what_the_models_generate() -> None:
    assert fixtures.main(["--check"]) == 0


def test_placeholders_round_trip() -> None:
    identity = fixtures.Identity("Wohnzimmer Heizung Tuer", IEEE, "0x00124b0000000001")
    messages = _trvzb()
    assert fixtures.from_placeholders(fixtures.to_placeholders(messages, identity), identity) == (
        messages
    )


def test_a_recorded_fixture_replaces_the_generated_one(tmp_path: Path) -> None:
    """The simulator publishes exactly what a recording holds."""
    for path in fixtures.FIXTURE_DIR.glob("*.json"):
        shutil.copy(path, tmp_path)
    recorded = json.loads((tmp_path / "SNZB-04P.json").read_text())
    recorded["source"] = "recorded: a test"
    topic = "binary_sensor/@IEEE@/contact/config"
    recorded["discovery"][topic]["icon"] = "mdi:recorded"
    recorded["state"]["voltage"] = 2900
    (tmp_path / "SNZB-04P.json").write_text(json.dumps(recorded))
    home = parse_home(
        {"rooms": [{"id": "r", "devices": [{"model": "SNZB-04P", "name": "Fenster"}]}]}
    )
    broker = InMemoryBroker()
    simulated = SimulatedHome(
        home,
        ManualClock(),
        broker.client("sim"),
        fritz_username="u",
        fritz_password="p",
        fixture_dir=tmp_path,
    )
    simulated.start()
    ieee = home.rooms[0].devices[0].ieee_address
    payload = json.loads(
        broker.retained[f"homeassistant/binary_sensor/{ieee}/contact/config"].payload
    )
    assert payload["icon"] == "mdi:recorded"
    assert payload["state_topic"] == "zigbee2mqtt/Fenster"
    assert json.loads(broker.messages("zigbee2mqtt/Fenster")[-1].payload)["voltage"] == 2900
    # A recording is never overwritten by the generator.
    assert fixtures.load("SNZB-04P", tmp_path).source == "recorded: a test"


def test_the_recorder_turns_a_device_s_messages_into_a_fixture() -> None:
    """Record the simulator itself: the result must equal the generated fixture.

    A fixture holds what the device's definition yields, before Zigbee2MQTT's ``device_options``,
    so the recording is made without them.
    """
    home = parse_home(
        {
            "zigbee2mqtt": {"device_options": {}},
            "rooms": [
                {
                    "id": "r",
                    "devices": [
                        {
                            "model": "TRVZB",
                            "name": "Fixture Device",
                            "ieee": fixtures.CANONICAL_IEEE,
                        }
                    ],
                }
            ]
        }
    )
    broker = InMemoryBroker()
    recorder = Recorder(broker.client("recorder"), "Fixture Device")
    SimulatedHome(
        home, ManualClock(), broker.client("sim"), fritz_username="u", fritz_password="p"
    ).start()

    recorded = recorder.fixture("TRVZB", "the simulator")
    generated = fixtures.load("TRVZB")
    assert recorded.source == "recorded: the simulator"
    assert recorded.discovery == generated.discovery
    assert recorded.state["system_mode"] == "heat"
    assert recorded.state["last_seen"] == "2026-01-01T00:00:00.000Z"


def test_a_recording_that_disagrees_with_its_model_is_reported() -> None:
    generated = fixtures.load("TRV-ZBT")
    discovery = json.loads(json.dumps(generated.discovery))
    climate = "climate/@IEEE@/climate/config"
    select = "select/@IEEE@/temperature_sensor_select/config"
    discovery[climate]["min_temp"] = "4"
    discovery[select]["options"] = ["internal", "external"]
    del discovery["switch/@IEEE@/switch_open_window/config"]
    recorded = fixtures.Fixture("TRV-ZBT", "recorded: a test", discovery, generated.state)

    problems = fixtures.differences(recorded, generated)

    assert len(problems) == 3
    assert any("min_temp is '4' on the real device but '5'" in p for p in problems)
    assert any("options" in p and "temperature_sensor_select" in p for p in problems)
    assert any("switch_open_window" in p and "remove it" in p for p in problems)
    assert all("models.py, model TRV-ZBT" in p for p in problems)
    assert fixtures.differences(generated, generated) == []
