"""Issue #13: the follow-ups from the reviews of the controller (work package 10).

* A window contact that is open and whose entity is then removed holds the window pause for
  ``window_unavailable_grace``, as one that goes silent while open does (concept 5.4).
* The three sequences that cost a duplicate command: two are removed, the third is kept and
  pinned, each with the exact commands the thermostat receives.

The tests of the three rules the issue lists as untested are in ``test_driver.py`` (the N2
correction for an unresponsive thermostat, the exemption window after a release on a slow
thermostat) and in ``test_sensor_freshness.py`` (the subscriptions following a room sensor's
device when it gains entities).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from tests.cave_heating.fakes import FakeHome, FakeThermostat, config, room

SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
LIVING = "climate.cave_living_room"
COMFORT = "number.cave_living_room_comfort_temperature"
WINDOW_OPEN = "binary_sensor.cave_living_room_window_open"
PROBLEM = "binary_sensor.cave_living_room_problem"
OFF = ("set_hvac_mode", {"hvac_mode": "off"})


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    """A set_temperature command as a thermostat receives it."""
    data: dict[str, Any] = {"temperature": target}
    if mode is not None:
        data["hvac_mode"] = mode
    return ("set_temperature", data)


async def _call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


# --- An open contact whose entity is removed ----------------------------------------------------


async def test_an_open_contact_whose_entity_is_removed_holds_the_pause_for_the_grace(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Concept 5.4: an entity that does not exist counts as unavailable, so an open contact
    removed without going unavailable first is a contact that went silent while open."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    home.set("binary_sensor.a", "on")
    await home.advance(freezer, 2 * MIN)
    assert trv.commands == [OFF]
    hass.states.async_remove("binary_sensor.a")
    await home.advance(freezer, 29 * MIN)
    assert home.state(WINDOW_OPEN).state == "on", "the removal did not end the pause"
    assert trv.commands == [OFF]
    await home.advance(freezer, MIN + 5 * SEC)
    assert home.state(WINDOW_OPEN).state == "off", "the grace has run out"
    assert trv.commands == [OFF, heat(21.0, "heat")]
    messages = home.state(PROBLEM).attributes["messages"]
    assert "binary_sensor.a (windows[0]) does not exist" in messages
    assert any("stopped reporting while open" in message for message in messages)


async def test_a_closed_contact_whose_entity_is_removed_changes_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The other side: a closed contact that disappears starts and holds nothing."""
    trv = await home.thermostat("climate.a", target=21.0)
    home.set("binary_sensor.a", "off")
    await home.start(config(room("living_room", "climate.a", windows=["binary_sensor.a"])))
    hass.states.async_remove("binary_sensor.a")
    await home.advance(freezer, 5 * MIN)
    assert home.state(WINDOW_OPEN).state == "off"
    assert trv.commands == []


# --- Duplicate commands -------------------------------------------------------------------------


async def _deaf_room(
    home: FakeHome, freezer: FrozenDateTimeFactory, *, slow: bool = False
) -> FakeThermostat:
    """A room at comfort 21 with one thermostat that reports only what the test makes it."""
    trv = await home.thermostat("climate.a", target=21.0)
    trv.deaf = True
    entry: dict[str, Any] = {"entity": "climate.a"} | ({"slow": True} if slow else {})
    await home.start(config(room("living_room", entry)))
    await home.advance(freezer, 5 * MIN)  # past the start's exemption window
    return trv


async def test_a_target_then_an_off_before_the_target_is_reported_sends_each_once(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Issue #13, first sequence: ``22``, ``off`` and not a second ``off``.

    The report of 22 empties the queue while the room wants off; the off in flight is what will
    make the thermostat match, so it is not sent again.
    """
    trv = await _deaf_room(home, freezer)
    await _call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=22)
    await home.advance(freezer, 3 * SEC)
    await _call(hass, "climate", "turn_off", entity_id=LIVING)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0), OFF]
    trv.report(target=22.0)
    await home.advance(freezer, 10 * SEC)
    trv.report(mode="off")
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == [heat(22.0), OFF]
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.a": "ok"}


async def test_an_off_that_never_arrives_after_its_target_is_resent_at_the_timeout(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The other side of the first sequence: the off is lost, so it is resent at
    confirm_timeout (5 min), as any unconfirmed command is."""
    trv = await _deaf_room(home, freezer)
    await _call(hass, "climate", "set_temperature", entity_id=LIVING, temperature=22)
    await home.advance(freezer, 3 * SEC)
    await _call(hass, "climate", "turn_off", entity_id=LIVING)
    await home.advance(freezer, 3 * SEC)
    trv.report(target=22.0)
    await home.advance(freezer, 4 * MIN + 50 * SEC)
    assert trv.commands == [heat(22.0), OFF]
    await home.advance(freezer, 15 * SEC)
    assert trv.commands == [heat(22.0), OFF, OFF]


async def test_an_off_at_the_device_before_a_target_only_command_arrives_is_put_back_at_once(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Review of #29, the other side of the waiting: ``set_temperature 22`` goes out without a
    mode (the thermostat is in heat). Before it arrives, the thermostat is switched off at the
    device (a Sonoff or Bosch keeps its setpoint in off); then 22 arrives and it reports off at
    22. Nothing in flight sets the mode, so it is put back after the debounce."""
    trv = await _deaf_room(home, freezer)
    await _call(hass, "number", "set_value", entity_id=COMFORT, value=22)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0)]
    trv.report(mode="off")
    await home.advance(freezer, 10 * SEC)
    trv.report(target=22.0)
    await home.advance(freezer, 10 * SEC)
    assert trv.commands == [heat(22.0), heat(22.0, "heat")], "put back at once"


async def _off_thermostat(home: FakeHome, freezer: FrozenDateTimeFactory) -> FakeThermostat:
    """A thermostat in ``off`` at 21 in a room that wants heat at 22: one heat command with
    its mode goes out at the start."""
    trv = await home.thermostat("climate.a", target=21.0, mode="off")
    trv.deaf = True
    await home.start(config(room("living_room", "climate.a", comfort_temperature=22.0)))
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0, "heat")]
    return trv


async def test_heat_whose_setpoint_is_reported_before_its_mode_is_sent_once(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Issue #13, second sequence: the device reports the setpoint, and the mode more than
    debounce later. The heat command carrying the mode is in flight: it is not sent twice."""
    trv = await _off_thermostat(home, freezer)
    trv.report(target=22.0)
    await home.advance(freezer, 30 * SEC)
    trv.report(mode="heat")
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == [heat(22.0, "heat")]
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.a": "ok"}


async def test_heat_whose_mode_never_arrives_is_resent_with_its_mode(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The other side of the second sequence: the mode is lost, so the whole command is resent
    at confirm_timeout, mode included."""
    trv = await _off_thermostat(home, freezer)
    trv.report(target=22.0)
    await home.advance(freezer, 4 * MIN + 50 * SEC)
    assert trv.commands == [heat(22.0, "heat")]
    await home.advance(freezer, 15 * SEC)
    assert trv.commands == [heat(22.0, "heat"), heat(22.0, "heat")]


async def test_heat_off_heat_on_a_slow_thermostat_costs_one_command_more(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Issue #13, third sequence, kept: four commands where three would do.

    22, off, and heat at 22 again are sent before the slow thermostat reports anything. Its
    report of 22 in heat matches what is wanted with nothing queued, which confirms it. Its late
    report of the off then differs from what is wanted, exactly as an off set at the device
    would (a FRITZ! dial turned all the way down, concept 5.6), and is corrected. Telling the two
    apart would mean holding back the correction of a thermostat that reports off while heat is
    wanted, for up to confirm_timeout_slow (20 min), on the belief that a command is still in
    flight; the duplicate costs one radio message.
    """
    trv = await _deaf_room(home, freezer, slow=True)
    await _call(hass, "number", "set_value", entity_id=COMFORT, value=22)
    await home.advance(freezer, 3 * SEC)
    await _call(hass, "climate", "turn_off", entity_id=LIVING)
    await home.advance(freezer, 3 * SEC)
    await _call(hass, "climate", "turn_on", entity_id=LIVING)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0), OFF, heat(22.0, "heat")]
    trv.report(target=22.0)
    await home.advance(freezer, MIN)
    trv.report(mode="off")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0), OFF, heat(22.0, "heat"), heat(22.0, "heat")]
    trv.report(mode="heat")
    await home.advance(freezer, 30 * MIN)
    assert trv.commands == [heat(22.0), OFF, heat(22.0, "heat"), heat(22.0, "heat")]
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.a": "ok"}
