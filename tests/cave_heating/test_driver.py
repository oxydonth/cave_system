"""Unit tests of driver.py, the per-thermostat state of concept section 5.5, with simulated time.

The integration tests in test_thermostats.py prove each rule through Home Assistant; these pin
down the edges of the state machine directly.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.cave_heating.driver import (
    Feed,
    Phase,
    Report,
    Status,
    ThermostatDriver,
    Want,
    round_target,
    setting_call,
    setting_matches,
)
from custom_components.cave_heating.schema import Timing

T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
SEC = timedelta(seconds=1)
TIMING = Timing()


def report(target: float | None = 21.0, mode: str = "heat", **fields: object) -> Report:
    """A thermostat report with the usual capabilities."""
    values: dict[str, object] = {
        "mode": mode,
        "target": target,
        "current": 20.0,
        "modes": ("heat", "off"),
        "min_temp": 5.0,
        "max_temp": 30.0,
        "step": 0.5,
    }
    values.update(fields)
    return Report(**values)  # type: ignore[arg-type]


def started(target: float = 21.0, want: float = 21.0) -> ThermostatDriver:
    """A driver that has seen its first report after the start."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False)
    driver.set_want(Want(heat=True, target=want), T0)
    driver.on_report(report(target), T0, initial=True)
    driver.on_report(report(target, current=20.5), T0)
    return driver


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        (21.25, 0.5, 21.5),
        (21.24, 0.5, 21.0),
        (21.5, 1.0, 22.0),
        (20.5, 1.0, 21.0),
        (21.05, 0.1, 21.1),
        (21.3, 0.0, 21.5),
        (35.0, 0.5, 30.0),
        (2.0, 0.5, 5.0),
    ],
)
def test_round_target_half_up_then_clamp(value: float, step: float, expected: float) -> None:
    """Rounded to the step, half up, then clamped; a step of zero falls back to 0.5 K."""
    assert round_target(value, step, 5.0, 30.0) == expected


@pytest.mark.parametrize(
    ("in_heat_mode", "target_changed", "not_known"),
    list(itertools.product([True, False], repeat=3)),
)
def test_a_hand_change_needs_every_condition(
    in_heat_mode: bool, target_changed: bool, not_known: bool
) -> None:
    """Heating mode, a changed target, and a target neither wanted, sent nor confirmed."""
    driver = started(target=21.0)
    driver.sent = [22.0]
    new_target = 23.0 if not_known else 22.0
    if not target_changed:
        driver.on_report(report(new_target, current=19.0), T0)
    outcome = driver.on_report(
        report(new_target, mode="heat" if in_heat_mode else "off"), T0, adopt=True
    )
    is_hand = in_heat_mode and target_changed and not_known
    assert (outcome.hand_target == 23.0) is is_hand


def test_nothing_is_adopted_when_adoption_is_not_allowed() -> None:
    """While the radiators are off the runtime passes adopt=False."""
    driver = started()
    assert driver.on_report(report(24.0), T0, adopt=False).hand_target is None
    assert driver.send_due == T0 + TIMING.debounce, "corrected instead"


def test_the_last_confirmed_target_is_not_a_hand_change() -> None:
    """A thermostat that reverts to its last confirmed target is corrected, not adopted."""
    driver = started(target=21.0)
    driver.set_want(Want(heat=True, target=22.0), T0)
    command = driver.tick(T0 + TIMING.debounce)
    assert command is not None
    driver.on_report(report(20.0), T0 + 3 * SEC, adopt=True)
    outcome = driver.on_report(report(21.0), T0 + 4 * SEC, adopt=True)
    assert outcome.hand_target is None


def test_sent_targets_are_forgotten_only_when_a_later_one_is_confirmed() -> None:
    """Targets sent after the confirmed one are kept."""
    driver = started(target=20.0, want=20.0)
    for target in (21.0, 22.0):
        driver.set_want(Want(heat=True, target=target), T0)
        driver.send_due = T0
        driver.tick(T0)
    assert driver.sent == [21.0, 22.0]
    driver.want = Want(heat=True, target=21.0)
    driver.on_report(report(21.0), T0 + SEC)
    assert driver.sent == [22.0]
    assert driver.last_confirmed == 21.0


def test_a_target_already_reported_when_the_send_falls_due_is_not_sent() -> None:
    """Nothing is sent to a thermostat that matches by the time the debounce has passed."""
    driver = started(target=21.0)
    driver.set_want(Want(heat=True, target=22.0), T0)
    driver.on_report(report(22.0), T0 + SEC)
    assert driver.tick(T0 + TIMING.debounce) is None
    assert driver.status(T0) is Status.OK


def test_a_due_send_waits_while_the_room_settles() -> None:
    """may_send=False holds a due command back without losing it."""
    driver = started(target=21.0)
    driver.set_want(Want(heat=True, target=22.0), T0)
    assert driver.tick(T0 + TIMING.debounce, may_send=False) is None
    assert driver.next_deadline(T0 + TIMING.debounce) is None, "no busy timer while waiting"
    assert driver.tick(T0 + TIMING.debounce + SEC) is not None


def test_budget_counts_every_command_within_a_sliding_hour() -> None:
    """The 21st command waits until the oldest is an hour old."""
    driver = started(target=21.0)
    for index in range(21):
        driver.set_want(Want(heat=True, target=22.0 + (index % 2)), T0)
        driver.send_due = T0 + index * SEC
        command = driver.tick(T0 + index * SEC)
        assert (command is not None) is (index < 20)
    assert driver.budget_exhausted
    assert driver.send_due == T0 + timedelta(hours=1)
    assert driver.tick(T0 + timedelta(hours=1)) is not None
    assert not driver.budget_exhausted


def test_off_without_an_off_mode_is_the_minimum_and_confirmed_by_it() -> None:
    """Off by the minimum target where there is no off mode."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False)
    driver.set_want(Want(heat=False, target=21.0), T0)
    driver.on_report(report(21.0, modes=("heat",), min_temp=7.0), T0, initial=True)
    command = driver.tick(T0 + TIMING.debounce)
    assert command is not None
    assert (command.service, dict(command.data), command.target) == (
        "set_temperature",
        {"temperature": 7.0},
        7.0,
    )
    driver.on_report(report(7.0, modes=("heat",), min_temp=7.0), T0 + 3 * SEC)
    assert driver.phase is Phase.OK


def test_missing_and_unavailable_status_and_return() -> None:
    """Missing, unavailable, and back with settings due (returned)."""
    driver = started()
    driver.on_report(None, T0, exists=False)
    assert driver.status(T0) is Status.MISSING
    driver.on_report(None, T0)
    assert driver.status(T0) is Status.UNAVAILABLE
    outcome = driver.on_report(report(21.0), T0)
    assert outcome.returned
    assert driver.status(T0) is Status.OK


def test_release_without_a_hold_changes_nothing() -> None:
    """Releasing a thermostat that is not held is harmless."""
    driver = started()
    driver.release(T0)
    assert driver.first_report is False
    assert driver.diagnostics(T0)["status"] == "ok"


def test_feed_threshold_and_intervals() -> None:
    """The feed rules on their own."""
    feed = Feed(TIMING)
    step = feed.step(T0, 20.0, allowed=True)
    assert step is not None
    assert not step.restarted
    feed.wrote(20.0, T0)
    assert feed.step(T0 + timedelta(minutes=10), 20.1, allowed=True) is None
    assert feed.next_deadline(T0, 20.1) == T0 + timedelta(minutes=25)
    assert feed.next_deadline(T0, 20.2) == T0 + timedelta(minutes=10)
    assert feed.step(T0 + timedelta(minutes=10), 20.2, allowed=True) is not None
    assert feed.step(T0, 20.0, allowed=True, paused=True) is None
    assert feed.step(T0, None, allowed=True) is None
    assert not feed.active
    restart = feed.step(T0, 20.0, allowed=True)
    assert restart is not None
    assert restart.restarted


@pytest.mark.parametrize(
    ("entity_id", "wanted", "state", "matches"),
    [
        ("select.s", "external", "external", True),
        ("select.s", "external", "internal", False),
        ("switch.w", "off", "off", True),
        ("switch.w", "off", "on", False),
        ("number.n", 1.5, "1.5", True),
        ("number.n", 1.5, "1.0", False),
        ("number.n", 1.5, "unknown", False),
    ],
)
def test_setting_matches(entity_id: str, wanted: str | float, state: str, matches: bool) -> None:
    """Whether a device setting is already at the configured value."""
    assert setting_matches(entity_id, wanted, state) is matches


def test_setting_calls() -> None:
    """The service call for each kind of setting."""
    assert setting_call("select.s", "external") == (
        "select",
        "select_option",
        {"option": "external"},
    )
    assert setting_call("switch.w", "on") == ("switch", "turn_on", {})
    assert setting_call("switch.w", "off") == ("switch", "turn_off", {})
    assert setting_call("number.n", 1.5) == ("number", "set_value", {"value": 1.5})
