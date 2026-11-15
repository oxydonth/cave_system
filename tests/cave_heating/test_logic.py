"""Unit tests of logic.py: the decision table of concept section 5.3 and the input rules of 5.4.

No Home Assistant here: every function takes the time as an argument, and the tests move it.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.cave_heating import logic
from custom_components.cave_heating.logic import (
    AwayTracker,
    Boost,
    Contact,
    HomeMode,
    Override,
    Presence,
    PresenceReadings,
    Preset,
    Reason,
    RoomMode,
    RoomState,
    ScheduleTracker,
    WindowPause,
    contact_of,
    decide,
    hand_from_dict,
    hand_to_dict,
    is_away,
    presence_of,
    room_temperature,
    schedule_on,
    sensor_fresh,
    someone_home,
)

T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
COMFORT, ECO, AWAY, BOOST_T = 21.0, 17.5, 16.0, 25.0


def test_module_imports_nothing_from_home_assistant() -> None:
    """The decision logic is a pure module (the review checks this)."""
    for name in ("logic.py", "driver.py", "schema.py"):
        source = Path(logic.__file__).with_name(name).read_text()
        imported = [
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [module for module in imported if module.startswith("homeassistant")], name


def _room(hand: Boost | Override | None = None, mode: RoomMode = RoomMode.HEAT) -> RoomState:
    return RoomState(comfort=COMFORT, eco=ECO, mode=mode, hand=hand)


def _decide(
    *,
    home_mode: HomeMode = HomeMode.AUTO,
    room: RoomState | None = None,
    window: bool = False,
    away: bool = False,
    schedule: bool = True,
) -> logic.Decision:
    return decide(
        home_mode=home_mode,
        room=room or _room(),
        window_paused=window,
        away=is_away(home_mode, not away),
        schedule_is_on=schedule,
        away_temperature=AWAY,
    )


# --- Section 5.3: the decision table, row by row ---------------------------------------------


def test_row_1_home_mode_off_is_summer() -> None:
    """Row 1 wins over everything below it, including a boost and an open window."""
    decision = _decide(
        home_mode=HomeMode.OFF,
        room=_room(Boost(BOOST_T, T0 + MIN), mode=RoomMode.OFF),
        window=True,
    )
    assert (decision.heating, decision.reason) == (False, Reason.SUMMER)


def test_row_2_room_off() -> None:
    """Row 2: the room switched off, over a window pause and a boost."""
    decision = _decide(room=_room(Boost(BOOST_T, T0 + MIN), mode=RoomMode.OFF), window=True)
    assert (decision.heating, decision.reason) == (False, Reason.ROOM_OFF)


def test_row_3_window_pause() -> None:
    """Row 3: radiators off with the reason window, also during a boost."""
    decision = _decide(room=_room(Boost(BOOST_T, T0 + MIN)), window=True)
    assert (decision.heating, decision.reason) == (False, Reason.WINDOW)


def test_row_4_boost_and_override() -> None:
    """Row 4: the boost temperature, or the chosen temperature."""
    boost = _decide(room=_room(Boost(BOOST_T, T0 + MIN)), away=True, schedule=False)
    assert (boost.heating, boost.target, boost.preset, boost.reason) == (
        True,
        BOOST_T,
        Preset.BOOST,
        Reason.BOOST,
    )
    override = _decide(room=_room(Override(T0 + MIN, target=23.0)), away=True)
    assert (override.target, override.preset, override.reason) == (
        23.0,
        Preset.NONE,
        Reason.OVERRIDE,
    )


def test_row_4_override_by_preset_follows_that_preset() -> None:
    """A preset chosen by hand is an override at that preset's temperature."""
    room = _room(Override(T0 + MIN, preset=Preset.ECO))
    decision = _decide(room=room)
    assert (decision.target, decision.preset, decision.reason) == (ECO, Preset.ECO, Reason.OVERRIDE)
    room.eco = 18.0
    assert _decide(room=room).target == 18.0
    away_override = _decide(room=_room(Override(T0 + MIN, preset=Preset.AWAY)))
    assert (away_override.target, away_override.reason) == (AWAY, Reason.OVERRIDE)


def test_row_4_later_hand_action_replaces_the_earlier_one() -> None:
    """If both a boost and an override were set, the later one wins."""
    room = _room()
    room.start_boost(T0, timedelta(minutes=30), BOOST_T)
    room.set_override(T0 + MIN, timedelta(hours=6), away=False, target=22.0)
    assert _decide(room=room).reason is Reason.OVERRIDE
    room.start_boost(T0 + 2 * MIN, timedelta(minutes=30), BOOST_T)
    assert _decide(room=room).reason is Reason.BOOST


@pytest.mark.parametrize("cause", ["home_mode", "presence"])
def test_row_5_away(cause: str) -> None:
    """Row 5: home mode away, or presence reporting nobody home: the away temperature."""
    decision = _decide(home_mode=HomeMode.AWAY) if cause == "home_mode" else _decide(away=True)
    assert (decision.heating, decision.target, decision.preset, decision.reason) == (
        True,
        AWAY,
        Preset.AWAY,
        Reason.AWAY,
    )


def test_row_6_schedule_on_or_no_schedule_is_comfort() -> None:
    """Row 6: a schedule that is on, or no schedule at all (schedule_is_on is True then)."""
    decision = _decide(schedule=True)
    assert (decision.target, decision.preset, decision.reason) == (
        COMFORT,
        Preset.COMFORT,
        Reason.COMFORT,
    )


def test_row_7_schedule_off_is_eco() -> None:
    """Row 7: the schedule is off."""
    decision = _decide(schedule=False)
    assert (decision.target, decision.preset, decision.reason) == (ECO, Preset.ECO, Reason.ECO)


HANDS = {
    "none": None,
    "boost": Boost(BOOST_T, T0 + MIN),
    "override": Override(T0 + MIN, target=23.0),
}


@pytest.mark.parametrize(
    ("home_mode", "room_mode", "window", "hand", "away", "schedule"),
    list(
        itertools.product(
            list(HomeMode), list(RoomMode), [False, True], list(HANDS), [False, True], [False, True]
        )
    ),
)
def test_first_matching_row_wins_in_every_combination(
    home_mode: HomeMode,
    room_mode: RoomMode,
    window: bool,
    hand: str,
    away: bool,
    schedule: bool,
) -> None:
    """Every combination of the table's conditions, against the table as the concept writes it.

    The target and preset always show the outcome of rows 4 to 7, also while the radiators are
    off, so the room returns to it when the pause ends.
    """
    decision = _decide(
        home_mode=home_mode,
        room=_room(HANDS[hand], mode=room_mode),
        window=window,
        away=away,
        schedule=schedule,
    )
    if hand == "boost":
        expected_target, expected_reason = BOOST_T, Reason.BOOST
    elif hand == "override":
        expected_target, expected_reason = 23.0, Reason.OVERRIDE
    elif home_mode is HomeMode.AWAY or away:
        expected_target, expected_reason = AWAY, Reason.AWAY
    elif schedule:
        expected_target, expected_reason = COMFORT, Reason.COMFORT
    else:
        expected_target, expected_reason = ECO, Reason.ECO
    if home_mode is HomeMode.OFF:
        expected_reason, heating = Reason.SUMMER, False
    elif room_mode is RoomMode.OFF:
        expected_reason, heating = Reason.ROOM_OFF, False
    elif window:
        expected_reason, heating = Reason.WINDOW, False
    else:
        heating = True
    assert decision.target == expected_target
    assert decision.reason is expected_reason
    assert decision.heating is heating


# --- Section 5.3: boost and override lifecycles -----------------------------------------------


def test_boost_ends_after_its_duration_and_its_clock_keeps_running_during_a_window_pause() -> None:
    """The boost's end is fixed when it starts; a window pause does not extend it."""
    room = _room()
    room.start_boost(T0, timedelta(minutes=30), BOOST_T)
    assert _decide(room=room, window=True).reason is Reason.WINDOW
    assert not room.expire(T0 + timedelta(minutes=29))
    assert _decide(room=room).reason is Reason.BOOST
    assert room.expire(T0 + timedelta(minutes=30))
    assert _decide(room=room).reason is Reason.COMFORT


def test_override_ends_after_override_max() -> None:
    """``override_max`` after it was set."""
    room = _room()
    room.set_override(T0, timedelta(hours=6), away=False, target=23.0)
    assert not room.expire(T0 + timedelta(hours=5, minutes=59))
    assert room.expire(T0 + timedelta(hours=6))
    assert room.hand is None


def test_override_ends_at_the_next_schedule_change() -> None:
    """The next change of the room's schedule ends an override, but not a boost."""
    room = _room()
    room.set_override(T0, timedelta(hours=6), away=False, target=23.0)
    room.on_schedule_change()
    assert room.hand is None
    room.start_boost(T0, timedelta(minutes=30), BOOST_T)
    room.on_schedule_change()
    assert isinstance(room.hand, Boost)


def test_override_set_while_away_waits_for_someone_home_before_a_schedule_change_ends_it() -> None:
    """Pre-heating from the phone: the first schedule change after someone is home ends it."""
    room = _room()
    room.set_override(T0, timedelta(hours=6), away=True, target=22.0)
    room.on_schedule_change()
    assert isinstance(room.hand, Override), "a schedule change while away does not end it"
    room.on_presence(away=True)
    room.on_schedule_change()
    assert isinstance(room.hand, Override)
    room.on_presence(away=False)
    assert isinstance(room.hand, Override), "coming home alone does not end it"
    room.on_schedule_change()
    assert room.hand is None


@pytest.mark.parametrize("mode", [HomeMode.AWAY, HomeMode.OFF])
@pytest.mark.parametrize("hand", ["boost", "override"])
def test_home_mode_switched_to_away_or_off_by_hand_ends_boost_and_override(
    mode: HomeMode, hand: str
) -> None:
    """Home mode switched to away or off by hand ends both."""
    room = _room(HANDS[hand])
    room.on_home_mode_by_hand(mode)
    assert room.hand is None


@pytest.mark.parametrize("hand", ["boost", "override"])
def test_home_mode_switched_to_auto_keeps_boost_and_override(hand: str) -> None:
    """Only away and off end them."""
    room = _room(HANDS[hand])
    room.on_home_mode_by_hand(HomeMode.AUTO)
    assert room.hand is HANDS[hand]


@pytest.mark.parametrize("hand", ["boost", "override"])
def test_room_switched_off_ends_boost_and_override(hand: str) -> None:
    """The room being switched off ends both."""
    room = _room(HANDS[hand])
    room.set_mode(RoomMode.HEAT)
    assert room.hand is HANDS[hand]
    room.set_mode(RoomMode.OFF)
    assert room.hand is None


@pytest.mark.parametrize("hand", ["boost", "override"])
def test_resuming_the_schedule_ends_boost_and_override(hand: str) -> None:
    """The button, the service and the preset none all resume the schedule."""
    room = _room(HANDS[hand])
    room.resume()
    assert room.hand is None


@pytest.mark.parametrize("hand", ["boost", "override"])
def test_becoming_away_through_presence_does_not_cancel_boost_or_override(hand: str) -> None:
    """Phones drop off the Wi-Fi when they sleep; presence alone ends nothing."""
    room = _room(HANDS[hand])
    room.on_presence(away=True)
    decision = _decide(room=room, away=True)
    assert decision.reason in (Reason.BOOST, Reason.OVERRIDE)


def test_hand_actions_are_stored_and_restored() -> None:
    """Boost and override survive the state file; an ended one is dropped."""
    for hand in (
        Boost(BOOST_T, T0 + MIN),
        Override(T0 + MIN, target=22.5),
        Override(T0 + MIN, preset=Preset.ECO, waiting_for_home=True),
    ):
        assert hand_from_dict(hand_to_dict(hand), T0) == hand
        assert hand_from_dict(hand_to_dict(hand), T0 + MIN) is None
    assert hand_to_dict(None) is None
    assert hand_from_dict({"kind": "boost"}, T0) is None
    assert hand_from_dict("nonsense", T0) is None
    assert hand_from_dict({"kind": "other", "until": (T0 + MIN).isoformat()}, T0) is None


# --- Section 5.3: the window pause --------------------------------------------------------------


def _pause(open_delay: timedelta = MIN, close_delay: timedelta = 2 * MIN) -> WindowPause:
    return WindowPause(open_delay, close_delay, timedelta(minutes=30))


def test_window_pause_starts_after_the_open_delay_and_ends_after_the_close_delay() -> None:
    """It starts when any contact has been open for the open delay; ends after the close delay."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.CLOSED, T0)
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    assert not pause.evaluate(T0 + timedelta(seconds=59))
    assert pause.next_deadline(T0 + timedelta(seconds=59)) == T0 + MIN
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + 5 * MIN)
    assert pause.evaluate(T0 + 6 * MIN)
    assert pause.next_deadline(T0 + 6 * MIN) == T0 + 7 * MIN
    assert not pause.evaluate(T0 + 7 * MIN)


def test_window_opened_briefly_causes_no_pause() -> None:
    """A window open for less than the open delay changes nothing."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + timedelta(seconds=30))
    assert not pause.evaluate(T0 + 5 * MIN)


def test_window_pause_ends_only_when_all_contacts_have_been_closed_for_the_close_delay() -> None:
    """With two contacts, one still open keeps the pause; reopening restarts the close delay."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    pause.update("binary_sensor.b", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + 2 * MIN)
    assert pause.evaluate(T0 + 10 * MIN), "b is still open"
    pause.update("binary_sensor.b", Contact.CLOSED, T0 + 10 * MIN)
    pause.update("binary_sensor.b", Contact.OPEN, T0 + 11 * MIN)
    pause.update("binary_sensor.b", Contact.CLOSED, T0 + 11 * MIN + timedelta(seconds=10))
    assert pause.evaluate(T0 + 12 * MIN + timedelta(seconds=30))
    assert not pause.evaluate(T0 + 13 * MIN + timedelta(seconds=10))


def test_window_pause_with_zero_delays() -> None:
    """Delays of zero pause and resume at once."""
    pause = _pause(timedelta(0), timedelta(0))
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    assert pause.evaluate(T0)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + MIN)
    assert not pause.evaluate(T0 + MIN)


# --- Section 5.4: inputs that are missing, unknown or unavailable -----------------------------


@pytest.mark.parametrize(
    ("state", "contact"),
    [
        ("on", Contact.OPEN),
        ("off", Contact.CLOSED),
        ("unavailable", Contact.SILENT),
        ("unknown", Contact.SILENT),
        (None, Contact.MISSING),
    ],
)
def test_contact_states(state: str | None, contact: Contact) -> None:
    """How a window contact's state is read."""
    assert contact_of(state) is contact


@pytest.mark.parametrize("state", [Contact.SILENT, Contact.MISSING])
def test_window_contact_unavailable_or_missing_counts_as_closed(state: Contact) -> None:
    """Section 5.4, window contact: treated as closed."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.CLOSED, T0)
    pause.update("binary_sensor.a", state, T0 + MIN)
    assert not pause.evaluate(T0 + timedelta(hours=1))
    assert pause.silent_open(T0 + timedelta(hours=1)) == []


def test_window_contact_silent_while_open_keeps_the_pause_for_the_grace_then_ends_it() -> None:
    """Section 5.4: the pause continues for window_unavailable_grace, then ends; a problem."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    silent_at = T0 + 5 * MIN
    pause.update("binary_sensor.a", Contact.SILENT, silent_at)
    assert pause.evaluate(silent_at + timedelta(minutes=29))
    assert pause.silent_open(silent_at + timedelta(minutes=29)) == []
    assert pause.next_deadline(silent_at) == silent_at + timedelta(minutes=30)
    assert not pause.evaluate(silent_at + timedelta(minutes=30)), "ends at once, no close delay"
    assert pause.silent_open(silent_at + timedelta(minutes=30)) == ["binary_sensor.a"]
    pause.update("binary_sensor.a", Contact.CLOSED, silent_at + timedelta(hours=1))
    assert pause.silent_open(silent_at + timedelta(hours=1)) == []


def test_window_contact_silent_while_open_and_reporting_open_again_continues_the_opening() -> None:
    """A contact that comes back still open keeps its original opening time."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    pause.update("binary_sensor.a", Contact.SILENT, T0 + timedelta(seconds=30))
    pause.update("binary_sensor.a", Contact.OPEN, T0 + timedelta(seconds=40))
    assert pause.evaluate(T0 + MIN)


def test_window_contact_silent_while_closed_keeps_its_closing_time() -> None:
    """A closed contact that goes silent and comes back closed was closed all along."""
    pause = _pause()
    pause.update("binary_sensor.a", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + 2 * MIN)
    pause.update("binary_sensor.a", Contact.SILENT, T0 + 3 * MIN)
    assert not pause.evaluate(T0 + 4 * MIN)
    pause.update("binary_sensor.a", Contact.OPEN, T0 + 5 * MIN)
    assert pause.evaluate(T0 + 6 * MIN)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + 7 * MIN)
    pause.update("binary_sensor.a", Contact.SILENT, T0 + 8 * MIN)
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + 8 * MIN + timedelta(seconds=30))
    assert not pause.evaluate(T0 + 9 * MIN)


@pytest.mark.parametrize(
    ("entity_id", "state", "presence"),
    [
        ("zone.home", "2", Presence.HOME),
        ("zone.home", "0", Presence.NOT_HOME),
        ("zone.home", "many", Presence.UNKNOWN),
        ("person.max", "home", Presence.HOME),
        ("person.max", "not_home", Presence.NOT_HOME),
        ("person.max", "work", Presence.NOT_HOME),
        ("device_tracker.phone", "home", Presence.HOME),
        ("device_tracker.phone", "not_home", Presence.NOT_HOME),
        ("input_boolean.guest", "on", Presence.HOME),
        ("input_boolean.guest", "off", Presence.NOT_HOME),
        ("binary_sensor.motion", "on", Presence.HOME),
        ("person.max", "unavailable", Presence.UNKNOWN),
        ("person.max", "unknown", Presence.UNKNOWN),
        ("person.max", None, Presence.UNKNOWN),
    ],
)
def test_presence_of_each_kind_of_entity(
    entity_id: str, state: str | None, presence: Presence
) -> None:
    """Section 5.1, presence: zones by count, persons and trackers by home, others by on."""
    assert presence_of(entity_id, state) is presence


def test_presence_missing_unknown_or_unavailable_counts_as_home() -> None:
    """Section 5.4, presence: treated as home; nobody configured is home too."""
    assert someone_home([])
    assert someone_home([Presence.NOT_HOME, Presence.UNKNOWN])
    assert someone_home([Presence.HOME, Presence.NOT_HOME])
    assert not someone_home([Presence.NOT_HOME, Presence.NOT_HOME])


def test_away_only_after_nobody_has_been_home_for_the_away_delay() -> None:
    """Row 5: presence reports nobody home for longer than the away delay."""
    tracker = AwayTracker(timedelta(minutes=30))
    tracker.update(False, T0)
    assert tracker.occupied(T0 + timedelta(minutes=29))
    assert tracker.next_deadline(T0) == T0 + timedelta(minutes=30)
    assert not tracker.occupied(T0 + timedelta(minutes=30))
    assert tracker.next_deadline(T0 + timedelta(minutes=30)) is None
    tracker.update(False, T0 + timedelta(hours=1))
    assert tracker.nobody_home_since == T0, "staying away does not restart the delay"
    tracker.update(True, T0 + timedelta(hours=2))
    assert tracker.occupied(T0 + timedelta(hours=2))
    assert tracker.next_deadline(T0 + timedelta(hours=2)) is None


def test_unavailable_presence_keeps_its_last_reading_for_the_grace_then_counts_as_home() -> None:
    """Section 5.4, presence (P7): presence_unavailable_grace, then home."""
    readings = PresenceReadings(timedelta(minutes=30))
    readings.update("device_tracker.a", Presence.NOT_HOME, T0)
    readings.update("device_tracker.a", Presence.UNKNOWN, T0 + MIN)
    assert readings.effective("device_tracker.a", T0 + 30 * MIN) is Presence.NOT_HOME
    assert readings.next_deadline(T0 + MIN) == T0 + 31 * MIN
    assert readings.effective("device_tracker.a", T0 + 31 * MIN) is Presence.UNKNOWN
    assert readings.next_deadline(T0 + 31 * MIN) is None
    readings.update("device_tracker.a", Presence.UNKNOWN, T0 + 40 * MIN)
    assert readings.effective("device_tracker.a", T0 + 40 * MIN) is Presence.UNKNOWN, (
        "the grace runs from when it went silent, not from the latest silent report"
    )
    readings.update("device_tracker.a", Presence.HOME, T0 + 50 * MIN)
    assert readings.effective("device_tracker.a", T0 + 50 * MIN) is Presence.HOME


def test_presence_without_a_last_reading_counts_as_home_at_once() -> None:
    """Nothing known: home."""
    readings = PresenceReadings(timedelta(minutes=30))
    readings.update("device_tracker.a", Presence.UNKNOWN, T0)
    assert readings.effective("device_tracker.a", T0) is Presence.UNKNOWN
    assert readings.effective("device_tracker.never_seen", T0) is Presence.UNKNOWN
    assert someone_home([readings.effective("device_tracker.a", T0)])


def test_presence_readings_survive_a_restart_for_the_grace() -> None:
    """The last readings are stored; after a restart they stand while the trackers load."""
    before = PresenceReadings(timedelta(minutes=30))
    before.update("device_tracker.a", Presence.NOT_HOME, T0)
    after = PresenceReadings(timedelta(minutes=30))
    after.restore(before.to_dict(), T0 + timedelta(hours=1))
    assert after.effective("device_tracker.a", T0 + timedelta(hours=1)) is Presence.NOT_HOME
    assert after.effective("device_tracker.a", T0 + timedelta(hours=2)) is Presence.UNKNOWN
    after.restore({"x.y": "nonsense", "z.w": "unknown"}, T0)
    after.restore("not a mapping", T0)
    assert set(after.last) == {"device_tracker.a"}


def test_restored_window_pause_waits_for_the_contacts_then_ends_by_the_normal_rule() -> None:
    """Section 5.5, start-up (P4): the pause is restored; contacts not heard from keep it."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.a"])
    pause.paused = True
    assert pause.evaluate(T0 + timedelta(hours=1)), "the contact has not reported yet"
    pause.update("binary_sensor.a", Contact.CLOSED, T0 + timedelta(hours=1))
    assert pause.evaluate(T0 + timedelta(hours=1) + MIN)
    assert not pause.evaluate(T0 + timedelta(hours=1) + 2 * MIN)


def test_restored_window_pause_with_a_contact_first_heard_from_as_unavailable() -> None:
    """Treated as silent while open: the pause lasts the grace, then ends."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.a"])
    pause.paused = True
    pause.update("binary_sensor.a", Contact.SILENT, T0)
    assert pause.evaluate(T0 + 29 * MIN)
    assert not pause.evaluate(T0 + 30 * MIN)


def test_window_pause_starts_when_one_of_two_contacts_opens() -> None:
    """Any contact, not all of them (S8)."""
    pause = WindowPause(MIN, 2 * MIN, timedelta(minutes=30), ["binary_sensor.a", "binary_sensor.b"])
    pause.update("binary_sensor.a", Contact.CLOSED, T0)
    pause.update("binary_sensor.b", Contact.CLOSED, T0)
    pause.update("binary_sensor.b", Contact.OPEN, T0)
    assert pause.evaluate(T0 + MIN)


def test_an_override_keeps_the_schedule_state_it_was_set_under() -> None:
    """Stored with the override, so a change during a restart still ends it (P4)."""
    room = _room()
    room.set_override(T0, timedelta(hours=6), away=False, target=22.0, schedule=True)
    stored = hand_from_dict(hand_to_dict(room.hand), T0)
    assert isinstance(stored, Override)
    assert stored.schedule is True
    tracker = ScheduleTracker()
    tracker.last = stored.schedule
    assert tracker.update("off"), "off after a restart under on is a change"


def test_a_waiting_override_takes_on_the_schedule_state_of_changes_that_did_not_end_it() -> None:
    """So only a change after someone is home ends it, also across a restart."""
    room = _room()
    room.set_override(T0, timedelta(hours=6), away=True, target=22.0, schedule=True)
    room.on_schedule_change(False)
    assert isinstance(room.hand, Override)
    assert room.hand.schedule is False
    room.on_presence(away=False)
    assert isinstance(room.hand, Override)
    assert room.hand.schedule is False
    assert not room.hand.waiting_for_home


@pytest.mark.parametrize(
    ("state", "on"),
    [("on", True), ("off", False), ("unavailable", True), ("unknown", True), (None, True)],
)
def test_schedule_missing_unknown_or_unavailable_counts_as_on(state: str | None, on: bool) -> None:
    """Section 5.4, schedule: on, so the room is at comfort."""
    assert schedule_on(state) is on


def test_schedule_changes_are_real_changes_only() -> None:
    """Going unavailable and back is not a schedule change; on to off is."""
    tracker = ScheduleTracker()
    assert not tracker.update("on"), "the first state seen is not a change"
    assert not tracker.update("unavailable")
    assert not tracker.update("on")
    assert tracker.update("off")
    assert not tracker.update("off")
    assert not tracker.update("unavailable"), "the off side as well (S8)"
    assert not tracker.update("unknown")
    assert not tracker.update("off")
    assert tracker.update("on")


def test_room_sensor_unavailable_or_stale_falls_back_to_the_thermostats_average() -> None:
    """Section 5.4, room temperature sensor: the average of the thermostats' own readings."""
    assert room_temperature(20.5, [22.0, 23.0]) == (20.5, True)
    assert room_temperature(None, [22.0, 23.0]) == (22.5, False)
    assert room_temperature(None, [21.04, 21.0, 21.0]) == (21.0, False)
    assert room_temperature(None, []) == (None, False)


def test_room_sensor_is_stale_once_its_last_report_is_sensor_stale_old() -> None:
    """Section 5.4: a report older than sensor_stale counts as no report."""
    stale = timedelta(hours=3)
    assert sensor_fresh(T0, T0 + timedelta(hours=2, minutes=59), stale)
    assert not sensor_fresh(T0, T0 + stale, stale)
