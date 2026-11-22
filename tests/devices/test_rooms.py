"""Rooms built from each thermostat model run the core scenarios of concept section 5.3, and a
mixed room with all four models stays in step.

Each room has the model's thermostat (with its external input and settings, where it has them),
an SNZB-02P room sensor and an SNZB-04P window contact. The schedule is Home Assistant's own
schedule helper, on from 08:00 to 09:00; presence is an ``input_boolean`` (concept 3.1: presence
is replaced by a test entity). Defaults of concept 5.1: comfort 21, eco 17.5, away 16, boost 25
for 30 minutes.
"""

from __future__ import annotations

import pytest

from custom_components.cave_heating.const import DOMAIN
from tests.devices.capabilities import devices
from tests.devices.rig import (
    CONTACT_ID,
    FRITZ_302,
    HOUR,
    MIN,
    MODELS,
    OFF,
    PRESENCE,
    ROOM_CLIMATE,
    SEC,
    WINDOW_OPEN,
    Rig,
    ThermostatModel,
    heat,
)

pytestmark = pytest.mark.usefixtures("hass")


# The tests that use Home Assistant's schedule helper (rig.start(schedule=True)).
_WITH_SCHEDULE = {"test_room_comfort_and_eco_by_schedule", "test_room_override_expires"}


@pytest.fixture
def expected_lingering_timers(request: pytest.FixtureRequest) -> bool:
    """Home Assistant's schedule helper leaves the timer of its next change behind when Home
    Assistant stops, which the test library would report as a leak of the test. Only the tests
    that use it allow that; in the others a leaked timer is still reported."""
    return request.node.originalname in _WITH_SCHEDULE


def _settle(model: ThermostatModel) -> float:
    """How long until a report of this model has reached Home Assistant (FRITZ!: one poll)."""
    return 5 * SEC if model.zigbee else 35 * SEC


@pytest.mark.parametrize("device", devices("room_comfort_and_eco_by_schedule"))
async def test_room_comfort_and_eco_by_schedule(rig: Rig, device: str) -> None:
    """Rows 6 and 7: eco while the schedule is off, comfort while it is on. One command per
    change: 17.5 at 07:58, 21 at 08:00, 17.5 at 09:00."""
    model = MODELS[device]
    await rig.start(model, schedule=True)
    assert rig.state("schedule.living_room_heating").state == "off"
    assert rig.room() == (17.5, "eco", "eco")
    assert rig.commands(model) == heat(model, 17.5)
    await rig.advance(2 * MIN + 5 * SEC)
    assert rig.room() == (21.0, "comfort", "comfort")
    assert rig.commands(model) == heat(model, 17.5) + heat(model, 21.0)
    await rig.advance(1 * HOUR)
    assert rig.room() == (17.5, "eco", "eco")
    await rig.advance(_settle(model))
    assert rig.state(model.climate).attributes["temperature"] == 17.5
    assert rig.commands(model) == heat(model, 17.5) + heat(model, 21.0) + heat(model, 17.5)
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("room_window_pause"))
async def test_room_window_pause(rig: Rig, device: str) -> None:
    """Row 3: the radiators are off during the window pause, while the room keeps showing its
    target. A dial turned during the pause is not adopted: nothing is adopted while the
    radiators are off. A Zigbee thermostat stays off when its dial is turned (its mode is off,
    which is what is wanted), so it receives nothing; a FRITZ! dial switches the thermostat from
    OFF to a temperature, so it is put back off with one command. The pause ends with one heat
    command."""
    model = MODELS[device]
    await rig.start(model)
    await rig.past_start_window()
    rig.mark()
    rig.sim.home.set_window(CONTACT_ID, True)
    await rig.advance(1 * MIN + 5 * SEC)
    assert rig.room() == (21.0, "comfort", "window")
    assert rig.state(WINDOW_OPEN).state == "on"
    await rig.refresh_fritzbox()
    assert rig.state(model.climate).state == "off"
    assert rig.commands(model) == OFF

    await rig.device(lambda: rig.sim.home.turn_dial(model.device_id, 24.0))
    await rig.advance(5 * SEC)
    put_back = [] if model.zigbee else OFF
    assert rig.commands(model) == OFF + put_back
    await rig.advance(1 * MIN)
    assert rig.room() == (21.0, "comfort", "window"), "the dial is not adopted"
    await rig.refresh_fritzbox()
    assert rig.state(model.climate).state == "off"

    rig.sim.home.set_window(CONTACT_ID, False)
    await rig.advance(2 * MIN + 5 * SEC)
    assert rig.room() == (21.0, "comfort", "comfort")
    await rig.advance(_settle(model))
    assert rig.state(model.climate).state == "heat"
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    expected = OFF + put_back + heat(model, 21.0, with_mode=True)
    assert rig.commands(model) == expected
    await rig.advance(30 * MIN)
    assert rig.commands(model) == expected
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("room_away"))
async def test_room_away(rig: Rig, device: str) -> None:
    """Row 5: nobody home for longer than the away delay (30 min) means the away temperature;
    someone home again means the schedule's temperature at once."""
    model = MODELS[device]
    await rig.start(model, presence=True)
    await rig.past_start_window()
    rig.mark()
    await rig.call("input_boolean", "turn_off", entity_id=PRESENCE)
    await rig.advance(29 * MIN)
    assert rig.room()[2] == "comfort"
    assert rig.commands(model) == []
    await rig.advance(1 * MIN + 5 * SEC)
    assert rig.room() == (16.0, "away", "away")
    assert rig.state("binary_sensor.cave_home_occupied").state == "off"
    assert rig.commands(model) == heat(model, 16.0)
    await rig.call("input_boolean", "turn_on", entity_id=PRESENCE)
    await rig.advance(5 * SEC)
    assert rig.room() == (21.0, "comfort", "comfort")
    await rig.advance(_settle(model))
    assert rig.commands(model) == heat(model, 16.0) + heat(model, 21.0)
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("room_boost"))
async def test_room_boost(rig: Rig, device: str) -> None:
    """Row 4: the preset boost heats at the boost temperature (25) for the boost duration
    (30 min); then the room returns to its schedule. One command at each end."""
    model = MODELS[device]
    await rig.start(model)
    await rig.past_start_window()
    rig.mark()
    await rig.call("climate", "set_preset_mode", entity_id=ROOM_CLIMATE, preset_mode="boost")
    await rig.advance(5 * SEC)
    assert rig.room() == (25.0, "boost", "boost")
    assert rig.state(ROOM_CLIMATE).attributes["boost_until"] is not None
    assert rig.commands(model) == heat(model, 25.0)
    await rig.advance(29 * MIN)
    assert rig.room()[2] == "boost"
    await rig.advance(1 * MIN + 5 * SEC)
    assert rig.room() == (21.0, "comfort", "comfort")
    await rig.advance(_settle(model))
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    assert rig.commands(model) == heat(model, 25.0) + heat(model, 21.0)
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("room_override_expires"))
async def test_room_override_expires(rig: Rig, device: str) -> None:
    """Row 4 and the override's end: a dial turned at 08:10 is the room's override until the
    next change of the room's schedule (09:00), when the room goes to eco. The turned device
    receives nothing for the override, and one command when it ends."""
    model = MODELS[device]
    await rig.start(model, schedule=True)
    await rig.advance(12 * MIN)  # 08:10, the schedule is on
    assert rig.room() == (21.0, "comfort", "comfort")
    rig.mark()
    await rig.device(lambda: rig.sim.home.turn_dial(model.device_id, 23.0))
    await rig.advance(50 * SEC)
    assert rig.room() == (23.0, "none", "override")
    await rig.advance(48 * MIN)
    assert rig.room() == (23.0, "none", "override")
    assert rig.commands(model) == []
    await rig.advance(2 * MIN)  # past 09:00
    assert rig.room() == (17.5, "eco", "eco")
    assert rig.state(ROOM_CLIMATE).attributes["override_until"] is None
    await rig.advance(_settle(model))
    assert rig.commands(model) == heat(model, 17.5)
    assert rig.state(model.climate).attributes["temperature"] == 17.5


@pytest.mark.parametrize("device", devices("mixed_room_stays_in_step"))
async def test_mixed_room_stays_in_step(rig: Rig, device: str) -> None:
    """Concept 5.5: hand changes are collected per room; manual_settle after the last one, the
    target of the thermostat that changed becomes the room's override and the room's other
    thermostats are brought to it, each with one command, the FRITZ! one through the FRITZ!Box
    and the Zigbee ones through Zigbee2MQTT. Resuming the schedule brings all four back."""
    turned = MODELS[device]
    models = list(MODELS.values())
    await rig.start(*models)
    await rig.past_start_window()
    rig.mark()
    await rig.device(lambda: rig.sim.home.turn_dial(turned.device_id, 23.0))
    await rig.advance(50 * SEC)
    assert rig.room() == (23.0, "none", "override")
    await rig.advance(1 * MIN)
    for model in models:
        expected = [] if model is turned else heat(model, 23.0)
        assert rig.commands(model) == expected, model.key
        assert rig.state(model.climate).attributes["temperature"] == 23.0, model.key
        assert rig.status(model) == "ok", model.key
    await rig.call(DOMAIN, "resume_schedule", entity_id=ROOM_CLIMATE)
    await rig.advance(1 * MIN)
    for model in models:
        before = [] if model is turned else heat(model, 23.0)
        assert rig.commands(model) == [*before, *heat(model, 21.0)], model.key
        assert rig.state(model.climate).attributes["temperature"] == 21.0, model.key
    assert rig.room() == (21.0, "comfort", "comfort")
    assert FRITZ_302 in models
