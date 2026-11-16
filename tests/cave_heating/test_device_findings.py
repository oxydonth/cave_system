"""Defects found by the device integration tests of work package 7, and one from issue #13.

F1, F2 and F3 are the findings of pull request #14; the reload item is from issue #13.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.cave_heating.const import DOMAIN, STORAGE_KEY
from custom_components.cave_heating.driver import Report, ThermostatDriver, Want
from custom_components.cave_heating.logic import Contact, WindowPause
from custom_components.cave_heating.schema import Timing
from tests.cave_heating.fakes import FRITZ, FakeHome, config, room

SEC, MIN = timedelta(seconds=1), timedelta(minutes=1)
T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
LIVING = "climate.cave_living_room"
WINDOW_OPEN = "binary_sensor.cave_living_room_window_open"
PROBLEM = "binary_sensor.cave_living_room_problem"
OFF = ("set_hvac_mode", {"hvac_mode": "off"})


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    """A set_temperature command as a thermostat receives it."""
    return ("set_temperature", {"temperature": target} | ({"hvac_mode": mode} if mode else {}))


# --- F1: a blocked thermostat that reports no target ----------------------------------------------


def _summer_report(current: float) -> Report:
    """A FRITZ! thermostat in summer mode: off, no target (presets only)."""
    return Report("off", None, current, ("heat", "off"), 8.0, 28.0, 0.5)


def test_f1_reports_without_a_target_are_not_a_target_change() -> None:
    """Two missing targets are no change: no retry before retry_interval."""
    timing = Timing()
    driver = ThermostatDriver("climate.fritz", timing, slow=True, now=T0)
    driver.set_want(Want(heat=True, target=21.0), T0)
    driver.on_report(_summer_report(20.0), T0)
    command = driver.tick(T0 + timing.debounce)
    assert command is not None
    assert driver.on_refused(T0 + timing.debounce, "Summer mode active", command)
    for minute in range(1, 29):
        driver.on_report(_summer_report(20.0 + minute / 10), T0 + minute * MIN)
        assert driver.send_due is None, f"retried at minute {minute}"
        assert driver.tick(T0 + minute * MIN + timing.debounce) is None


async def test_f1_a_fritz_thermostat_in_summer_mode_is_retried_every_retry_interval(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The box refuses; Home Assistant shows off with no target; polls change the reading."""
    trv = await home.thermostat(
        "climate.fritz", mode="off", target=None, integration=FRITZ, unique_id="fritz-f1"
    )
    trv.refuse = "Summer mode active"
    await home.start(config(room("living_room", "climate.fritz")))
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 1
    for minute in range(29):
        trv.report(current=20.0 + minute / 10)
        await home.advance(freezer, MIN)
    assert len(trv.commands) == 1, "not resent into while blocked"
    await home.advance(freezer, 1 * MIN)
    assert len(trv.commands) == 2, "tried again after retry_interval"


# --- F2: a contact that went silent while open answers again, closed --------------------------


def test_f2_a_contact_answering_after_the_grace_starts_a_new_opening() -> None:
    """Unavailable, open, closed in one instant after the grace: no pause."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.w"])
    pause.update("binary_sensor.w", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.w", Contact.SILENT, T0 + 5 * MIN)
    assert not pause.evaluate(T0 + 35 * MIN), "the grace has run out"
    back = T0 + 40 * MIN
    pause.update("binary_sensor.w", Contact.OPEN, back)
    assert not pause.evaluate(back), "the old opening is over: this one has just begun"
    pause.update("binary_sensor.w", Contact.CLOSED, back)
    assert not pause.evaluate(back)
    assert pause.next_deadline(back) is None


def test_f2_a_contact_answering_open_within_the_grace_keeps_its_opening() -> None:
    """Within the grace the window has been open all along: the pause goes on."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.w"])
    pause.update("binary_sensor.w", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.w", Contact.SILENT, T0 + 5 * MIN)
    pause.update("binary_sensor.w", Contact.OPEN, T0 + 20 * MIN)
    assert pause.evaluate(T0 + 20 * MIN)


async def test_f2_a_contact_that_answers_closed_after_the_grace_sends_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Zigbee2MQTT publishes availability before the state: unavailable, on, off at once."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.w", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.w"])))
    home.set("binary_sensor.w", "on")
    await home.advance(freezer, 2 * MIN)
    home.set("binary_sensor.w", "unavailable")
    await home.advance(freezer, 31 * MIN)
    assert trv.commands == [OFF, heat(21.0, "heat")], "paused, and resumed after the grace"
    home.set("binary_sensor.w", "on")
    home.set("binary_sensor.w", "off")
    await home.advance(freezer, 5 * MIN)
    assert home.state(WINDOW_OPEN).state == "off"
    assert trv.commands == [OFF, heat(21.0, "heat")], "off and heat again for nothing"


# --- F3: a boost temperature off the 0.5 K grid ------------------------------------------------


async def test_f3_a_boost_temperature_is_rounded_to_the_room_step(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Concept 5.2: the room thermostat's target is in 0.5 K steps."""
    trv = await home.thermostat("climate.a", target=21.0)
    await home.start(config(room("living_room", "climate.a")))
    await hass.services.async_call(
        DOMAIN, "boost", {"entity_id": LIVING, "temperature": 22.25}, blocking=True
    )
    assert home.state(LIVING).attributes["temperature"] == 22.5
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.5)]
    stored = home.runtime().diagnostics()["rooms"]["living_room"]["state"]["hand"]
    assert stored["temperature"] == 22.5, "the state file keeps the rounded value"


async def test_f3_a_boost_stored_before_the_fix_is_rounded_on_load(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, hass_storage: Any
) -> None:
    """A boost has no maximum duration, so an unrounded one can survive the update."""
    trv = await home.thermostat("climate.a", target=21.0)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "home": {"mode": "auto", "away_temperature": 16.0},
            "rooms": {
                "living_room": {
                    "mode": "heat",
                    "hand": {
                        "kind": "boost",
                        "temperature": 22.25,
                        "until": (dt_util.utcnow() + timedelta(hours=2)).isoformat(),
                    },
                }
            },
        },
    }
    await home.start(config(room("living_room", "climate.a")))
    assert home.state(LIVING).attributes["temperature"] == 22.5
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.5)]


# --- Issue #13: a reload during a pause with a second, unavailable contact ----------------------


async def test_a_reload_during_a_pause_does_not_hold_it_for_an_unavailable_second_contact(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The reload keeps the contacts' state; the pause ends by the close delay."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    home.set("binary_sensor.b", "unavailable")
    file = config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    await home.start(file)
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    assert trv.commands == [OFF]
    await home.reload(file)
    home.set("binary_sensor.a", "off")
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert home.state(WINDOW_OPEN).state == "off", "held for window_unavailable_grace"
    assert trv.commands == [OFF, heat(21.0, "heat")]


# --- Review of #15: a reload that adds a contact during a pause ----------------------------------


async def test_reload_adds_unavailable_contact_during_pause(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The reviewer's test: a contact named before it works, added while a window is open."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    assert trv.commands == [OFF]
    home.set("binary_sensor.b", "unavailable")
    await home.reload(
        config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    )
    home.set("binary_sensor.a", "off")
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert home.state(WINDOW_OPEN).state == "off", "held for window_unavailable_grace"
    assert trv.commands == [OFF, heat(21.0, "heat")]


async def test_reload_adds_missing_contact_during_pause(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """From the reviewer's sequences: the added contact is not paired yet."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    assert trv.commands == [OFF]
    await home.reload(
        config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    )
    home.set("binary_sensor.a", "off")
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert home.state(WINDOW_OPEN).state == "off", "held for window_unavailable_grace"
    assert trv.commands == [OFF, heat(21.0, "heat")]


async def test_reload_adds_missing_contact_false_problem_message(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The reviewer's test: no "stopped reporting while open" for a contact never open."""
    await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    await home.reload(
        config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    )
    home.set("binary_sensor.a", "off")
    await home.advance(freezer, 40 * MIN)
    messages = home.state(PROBLEM).attributes["messages"]
    assert not any("stopped reporting while open" in m for m in messages), messages


async def test_a_contact_added_by_a_reload_stays_added_through_a_second_reload(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Two reloads while Home Assistant starts, before the added contact has a state."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    hass.set_state(CoreState.starting)
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    assert home.state(WINDOW_OPEN).state == "on"
    both = config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    await home.reload(both)
    await home.reload(both)
    home.set("binary_sensor.a", "off")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert home.state(WINDOW_OPEN).state == "off", "held for window_unavailable_grace"
    assert trv.commands == [OFF, heat(21.0, "heat")]


# --- The other side of each rule ----------------------------------------------------------------


def _blocked() -> tuple[ThermostatDriver, Timing]:
    """From the reviewer's sequences: a FRITZ! thermostat blocked in summer mode."""
    timing = Timing()
    driver = ThermostatDriver("climate.fritz", timing, slow=True, now=T0)
    driver.set_want(Want(heat=True, target=21.0), T0)
    driver.on_report(_summer_report(20.0), T0)
    command = driver.tick(T0 + timing.debounce)
    assert command is not None
    assert driver.on_refused(T0 + timing.debounce, "Summer mode active", command)
    return driver, timing


def _report(mode: str, target: float | None) -> Report:
    return Report(mode, target, 20.0, ("heat", "off"), 8.0, 28.0, 0.5)


def test_f1_a_blocked_thermostat_is_retried_at_once_when_a_target_appears() -> None:
    """Missing to a value is a target change."""
    driver, timing = _blocked()
    driver.on_report(_report("off", 16.0), T0 + MIN)
    assert driver.send_due == T0 + MIN + timing.debounce


def test_f1_a_blocked_thermostat_is_retried_at_once_when_its_target_disappears() -> None:
    """A value to missing is a target change."""
    driver, timing = _blocked()
    driver.on_report(_report("off", 16.0), T0 + MIN)
    command = driver.tick(T0 + MIN + timing.debounce)
    assert command is not None
    driver.on_refused(T0 + MIN + timing.debounce, "Key lock", command)
    driver.on_report(_report("off", None), T0 + 2 * MIN)
    assert driver.send_due == T0 + 2 * MIN + timing.debounce


def test_f1_a_blocked_thermostat_is_retried_at_once_when_its_mode_changes() -> None:
    """No target before or after, but the mode changed."""
    driver, timing = _blocked()
    driver.on_report(_report("heat", None), T0 + MIN)
    assert driver.send_due == T0 + MIN + timing.debounce


async def test_f1_a_fritz_thermostat_leaving_summer_mode_is_retried_at_once(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """From the reviewer's sequences, through Home Assistant."""
    trv = await home.thermostat(
        "climate.fritz", mode="off", target=None, integration=FRITZ, unique_id="fritz-x"
    )
    trv.refuse = "Summer mode active"
    await home.start(config(room("living_room", "climate.fritz")))
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 1
    await home.advance(freezer, 5 * MIN)
    trv.refuse = None
    trv.report(target=16.0)  # the box leaves summer mode: still off, a target appears
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 2
    assert trv.hvac_mode == "heat"


def test_f2_a_contact_answering_open_after_the_grace_pauses_after_the_open_delay() -> None:
    """A new opening: the pause comes back after open_delay, not at once."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.w"])
    pause.update("binary_sensor.w", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.w", Contact.SILENT, T0 + 5 * MIN)
    assert not pause.evaluate(T0 + 35 * MIN)
    pause.update("binary_sensor.w", Contact.OPEN, T0 + 40 * MIN)
    assert not pause.evaluate(T0 + 40 * MIN + 59 * SEC)
    assert pause.evaluate(T0 + 41 * MIN)


async def test_f2_a_contact_answering_open_after_the_grace_pauses_again_through_ha(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """From the reviewer's sequences: off, heat, and off again one open delay later."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.w", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.w"])))
    home.set("binary_sensor.w", "on")
    await home.advance(freezer, 2 * MIN)
    home.set("binary_sensor.w", "unavailable")
    await home.advance(freezer, 31 * MIN)
    assert trv.commands == [OFF, heat(21.0, "heat")]
    home.set("binary_sensor.w", "on")
    await home.advance(freezer, MIN - SEC)
    assert home.state(WINDOW_OPEN).state == "off"
    await home.advance(freezer, SEC + 3 * SEC)
    assert trv.commands == [OFF, heat(21.0, "heat"), OFF]


async def test_a_restart_holds_a_restored_pause_for_a_contact_first_heard_as_unavailable(
    hass: HomeAssistant,
    home: FakeHome,
    freezer: FrozenDateTimeFactory,
    hass_storage: Any,
) -> None:
    """Concept 5.5: after a restart nobody knows whether that window was open; it is held."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "home": {"mode": "auto", "away_temperature": 16.0},
            "rooms": {"living_room": {"mode": "heat", "window_paused": True}},
        },
    }
    trv = await home.thermostat("climate.a", mode="off", target=21.0)
    home.set("binary_sensor.a", "off")
    home.set("binary_sensor.b", "unavailable")
    hass.set_state(CoreState.starting)
    await home.start(
        config(room("living_room", "climate.a", windows=["binary_sensor.a", "binary_sensor.b"]))
    )
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await home.advance(freezer, 29 * MIN)
    assert trv.commands == [], "held while b may still be open"
    await home.advance(freezer, 1 * MIN + 3 * SEC)
    assert trv.commands == [heat(21.0, "heat")], "and resumed once the grace has run out"
