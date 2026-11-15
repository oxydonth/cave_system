"""Integration tests for concept sections 5.3 and 5.4 through Home Assistant.

The decision table itself is tested exhaustively in test_logic.py; these tests show that the
entities, services and input entities reach it, and that the outcome reaches the thermostats.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar

from custom_components.cave_heating.const import DOMAIN
from tests.cave_heating.fakes import FakeHome, FakeThermostat, config, room

SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
LIVING = "climate.cave_living_room"
MODE = "select.cave_home_mode"


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    """A set_temperature command as a thermostat receives it."""
    data: dict[str, Any] = {"temperature": target}
    if mode is not None:
        data["hvac_mode"] = mode
    return ("set_temperature", data)


OFF = ("set_hvac_mode", {"hvac_mode": "off"})


def shown(home: FakeHome) -> tuple[float, str, str]:
    """The room thermostat's target, preset and reason."""
    attributes = home.state(LIVING).attributes
    return attributes["temperature"], attributes["preset_mode"], attributes["reason"]


async def living_room(home: FakeHome, **fields: Any) -> FakeThermostat:
    """One room with a thermostat at comfort, a window, a schedule that is on, and presence."""
    trv = await home.thermostat("climate.trv", target=21.0)
    home.set("binary_sensor.window", "off")
    home.set("schedule.living", "on")
    home.set("person.max", "home")
    top = {key: fields.pop(key) for key in ("presence", "away_delay", "timing") if key in fields}
    await home.start(
        config(
            room(
                "living_room",
                "climate.trv",
                windows=["binary_sensor.window"],
                schedule="schedule.living",
                **fields,
            ),
            presence=top.pop("presence", ["person.max"]),
            **top,
        )
    )
    trv.report(current=20.0)
    return trv


async def call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    """A blocking service call."""
    await hass.services.async_call(domain, service, data, blocking=True)


async def set_home_mode(hass: HomeAssistant, mode: str) -> None:
    """Switch the home mode by hand."""
    await call(hass, "select", "select_option", entity_id=MODE, option=mode)


# --- Rows 1 to 3: radiators off ---------------------------------------------------------------


async def test_row_1_home_mode_off_turns_every_room_off_for_the_summer(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Row 1, with the target and preset still showing the outcome of rows 4 to 7."""
    trv = await living_room(home)
    await set_home_mode(hass, "off")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [OFF]
    assert shown(home) == (21.0, "comfort", "summer")
    assert home.state("sensor.cave_living_room_reason").state == "summer"
    assert home.state(LIVING).attributes["hvac_action"] == "off"
    await set_home_mode(hass, "auto")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands[1:] == [heat(21.0, "heat")]


async def test_row_2_room_switched_off(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Row 2: the room thermostat's mode off."""
    trv = await living_room(home)
    await call(hass, "climate", "set_hvac_mode", entity_id=LIVING, hvac_mode="off")
    await home.advance(freezer, 3 * SEC)
    assert home.state(LIVING).state == "off"
    assert shown(home) == (21.0, "comfort", "room_off")
    assert trv.commands == [OFF]
    await call(hass, "climate", "turn_on", entity_id=LIVING)
    await home.advance(freezer, 3 * SEC)
    assert home.state(LIVING).state == "heat"
    assert trv.commands[1:] == [heat(21.0, "heat")]


async def test_row_3_window_pause_starts_after_the_open_delay_and_ends_after_the_close_delay(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The room's own delays; the window sensor is on exactly while the pause is in effect."""
    trv = await living_room(home, window_open_delay="00:05:00", window_close_delay="00:01:00")
    window_open = "binary_sensor.cave_living_room_window_open"
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 4 * MIN + 59 * SEC)
    assert home.state(window_open).state == "off"
    assert trv.commands == []
    await home.advance(freezer, 1 * SEC)
    assert home.state(window_open).state == "on"
    assert shown(home) == (21.0, "comfort", "window")
    await home.advance(freezer, 2 * SEC)
    assert trv.commands == [OFF]
    home.set("binary_sensor.window", "off")
    await home.advance(freezer, 59 * SEC)
    assert home.state(window_open).state == "on"
    await home.advance(freezer, 1 * SEC)
    assert home.state(window_open).state == "off"
    await home.advance(freezer, 2 * SEC)
    assert trv.commands == [OFF, heat(21.0, "heat")]


# --- Row 4: boost and override ----------------------------------------------------------------


async def test_boost_by_preset_and_by_service_ends_after_its_duration(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The preset uses the room's boost values; the service may give others."""
    trv = await living_room(home, boost_temperature=24.0, boost_duration="00:20:00")
    await call(hass, "climate", "set_preset_mode", entity_id=LIVING, preset_mode="boost")
    assert shown(home) == (24.0, "boost", "boost")
    until = home.state(LIVING).attributes["boost_until"]
    assert until == (home.now() + timedelta(minutes=20)).isoformat()
    await home.advance(freezer, 20 * MIN)
    assert shown(home) == (21.0, "comfort", "comfort")
    await home.advance(freezer, 3 * SEC)
    await call(hass, DOMAIN, "boost", entity_id=LIVING, duration="00:10:00", temperature=26)
    assert shown(home) == (26.0, "boost", "boost")
    await home.advance(freezer, 10 * MIN + 3 * SEC)
    assert shown(home)[2] == "comfort"
    assert trv.commands == [heat(24.0), heat(21.0), heat(26.0), heat(21.0)]


async def test_boost_service_targets_rooms_by_area(hass: HomeAssistant, home: FakeHome) -> None:
    """Rooms are devices in areas, so a boost can name the area."""
    await living_room(home)
    area = ar.async_get(hass).async_get_area_by_name("Living Room")
    assert area is not None
    await call(hass, DOMAIN, "boost", area_id=area.id)
    assert shown(home)[2] == "boost"
    await call(hass, DOMAIN, "resume_schedule", area_id=area.id)
    assert shown(home)[2] == "comfort"


async def test_boost_clock_keeps_running_during_a_window_pause(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A window pause does not extend the boost."""
    await living_room(home)
    await call(hass, DOMAIN, "boost", entity_id=LIVING, duration="00:10:00")
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 5 * MIN)
    assert shown(home) == (25.0, "boost", "window")
    home.set("binary_sensor.window", "off")
    await home.advance(freezer, 5 * MIN)
    assert shown(home)[2] == "comfort", "the boost ended during the pause"


async def test_override_by_temperature_and_by_preset(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A temperature set by hand, or comfort, eco or away chosen by hand."""
    trv = await living_room(home)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=22.3)
    assert shown(home) == (22.5, "none", "override"), "rounded to the room's 0.5 K step"
    assert (
        home.state(LIVING).attributes["override_until"]
        == (home.now() + timedelta(hours=6)).isoformat()
    )
    for preset, target in (("eco", 17.5), ("away", 16.0), ("comfort", 21.0)):
        await call(hass, "climate", "set_preset_mode", entity_id=LIVING, preset_mode=preset)
        assert shown(home) == (target, preset, "override")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [], "four changes within the debounce, ending where it started"


async def test_later_hand_action_replaces_the_earlier_one(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """A boost replaces an override and the other way round."""
    await living_room(home)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    await call(hass, DOMAIN, "boost", entity_id=LIVING)
    assert shown(home)[2] == "boost"
    assert home.state(LIVING).attributes["override_until"] is None
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    assert shown(home)[2] == "override"
    assert home.state(LIVING).attributes["boost_until"] is None


async def test_override_ends_after_override_max(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The room's override_max."""
    await living_room(home, override_max="01:00:00")
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    await home.advance(freezer, HOUR - SEC)
    assert shown(home)[2] == "override"
    await home.advance(freezer, SEC)
    assert shown(home)[2] == "comfort"


async def test_override_ends_at_the_next_schedule_change_but_a_boost_does_not(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """The next change of the room's schedule."""
    await living_room(home)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    home.set("schedule.living", "unavailable")
    home.set("schedule.living", "on")
    await home.settle()
    assert shown(home)[2] == "override", "going unavailable and back is no schedule change"
    home.set("schedule.living", "off")
    await home.settle()
    assert shown(home) == (17.5, "eco", "eco")
    await call(hass, DOMAIN, "boost", entity_id=LIVING)
    home.set("schedule.living", "on")
    await home.settle()
    assert shown(home)[2] == "boost"


async def test_override_set_while_away_lasts_until_the_first_schedule_change_after_coming_home(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Pre-heating the flat from the phone."""
    await living_room(home)
    home.set("person.max", "not_home")
    await home.advance(freezer, 30 * MIN)
    assert shown(home) == (16.0, "away", "away")
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=22)
    home.set("schedule.living", "off")
    await home.settle()
    assert shown(home) == (22.0, "none", "override"), "still away: the change does not end it"
    home.set("person.max", "home")
    await home.settle()
    assert shown(home)[2] == "override"
    home.set("schedule.living", "on")
    await home.settle()
    assert shown(home) == (21.0, "comfort", "comfort")


@pytest.mark.parametrize("mode", ["away", "off"])
@pytest.mark.parametrize("hand", ["boost", "override"])
async def test_home_mode_switched_to_away_or_off_by_hand_ends_boost_and_override(
    hass: HomeAssistant, home: FakeHome, mode: str, hand: str
) -> None:
    """Switching back to auto does not bring them back."""
    await living_room(home)
    if hand == "boost":
        await call(hass, DOMAIN, "boost", entity_id=LIVING)
    else:
        await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    await set_home_mode(hass, mode)
    await set_home_mode(hass, "auto")
    assert shown(home)[2] == "comfort"


@pytest.mark.parametrize("hand", ["boost", "override"])
async def test_room_switched_off_ends_boost_and_override(
    hass: HomeAssistant, home: FakeHome, hand: str
) -> None:
    """Switching it on again does not bring them back."""
    await living_room(home)
    if hand == "boost":
        await call(hass, DOMAIN, "boost", entity_id=LIVING)
    else:
        await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    await call(hass, "climate", "turn_off", entity_id=LIVING)
    await call(hass, "climate", "turn_on", entity_id=LIVING)
    assert shown(home)[2] == "comfort"


@pytest.mark.parametrize("how", ["button", "service", "preset_none"])
@pytest.mark.parametrize("hand", ["boost", "override"])
async def test_resuming_the_schedule_ends_boost_and_override(
    hass: HomeAssistant, home: FakeHome, how: str, hand: str
) -> None:
    """The button, the service, or the preset none."""
    await living_room(home)
    if hand == "boost":
        await call(hass, DOMAIN, "boost", entity_id=LIVING)
    else:
        await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    if how == "button":
        await call(hass, "button", "press", entity_id="button.cave_living_room_resume_schedule")
    elif how == "service":
        await call(hass, DOMAIN, "resume_schedule", entity_id=LIVING)
    else:
        await call(hass, "climate", "set_preset_mode", entity_id=LIVING, preset_mode="none")
    assert shown(home) == (21.0, "comfort", "comfort")


@pytest.mark.parametrize("hand", ["boost", "override"])
async def test_becoming_away_through_presence_does_not_cancel_boost_or_override(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, hand: str
) -> None:
    """Phones drop off the Wi-Fi when they sleep."""
    await living_room(home, boost_duration="02:00:00")
    if hand == "boost":
        await call(hass, DOMAIN, "boost", entity_id=LIVING)
    else:
        await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    home.set("person.max", "not_home")
    await home.advance(freezer, HOUR)
    assert home.state("binary_sensor.cave_home_occupied").state == "off"
    assert shown(home)[2] == hand


# --- Hand actions while the radiators are off ---------------------------------------------------


@pytest.mark.parametrize("off_by", ["summer", "room_off"])
@pytest.mark.parametrize("action", ["temperature", "preset", "preset_none", "boost"])
async def test_hand_actions_are_refused_while_the_home_or_the_room_is_off(
    hass: HomeAssistant, home: FakeHome, off_by: str, action: str
) -> None:
    """Refused with a message saying why."""
    await living_room(home)
    if off_by == "summer":
        await set_home_mode(hass, "off")
    else:
        await call(hass, "climate", "turn_off", entity_id=LIVING)
    domain, service, data = {
        "temperature": ("climate", "set_temperature", {"temperature": 23}),
        "boost": (DOMAIN, "boost", {}),
        "preset": ("climate", "set_preset_mode", {"preset_mode": "eco"}),
        "preset_none": ("climate", "set_preset_mode", {"preset_mode": "none"}),
    }[action]
    with pytest.raises(ServiceValidationError) as caught:
        await call(hass, domain, service, entity_id=LIVING, **data)
    message = str(caught.value)
    if off_by == "summer":
        assert message == (
            "The heating is off for the summer (home mode off), so Living Room cannot be set. "
            "Switch the home mode to auto first"
        )
    else:
        assert message == (
            "Living Room is switched off. Switch the room on before setting a temperature or preset"
        )
    assert shown(home)[2] in ("summer", "room_off")


async def test_hand_actions_during_a_window_pause_take_effect_when_it_ends(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Accepted and shown at once; the radiators follow when the pause ends."""
    trv = await living_room(home)
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 63 * SEC)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=24)
    assert shown(home) == (24.0, "none", "window")
    await home.advance(freezer, 10 * SEC)
    assert trv.commands == [OFF]
    home.set("binary_sensor.window", "off")
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert shown(home) == (24.0, "none", "override")
    assert trv.commands == [OFF, heat(24.0, "heat")]


# --- Row 5: away ------------------------------------------------------------------------------


async def test_row_5_away_by_presence_after_the_away_delay(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """binary_sensor.cave_home_occupied turns off after the away delay; the room follows."""
    trv = await living_room(home, away_delay="00:10:00")
    home.set("person.max", "not_home")
    await home.advance(freezer, 10 * MIN - SEC)
    assert home.state("binary_sensor.cave_home_occupied").state == "on"
    assert shown(home)[2] == "comfort"
    await home.advance(freezer, SEC)
    assert home.state("binary_sensor.cave_home_occupied").state == "off"
    assert shown(home) == (16.0, "away", "away")
    await call(hass, "number", "set_value", entity_id="number.cave_away_temperature", value=15)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands[-1] == heat(15.0)
    home.set("person.max", "home")
    await home.settle()
    assert home.state("binary_sensor.cave_home_occupied").state == "on"
    assert shown(home)[2] == "comfort"


async def test_row_5_away_by_home_mode(hass: HomeAssistant, home: FakeHome) -> None:
    """Home mode away forces the setback, also with someone at home."""
    await living_room(home)
    await set_home_mode(hass, "away")
    assert shown(home) == (16.0, "away", "away")
    assert home.state("binary_sensor.cave_home_occupied").state == "on"


@pytest.mark.parametrize(
    ("states", "occupied"),
    [
        ({"zone.home": "0", "person.max": "not_home"}, False),
        ({"zone.home": "1", "person.max": "not_home"}, True),
        ({"zone.home": "0", "person.max": "unavailable"}, True),
        ({"zone.home": "0"}, True),
    ],
)
async def test_presence_list_and_unknown_presence_counts_as_home(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    states: dict[str, str],
    occupied: bool,
) -> None:
    """Section 5.4: missing, unknown or unavailable presence is home."""
    await living_room(home, presence=["zone.home", "person.max"], away_delay="00:00:00")
    hass.states.async_remove("person.max")
    for entity_id, state in states.items():
        home.set(entity_id, state)
    await home.advance(freezer, SEC)
    assert (home.state("binary_sensor.cave_home_occupied").state == "on") is occupied


# --- Rows 6 and 7 -----------------------------------------------------------------------------


async def test_rows_6_and_7_follow_the_schedule(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Comfort while the schedule is on, eco while it is off; the numbers set the values."""
    trv = await living_room(home)
    home.set("schedule.living", "off")
    await home.advance(freezer, 3 * SEC)
    assert shown(home) == (17.5, "eco", "eco")
    await call(
        hass, "number", "set_value", entity_id="number.cave_living_room_eco_temperature", value=18
    )
    await home.advance(freezer, 3 * SEC)
    assert shown(home)[0] == 18.0
    home.set("schedule.living", "on")
    await home.advance(freezer, 3 * SEC)
    assert shown(home) == (21.0, "comfort", "comfort")
    assert trv.commands == [heat(17.5), heat(18.0), heat(21.0)]


async def test_room_without_a_schedule_is_at_comfort(hass: HomeAssistant, home: FakeHome) -> None:
    """Row 6: no schedule."""
    await home.thermostat("climate.trv", target=21.0)
    await home.start(config(room("living_room", "climate.trv")))
    assert shown(home) == (21.0, "comfort", "comfort")


# --- Section 5.4 --------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
async def test_schedule_missing_or_unavailable_counts_as_on(
    hass: HomeAssistant, home: FakeHome, state: str | None
) -> None:
    """The room is at comfort."""
    await living_room(home)
    home.set("schedule.living", "off")
    await home.settle()
    if state is None:
        hass.states.async_remove("schedule.living")
    else:
        home.set("schedule.living", state)
    await home.settle()
    assert shown(home) == (21.0, "comfort", "comfort")


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
async def test_window_contact_unavailable_counts_as_closed(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, state: str | None
) -> None:
    """A contact that was closed and goes silent does not start a pause."""
    await living_room(home)
    if state is None:
        hass.states.async_remove("binary_sensor.window")
    else:
        home.set("binary_sensor.window", state)
    await home.advance(freezer, 10 * MIN)
    assert home.state("binary_sensor.cave_living_room_window_open").state == "off"


async def test_window_contact_silent_while_open_keeps_the_pause_for_the_grace_then_reports(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The pause continues for window_unavailable_grace, then ends; the problem sensor turns on."""
    trv = await living_room(home)
    problem = "binary_sensor.cave_living_room_problem"
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 2 * MIN)
    home.set("binary_sensor.window", "unavailable")
    await home.advance(freezer, 30 * MIN - SEC)
    assert shown(home)[2] == "window"
    assert home.state(problem).state == "off"
    await home.advance(freezer, SEC)
    assert shown(home)[2] == "comfort"
    assert home.state(problem).state == "on"
    assert home.state(problem).attributes["messages"] == [
        "window contact binary_sensor.window stopped reporting while open; the window pause "
        "has ended"
    ]
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [OFF, heat(21.0, "heat")]
    home.set("binary_sensor.window", "off")
    await home.settle()
    assert home.state(problem).state == "off"


async def test_room_sensor_missing_unavailable_or_stale_shows_the_thermostats_average(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Section 5.4, room temperature sensor; and it is used again once it reports."""
    first = await home.thermostat("climate.a", target=21.0, current=22.0)
    second = await home.thermostat("climate.b", target=21.0, current=23.0)
    home.set("sensor.room", "20.5")
    await home.start(
        config(room("living_room", "climate.a", "climate.b", temperature_sensor="sensor.room"))
    )
    assert home.state(LIVING).attributes["current_temperature"] == 20.5
    await home.advance(freezer, 3 * HOUR - SEC, step=HOUR)
    assert home.state(LIVING).attributes["current_temperature"] == 20.5
    await home.advance(freezer, SEC)
    assert home.state(LIVING).attributes["current_temperature"] == 22.5, "stale"
    home.set("sensor.room", "20.5")
    await home.settle()
    assert home.state(LIVING).attributes["current_temperature"] == 20.5
    home.set("sensor.room", "unavailable")
    await home.settle()
    assert home.state(LIVING).attributes["current_temperature"] == 22.5
    second.set_available(False)
    await home.settle()
    assert home.state(LIVING).attributes["current_temperature"] == 22.0
    assert first.commands == []


async def test_stale_sensor_that_repeats_its_value_counts_again(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A report of the same value (no state change) makes a stale sensor fresh again."""
    await home.thermostat("climate.a", target=21.0, current=22.0)
    home.set("sensor.room", "20.5")
    await home.start(config(room("living_room", "climate.a", temperature_sensor="sensor.room")))
    await home.advance(freezer, 3 * HOUR, step=HOUR)
    assert home.state(LIVING).attributes["current_temperature"] == 22.0
    hass.states.async_set("sensor.room", "20.5")
    await home.settle()
    assert home.state(LIVING).attributes["current_temperature"] == 20.5


async def test_humidity_shown_only_while_its_sensor_is_available(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Section 5.4, humidity sensor: not shown."""
    await home.thermostat("climate.a", target=21.0)
    home.set("sensor.humidity", "55")
    await home.start(config(room("living_room", "climate.a", humidity_sensor="sensor.humidity")))
    assert home.state(LIVING).attributes["current_humidity"] == 55.0
    home.set("sensor.humidity", "unavailable")
    await home.settle()
    assert "current_humidity" not in home.state(LIVING).attributes


async def test_unavailable_thermostat_is_skipped_and_brought_in_line_when_it_returns(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The room works with the others; with none available the room entity is unavailable."""
    first = await home.thermostat("climate.a", target=21.0)
    second = await home.thermostat("climate.b", target=21.0)
    await home.start(config(room("living_room", "climate.a", "climate.b")))
    second.set_available(False)
    await call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=23)
    await home.advance(freezer, 3 * SEC)
    assert first.commands == [heat(23.0)]
    assert second.commands == []
    first.set_available(False)
    await home.settle()
    assert home.state(LIVING).state == "unavailable"
    second.set_available(True)
    await home.advance(freezer, 3 * SEC)
    assert home.state(LIVING).state == "heat"
    assert second.commands == [heat(23.0)]


async def test_hvac_action_follows_the_thermostats(hass: HomeAssistant, home: FakeHome) -> None:
    """Heating while a thermostat says it heats, idle otherwise."""
    trv = await home.thermostat("climate.a", target=21.0)
    await home.start(config(room("living_room", "climate.a")))
    assert home.state(LIVING).attributes["hvac_action"] == "idle"
    trv.report(action="heating")
    await home.settle()
    assert home.state(LIVING).attributes["hvac_action"] == "heating"
