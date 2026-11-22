"""Unit tests of driver.py, the per-thermostat state of concept section 5.5, with simulated time.

The integration tests in test_thermostats.py prove each rule through Home Assistant; these pin
down the edges of the state machine directly.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.cave_heating.driver import (
    Command,
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
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
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
    """A driver started an hour before T0, so the start's exemption window is over."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0 - HOUR)
    driver.set_want(Want(heat=True, target=want), T0 - HOUR)
    driver.on_report(report(target), T0 - HOUR)
    return driver


def send(driver: ThermostatDriver, target: float, now: datetime) -> Command:
    """Want ``target`` and send it at ``now``."""
    driver.set_want(Want(heat=True, target=target), now)
    driver.send_due = now
    command = driver.tick(now)
    assert command is not None
    return command


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
    """Heating mode, a changed target, and a target neither wanted, queued nor confirmed."""
    driver = started(target=21.0)
    driver.queue = [22.0]
    new_target = 23.0 if not_known else 22.0
    if not target_changed:
        driver.on_report(report(new_target, current=19.0), T0)
    outcome = driver.on_report(
        report(new_target, mode="heat" if in_heat_mode else "off"), T0, adopt=True
    )
    is_hand = in_heat_mode and target_changed and not_known
    assert (outcome.hand_target is not None) is is_hand


def test_nothing_is_adopted_when_adoption_is_not_allowed() -> None:
    """While the radiators are off the runtime passes adopt=False."""
    driver = started()
    assert driver.on_report(report(24.0), T0, adopt=False).hand_target is None
    assert driver.send_due == T0 + TIMING.debounce, "corrected instead"


def test_the_last_confirmed_target_is_not_a_hand_change() -> None:
    """A thermostat that reverts to its last confirmed target is corrected, not adopted."""
    driver = started(target=21.0)
    send(driver, 22.0, T0)
    driver.on_report(report(20.0), T0 + 3 * SEC, adopt=True)
    outcome = driver.on_report(report(21.0), T0 + 4 * SEC, adopt=True)
    assert outcome.hand_target is None


# --- The queue of sent targets (P3) -------------------------------------------------------------


def test_late_reports_of_a_target_sent_twice_are_consumed_in_order() -> None:
    """21, 25 and 21 sent; the reports 21, 25, 21 each consume their entry, none is adopted."""
    driver = started(target=18.0, want=18.0)
    for index, target in enumerate((21.0, 25.0, 21.0)):
        send(driver, target, T0 + index * MIN)
    assert driver.queue == [21.0, 25.0, 21.0]
    for index, target in enumerate((21.0, 25.0, 21.0)):
        outcome = driver.on_report(report(target), T0 + (5 + index) * MIN, adopt=True)
        assert outcome.hand_target is None, f"the report of {target} is a late report"
    assert driver.queue == []
    assert driver.phase is Phase.OK, "confirmed once it matches and nothing is queued"


def test_a_match_while_entries_remain_stays_pending_until_the_timeout() -> None:
    """The queue is dropped when the confirmation timeout passes with the thermostat matching."""
    driver = started(target=18.0, want=18.0)
    send(driver, 21.0, T0)
    send(driver, 25.0, T0 + MIN)
    send(driver, 21.0, T0 + 2 * MIN)
    assert driver.queue == [21.0, 25.0, 21.0]
    driver.on_report(report(21.0), T0 + 3 * MIN)
    assert driver.queue == [25.0, 21.0], "up to and including the first 21"
    assert driver.matches()
    assert driver.phase is Phase.PENDING, "it matches, but a queued command may still report"
    assert driver.tick(T0 + 2 * MIN + TIMING.confirm_timeout) is None, "no resend: it matches"
    assert driver.queue == []
    assert driver.phase is Phase.OK
    outcome = driver.on_report(report(25.0), T0 + 10 * MIN, adopt=True)
    assert outcome.hand_target == 25.0, "after the timeout no earlier command can report"


def test_a_target_the_thermostat_already_has_is_not_queued() -> None:
    """A heat command that only changes the mode is not reported back as a target change."""
    driver = started(target=21.0)
    driver.on_report(report(21.0, mode="off"), T0)
    command = driver.tick(T0 + TIMING.debounce)
    assert command is not None
    assert dict(command.data) == {"temperature": 21.0, "hvac_mode": "heat"}
    assert driver.queue == []
    driver.on_report(report(21.0), T0 + 3 * SEC)
    assert driver.phase is Phase.OK


# --- No hand change for a confirmation timeout (P10) --------------------------------------------


@pytest.mark.parametrize("moment", ["start", "return", "release"])
def test_no_hand_change_for_one_confirmation_timeout_after_each_moment(moment: str) -> None:
    """After a start, a return from unavailable, or a release, changes are corrected."""
    if moment == "start":
        driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0)
        driver.set_want(Want(heat=True, target=21.0), T0)
        driver.on_report(report(21.0), T0)
    else:
        driver = started()
        if moment == "return":
            driver.on_report(None, T0)
            driver.on_report(report(21.0), T0)
        else:
            driver.hold(T0 - MIN, timedelta(minutes=15))
            driver.release(T0)
    inside = driver.on_report(report(23.0), T0 + TIMING.confirm_timeout - SEC, adopt=True)
    assert inside.hand_target is None
    assert driver.send_due is not None, "corrected"
    driver.on_report(report(21.0), T0 + TIMING.confirm_timeout)
    after = driver.on_report(report(23.5), T0 + TIMING.confirm_timeout + SEC, adopt=True)
    assert after.hand_target == 23.5


@pytest.mark.parametrize("moment", ["start", "return", "release"])
def test_a_slow_thermostat_is_exempt_for_confirm_timeout_too(moment: str) -> None:
    """P10 as decided: confirm_timeout for every thermostat, not confirm_timeout_slow.

    ``release`` is issue #13's: the window after a hold ends, on a slow thermostat.
    """
    start = T0 - HOUR if moment == "release" else T0
    driver = ThermostatDriver("climate.t", TIMING, slow=True, now=start)
    driver.set_want(Want(heat=True, target=21.0), start)
    driver.on_report(report(21.0), start)
    if moment == "return":
        driver.on_report(None, T0)
        driver.on_report(report(21.0), T0)
    if moment == "release":
        driver.hold(T0 - 10 * MIN, timedelta(minutes=15))
        driver.release(T0)
    inside = driver.on_report(report(23.0), T0 + TIMING.confirm_timeout - SEC, adopt=True)
    assert inside.hand_target is None
    driver.on_report(report(21.0), T0 + TIMING.confirm_timeout)
    after = driver.on_report(report(22.5), T0 + TIMING.confirm_timeout + MIN, adopt=True)
    assert after.hand_target == 22.5, "a dial turned six minutes after a short outage counts"


# --- Blocked (P5), budget (P6), refusals (O3), modes in flight (O1) -----------------------------


def test_a_blocked_thermostats_target_change_is_never_adopted() -> None:
    """It is the trigger to try again."""
    driver = started(target=21.0, want=21.0)
    command = send(driver, 22.0, T0)
    assert driver.on_refused(T0, "holiday mode", command)
    outcome = driver.on_report(report(17.0), T0 + MIN, adopt=True)
    assert outcome.hand_target is None
    assert driver.send_due == T0 + MIN + TIMING.debounce


def test_a_refusal_after_the_thermostat_confirmed_changes_nothing() -> None:
    """The service call failed late, but the thermostat already shows what is wanted."""
    driver = started(target=21.0)
    command = send(driver, 22.0, T0)
    driver.on_report(report(22.0), T0 + SEC)
    assert not driver.on_refused(T0 + 2 * SEC, "timeout", command)
    assert driver.status(T0 + 2 * SEC) is Status.OK


def test_the_mode_of_commands_in_flight_decides_whether_heat_needs_the_mode() -> None:
    """Off, then heat at a new target before the off has arrived: heat includes the mode.

    The report still says heat, but the thermostat will be off once the command in flight
    arrives; without the mode the heat command would leave it off and need a fourth command.
    """
    driver = started(target=20.0, want=20.0)
    driver.set_want(Want(heat=False, target=20.0), T0)
    off = driver.tick(T0 + TIMING.debounce)
    assert off is not None
    assert off.service == "set_hvac_mode"
    driver.set_want(Want(heat=True, target=21.0), T0 + MIN)
    again = driver.tick(T0 + MIN + TIMING.debounce)
    assert again is not None
    assert dict(again.data) == {"temperature": 21.0, "hvac_mode": "heat"}


def test_budget_counts_every_command_within_a_sliding_hour() -> None:
    """The 21st command waits until the oldest is an hour old."""
    driver = started(target=21.0)
    for index in range(21):
        driver.set_want(Want(heat=True, target=22.0 + (index % 2)), T0)
        driver.send_due = T0 + index * SEC
        command = driver.tick(T0 + index * SEC)
        assert (command is not None) is (index < 20)
    assert driver.budget_exhausted
    assert driver.send_due == T0 + HOUR
    assert driver.tick(T0 + HOUR) is not None
    assert not driver.budget_exhausted


def _disagree_for(driver: ThermostatDriver, hours: int) -> int:
    """A thermostat that falls back to auto after every command; returns the commands sent."""
    sent = 0
    now = T0
    end = T0 + hours * HOUR
    while now < end and driver.status(now) is not Status.UNRESPONSIVE:
        if driver.tick(now) is not None:
            sent += 1
            driver.on_report(report(21.0), now + SEC)
            driver.on_report(
                report(21.0, mode="auto", modes=("heat", "auto", "off")), now + 2 * SEC
            )
        now += MIN
    return sent


def test_a_thermostat_whose_budget_is_used_up_hours_in_a_row_becomes_unresponsive() -> None:
    """budget_exhausted_hours (3): then only retry_interval, until it agrees for a whole one."""
    driver = started(target=21.0)
    driver.on_report(report(21.0, mode="auto", modes=("heat", "auto", "off")), T0)
    sent = _disagree_for(driver, 5)
    assert driver.status(T0) is Status.UNRESPONSIVE
    assert driver.disagreeing
    assert 60 <= sent <= 81, "about 20 an hour for three hours"
    start = driver.deadline
    assert start is not None
    now = start - TIMING.retry_interval + MIN
    assert driver.tick(now) is None, "nothing between retries"
    driver.on_report(report(21.0, mode="auto", modes=("heat", "auto", "off")), now)
    assert driver.send_due is None, "a differing report sends nothing"
    retry = driver.tick(start)
    assert retry is not None, "tried again after retry_interval"
    driver.on_report(report(21.0), start + SEC)
    assert driver.status(start + SEC) is Status.UNRESPONSIVE, "one match is not enough"
    assert driver.tick(start + TIMING.retry_interval) is None
    assert driver.status(start + TIMING.retry_interval) is Status.OK, "matched for a whole one"


def test_budget_streak_ends_after_an_hour_without_exhaustion() -> None:
    """Being held back now and then is not disagreement."""
    driver = started(target=21.0)
    driver._budget_used_up(T0)
    driver._budget_used_up(T0 + 2 * HOUR)
    driver._budget_used_up(T0 + 4 * HOUR)
    assert not driver.disagreeing


def _disagreeing() -> ThermostatDriver:
    """A thermostat made unresponsive by the budget, its next retry due."""
    driver = started(target=21.0)
    driver.on_report(report(21.0, mode="auto", modes=("heat", "auto", "off")), T0)
    _disagree_for(driver, 5)
    assert driver.disagreeing
    return driver


def test_a_disagreeing_thermostat_stays_unresponsive_if_it_disagreed_since_the_last_retry() -> None:
    """Reading 3: matching at a retry is not enough after a differing report in between."""
    driver = _disagreeing()
    first = driver.deadline
    assert first is not None
    assert driver.tick(first) is not None, "the retry"
    driver.on_report(report(21.0), first + SEC)
    driver.on_report(report(21.0, mode="auto", modes=("heat", "auto", "off")), first + MIN)
    driver.on_report(report(21.0), first + 2 * MIN)
    second = first + TIMING.retry_interval
    assert driver.tick(second) is None, "it matches: nothing to send"
    assert driver.status(second) is Status.UNRESPONSIVE, "but it disagreed in between"
    assert driver.tick(second + TIMING.retry_interval) is None
    assert driver.status(second + TIMING.retry_interval) is Status.OK


def test_a_new_wanted_value_waits_for_the_retry_of_a_disagreeing_thermostat() -> None:
    """Reading 3: only retry_interval tries it again, also with something new to send."""
    driver = _disagreeing()
    due = driver.deadline
    assert due is not None
    driver.set_want(Want(heat=True, target=23.0), due - 10 * MIN)
    assert driver.send_due is None
    assert driver.tick(due - 5 * MIN) is None
    retry = driver.tick(due)
    assert retry is not None
    assert retry.data["temperature"] == 23.0


def test_a_report_with_an_unchanged_target_consumes_nothing() -> None:
    """P3: only a report whose target has changed removes queued targets.

    21, 25, 21 are in flight; the late report of the first 21 arrives, then a reading with the
    target still at 21. That reading must not take the second 21 out of the queue, or the late
    report of 25 that follows would look like a hand change.
    """
    driver = started(target=18.0, want=18.0)
    for index, target in enumerate((21.0, 25.0, 21.0)):
        send(driver, target, T0 + index * MIN)
    driver.on_report(report(21.0), T0 + 5 * MIN)
    assert driver.queue == [25.0, 21.0]
    driver.on_report(report(21.0, current=19.5), T0 + 6 * MIN)
    assert driver.queue == [25.0, 21.0]
    late = driver.on_report(report(25.0), T0 + 7 * MIN, adopt=True)
    assert late.hand_target is None, "the late report of 25 is still recognised"


def test_a_refused_commands_target_leaves_the_queue() -> None:
    """Reading 5: a refused command was never sent to the device."""
    driver = started(target=21.0)
    command = send(driver, 22.0, T0)
    assert driver.queue == [22.0]
    driver.on_refused(T0 + SEC, "key lock", command)
    assert driver.queue == []


def test_the_queue_is_cleared_when_the_thermostat_returns() -> None:
    """Reading 5: the exemption window covers late reports after a return instead."""
    driver = started(target=21.0)
    send(driver, 22.0, T0)
    assert driver.queue == [22.0]
    driver.on_report(None, T0 + MIN)
    driver.on_report(report(20.0), T0 + 2 * MIN)
    assert driver.queue == []
    late = driver.on_report(report(22.0), T0 + 2 * MIN + TIMING.confirm_timeout, adopt=True)
    assert late.hand_target is None, "22 is what is wanted"


def test_a_report_that_empties_the_queue_while_differing_is_corrected_at_once() -> None:
    """N2 and the concept's confirmation rule."""
    driver = started(target=21.0)
    send(driver, 25.0, T0)
    driver.set_want(Want(heat=True, target=21.0), T0 + 10 * SEC)
    assert driver.send_due is None, "it still reports 21: nothing to send yet"
    driver.on_report(report(25.0), T0 + 20 * SEC)
    assert driver.queue == []
    assert driver.send_due == T0 + 20 * SEC + TIMING.debounce
    correction = driver.tick(T0 + 20 * SEC + TIMING.debounce)
    assert correction is not None
    assert correction.data["temperature"] == 21.0


def test_an_unresponsive_thermostat_that_reports_its_last_target_is_corrected_at_once() -> None:
    """Issue #13: the correction when a report empties the queue applies while unresponsive.

    25 is sent and never confirmed until the resends are used up. Meanwhile 21 is wanted again,
    which the thermostat still reports, so nothing is sent. Then the late report of 25 arrives:
    the thermostat answers after all, and it is corrected after the debounce, not at the next
    retry_interval.
    """
    driver = started(target=21.0)
    now = T0
    send(driver, 25.0, now)
    while driver.phase is not Phase.UNRESPONSIVE:
        now += TIMING.confirm_timeout
        driver.tick(now)
    assert driver.queue == [25.0]
    driver.set_want(Want(heat=True, target=21.0), now + SEC)
    assert driver.send_due is None, "it still reports 21"
    driver.on_report(report(25.0), now + 2 * SEC)
    assert driver.phase is Phase.OK
    correction = driver.tick(now + 2 * SEC + TIMING.debounce)
    assert correction is not None
    assert correction.data["temperature"] == 21.0
    assert driver.deadline == now + 2 * SEC + TIMING.debounce + TIMING.confirm_timeout


def test_a_target_reported_while_an_off_is_in_flight_is_not_corrected() -> None:
    """Issue #13: 22, then off before 22 is reported. The report of 22 empties the queue, but
    the off in flight is what makes the thermostat match: it is not sent again, and the
    confirmation timeout still applies to it."""
    driver = started(target=21.0)
    assert send(driver, 22.0, T0).data == {"temperature": 22.0}
    driver.set_want(Want(heat=False, target=22.0), T0 + SEC)
    off = driver.tick(T0 + SEC + TIMING.debounce)
    assert off is not None
    assert off.service == "set_hvac_mode"
    sent_off = T0 + SEC + TIMING.debounce
    driver.on_report(report(22.0), sent_off + SEC)
    assert driver.queue == []
    assert driver.phase is Phase.PENDING, "waiting for the off"
    assert driver.send_due is None, "nothing sent again"
    assert driver.next_deadline(sent_off + SEC) == sent_off + TIMING.confirm_timeout
    driver.on_report(report(22.0, mode="off"), sent_off + 5 * SEC)
    assert driver.phase is Phase.OK


def test_a_setpoint_reported_before_the_mode_of_its_command_is_not_corrected() -> None:
    """Issue #13: heat with its mode, the setpoint reported first. Not sent again; and if the
    mode never arrives, the command is resent at the confirmation timeout, mode included."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0 - HOUR)
    driver.set_want(Want(heat=False, target=21.0), T0 - HOUR)
    driver.on_report(report(21.0, mode="off"), T0 - HOUR)
    command = send(driver, 22.0, T0)
    assert command.data == {"temperature": 22.0, "hvac_mode": "heat"}
    driver.on_report(report(22.0, mode="off"), T0 + SEC)
    assert driver.queue == []
    assert driver.phase is Phase.PENDING, "waiting for the mode"
    assert driver.tick(T0 + SEC + TIMING.debounce) is None, "nothing sent again"
    resend = driver.tick(T0 + TIMING.confirm_timeout)
    assert resend is not None
    assert resend.data == {"temperature": 22.0, "hvac_mode": "heat"}


@pytest.mark.parametrize("slow", [False, True])
def test_an_off_at_the_device_while_a_target_only_command_is_in_flight_is_corrected_at_once(
    slow: bool,
) -> None:
    """Review of #29: the wait applies only when the pending command sets the mode.

    Heat at 21 is confirmed; 22 goes out without a mode, since the thermostat is in heat.
    Before it arrives, the thermostat is switched off at the device and then reports off at 22.
    Nothing in flight will put the mode back, so it is corrected after the debounce, with the
    mode, on a slow thermostat as on a fast one (not at the confirmation timeout).
    """
    driver = ThermostatDriver("climate.t", TIMING, slow=slow, now=T0 - HOUR)
    driver.set_want(Want(heat=True, target=21.0), T0 - HOUR)
    driver.on_report(report(21.0), T0 - HOUR)
    command = send(driver, 22.0, T0)
    assert command.data == {"temperature": 22.0}, "a target-only command"
    driver.on_report(report(21.0, mode="off"), T0 + 10 * SEC)
    driver.on_report(report(22.0, mode="off"), T0 + 20 * SEC)
    assert driver.phase is Phase.OK, "not waiting: no mode is in flight"
    correction = driver.tick(T0 + 20 * SEC + TIMING.debounce)
    assert correction is not None
    assert correction.data == {"temperature": 22.0, "hvac_mode": "heat"}


def test_a_report_that_empties_the_queue_with_a_different_target_is_still_corrected() -> None:
    """The other side: only a difference the mode in flight would remove is waited for. The
    thermostat reports the target sent but in the wrong mode and another target is wanted by
    then: corrected at once."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0 - HOUR)
    driver.set_want(Want(heat=False, target=21.0), T0 - HOUR)
    driver.on_report(report(21.0, mode="off"), T0 - HOUR)
    send(driver, 22.0, T0)
    driver.set_want(Want(heat=True, target=23.0), T0 + SEC)
    driver.on_report(report(22.0, mode="off"), T0 + 2 * SEC)
    assert driver.phase is Phase.OK, "no longer waiting for the command in flight"
    correction = driver.tick(T0 + 2 * SEC + TIMING.debounce)
    assert correction is not None
    assert correction.data == {"temperature": 23.0, "hvac_mode": "heat"}


# --- Other edges ---------------------------------------------------------------------------------


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


def test_off_without_an_off_mode_is_the_minimum_and_confirmed_by_it() -> None:
    """Off by the minimum target where there is no off mode."""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0)
    driver.set_want(Want(heat=False, target=21.0), T0)
    driver.on_report(report(21.0, modes=("heat",), min_temp=7.0), T0)
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
    exempt = driver.exempt_until
    driver.release(T0)
    assert driver.exempt_until == exempt
    assert driver.diagnostics(T0)["status"] == "ok"


def test_feed_threshold_and_intervals() -> None:
    """The feed rules on their own."""
    feed = Feed(TIMING)
    step = feed.step(T0, 20.0, allowed=True)
    assert step is not None
    assert (step.value, step.restarted) == (20.0, False)
    feed.wrote(20.0, T0)
    assert feed.step(T0 + 10 * MIN, 20.1, allowed=True) is None
    assert feed.next_deadline(T0, 20.1) == T0 + 25 * MIN
    assert feed.next_deadline(T0, 20.2) == T0 + 10 * MIN
    changed = feed.step(T0 + 10 * MIN, 20.2, allowed=True)
    assert changed is not None
    assert changed.value == 20.2
    assert feed.step(T0, 20.0, allowed=True, paused=True) is None
    assert feed.step(T0, None, allowed=True) is None
    assert not feed.active


def test_feed_starting_again_writes_at_once_unless_it_wrote_within_the_min_interval() -> None:
    """P8: a restart within feed_min_interval of the last write follows the normal rule."""
    feed = Feed(TIMING)
    feed.step(T0, 20.0, allowed=True)
    feed.wrote(20.0, T0)
    assert feed.step(T0 + MIN, None, allowed=True) is None
    soon = feed.step(T0 + 2 * MIN, 20.5, allowed=True)
    assert soon is not None
    assert (soon.value, soon.restarted) == (None, True), "started again, but nothing written"
    assert feed.next_deadline(T0 + 2 * MIN, 20.5) == T0 + 10 * MIN
    feed.step(T0 + 3 * MIN, None, allowed=True)
    later = feed.step(T0 + 11 * MIN, 20.0, allowed=True)
    assert later is not None
    assert (later.value, later.restarted) == (20.0, True)


@pytest.mark.parametrize(
    ("entity_id", "wanted", "state", "matches"),
    [
        ("select.s", "external", "external", True),
        ("select.s", "external", "internal", False),
        ("select.s", "external", "unknown", False),
        ("switch.w", "off", "off", True),
        ("switch.w", "off", "on", False),
        ("number.n", 1.5, "1.5", True),
        ("number.n", 1.5, "1.0", False),
        ("number.n", 1.5, "unknown", False),
    ],
)
def test_setting_matches(entity_id: str, wanted: str | float, state: str, matches: bool) -> None:
    """Whether a device setting is already at the configured value; no value differs."""
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


def test_a_target_only_command_after_a_mode_command_whose_mode_already_arrived() -> None:
    """A heat command with its mode, from off; the mode arrives first (heat at 21). A new target
    then goes out without the mode, so no mode is on its way any more. The 22 of the first
    command and the 23 arrive in heat: confirmed, nothing more sent.
    (From the second review of pull request #29.)"""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0 - HOUR)
    driver.set_want(Want(heat=False, target=21.0), T0 - HOUR)
    driver.on_report(report(21.0, mode="off"), T0 - HOUR)
    first = send(driver, 22.0, T0)
    assert first.data == {"temperature": 22.0, "hvac_mode": "heat"}
    driver.on_report(report(21.0, mode="heat"), T0 + 5 * SEC)
    second = send(driver, 23.0, T0 + 10 * SEC)
    assert second.data == {"temperature": 23.0}
    assert driver.mode_sent is False
    driver.on_report(report(22.0, mode="heat"), T0 + 20 * SEC)
    driver.on_report(report(23.0, mode="heat"), T0 + 30 * SEC)
    assert driver.phase is Phase.OK
    assert driver.tick(T0 + 40 * SEC) is None


def test_a_mode_still_on_its_way_when_the_next_target_goes_out_is_sent_again() -> None:
    """Off at 21; heat 22 with its mode is sent, and before anything is reported 23 is wanted.
    The report still says off, so the second command carries the mode too, and the wait applies
    when 23 is reported before the mode. (From the second review of pull request #29.)"""
    driver = ThermostatDriver("climate.t", TIMING, slow=False, now=T0 - HOUR)
    driver.set_want(Want(heat=False, target=21.0), T0 - HOUR)
    driver.on_report(report(21.0, mode="off"), T0 - HOUR)
    send(driver, 22.0, T0)
    second = send(driver, 23.0, T0 + 10 * SEC)
    assert second.data == {"temperature": 23.0, "hvac_mode": "heat"}
    driver.on_report(report(23.0, mode="off"), T0 + 20 * SEC)
    assert driver.phase is Phase.PENDING
    assert driver.tick(T0 + 20 * SEC + TIMING.debounce) is None
    driver.on_report(report(23.0, mode="heat"), T0 + 30 * SEC)
    assert driver.phase is Phase.OK
