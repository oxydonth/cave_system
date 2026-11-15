"""Integration tests for concept section 5.5, how thermostats are driven.

Fake thermostat entities stand in for the devices (see fakes.py); commands reach them through
Home Assistant's real climate services, and time is moved with the test library's freezer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from probatio import MultipleInvalid

from custom_components.cave_heating.const import DOMAIN
from tests.cave_heating.fakes import (
    FRITZ,
    FakeHome,
    FakeNumber,
    FakeSelect,
    FakeSwitch,
    FakeThermostat,
    config,
    room,
)

SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
LIVING = "climate.cave_living_room"
PROBLEM = "binary_sensor.cave_living_room_problem"


def heat(target: float, mode: str | None = None) -> tuple[str, dict[str, Any]]:
    """A set_temperature command as a thermostat receives it."""
    data: dict[str, Any] = {"temperature": target}
    if mode is not None:
        data["hvac_mode"] = mode
    return ("set_temperature", data)


OFF = ("set_hvac_mode", {"hvac_mode": "off"})


async def start(home: FakeHome, *trvs: str | dict[str, Any], **fields: Any) -> None:
    """One room, ``living_room``, with the given thermostats."""
    timing = fields.pop("timing", None)
    top = {"timing": timing} if timing else {}
    await home.start(config(room("living_room", *trvs, **fields), **top))


async def set_comfort(hass: HomeAssistant, value: float) -> None:
    """Change the room's comfort temperature in the UI."""
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.cave_living_room_comfort_temperature", "value": value},
        blocking=True,
    )


def statuses(home: FakeHome) -> dict[str, str]:
    """The problem sensor's thermostats attribute."""
    return dict(home.state(PROBLEM).attributes["thermostats"])


async def past_start_window(home: FakeHome, freezer: FrozenDateTimeFactory) -> None:
    """For one confirmation timeout after a start no report is a hand change: let it pass."""
    await home.advance(freezer, 5 * MIN)


# --- What is wanted: rounding and clamping ------------------------------------------------------


async def test_target_is_rounded_half_up_to_the_thermostat_step(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A whole-degree thermostat gets 21.5 as 22 (half up); a half-degree one gets 21.5."""
    whole = await home.thermostat("climate.whole", target=21.0, step=1.0)
    half = await home.thermostat("climate.half", target=21.0, step=0.5)
    await start(home, "climate.whole", "climate.half")
    await set_comfort(hass, 21.5)
    await home.advance(freezer, 3 * SEC)
    assert whole.commands == [heat(22.0)]
    assert half.commands == [heat(21.5)]


async def test_target_is_clamped_to_the_thermostat_range(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A target above the thermostat's maximum is sent as the maximum, and compared as such."""
    trv = await home.thermostat("climate.low_max", target=19.0, max_temp=20.0)
    await start(home, "climate.low_max")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(20.0)]
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == [heat(20.0)], "20 matches the clamped target: nothing more is sent"
    assert statuses(home) == {"climate.low_max": "ok"}


async def test_thermostat_without_a_step_is_rounded_to_its_precision(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """No target_temp_step, as with FRITZ!: the thermostat's precision is the step."""
    trv = await home.thermostat("climate.no_step", target=21.0, step=None, precision=1.0)
    await start(home, "climate.no_step")
    assert "target_temp_step" not in home.state("climate.no_step").attributes
    await set_comfort(hass, 21.5)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0)]


async def test_thermostat_without_step_or_known_precision_uses_half_degrees(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """With neither a step nor an entity to ask for its precision, 0.5 K is the step."""
    home.set(
        "climate.plain",
        "heat",
        hvac_modes=["heat", "off"],
        temperature=21.0,
        min_temp=5,
        max_temp=30,
    )
    await start(home, "climate.plain")
    driver = home.runtime().drivers["climate.plain"]
    assert driver.report.step == 0.5
    await set_comfort(hass, 21.3)
    assert driver.wanted_target() == 21.5


# --- Commands ------------------------------------------------------------------------------------


async def test_heat_is_one_set_temperature_call_with_the_mode_only_when_needed(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Heat is set_temperature with the target, with hvac_mode only when not in heat already."""
    in_heat = await home.thermostat("climate.in_heat", mode="heat", target=18.0)
    in_off = await home.thermostat("climate.in_off", mode="off", target=18.0)
    await start(home, "climate.in_heat", "climate.in_off")
    await home.advance(freezer, 3 * SEC)
    assert in_heat.commands == [heat(21.0)]
    assert in_off.commands == [heat(21.0, "heat")]
    assert not any(name == "set_hvac_mode" for name, _ in in_off.commands + in_heat.commands), (
        "the mode is never set to heat on its own"
    )


async def test_heat_uses_the_only_heating_mode_of_a_thermostat_without_heat(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat whose only heating mode is auto is driven in auto."""
    trv = await home.thermostat("climate.auto_only", modes=("auto", "off"), mode="off")
    await start(home, "climate.auto_only")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(21.0, "auto")]
    assert statuses(home) == {"climate.auto_only": "ok"}


async def test_off_is_the_off_mode_where_there_is_one_and_otherwise_the_minimum(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Off: climate.set_hvac_mode off, or the minimum target without an off mode."""
    with_off = await home.thermostat("climate.with_off", target=21.0)
    heat_only = await home.thermostat(
        "climate.heat_only", modes=("heat",), target=21.0, min_temp=7.0
    )
    await start(home, "climate.with_off", "climate.heat_only")
    await hass.services.async_call("climate", "turn_off", {"entity_id": LIVING}, blocking=True)
    await home.advance(freezer, 3 * SEC)
    assert with_off.commands == [OFF]
    assert heat_only.commands == [heat(7.0)]
    assert statuses(home) == {"climate.with_off": "ok", "climate.heat_only": "ok"}


# --- When a command is sent ---------------------------------------------------------------------


async def test_nothing_is_sent_to_a_thermostat_that_already_matches(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Not at start, not over time, not on reports that change nothing that matters."""
    trv = await home.thermostat("climate.match", target=21.0)
    await start(home, "climate.match")
    await home.advance(freezer, 2 * timedelta(hours=1), step=10 * MIN)
    trv.report(current=19.0)
    trv.report(current=19.5)
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == []


async def test_changes_within_the_debounce_are_sent_once(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Three changes within two seconds produce one command with the last value."""
    trv = await home.thermostat("climate.debounce", target=21.0)
    await start(home, "climate.debounce")
    for value in (21.5, 22.0, 22.5):
        await set_comfort(hass, value)
        await home.advance(freezer, timedelta(milliseconds=500))
    assert trv.commands == []
    await home.advance(freezer, 2 * SEC)
    assert trv.commands == [heat(22.5)]


async def test_nothing_is_sent_until_the_thermostat_is_available(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat unavailable at start is brought in line when it returns."""
    trv = await home.thermostat("climate.late", target=18.0)
    trv.set_available(False)
    await start(home, "climate.late")
    await home.advance(freezer, 10 * MIN)
    assert trv.commands == []
    assert statuses(home) == {"climate.late": "unavailable"}
    assert home.state(LIVING).state == "unavailable", "no thermostat of the room is available"
    trv.set_available(True)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(21.0)]
    assert home.state(LIVING).state == "heat"


# --- Confirmation, resend, unresponsive ---------------------------------------------------------


async def test_command_is_confirmed_by_the_thermostats_report(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Pending until the thermostat reports heat and the wanted target."""
    trv = await home.thermostat("climate.lagging", target=18.0, delay=2 * MIN)
    await start(home, "climate.lagging")
    await home.advance(freezer, 3 * SEC)
    assert statuses(home) == {"climate.lagging": "pending"}
    assert home.state(PROBLEM).state == "off", "pending is not a problem"
    await home.advance(freezer, 2 * MIN)
    assert statuses(home) == {"climate.lagging": "ok"}
    assert trv.commands == [heat(21.0)]


async def test_unconfirmed_command_is_resent_then_unresponsive_then_retried(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Resent every confirm_timeout up to resend_attempts times; then unresponsive and retried."""
    trv = await home.thermostat("climate.deaf", target=18.0)
    trv.deaf = True
    await start(home, "climate.deaf")
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 1
    for sends in (2, 3, 4):
        await home.advance(freezer, 5 * MIN)
        assert len(trv.commands) == sends
        assert statuses(home) == {"climate.deaf": "pending"}
    await home.advance(freezer, 5 * MIN)
    assert len(trv.commands) == 4, "three resends, then no more"
    assert statuses(home) == {"climate.deaf": "unresponsive"}
    assert home.state(PROBLEM).state == "on"
    assert "climate.deaf does not confirm commands" in home.state(PROBLEM).attributes["messages"]
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "unresponsive_climate.deaf") is not None

    await home.advance(freezer, 29 * MIN)
    assert len(trv.commands) == 4
    await home.advance(freezer, 1 * MIN)
    assert len(trv.commands) == 5, "retried after retry_interval"
    await home.advance(freezer, 30 * MIN)
    assert len(trv.commands) == 6
    assert all(command == heat(21.0) for command in trv.commands)

    trv.deaf = False
    await home.advance(freezer, 30 * MIN)
    assert statuses(home) == {"climate.deaf": "ok"}
    assert home.state(PROBLEM).state == "off"
    assert issues.async_get_issue(DOMAIN, "unresponsive_climate.deaf") is None


@pytest.mark.parametrize("how", ["fritzbox", "slow_true"])
async def test_slow_thermostat_waits_confirm_timeout_slow(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, how: str
) -> None:
    """FRITZ! thermostats are slow by default, others with slow: true: 20 minutes."""
    if how == "fritzbox":
        trv = await home.thermostat(
            "climate.slow", target=18.0, integration=FRITZ, unique_id="fritz-1"
        )
        await start(home, "climate.slow")
    else:
        trv = await home.thermostat("climate.slow", target=18.0)
        await start(home, {"entity": "climate.slow", "slow": True})
    trv.deaf = True
    await set_comfort(hass, 22.0)
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 1
    await home.advance(freezer, 19 * MIN, step=MIN)
    assert len(trv.commands) == 1, "no resend after five minutes"
    await home.advance(freezer, 1 * MIN)
    assert len(trv.commands) == 2


async def test_fast_thermostat_from_a_slow_integration_can_be_marked_fast(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """slow: false overrides the FRITZ! default."""
    trv = await home.thermostat("climate.fritz", target=18.0, integration=FRITZ, unique_id="f-2")
    trv.deaf = True
    await start(home, {"entity": "climate.fritz", "slow": False})
    await home.advance(freezer, 3 * SEC)
    await home.advance(freezer, 5 * MIN)
    assert len(trv.commands) == 2


# --- Refused commands ---------------------------------------------------------------------------


async def test_refused_command_blocks_and_is_retried_every_retry_interval(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A rejected command makes the thermostat blocked; it is not resent into."""
    trv = await home.thermostat("climate.locked", target=18.0)
    trv.refuse = "Key lock enabled"
    await start(home, "climate.locked")
    await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 1
    assert statuses(home) == {"climate.locked": "blocked"}
    assert home.state(PROBLEM).state == "on"
    assert any("Key lock enabled" in m for m in home.state(PROBLEM).attributes["messages"])

    await set_comfort(hass, 22.0)
    await home.advance(freezer, 29 * MIN, step=MIN)
    assert len(trv.commands) == 1, "neither resent nor sent a new target while blocked"
    await home.advance(freezer, 1 * MIN)
    assert trv.commands[1:] == [heat(22.0)]
    assert statuses(home) == {"climate.locked": "blocked"}

    trv.refuse = None
    await home.advance(freezer, 30 * MIN)
    assert trv.commands[2:] == [heat(22.0)]
    assert statuses(home) == {"climate.locked": "ok"}
    assert home.state(PROBLEM).state == "off"


@pytest.mark.parametrize("change", ["mode", "target", "availability"])
async def test_blocked_thermostat_is_retried_sooner_when_it_changes(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, change: str
) -> None:
    """Sooner than retry_interval when its availability, mode or target changes.

    The target change here is to 21, which the controller sent before it was blocked: a change
    to a target it never sent would be a hand change and become the room's override instead.
    """
    trv = await home.thermostat("climate.summer", target=18.0)
    trv.refuse = "Summer mode active"
    await start(home, "climate.summer")
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(21.0)]
    assert statuses(home) == {"climate.summer": "blocked"}
    trv.refuse = None
    trv.report(current=19.0)
    await set_comfort(hass, 22.0)
    await home.advance(freezer, 5 * MIN)
    assert len(trv.commands) == 1, "neither a reading nor a new target retries"
    if change == "mode":
        trv.report(mode="off")
    elif change == "target":
        trv.report(target=21.0)
    else:
        trv.set_available(False)
        trv.set_available(True)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands[1:] == [heat(22.0, "heat" if change == "mode" else None)]
    assert statuses(home) == {"climate.summer": "ok"}


# --- Changes made on the thermostat -------------------------------------------------------------


async def test_dial_turn_becomes_the_room_override_and_the_others_follow(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """manual_settle after the change, the dial's target is the override for the whole room."""
    turned = await home.thermostat("climate.a", target=21.0)
    other = await home.thermostat("climate.b", target=21.0)
    await start(home, "climate.a", "climate.b")
    await past_start_window(home, freezer)
    turned.turn_dial(23.0)
    await home.advance(freezer, 44 * SEC)
    assert other.commands == []
    assert home.state(LIVING).attributes["temperature"] == 21.0
    await home.advance(freezer, 1 * SEC)
    assert home.state(LIVING).attributes["temperature"] == 23.0
    assert home.state(LIVING).attributes["preset_mode"] == "none"
    assert home.state(LIVING).attributes["reason"] == "override"
    assert home.state(LIVING).attributes["override_until"] is not None
    await home.advance(freezer, 3 * SEC)
    assert other.commands == [heat(23.0)]
    assert turned.commands == []


async def test_late_report_of_an_earlier_command_is_not_a_hand_change(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A slow thermostat reporting 22 while 23 waits: no override, no extra command.

    23 is sent two minutes after 22, so the report of 22 stands alone for longer than
    manual_settle: were it taken for a hand change, 22 would become the room's override. Both
    are sent after the start's exemption window, so only the queue of sent targets protects.
    """
    trv = await home.thermostat("climate.slowpoke", target=21.0, delay=3 * MIN)
    await start(home, "climate.slowpoke")
    await past_start_window(home, freezer)
    assert trv.commands == []
    await set_comfort(hass, 22.0)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0)]
    await home.advance(freezer, 2 * MIN)
    await set_comfort(hass, 23.0)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(22.0), heat(23.0)]
    await home.advance(freezer, 1 * MIN)
    assert home.state("climate.slowpoke").attributes["temperature"] == 22.0, "the late report"
    assert statuses(home) == {"climate.slowpoke": "pending"}
    await home.advance(freezer, 1 * MIN + 50 * SEC)
    assert home.state("climate.slowpoke").attributes["temperature"] == 22.0
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert home.state(LIVING).attributes["temperature"] == 23.0
    await home.advance(freezer, 15 * SEC)
    assert home.state("climate.slowpoke").attributes["temperature"] == 23.0
    assert statuses(home) == {"climate.slowpoke": "ok"}
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert trv.commands == [heat(22.0), heat(23.0)]


async def test_dial_turned_to_a_target_sent_earlier_and_since_superseded_counts(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A sent target is forgotten once the thermostat has reported it back."""
    trv = await home.thermostat("climate.dial", target=20.0)
    await start(home, "climate.dial")
    await past_start_window(home, freezer)
    await home.advance(freezer, 3 * SEC)
    await set_comfort(hass, 22.0)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(21.0), heat(22.0)]
    trv.turn_dial(21.0)
    await home.advance(freezer, 46 * SEC)
    assert home.state(LIVING).attributes["temperature"] == 21.0
    assert home.state(LIVING).attributes["reason"] == "override"
    assert trv.commands == [heat(21.0), heat(22.0)]


async def test_thermostat_that_differs_without_a_hand_change_is_corrected(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat that changes its mode by itself is put back; its target is not adopted."""
    trv = await home.thermostat("climate.wilful", target=21.0, modes=("heat", "auto", "off"))
    await start(home, "climate.wilful")
    await past_start_window(home, freezer)
    trv.report(mode="auto", target=17.0)
    await home.advance(freezer, 50 * SEC)
    assert trv.commands == [heat(21.0, "heat")]
    assert home.state(LIVING).attributes["reason"] == "comfort", "nothing adopted"
    trv.report(mode="off")
    await home.advance(freezer, 50 * SEC)
    assert trv.commands[1:] == [heat(21.0, "heat")]


async def test_dial_turned_through_intermediate_values_gives_one_override_and_one_round(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Each intermediate value restarts manual_settle; only the last one is adopted."""
    turned = await home.thermostat("climate.a", target=21.0)
    other = await home.thermostat("climate.b", target=21.0)
    await start(home, "climate.a", "climate.b")
    await past_start_window(home, freezer)
    for target in (21.5, 22.0, 22.5, 23.0):
        turned.turn_dial(target)
        await home.advance(freezer, 2 * SEC)
    await home.advance(freezer, 40 * SEC)
    assert other.commands == []
    await home.advance(freezer, 10 * SEC)
    assert home.state(LIVING).attributes["temperature"] == 23.0
    assert other.commands == [heat(23.0)]
    assert turned.commands == []


async def test_two_dials_turned_within_seconds_give_one_override_and_one_round(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The thermostat that changed last wins; the other is brought to it once."""
    first = await home.thermostat("climate.a", target=21.0)
    last = await home.thermostat("climate.b", target=21.0)
    third = await home.thermostat("climate.c", target=21.0)
    await start(home, "climate.a", "climate.b", "climate.c")
    await past_start_window(home, freezer)
    first.turn_dial(22.0)
    await home.advance(freezer, 5 * SEC)
    last.turn_dial(24.0)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["temperature"] == 24.0
    assert first.commands == [heat(24.0)]
    assert third.commands == [heat(24.0)]
    assert last.commands == []


async def test_dial_turned_back_to_the_wanted_target_within_the_settle_time_adopts_nothing(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The settled state of the dial counts, not a value it passed on the way."""
    trv = await home.thermostat("climate.fidget", target=21.0)
    await start(home, "climate.fidget")
    await past_start_window(home, freezer)
    trv.turn_dial(22.0)
    await home.advance(freezer, 5 * SEC)
    trv.turn_dial(21.0)
    await home.advance(freezer, 60 * SEC)
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert trv.commands == []


async def test_dial_turned_during_a_window_pause_is_not_adopted_and_is_corrected(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Nothing is adopted while the radiators are off (rows 1 to 3)."""
    trv = await home.thermostat("climate.paused", target=21.0)
    home.set("binary_sensor.window", "off")
    await start(home, "climate.paused", windows=["binary_sensor.window"])
    await past_start_window(home, freezer)
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 63 * SEC)
    assert trv.commands == [OFF]
    trv.report(mode="heat", target=24.0)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [OFF, OFF], "corrected at once, not collected as a hand change"
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "window"
    assert home.state(LIVING).attributes["temperature"] == 21.0
    home.set("binary_sensor.window", "off")
    await home.advance(freezer, 2 * MIN + 3 * SEC)
    assert trv.commands[2:] == [heat(21.0, "heat")]


@pytest.mark.parametrize("radiators_off_by", ["room_off", "summer"])
async def test_dial_turned_while_the_room_or_home_is_off_is_not_adopted(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, radiators_off_by: str
) -> None:
    """Rows 1 and 2 as well: a heat-only thermostat at its minimum is put back."""
    trv = await home.thermostat("climate.min", modes=("heat",), target=5.0)
    await start(home, "climate.min")
    if radiators_off_by == "room_off":
        await hass.services.async_call("climate", "turn_off", {"entity_id": LIVING}, blocking=True)
    else:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.cave_home_mode", "option": "off"},
            blocking=True,
        )
    await past_start_window(home, freezer)
    trv.turn_dial(22.0)
    await home.advance(freezer, 3 * SEC)
    assert trv.commands == [heat(5.0)], "corrected at once, not collected as a hand change"
    await home.advance(freezer, 50 * SEC)
    assert trv.commands == [heat(5.0)]
    assert home.state(LIVING).attributes["reason"] in ("room_off", "summer")


async def test_no_report_is_a_hand_change_for_a_confirmation_timeout_after_a_start(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """P10: changes within confirm_timeout of the start are corrected; later ones adopted."""
    trv = await home.thermostat("climate.fresh", target=21.0)
    await start(home, "climate.fresh")
    trv.turn_dial(24.0)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert trv.commands == [heat(21.0)]
    await home.advance(freezer, 3 * MIN)
    trv.turn_dial(24.0)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "comfort", "still within five minutes"
    assert trv.commands == [heat(21.0), heat(21.0)]
    await home.advance(freezer, 1 * MIN)
    trv.turn_dial(23.0)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "override"
    assert home.state(LIVING).attributes["temperature"] == 23.0


async def test_first_report_after_returning_from_unavailable_is_not_a_hand_change(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat that comes back with another target is corrected, not adopted."""
    trv = await home.thermostat("climate.flaky", target=21.0)
    await start(home, "climate.flaky")
    await past_start_window(home, freezer)
    trv.set_available(False)
    await home.advance(freezer, 10 * MIN)
    assert statuses(home) == {"climate.flaky": "unavailable"}
    assert home.state(PROBLEM).state == "on"
    trv._attr_target_temperature = 25.0
    trv.set_available(True)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert trv.commands == [heat(21.0)]
    assert statuses(home) == {"climate.flaky": "ok"}


# --- Hold ----------------------------------------------------------------------------------------


async def _hold(hass: HomeAssistant, entity_id: str, duration: str | None = None) -> None:
    data: dict[str, Any] = {"entity_id": entity_id}
    if duration is not None:
        data["duration"] = duration
    await hass.services.async_call(DOMAIN, "hold", data, blocking=True)


async def test_held_thermostat_receives_nothing_and_nothing_is_adopted_until_released(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Held: status held (not a problem); released by the service and corrected."""
    held = await home.thermostat("climate.held", target=21.0)
    other = await home.thermostat("climate.other", target=21.0)
    await start(home, "climate.held", "climate.other")
    await past_start_window(home, freezer)
    await _hold(hass, "climate.held")
    assert statuses(home) == {"climate.held": "held", "climate.other": "ok"}
    assert home.state(PROBLEM).state == "off", "a hold is a status, not a problem"

    await set_comfort(hass, 22.0)
    held.turn_dial(21.5)
    await home.advance(freezer, 2 * MIN, step=10 * SEC)
    assert held.commands == []
    assert other.commands == [heat(22.0)]
    assert home.state(LIVING).attributes["reason"] == "comfort", "nothing adopted"

    await hass.services.async_call(DOMAIN, "release", {"entity_id": "climate.held"}, blocking=True)
    await home.advance(freezer, 3 * SEC)
    assert held.commands == [heat(22.0)]
    assert statuses(home) == {"climate.held": "ok", "climate.other": "ok"}


async def test_first_report_after_a_hold_is_released_is_not_a_hand_change(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A late report of the device check's own command arrives after the release."""
    trv = await home.thermostat("climate.checked", target=21.0)
    await start(home, "climate.checked")
    await past_start_window(home, freezer)
    await _hold(hass, "climate.checked")
    await hass.services.async_call(
        DOMAIN, "release", {"entity_id": "climate.checked"}, blocking=True
    )
    trv.turn_dial(21.5)
    await home.advance(freezer, 50 * SEC)
    assert home.state(LIVING).attributes["reason"] == "comfort"
    assert trv.commands == [heat(21.0)]


async def test_hold_ends_by_itself_after_its_duration(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """15 minutes unless another duration is given."""
    default = await home.thermostat("climate.default", target=21.0)
    given = await home.thermostat("climate.given", target=21.0)
    await start(home, "climate.default", "climate.given")
    await _hold(hass, "climate.default")
    await _hold(hass, "climate.given", "00:05:00")
    await set_comfort(hass, 22.0)
    await home.advance(freezer, 4 * MIN + 50 * SEC, step=10 * SEC)
    assert given.commands == []
    await home.advance(freezer, 13 * SEC)
    assert given.commands == [heat(22.0)]
    await home.advance(freezer, 9 * MIN + 50 * SEC, step=10 * SEC)
    assert default.commands == []
    await home.advance(freezer, 13 * SEC)
    assert default.commands == [heat(22.0)]


async def test_hold_duration_is_capped_and_only_configured_thermostats_can_be_held(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """A hold longer than four hours, or of an unknown thermostat, is refused."""
    await home.thermostat("climate.capped", target=21.0)
    await start(home, "climate.capped")
    with pytest.raises(MultipleInvalid):
        await _hold(hass, "climate.capped", "04:00:01")
    await _hold(hass, "climate.capped", "04:00:00")
    with pytest.raises(ServiceValidationError):
        await _hold(hass, "climate.not_in_the_file")
    await hass.services.async_call(
        DOMAIN, "release", {"entity_id": "climate.capped"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "release", {"entity_id": "climate.capped"}, blocking=True
    )


# --- Command budget ------------------------------------------------------------------------------


async def test_command_budget_holds_commands_back_until_the_hour_has_passed(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """At most 20 commands an hour; beyond that the problem sensor turns on."""
    trv = await home.thermostat("climate.chatty", target=18.0)
    trv.deaf = True
    feed_input = FakeNumber("number.chatty_external", 0.0)
    await home.add(feed_input)
    home.set("sensor.room", "20.0")
    await start(
        home,
        {"entity": "climate.chatty", "external_temperature": "number.chatty_external"},
        temperature_sensor="sensor.room",
    )
    for index in range(25):
        await set_comfort(hass, 21.0 if index % 2 else 21.5)
        await home.advance(freezer, 3 * SEC)
    assert len(trv.commands) == 20
    problem = home.state(PROBLEM)
    assert problem.state == "on"
    assert any("20 commands per hour" in message for message in problem.attributes["messages"])
    home.set("sensor.room", "21.0")
    await home.advance(freezer, 10 * MIN)
    assert feed_input.writes[-1] == 21.0, "the feed is not counted"
    await home.advance(freezer, 50 * MIN, step=5 * MIN)
    assert len(trv.commands) > 20, "sent again once the hour has passed"


# --- Room temperature to the thermostat ----------------------------------------------------------


async def _feed_room(home: FakeHome, **trv: Any) -> FakeNumber:
    await home.thermostat("climate.fed", target=21.0)
    feed_input = FakeNumber("number.fed_external", 0.0, step=0.1)
    await home.add(feed_input)
    home.set("sensor.room", "20.5")
    home.set("binary_sensor.window", "off")
    await start(
        home,
        {"entity": "climate.fed", "external_temperature": "number.fed_external", **trv},
        temperature_sensor="sensor.room",
        windows=["binary_sensor.window"],
    )
    return feed_input


async def test_feed_writes_on_change_and_at_least_every_max_interval(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """feed_threshold and feed_min_interval for changes; feed_max_interval regardless."""
    feed_input = await _feed_room(home)
    await home.settle()
    assert feed_input.writes == [20.5], "written at once when the feed starts"
    home.set("sensor.room", "20.6")
    await home.advance(freezer, 11 * MIN)
    assert feed_input.writes == [20.5], "0.1 K is below the threshold"
    home.set("sensor.room", "20.8")
    await home.settle()
    assert feed_input.writes == [20.5, 20.8], "0.3 K and more than ten minutes"
    home.set("sensor.room", "21.2")
    await home.advance(freezer, 9 * MIN)
    assert feed_input.writes == [20.5, 20.8], "changed, but not ten minutes yet"
    await home.advance(freezer, 1 * MIN)
    assert feed_input.writes == [20.5, 20.8, 21.2]
    await home.advance(freezer, 24 * MIN, step=4 * MIN)
    assert feed_input.writes == [20.5, 20.8, 21.2]
    home.set("sensor.room", "21.2")
    await home.advance(freezer, 1 * MIN)
    assert feed_input.writes == [20.5, 20.8, 21.2, 21.2], "unchanged, after 25 minutes"


@pytest.mark.parametrize("stop", ["home_off", "room_off", "sensor_unavailable", "sensor_stale"])
async def test_feed_stops_and_writes_at_once_when_it_starts_again(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, stop: str
) -> None:
    """Stops with home mode off, the room off, the sensor unavailable or stale."""
    feed_input = await _feed_room(home)
    await home.settle()
    assert feed_input.writes == [20.5]
    if stop == "home_off":
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.cave_home_mode", "option": "off"},
            blocking=True,
        )
    elif stop == "room_off":
        await hass.services.async_call("climate", "turn_off", {"entity_id": LIVING}, blocking=True)
    elif stop == "sensor_unavailable":
        home.set("sensor.room", "unavailable")
    else:
        await home.advance(freezer, 3 * timedelta(hours=1), step=25 * MIN)
        assert len(feed_input.writes) == 1 + 7, "every 25 minutes until it goes stale"
        feed_input.commands.clear()
        feed_input.commands.append(("set_value", {"value": 20.5}))
    await home.advance(freezer, 2 * timedelta(hours=1), step=10 * MIN)
    assert feed_input.writes == [20.5], "no writes while stopped"
    if stop == "home_off":
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.cave_home_mode", "option": "auto"},
            blocking=True,
        )
    elif stop == "room_off":
        await hass.services.async_call("climate", "turn_on", {"entity_id": LIVING}, blocking=True)
    home.set("sensor.room", "20.5")
    await home.settle()
    assert feed_input.writes == [20.5, 20.5], "written at once when it starts again"


async def test_feed_continues_during_a_window_pause(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The window pause does not stop the feed."""
    feed_input = await _feed_room(home)
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 2 * MIN)
    assert home.state(LIVING).attributes["reason"] == "window"
    home.set("sensor.room", "18.0")
    await home.advance(freezer, 10 * MIN)
    assert feed_input.writes == [20.5, 18.0]


async def test_feed_waits_while_the_thermostat_is_held(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A held thermostat receives nothing, also no temperature."""
    feed_input = await _feed_room(home)
    await _hold(hass, "climate.fed", "01:00:00")
    home.set("sensor.room", "19.0")
    await home.advance(freezer, 59 * MIN, step=10 * MIN)
    assert feed_input.writes == [20.5]
    await home.advance(freezer, 1 * MIN)
    assert feed_input.writes == [20.5, 19.0]


# --- Device settings -----------------------------------------------------------------------------

SENSOR_SELECT = "select.trv_temperature_sensor_select"
WINDOW_SWITCH = "switch.trv_open_window"


async def _settings_room(
    home: FakeHome, *, select: str = "internal", switch: bool = True
) -> tuple[FakeThermostat, FakeSelect, FakeSwitch, FakeNumber]:
    trv = await home.thermostat("climate.trv", target=21.0)
    sensor_select = FakeSelect(SENSOR_SELECT, ["internal", "external"], select)
    window_switch = FakeSwitch(WINDOW_SWITCH, switch)
    feed_input = FakeNumber("number.trv_external", 0.0)
    await home.add(sensor_select, window_switch, feed_input)
    home.set("sensor.room", "20.5")
    await start(
        home,
        {
            "entity": "climate.trv",
            "external_temperature": "number.trv_external",
            "settings": {SENSOR_SELECT: "external", WINDOW_SWITCH: "off"},
        },
        temperature_sensor="sensor.room",
    )
    return trv, sensor_select, window_switch, feed_input


async def test_settings_are_written_when_the_configuration_is_loaded(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Each setting that differs is written once; one that matches is left alone."""
    _trv, select, switch, _feed = await _settings_room(home)
    await home.settle()
    assert select.commands == [("select_option", {"option": "external"})]
    assert switch.commands == [("turn_off", {})]
    await home.advance(freezer, timedelta(hours=2), step=10 * MIN)
    assert len(select.commands) == 1
    assert len(switch.commands) == 1


async def test_settings_that_already_match_are_not_written(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """Nothing is written when the device is already set."""
    _trv, select, switch, _feed = await _settings_room(home, select="external", switch=False)
    await home.settle()
    assert select.commands == []
    assert switch.commands == []


async def test_setting_the_device_changes_by_itself_is_left_alone(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Settings are not watched in between: a Sonoff switching its sensor is not fought."""
    _trv, select, _switch, _feed = await _settings_room(home)
    await home.settle()
    select.change("internal")
    await home.advance(freezer, timedelta(hours=1), step=10 * MIN)
    assert select.commands == [("select_option", {"option": "external"})]
    assert home.state(SENSOR_SELECT).state == "internal"


async def test_settings_are_written_when_the_thermostat_returns(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The second moment: the thermostat returns from being unavailable."""
    trv, select, _switch, _feed = await _settings_room(home)
    await home.settle()
    select.change("internal")
    trv.set_available(False)
    await home.advance(freezer, 10 * MIN)
    trv.set_available(True)
    await home.settle()
    assert select.commands == [("select_option", {"option": "external"})] * 2


async def test_settings_are_written_confirm_timeout_after_the_feed_starts_again(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The third moment: a Sonoff that does not return to the external sensor by itself."""
    _trv, select, _switch, feed_input = await _settings_room(home)
    await home.settle()
    home.set("sensor.room", "unavailable")
    await home.settle()
    select.change("internal")
    await home.advance(freezer, 30 * MIN, step=10 * MIN)
    home.set("sensor.room", "20.0")
    await home.settle()
    assert feed_input.writes[-1] == 20.0
    await home.advance(freezer, 4 * MIN + 50 * SEC)
    assert len(select.commands) == 1
    await home.advance(freezer, 10 * SEC)
    assert select.commands == [("select_option", {"option": "external"})] * 2


async def test_setting_refused_by_home_assistant_is_reported(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """For example a wrong option name: the problem sensor says so."""
    trv = await home.thermostat("climate.trv", target=21.0)
    select = FakeSelect(SENSOR_SELECT, ["internal", "external"], "internal")
    select.refuse = "Option remote_temperature is not valid"
    await home.add(select)
    await start(home, {"entity": "climate.trv", "settings": {SENSOR_SELECT: "remote_temperature"}})
    await home.settle()
    problem = home.state(PROBLEM)
    assert problem.state == "on"
    [message] = problem.attributes["messages"]
    assert message.startswith(f"setting {SENSOR_SELECT} was refused: Option remote_temperature")
    assert trv.commands == []


async def test_numeric_setting_is_written_as_a_number(hass: HomeAssistant, home: FakeHome) -> None:
    """A number entity in the settings block, for example a temperature offset."""
    await home.thermostat("climate.trv", target=21.0)
    offset = FakeNumber("number.trv_offset", 0.0)
    await home.add(offset)
    await start(home, {"entity": "climate.trv", "settings": {"number.trv_offset": 1.5}})
    await home.settle()
    assert offset.writes == [1.5]


async def test_hand_change_still_settling_when_a_window_pause_starts_is_not_adopted(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Whether to adopt is decided when the changes have settled; then the radiators are off."""
    trv = await home.thermostat("climate.late_turn", target=21.0)
    home.set("binary_sensor.window", "off")
    await start(home, "climate.late_turn", windows=["binary_sensor.window"], window_open_delay=0)
    await past_start_window(home, freezer)
    trv.turn_dial(23.0)
    await home.advance(freezer, 10 * SEC)
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 30 * SEC)
    assert trv.commands == [], "nothing is sent while the hand changes settle"
    await home.advance(freezer, 10 * SEC)
    assert home.state(LIVING).attributes["reason"] == "window"
    assert home.state(LIVING).attributes["temperature"] == 21.0
    assert trv.commands == [OFF]
