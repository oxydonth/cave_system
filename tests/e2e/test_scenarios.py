"""The heating scenarios of concept section 8 on the running demo: real images, simulated devices.

``./cave demo`` has started the stack with the simulators and the demo home's
``sim/demo/heating.yaml`` (timings in seconds). Each scenario acts the way a person or a device
would, through Home Assistant's API or the simulators' control API, and waits for the outcome
with a limit, never for a fixed time. Outcomes are checked at both ends: what the room thermostat
in Home Assistant decides, and what the simulated device then has, Zigbee and FRITZ! alike.

The tests run in the order written; each leaves the home as it found it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_socket

from cave_ops.commands.demo import PRESENCE_SWITCH
from custom_components.cave_heating.schema import load_file

COMFORT, ECO, AWAY, BOOST = 21.0, 17.5, 16.0, 25.0
TIMING = load_file(Path(__file__).parents[2] / "sim" / "demo" / "heating.yaml").timing
CONFIRM_TIMEOUT = TIMING.confirm_timeout.total_seconds()
MANUAL_SETTLE = TIMING.manual_settle.total_seconds()

# Room -> its thermostats: (Home Assistant entity, simulator device ID, family).
ROOMS = {
    "living_room": (
        ("climate.wohnzimmer_heizung_fenster", "wohnzimmer_heizung_fenster", "zigbee"),
        ("climate.wohnzimmer_heizung_tuer", "wohnzimmer_heizung_tuer", "zigbee"),
    ),
    "bedroom": (("climate.schlafzimmer_heizung", "schlafzimmer_heizung", "zigbee"),),
    "bathroom": (("climate.bad_heizung", "bad_heizung", "fritz"),),
}
SCHEDULES = {
    "living_room": "living_room_heating",
    "bedroom": "bedroom_heating",
    "bathroom": "bathroom_heating",
}
ALL_DAY = [{"from": "00:00:00", "to": "24:00:00"}]
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@pytest.fixture(scope="module", autouse=True)
def demo_stack(e2e_stack: str) -> None:
    if e2e_stack != "demo":
        pytest.skip("the scenarios need the demo: CAVE_E2E=demo")


class Home:
    """What the scenarios do and look at, on top of the stack's APIs."""

    def __init__(self, stack: Any, wait_until: Callable[..., Any]) -> None:
        self.ready = 0.0  # when the demo was settled: every room at comfort, all confirmed
        self.stack = stack
        self.wait_until = wait_until
        self.api = stack.api()

    # --- Home Assistant
    def state(self, entity_id: str) -> dict[str, Any]:
        state = self.api.get_state(entity_id)
        assert state is not None, f"{entity_id} does not exist"
        return state

    def call(self, domain: str, service: str, **data: Any) -> None:
        self.api.call_service(domain, service, data)

    def room(self, room: str) -> dict[str, Any]:
        return self.state(f"climate.cave_{room}")

    def reason(self, room: str) -> str:
        return str(self.state(f"sensor.cave_{room}_reason")["state"])

    def statuses(self, room: str) -> dict[str, str]:
        return dict(self.state(f"binary_sensor.cave_{room}_problem")["attributes"]["thermostats"])

    def poll_fritzbox(self) -> None:
        """Make Home Assistant ask the FRITZ!Box now instead of at its next 30 s poll."""
        self.call("homeassistant", "update_entity", entity_id="climate.bad_heizung")

    def set_schedule(self, room: str, on: bool) -> None:
        """The room's schedule on or off all week, as an edit in the UI would make it."""
        (items,) = self.stack.ws(("schedule/list", {}))
        item = next(i for i in items if i["id"] == SCHEDULES[room])
        days = {day: ALL_DAY if on else [] for day in DAYS}
        self.stack.ws(
            (
                "schedule/update",
                {"schedule_id": item["id"], "name": item["name"], "icon": item["icon"], **days},
            )
        )
        self.wait_until(
            lambda: self.state(f"schedule.{SCHEDULES[room]}")["state"] == ("on" if on else "off"),
            f"schedule.{SCHEDULES[room]} to be {'on' if on else 'off'}",
            10,
        )

    # --- the simulated devices
    def device_target(self, device: str) -> object:
        """What the device itself is set to: a temperature, or "off"."""
        found = self.stack.device(device)
        state = found["state"]
        if found["family"] == "fritz":
            return state["target_temperature"]
        if state.get("system_mode") == "off":
            return "off"
        return state["occupied_heating_setpoint"]

    # --- waiting for outcomes
    def room_at(self, room: str, target: float, reason: str, timeout: float = 30) -> None:
        def reached() -> bool:
            climate = self.room(room)
            return (
                climate["attributes"].get("temperature") == target and self.reason(room) == reason
            )

        self.wait_until(reached, f"room {room} to be at {target} °C ({reason})", timeout)

    def devices_at(self, room: str, target: object, timeout: float = 60) -> None:
        """Every thermostat of the room has ``target`` on the device and confirmed it."""

        def reached() -> bool:
            if any(family == "fritz" for _, _, family in ROOMS[room]):
                self.poll_fritzbox()
            on_devices = all(self.device_target(d) == target for _, d, _ in ROOMS[room])
            confirmed = all(status == "ok" for status in self.statuses(room).values())
            return on_devices and confirmed

        self.wait_until(reached, f"the thermostats of {room} to be at {target}, confirmed", timeout)


@pytest.fixture(scope="module")
def home(stack: Any, wait_until: Callable[..., Any]) -> Home:
    """The demo, brought to a known state: everyone home, every schedule on, all at comfort."""
    # Home Assistant's test plugin blocks sockets before each test, and its `socket_enabled`
    # fixture (conftest's `network`) is set up after module fixtures like this one.
    pytest_socket.enable_socket()
    home = Home(stack, wait_until)
    home.wait_until(
        lambda: all(home.api.get_state(f"climate.cave_{room}") for room in ROOMS),
        "cave_heating's room thermostats",
        60,
    )
    home.call("input_boolean", "turn_on", entity_id=PRESENCE_SWITCH[0])
    home.call("select", "select_option", entity_id="select.cave_home_mode", option="auto")
    for room in ROOMS:
        home.set_schedule(room, on=True)
    for room in ROOMS:
        home.room_at(room, COMFORT, "comfort")
        home.devices_at(room, COMFORT, timeout=90)
    home.ready = time.monotonic()
    return home


# ------------------------------------------------------------------------------------ scenarios
def test_the_demo_starts_with_both_families_under_control(home: Home) -> None:
    for room, thermostats in ROOMS.items():
        assert home.statuses(room) == {entity: "ok" for entity, _, _ in thermostats}
        assert home.state(f"binary_sensor.cave_{room}_problem")["state"] == "off"
    assert home.state("binary_sensor.cave_home_occupied")["state"] == "on"
    # FRITZ!SmartHome is set up against the fake FRITZ!Box; FRITZ!Box Tools is not (no TR-064).
    entries = home.api.request("GET", "/api/config/config_entries/entry")
    domains = {entry["domain"]: entry["state"] for entry in entries}
    assert domains["fritzbox"] == "loaded"
    assert "fritz" not in domains


def test_schedule_moves_rooms_between_comfort_and_eco(home: Home) -> None:
    for room in ("living_room", "bathroom"):  # Sonoff and FRITZ!
        home.set_schedule(room, on=False)
    for room in ("living_room", "bathroom"):
        home.room_at(room, ECO, "eco")
        home.devices_at(room, ECO, timeout=90)

    for room in ("living_room", "bathroom"):
        home.set_schedule(room, on=True)
    for room in ("living_room", "bathroom"):
        home.room_at(room, COMFORT, "comfort")
        home.devices_at(room, COMFORT, timeout=90)


def test_an_open_window_pauses_the_room_and_closing_it_resumes(home: Home) -> None:
    home.stack.control("POST", "/api/devices/wohnzimmer_fenster/window", {"open": True})

    home.wait_until(
        lambda: home.state("binary_sensor.cave_living_room_window_open")["state"] == "on",
        "the window pause to start",
        30,
    )
    assert home.reason("living_room") == "window"
    # The radiators are off; the room still shows the target it returns to.
    home.devices_at("living_room", "off")
    assert home.room("living_room")["attributes"]["temperature"] == COMFORT

    home.stack.control("POST", "/api/devices/wohnzimmer_fenster/window", {"open": False})

    home.wait_until(
        lambda: home.state("binary_sensor.cave_living_room_window_open")["state"] == "off",
        "the window pause to end",
        30,
    )
    home.room_at("living_room", COMFORT, "comfort")
    home.devices_at("living_room", COMFORT)


def test_away_sets_back_every_room_and_coming_home_restores_them(home: Home) -> None:
    home.call("input_boolean", "turn_off", entity_id=PRESENCE_SWITCH[0])

    home.wait_until(
        lambda: home.state("binary_sensor.cave_home_occupied")["state"] == "off",
        "the home to count as away after the away delay",
        30,
    )
    for room in ROOMS:
        home.room_at(room, AWAY, "away")
    for room in ROOMS:  # the Bosch, both Sonoffs and the FRITZ!
        home.devices_at(room, AWAY, timeout=90)

    home.call("input_boolean", "turn_on", entity_id=PRESENCE_SWITCH[0])

    home.wait_until(
        lambda: home.state("binary_sensor.cave_home_occupied")["state"] == "on",
        "the home to count as occupied",
        30,
    )
    for room in ROOMS:
        home.room_at(room, COMFORT, "comfort")
    for room in ROOMS:
        home.devices_at(room, COMFORT, timeout=90)


def test_boost_heats_for_its_duration_and_then_ends(home: Home) -> None:
    home.call("cave_heating", "boost", entity_id="climate.cave_bedroom", duration="00:00:15")

    home.room_at("bedroom", BOOST, "boost")
    assert home.room("bedroom")["attributes"]["boost_until"]
    home.devices_at("bedroom", BOOST)  # the Bosch

    home.room_at("bedroom", COMFORT, "comfort", timeout=45)
    home.devices_at("bedroom", COMFORT)


def test_the_room_sensor_feeds_the_thermostats(home: Home) -> None:
    home.stack.control("POST", "/api/rooms/living_room/temperature", {"temperature": 23.4})
    home.stack.control("POST", "/api/rooms/bedroom/temperature", {"temperature": 22.6})

    def sensor(name: str) -> float:
        return float(home.state(f"sensor.{name}_temperature")["state"])

    home.wait_until(
        lambda: abs(sensor("wohnzimmer") - 23.4) < 0.5 and abs(sensor("schlafzimmer") - 22.6) < 0.5,
        "the room sensors to report the new temperatures",
        60,
    )

    def fed() -> bool:
        room = sensor("wohnzimmer")
        sonoffs = all(
            abs(home.stack.device(d)["state"]["external_temperature_input"] - room) <= 0.1
            for d in ("wohnzimmer_heizung_fenster", "wohnzimmer_heizung_tuer")
        )
        bosch = home.stack.device("schlafzimmer_heizung")["state"]["remote_temperature"]
        return sonoffs and abs(bosch - sensor("schlafzimmer")) <= 0.2

    home.wait_until(fed, "the room temperatures to reach the thermostats' inputs", 60)
    # The thermostats regulate on what they were fed.
    for device in ("wohnzimmer_heizung_fenster", "wohnzimmer_heizung_tuer"):
        found = home.stack.device(device)
        assert abs(found["regulation_temperature"] - sensor("wohnzimmer")) <= 0.5


def test_a_slow_fritz_thermostat_is_waited_for_not_resent_to(home: Home) -> None:
    delay = 40  # longer than confirm_timeout, shorter than confirm_timeout_slow
    assert CONFIRM_TIMEOUT < delay < TIMING.confirm_timeout_slow.total_seconds() - 30
    home.stack.control("POST", "/api/fritzbox", {"report_delay": delay})
    try:
        sent = time.monotonic()
        home.call("climate", "set_temperature", entity_id="climate.cave_bathroom", temperature=23)
        home.room_at("bathroom", 23.0, "override")

        seen: set[str] = set()

        def confirmed() -> bool:
            home.poll_fritzbox()
            status = home.statuses("bathroom")["climate.bad_heizung"]
            seen.add(status)
            reported = home.stack.device("bad_heizung")["state"]["reported_target_temperature"]
            return status == "ok" and reported == 23.0

        home.wait_until(confirmed, "the slow FRITZ! thermostat to confirm 23 °C", 120)
        took = time.monotonic() - sent
        assert took > CONFIRM_TIMEOUT  # longer than a fast thermostat may take
        assert "pending" in seen  # it was waited for
        assert "unresponsive" not in seen
        assert home.state("binary_sensor.cave_bathroom_problem")["state"] == "off"
        assert home.state("climate.bad_heizung")["attributes"]["temperature"] == 23.0
    finally:
        home.stack.control("POST", "/api/fritzbox", {"report_delay": 0})
        home.call("cave_heating", "resume_schedule", entity_id="climate.cave_bathroom")
    home.room_at("bathroom", COMFORT, "comfort")
    home.devices_at("bathroom", COMFORT, timeout=90)


def test_a_dial_turned_on_a_thermostat_becomes_the_rooms_override(home: Home) -> None:
    # A dial turned within confirm_timeout of a start is put back, not adopted
    # (custom_components/cave_heating/README.md); the demo has been settled for longer.
    settled = home.ready + CONFIRM_TIMEOUT + 2
    home.wait_until(lambda: time.monotonic() > settled, "the start's confirm_timeout to pass", 60)

    home.stack.control("POST", "/api/devices/wohnzimmer_heizung_fenster/dial", {"target": 23.5})

    home.room_at("living_room", 23.5, "override", timeout=MANUAL_SETTLE + 30)
    assert home.room("living_room")["attributes"]["override_until"]
    home.devices_at("living_room", 23.5)  # the other Sonoff follows the dial

    home.call("cave_heating", "resume_schedule", entity_id="climate.cave_living_room")
    home.room_at("living_room", COMFORT, "comfort")
    home.devices_at("living_room", COMFORT)


def test_a_zigbee_thermostat_that_goes_offline_is_brought_in_line_when_it_returns(
    home: Home,
) -> None:
    home.stack.control(
        "POST", "/api/devices/wohnzimmer_heizung_tuer/availability", {"online": False}
    )
    home.wait_until(
        lambda: home.statuses("living_room")["climate.wohnzimmer_heizung_tuer"] == "unavailable",
        "the room to see the thermostat unavailable",
        30,
    )
    # The room carries on with its other thermostat.
    home.set_schedule("living_room", on=False)
    home.room_at("living_room", ECO, "eco")
    home.wait_until(
        lambda: home.device_target("wohnzimmer_heizung_fenster") == ECO,
        "the available thermostat to follow",
        30,
    )
    assert home.device_target("wohnzimmer_heizung_tuer") == COMFORT  # out of reach meanwhile
    assert home.room("living_room")["state"] == "heat"

    home.stack.control(
        "POST", "/api/devices/wohnzimmer_heizung_tuer/availability", {"online": True}
    )

    home.devices_at("living_room", ECO)  # brought in line on its return
    home.set_schedule("living_room", on=True)
    home.room_at("living_room", COMFORT, "comfort")
    home.devices_at("living_room", COMFORT)


def test_a_fritz_thermostat_that_goes_offline_makes_its_room_unavailable_until_it_returns(
    home: Home,
) -> None:
    home.stack.control("POST", "/api/devices/bad_heizung/availability", {"online": False})

    def gone() -> bool:
        home.poll_fritzbox()
        return home.room("bathroom")["state"] == "unavailable"

    home.wait_until(gone, "the bathroom, whose only thermostat is gone, to be unavailable", 60)
    assert home.statuses("bathroom")["climate.bad_heizung"] == "unavailable"

    home.stack.control("POST", "/api/devices/bad_heizung/availability", {"online": True})

    def back() -> bool:
        home.poll_fritzbox()
        return home.room("bathroom")["state"] == "heat"

    home.wait_until(back, "the bathroom to be back", 60)
    home.devices_at("bathroom", COMFORT, timeout=90)
