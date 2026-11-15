"""The room model, the home description, the clock and the in-process broker."""

import math
from typing import Any

import pytest

from cave_sim.clock import ManualClock, PeriodicTask
from cave_sim.home import FRITZ_THERMO_302, HomeError, load_home, parse_home
from cave_sim.mqtt import InMemoryBroker, topic_matches
from cave_sim.room import Room, RoomParameters
from cave_sim.testing import DEMO_HOME
from cave_sim.zigbee.models import MODELS


def _room(temperature: float = 20.0) -> Room:
    return Room("r", "R", temperature, 50.0, RoomParameters(2.0, 0.1, 2.0))


def test_a_closed_room_without_heating_cools_towards_outdoors() -> None:
    room = _room()
    room.step(3600, outdoor=0.0)
    assert room.temperature == pytest.approx(20.0 * math.exp(-0.1))


def test_an_open_valve_settles_at_outdoor_plus_heating_over_loss() -> None:
    room = _room()
    room.valve_openings.append(lambda: 1.0)
    room.step(100 * 3600, outdoor=5.0)
    assert room.temperature == pytest.approx(25.0, abs=1e-3)


def test_an_open_window_cools_much_faster() -> None:
    closed, opened = _room(), _room()
    opened.window_states.append(lambda: True)
    closed.step(1800, outdoor=0.0)
    opened.step(1800, outdoor=0.0)
    assert 20.0 - opened.temperature > 10 * (20.0 - closed.temperature)


def test_the_result_does_not_depend_on_the_step_length() -> None:
    coarse, fine = _room(), _room()
    for room in (coarse, fine):
        room.valve_openings.append(lambda: 0.5)
    coarse.step(3600, outdoor=5.0)
    for _ in range(60):
        fine.step(60, outdoor=5.0)
    assert coarse.temperature == pytest.approx(fine.temperature)


def test_openings_are_clamped() -> None:
    room = _room()
    room.valve_openings.extend([lambda: 3.0, lambda: -1.0])
    assert room.heating_input() == 1.0


def test_the_demo_home_uses_every_model_and_both_families() -> None:
    home = load_home(DEMO_HOME)
    models = {device.model for _, device in home.devices()}
    assert models == {*MODELS, FRITZ_THERMO_302}
    assert {device.family for _, device in home.devices()} == {"zigbee", "fritz"}
    assert len(home.rooms) == 3


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"rooms": [{"id": "Bad Room"}]}, "lower-case"),
        ({"rooms": [{"id": "a", "devices": [{"model": "XYZ", "name": "x"}]}]}, "not simulated"),
        ({"rooms": [{"id": "a"}, {"id": "a"}]}, "room IDs"),
        (
            {
                "rooms": [
                    {
                        "id": "a",
                        "devices": [
                            {"model": "TRVZB", "name": "Same Name"},
                            {"model": "SNZB-02P", "name": "same  name"},
                        ],
                    }
                ]
            },
            "device IDs",
        ),
        ({"rooms": [{"id": "a", "temperature": "warm"}]}, "number"),
        ({"rooms": [], "colour": "red"}, "unknown keys"),
        (
            {"rooms": [{"id": "a", "devices": [{"model": "TRVZB", "name": "x", "ieee": "1"}]}]},
            "ieee",
        ),
    ],
)
def test_invalid_homes_are_refused(raw: Any, message: str) -> None:
    with pytest.raises(HomeError, match=message):
        parse_home(raw)


def test_defaults_are_derived_from_the_name() -> None:
    home = parse_home({"rooms": [{"id": "a", "devices": [{"model": "TRVZB", "name": "A  B"}]}]})
    device = home.rooms[0].devices[0]
    assert device.device_id == "a_b"
    assert device.ieee_address.startswith("0x")
    assert len(device.ieee_address) == 18
    assert (
        device.ieee_address
        == parse_home({"rooms": [{"id": "a", "devices": [{"model": "TRVZB", "name": "A  B"}]}]})
        .rooms[0]
        .devices[0]
        .ieee_address
    )


def test_the_manual_clock_runs_callbacks_in_time_order() -> None:
    clock = ManualClock(start=0)
    calls: list[tuple[str, float]] = []
    clock.call_later(20, lambda: calls.append(("b", clock.now())))
    clock.call_later(10, lambda: calls.append(("a", clock.now())))
    cancelled = clock.call_later(15, lambda: calls.append(("x", clock.now())))
    cancelled.cancel()
    clock.advance(30)
    assert calls == [("a", 10), ("b", 20)]
    assert clock.now() == 30


def test_a_periodic_task_ticks_until_stopped() -> None:
    clock = ManualClock(start=0)
    ticks: list[float] = []
    task = PeriodicTask(clock, 60, lambda: ticks.append(clock.now()))
    task.start()
    clock.advance(185)
    task.stop()
    clock.advance(600)
    assert ticks == [60, 120, 180]


@pytest.mark.parametrize(
    ("topic_filter", "topic", "matches"),
    [
        ("a/#", "a/b/c", True),
        ("a/+/c", "a/b/c", True),
        ("a/+", "a/b/c", False),
        ("#", "a", True),
        ("a/b", "a/b", True),
        ("a/b", "a/bc", False),
        ("homeassistant/+/+/+/config", "homeassistant/climate/0x1/climate/config", True),
    ],
)
def test_topic_matching(topic_filter: str, topic: str, matches: bool) -> None:
    assert topic_matches(topic_filter, topic) is matches


def test_the_broker_keeps_retained_messages_and_delivers_once_per_client() -> None:
    broker = InMemoryBroker()
    sender = broker.client("sender")
    sender.publish("x/1", "kept", retain=True)
    sender.publish("x/2", "gone", retain=True)
    sender.publish("x/2", "", retain=True)  # an empty retained message deletes the stored one
    received: list[tuple[str, bytes]] = []
    receiver = broker.client("receiver")
    receiver.subscribe("x/#", lambda topic, payload: received.append((topic, payload)))
    assert received == [("x/1", b"kept")]
    sender.publish("x/3", "live")
    assert received[-1] == ("x/3", b"live")
    raw: list[str] = []
    broker.connect("raw", lambda topic, _payload, _qos, _retain: raw.append(topic))
    broker.subscribe("raw", "x/#")
    broker.subscribe("raw", "x/+")
    sender.publish("x/4", "once")
    assert raw.count("x/4") == 1
