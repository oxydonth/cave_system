"""The Zigbee2MQTT stand-in on an in-process broker, with a manual clock."""

import json
from typing import Any

import pytest

from cave_sim.clock import ManualClock
from cave_sim.home import parse_home
from cave_sim.mqtt import InMemoryBroker
from cave_sim.runtime import ControlError, SimulatedHome
from cave_sim.zigbee.devices import BoschTrv, SonoffTrv

HOME = parse_home(
    {
        "outdoor_temperature": 5.0,
        "rooms": [
            {
                "id": "room",
                "temperature": 19.0,
                "devices": [
                    {"model": "TRVZB", "name": "Sonoff", "ieee": "0x0000000000000001"},
                    {"model": "TRV-ZBT", "name": "Gen Two", "ieee": "0x0000000000000002"},
                    {"model": "BTH-RA", "name": "Bosch", "ieee": "0x0000000000000003"},
                    {"model": "SNZB-02P", "name": "Sensor", "ieee": "0x0000000000000004"},
                    {"model": "SNZB-04P", "name": "Window", "ieee": "0x0000000000000005"},
                ],
            }
        ],
    }
)


class Rig:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.broker = InMemoryBroker()
        self.home = SimulatedHome(
            HOME, self.clock, self.broker.client("sim"), fritz_username="u", fritz_password="p"
        )
        self.ha = self.broker.client("ha")
        self.home.start()

    def states(self, name: str) -> list[dict[str, Any]]:
        return [json.loads(m.payload) for m in self.broker.messages(f"zigbee2mqtt/{name}")]

    def last(self, name: str) -> dict[str, Any]:
        return self.states(name)[-1]

    def set(self, name: str, payload: str, attribute: str | None = None) -> None:
        topic = f"zigbee2mqtt/{name}/set" + (f"/{attribute}" if attribute else "")
        self.ha.publish(topic, payload)


@pytest.fixture
def rig() -> Rig:
    return Rig()


def test_start_publishes_bridge_discovery_availability_and_state(rig: Rig) -> None:
    retained = rig.broker.retained
    assert json.loads(retained["zigbee2mqtt/bridge/state"].payload) == {"state": "online"}
    assert json.loads(retained["zigbee2mqtt/Sonoff/availability"].payload) == {"state": "online"}
    climate = json.loads(
        retained["homeassistant/climate/0x0000000000000001/climate/config"].payload
    )
    assert climate["default_entity_id"] == "climate.sonoff"
    assert (
        climate["temperature_command_topic"] == "zigbee2mqtt/Sonoff/set/occupied_heating_setpoint"
    )
    state = rig.last("Sonoff")
    assert state["local_temperature"] == 19.0
    assert state["last_seen"] == "2026-01-01T00:00:00.000Z"
    assert state["update"] == {"installed_version": -1, "latest_version": -1}
    # State messages are not retained, discovery and availability are.
    assert "zigbee2mqtt/Sonoff" not in retained


def test_a_setpoint_is_published_as_sent_and_then_as_the_device_rounded_it(rig: Rig) -> None:
    rig.set("Sonoff", "21.3", "occupied_heating_setpoint")
    published = [s["occupied_heating_setpoint"] for s in rig.states("Sonoff")]
    assert published[published.index(21.3) + 1] == 21.5
    assert rig.home.zigbee.devices["Sonoff"].state["occupied_heating_setpoint"] == 21.5


def test_a_json_set_message_with_several_keys(rig: Rig) -> None:
    rig.set("Gen Two", json.dumps({"system_mode": "off", "child_lock": "LOCK"}))
    state = rig.last("Gen Two")
    assert state["system_mode"] == "off"
    assert state["child_lock"] == "LOCK"


@pytest.mark.parametrize(
    ("payload", "attribute"),
    [
        ("40", "occupied_heating_setpoint"),  # above the device's maximum of 35 °C
        ("warm", "system_mode"),  # not one of the modes
        ("1", "no_such_property"),  # no converter
    ],
)
def test_an_invalid_command_changes_nothing(rig: Rig, payload: str, attribute: str) -> None:
    rig.clock.advance(60)  # let the valve settle, so that nothing else is reported
    before = rig.states("Sonoff")
    rig.set("Sonoff", payload, attribute)
    assert rig.states("Sonoff") == before


def test_an_offline_device_publishes_unavailability_and_ignores_commands(rig: Rig) -> None:
    rig.home.set_online("sonoff", False)
    assert json.loads(rig.broker.retained["zigbee2mqtt/Sonoff/availability"].payload) == {
        "state": "offline"
    }
    before = len(rig.states("Sonoff"))
    rig.set("Sonoff", "25", "occupied_heating_setpoint")
    rig.clock.advance(3600)
    assert len(rig.states("Sonoff")) == before
    rig.home.set_online("sonoff", True)
    assert json.loads(rig.broker.retained["zigbee2mqtt/Sonoff/availability"].payload) == {
        "state": "online"
    }
    assert rig.last("Sonoff")["occupied_heating_setpoint"] == 20.0


def test_sonoff_falls_back_after_two_hours_without_external_temperature(rig: Rig) -> None:
    rig.set("Sonoff", "remote_temperature", "temperature_sensor_select")
    rig.set("Sonoff", "21.4", "external_temperature_input")
    sonoff = rig.home.zigbee.devices["Sonoff"]
    assert isinstance(sonoff, SonoffTrv)
    assert rig.last("Sonoff")["local_temperature"] == 21.4

    rig.clock.advance(2 * 3600 - 60)
    assert sonoff.state["temperature_sensor_select"] == "remote_temperature"
    rig.clock.advance(60)
    assert sonoff.state["temperature_sensor_select"] == "remote_source_offline"
    assert rig.last("Sonoff")["local_temperature"] != 21.4

    # The next value brings it back by itself (zigbee-herdsman-converters PR 8873).
    rig.set("Sonoff", "21.0", "external_temperature_input")
    assert sonoff.state["temperature_sensor_select"] == "remote_temperature"
    assert rig.last("Sonoff")["local_temperature"] == 21.0


def test_a_sonoff_that_does_not_return_by_itself(rig: Rig) -> None:
    rig.home.set_returns_on_feed("sonoff", False)
    rig.set("Sonoff", "remote_temperature", "temperature_sensor_select")
    rig.set("Sonoff", "21.4", "external_temperature_input")
    rig.clock.advance(2 * 3600)
    rig.set("Sonoff", "21.0", "external_temperature_input")
    assert rig.home.zigbee.devices["Sonoff"].state["temperature_sensor_select"] == (
        "remote_source_offline"
    )


def test_feeding_the_sonoff_keeps_it_on_the_external_sensor(rig: Rig) -> None:
    rig.set("Sonoff", "remote_temperature", "temperature_sensor_select")
    for _ in range(6):
        rig.set("Sonoff", "21.4", "external_temperature_input")
        rig.clock.advance(3600)
    assert rig.home.zigbee.devices["Sonoff"].state["temperature_sensor_select"] == (
        "remote_temperature"
    )


def test_bosch_uses_the_remote_temperature_for_thirty_minutes(rig: Rig) -> None:
    bosch = rig.home.zigbee.devices["Bosch"]
    assert isinstance(bosch, BoschTrv)
    rig.set("Bosch", "23.2", "remote_temperature")
    assert bosch.regulation_temperature() == 23.2
    rig.clock.advance(29 * 60)
    assert bosch.regulation_temperature() == 23.2
    rig.clock.advance(60)
    assert bosch.regulation_temperature() != 23.2


def test_bosch_mode_off_is_operating_mode_pause(rig: Rig) -> None:
    rig.set("Bosch", json.dumps({"operating_mode": "pause"}))
    bosch = rig.home.zigbee.devices["Bosch"]
    rig.clock.advance(60)
    assert rig.last("Bosch")["operating_mode"] == "pause"
    assert bosch.state["system_mode"] == "heat"
    assert isinstance(bosch, BoschTrv)
    assert bosch.opening == 0.0


def test_bosch_boost_ends_after_five_minutes(rig: Rig) -> None:
    rig.set("Bosch", "ON", "boost_heating")
    rig.clock.advance(60)
    bosch = rig.home.zigbee.devices["Bosch"]
    assert isinstance(bosch, BoschTrv)
    assert bosch.opening == 1.0
    rig.clock.advance(4 * 60)
    assert rig.last("Bosch")["boost_heating"] == "OFF"


def test_bosch_window_detection_closes_the_valve(rig: Rig) -> None:
    rig.set("Bosch", "25", "occupied_heating_setpoint")
    rig.clock.advance(60)
    bosch = rig.home.zigbee.devices["Bosch"]
    assert isinstance(bosch, BoschTrv)
    assert bosch.opening == 1.0
    rig.set("Bosch", "ON", "window_detection")
    rig.clock.advance(60)
    assert bosch.opening == 0.0


def test_a_dial_turn_is_reported_rounded_and_bosch_names_the_source(rig: Rig) -> None:
    rig.home.turn_dial("bosch", 22.3)
    state = rig.last("Bosch")
    assert state["occupied_heating_setpoint"] == 22.5
    assert state["setpoint_change_source"] == "manual"
    rig.home.turn_dial("sonoff", 40)
    assert rig.last("Sonoff")["occupied_heating_setpoint"] == 35


def test_the_window_contact_reports_at_once(rig: Rig) -> None:
    rig.home.set_window("window", True)
    assert rig.last("Window")["contact"] is False
    rig.home.set_window("window", False)
    assert rig.last("Window")["contact"] is True


def test_the_room_sensor_reports_by_its_reporting_configuration(rig: Rig) -> None:
    rig.home.set_room_temperature("room", 19.5)  # less than the reportable change of 1 °C
    rig.clock.advance(59 * 60)
    assert rig.last("Sensor")["temperature"] == 19.0
    rig.clock.advance(60)  # the maximum interval of one hour is over
    assert rig.last("Sensor")["temperature"] != 19.0


def test_a_large_change_is_reported_after_the_minimum_interval(rig: Rig) -> None:
    rig.clock.advance(5)
    rig.home.set_room_temperature("room", 22.0)
    assert rig.last("Sensor")["temperature"] == 19.0
    rig.clock.advance(5)  # ten seconds after the last report
    assert rig.last("Sensor")["temperature"] == 22.0


def test_home_assistant_coming_online_makes_the_bridge_republish(rig: Rig) -> None:
    before = len(rig.states("Sensor"))
    rig.ha.publish("homeassistant/status", "online")
    rig.clock.advance(29)
    assert len(rig.states("Sensor")) == before
    rig.clock.advance(1)
    assert len(rig.states("Sensor")) == before + 1


def test_get_answers_with_the_current_value(rig: Rig) -> None:
    before = len(rig.states("Sonoff"))
    rig.ha.publish("zigbee2mqtt/Sonoff/get", json.dumps({"child_lock": ""}))
    assert len(rig.states("Sonoff")) == before + 1


def test_bridge_requests_are_answered(rig: Rig) -> None:
    replies: list[dict[str, Any]] = []
    rig.ha.subscribe("zigbee2mqtt/bridge/response/#", lambda _t, p: replies.append(json.loads(p)))
    rig.ha.publish("zigbee2mqtt/bridge/request/permit_join", '{"time": 254}')
    assert replies[-1]["status"] == "ok"
    assert json.loads(rig.broker.retained["zigbee2mqtt/bridge/info"].payload)["permit_join"]
    rig.ha.publish("zigbee2mqtt/bridge/request/device/ota_update/update", '{"id": "x"}')
    assert replies[-1]["status"] == "error"


def test_an_open_valve_warms_the_room_and_an_open_window_cools_it(rig: Rig) -> None:
    rig.set("Sonoff", "30", "occupied_heating_setpoint")
    rig.clock.advance(3600)
    warmed = rig.home.rooms["room"].temperature
    assert warmed > 19.5
    rig.set("Sonoff", "4", "occupied_heating_setpoint")
    rig.home.set_window("window", True)
    rig.clock.advance(1800)
    assert rig.home.rooms["room"].temperature < warmed - 3


def _availability(rig: Rig, name: str) -> str:
    return str(json.loads(rig.broker.retained[f"zigbee2mqtt/{name}/availability"].payload)["state"])


def test_availability_is_published_as_zigbee2mqtt_serialises_it(rig: Rig) -> None:
    assert rig.broker.retained["zigbee2mqtt/Sonoff/availability"].payload == b'{"state":"online"}'


def test_a_silent_thermostat_stays_available_and_ignores_commands(rig: Rig) -> None:
    rig.clock.advance(60)
    rig.home.set_silent("sonoff", True)
    before = rig.states("Sonoff")
    rig.set("Sonoff", "25", "occupied_heating_setpoint")
    rig.ha.publish("zigbee2mqtt/Sonoff/get", json.dumps({"child_lock": ""}))
    rig.clock.advance(3600)
    assert rig.states("Sonoff") == before  # nothing at all is published for it
    assert _availability(rig, "Sonoff") == "online"
    assert rig.home.zigbee.devices["Sonoff"].state["occupied_heating_setpoint"] == 20.0


def test_a_silent_device_turns_unavailable_after_the_passive_timeout(rig: Rig) -> None:
    rig.clock.advance(600)
    rig.home.set_silent("sensor", True)
    last = rig.last("Sensor")["last_seen"]
    rig.clock.advance(1500 * 60 - 1)
    assert _availability(rig, "Sensor") == "online"
    rig.clock.advance(2)
    assert _availability(rig, "Sensor") == "offline"
    assert rig.last("Sensor")["last_seen"] == last


def test_an_answering_device_never_times_out(rig: Rig) -> None:
    rig.clock.advance(30 * 3600)
    names = ("Sonoff", "Gen Two", "Bosch", "Sensor", "Window")
    assert {_availability(rig, name) for name in names} == {"online"}


def test_a_silent_room_sensor_goes_stale_while_available(rig: Rig) -> None:
    rig.home.set_silent("sensor", True)
    rig.home.set_room_temperature("room", 25.0)
    rig.clock.advance(4 * 3600)
    assert rig.last("Sensor")["temperature"] == 19.0
    assert _availability(rig, "Sensor") == "online"


def test_a_window_contact_that_goes_silent_while_open(rig: Rig) -> None:
    rig.home.set_window("window", True)
    rig.home.set_silent("window", True)
    rig.home.set_window("window", False)
    rig.clock.advance(24 * 3600)
    assert rig.last("Window")["contact"] is False  # Home Assistant still sees it open
    assert _availability(rig, "Window") == "online"
    rig.clock.advance(3600 + 60)
    assert _availability(rig, "Window") == "offline"
    rig.home.set_silent("window", False)
    assert rig.last("Window")["contact"] is True
    assert _availability(rig, "Window") == "online"


def test_a_dial_turned_while_silent_is_reported_on_return(rig: Rig) -> None:
    rig.home.set_silent("bosch", True)
    rig.home.turn_dial("bosch", 23.5)
    assert rig.last("Bosch")["occupied_heating_setpoint"] == 20.0
    rig.home.set_silent("bosch", False)
    state = rig.last("Bosch")
    assert state["occupied_heating_setpoint"] == 23.5
    assert state["setpoint_change_source"] == "manual"


def test_a_device_made_unavailable_at_once_and_back(rig: Rig) -> None:
    rig.home.set_online("gen_two", False)
    assert _availability(rig, "Gen Two") == "offline"
    rig.home.turn_dial("gen_two", 24)
    rig.home.set_online("gen_two", True)
    assert _availability(rig, "Gen Two") == "online"
    assert rig.last("Gen Two")["occupied_heating_setpoint"] == 24


def test_the_passive_timeout_is_configurable(rig: Rig) -> None:
    rig.home.configure_zigbee2mqtt(passive_timeout_minutes=5)
    rig.home.set_silent("sonoff", True)
    rig.clock.advance(5 * 60 + 1)
    assert _availability(rig, "Sonoff") == "offline"
    home = parse_home({"zigbee2mqtt": {"passive_timeout": 2}, "rooms": []})
    assert home.zigbee2mqtt.passive_timeout_minutes == 2


@pytest.mark.parametrize(
    ("device_id", "name", "mode", "prop"),
    [
        ("sonoff", "Sonoff", "off", "system_mode"),
        ("gen_two", "Gen Two", "auto", "system_mode"),
        ("bosch", "Bosch", "pause", "operating_mode"),
    ],
)
def test_a_mode_changed_on_the_device_is_reported(
    rig: Rig, device_id: str, name: str, mode: str, prop: str
) -> None:
    rig.home.set_device_mode(device_id, mode)
    assert rig.last(name)[prop] == mode


def test_an_unknown_device_mode_is_refused(rig: Rig) -> None:
    with pytest.raises(ControlError, match="system_mode"):
        rig.home.set_device_mode("sonoff", "pause")
    with pytest.raises(ControlError, match="thermostat"):
        rig.home.set_device_mode("sensor", "off")


def test_an_external_temperature_out_of_range_is_published_but_ignored(rig: Rig) -> None:
    rig.set("Sonoff", "remote_temperature", "temperature_sensor_select")
    rig.set("Sonoff", "120", "external_temperature_input")
    sonoff = rig.home.zigbee.devices["Sonoff"]
    assert isinstance(sonoff, SonoffTrv)
    assert sonoff.cache["external_temperature_input"] == 120  # Zigbee2MQTT's optimistic state
    assert sonoff.state["external_temperature_input"] == 0  # the device ignored it
    assert sonoff.external_received is None
