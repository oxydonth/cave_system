"""Defects found by the device integration tests of work package 7, and one from issue #13.

F1, F2 and F3 are the findings of pull request #14; the reload item is from issue #13.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.cave_heating.const import DOMAIN
from custom_components.cave_heating.driver import Report, ThermostatDriver, Want
from custom_components.cave_heating.logic import Contact, WindowPause
from custom_components.cave_heating.schema import Timing
from tests.cave_heating.fakes import FRITZ, FakeHome, config, room

SEC, MIN = timedelta(seconds=1), timedelta(minutes=1)
T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
LIVING = "climate.cave_living_room"
WINDOW_OPEN = "binary_sensor.cave_living_room_window_open"
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
