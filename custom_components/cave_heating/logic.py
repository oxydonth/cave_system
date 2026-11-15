"""What a room does: the decision table of concept section 5.3 and the input rules of 5.4.

Pure logic without Home Assistant: every function takes the current time as an argument, so the
tests drive time explicitly. The controller in ``runtime.py`` feeds it the states it reads from
Home Assistant and applies the outcome to the thermostats through ``driver.py`` (section 5.5).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from typing import Any, Final

UNAVAILABLE_STATES: Final = frozenset({"unavailable", "unknown"})


class HomeMode(StrEnum):
    """``select.cave_home_mode``."""

    AUTO = "auto"
    AWAY = "away"
    OFF = "off"


class RoomMode(StrEnum):
    """Whether a room is switched on (``heat``) or ``off`` in its thermostat entity."""

    HEAT = "heat"
    OFF = "off"


class Reason(StrEnum):
    """Why a room is at its current target, one per row of the decision table."""

    SUMMER = "summer"
    ROOM_OFF = "room_off"
    WINDOW = "window"
    BOOST = "boost"
    OVERRIDE = "override"
    AWAY = "away"
    COMFORT = "comfort"
    ECO = "eco"


class Preset(StrEnum):
    """Presets of the room thermostat."""

    COMFORT = "comfort"
    ECO = "eco"
    AWAY = "away"
    BOOST = "boost"
    NONE = "none"


# --- Section 5.4: presence -------------------------------------------------------------------


class Presence(Enum):
    """What one presence entity says."""

    HOME = "home"
    NOT_HOME = "not_home"
    UNKNOWN = "unknown"


def presence_of(entity_id: str, state: str | None) -> Presence:
    """Read one presence entity (concept section 5.1, ``presence``).

    A zone is home when its count is above zero, a person or device tracker when it is ``home``,
    anything else when it is ``on``. A missing, unknown or unavailable entity says nothing.
    """
    if state is None or state in UNAVAILABLE_STATES:
        return Presence.UNKNOWN
    domain = entity_id.split(".", 1)[0]
    if domain == "zone":
        try:
            return Presence.HOME if float(state) > 0 else Presence.NOT_HOME
        except ValueError:
            return Presence.UNKNOWN
    if domain in ("person", "device_tracker"):
        return Presence.HOME if state == "home" else Presence.NOT_HOME
    return Presence.HOME if state == "on" else Presence.NOT_HOME


def someone_home(readings: Iterable[Presence]) -> bool:
    """Someone is home unless every presence entity reliably says nobody is (fail warm)."""
    readings = list(readings)
    return not readings or any(reading is not Presence.NOT_HOME for reading in readings)


class AwayTracker:
    """Presence after the away delay: ``binary_sensor.cave_home_occupied``."""

    def __init__(self, delay: timedelta) -> None:
        """Start with someone at home."""
        self.delay = delay
        self.nobody_home_since: datetime | None = None

    def update(self, home: bool, now: datetime, since: datetime | None = None) -> None:
        """Record what presence says now. ``since`` dates a "nobody home" found at start-up."""
        if home:
            self.nobody_home_since = None
        elif self.nobody_home_since is None:
            self.nobody_home_since = since or now

    def occupied(self, now: datetime) -> bool:
        """False once presence has reported nobody home for the away delay."""
        return self.nobody_home_since is None or now < self.nobody_home_since + self.delay

    def next_deadline(self, now: datetime) -> datetime | None:
        """When ``occupied`` changes next without new input."""
        if self.nobody_home_since is None:
            return None
        deadline = self.nobody_home_since + self.delay
        return deadline if deadline > now else None


# --- Section 5.4: window contacts and the window pause (5.3) ---------------------------------


class Contact(Enum):
    """What one window contact says."""

    OPEN = "open"
    CLOSED = "closed"
    SILENT = "silent"  # unavailable or unknown
    MISSING = "missing"  # the entity does not exist


def contact_of(state: str | None) -> Contact:
    """Read one window contact entity."""
    if state is None:
        return Contact.MISSING
    if state in UNAVAILABLE_STATES:
        return Contact.SILENT
    return Contact.OPEN if state == "on" else Contact.CLOSED


@dataclass(slots=True)
class _ContactTrack:
    state: Contact
    changed: datetime  # when the current state began
    open_since: datetime | None  # start of the current opening, if it is or was open
    closed_since: datetime | None  # start of the current closed period, if known
    silent_open: bool  # went silent while open


class WindowPause:
    """The window pause of one room (concept sections 5.3 and 5.4).

    It starts when any contact has been open for ``open_delay`` and ends when all have been
    closed for ``close_delay``. A contact that is unavailable counts as closed, except one that
    went silent while open: that one keeps the pause going for ``grace`` and then counts as
    closed at once, and it is reported as a problem until it is heard from again.
    """

    def __init__(self, open_delay: timedelta, close_delay: timedelta, grace: timedelta) -> None:
        """Start without a pause and without contacts."""
        self.open_delay = open_delay
        self.close_delay = close_delay
        self.grace = grace
        self.paused = False
        self._contacts: dict[str, _ContactTrack] = {}

    def update(self, entity_id: str, state: Contact, changed: datetime) -> None:
        """Record a contact's state and when it began."""
        previous = self._contacts.get(entity_id)
        if previous is not None and previous.state is state:
            return
        was_open = previous is not None and (
            previous.state is Contact.OPEN
            or (previous.state is Contact.SILENT and previous.silent_open)
        )
        previous_closed = previous.closed_since if previous is not None else None
        open_since = previous.open_since if was_open and previous is not None else None
        if state is Contact.OPEN:
            track = _ContactTrack(state, changed, open_since or changed, None, False)
        elif state is Contact.SILENT:
            track = _ContactTrack(
                state, changed, open_since, None if was_open else previous_closed, was_open
            )
        elif state is Contact.CLOSED:
            closed_since = changed
            if previous is not None and previous.state is Contact.SILENT and not was_open:
                closed_since = previous_closed or changed
            track = _ContactTrack(state, changed, None, closed_since, False)
        else:
            track = _ContactTrack(state, changed, None, None, False)
        self._contacts[entity_id] = track

    def _is_open(self, track: _ContactTrack, now: datetime) -> bool:
        if track.state is Contact.OPEN:
            return True
        return (
            track.state is Contact.SILENT and track.silent_open and now < track.changed + self.grace
        )

    def _closed_ready(self, track: _ContactTrack) -> datetime | None:
        """From when a closed contact lets the pause end; None while it does not."""
        if track.state is Contact.MISSING:
            return None
        if track.state is Contact.SILENT and track.silent_open:
            return track.changed + self.grace
        return (track.closed_since or track.changed) + self.close_delay

    def evaluate(self, now: datetime) -> bool:
        """Advance the pause to ``now`` and return whether it is in effect."""
        tracks = list(self._contacts.values())
        if not self.paused:
            self.paused = any(
                self._is_open(track, now)
                and track.open_since is not None
                and track.open_since + self.open_delay <= now
                for track in tracks
            )
        else:
            self.paused = not all(
                not self._is_open(track, now)
                and ((ready := self._closed_ready(track)) is None or ready <= now)
                for track in tracks
            )
        return self.paused

    def silent_open(self, now: datetime) -> list[str]:
        """Contacts that went silent while open and whose grace has run out."""
        return sorted(
            entity_id
            for entity_id, track in self._contacts.items()
            if track.state is Contact.SILENT
            and track.silent_open
            and now >= track.changed + self.grace
        )

    def next_deadline(self, now: datetime) -> datetime | None:
        """When the pause can change next without new input."""
        candidates: list[datetime] = []
        tracks = list(self._contacts.values())
        for track in tracks:
            if track.state is Contact.SILENT and track.silent_open:
                candidates.append(track.changed + self.grace)
            if not self.paused and self._is_open(track, now) and track.open_since is not None:
                candidates.append(track.open_since + self.open_delay)
        if self.paused and not any(self._is_open(track, now) for track in tracks):
            readies = [ready for track in tracks if (ready := self._closed_ready(track))]
            if readies:
                candidates.append(max(readies))
        future = [candidate for candidate in candidates if candidate > now]
        return min(future) if future else None


# --- Section 5.4: schedule and sensors -------------------------------------------------------


def schedule_on(state: str | None) -> bool:
    """A schedule that is missing, unknown or unavailable counts as on (comfort)."""
    return state != "off"


class ScheduleTracker:
    """Detects changes of a room's schedule, which end an override (section 5.3)."""

    def __init__(self) -> None:
        """No state seen yet."""
        self.last: bool | None = None

    def update(self, state: str | None) -> bool:
        """Record the schedule's state; True when it changed between on and off.

        Going unavailable and coming back is not a change; only a real on or off counts.
        """
        if state not in ("on", "off"):
            return False
        value = state == "on"
        changed = self.last is not None and value != self.last
        self.last = value
        return changed


def sensor_fresh(last_reported: datetime, now: datetime, stale: timedelta) -> bool:
    """A sensor is stale once its last report is ``sensor_stale`` old."""
    return now - last_reported < stale


def room_temperature(
    sensor: float | None, thermostat_readings: Sequence[float]
) -> tuple[float | None, bool]:
    """The room's temperature and whether it comes from the room sensor.

    ``sensor`` is the room sensor's reading if it is available and fresh, otherwise None; then
    the room shows the average of its thermostats' own readings.
    """
    if sensor is not None:
        return sensor, True
    if thermostat_readings:
        return round(sum(thermostat_readings) / len(thermostat_readings), 1), False
    return None, False


# --- Section 5.3: boost, override and the decision table --------------------------------------


@dataclass(frozen=True, slots=True)
class Boost:
    """An active boost."""

    temperature: float
    until: datetime


@dataclass(frozen=True, slots=True)
class Override:
    """An active override: a temperature, or a preset chosen by hand."""

    until: datetime
    target: float | None = None
    preset: Preset | None = None
    # Set while the home is away: the next schedule change does not end it until someone is
    # home again, so the flat can be pre-heated from the phone.
    waiting_for_home: bool = False


type HandAction = Boost | Override


@dataclass(slots=True)
class RoomState:
    """The state of a room that is kept across restarts."""

    comfort: float
    eco: float
    mode: RoomMode = RoomMode.HEAT
    hand: HandAction | None = None

    def set_mode(self, mode: RoomMode) -> None:
        """Switch the room on or off. Switching it off ends a boost or override."""
        self.mode = mode
        if mode is RoomMode.OFF:
            self.hand = None

    def set_override(
        self,
        now: datetime,
        override_max: timedelta,
        *,
        away: bool,
        target: float | None = None,
        preset: Preset | None = None,
    ) -> None:
        """A hand action: it replaces any earlier boost or override."""
        self.hand = Override(
            until=now + override_max, target=target, preset=preset, waiting_for_home=away
        )

    def start_boost(self, now: datetime, duration: timedelta, temperature: float) -> None:
        """Start a boost; it replaces any earlier boost or override."""
        self.hand = Boost(temperature=temperature, until=now + duration)

    def resume(self) -> None:
        """Resume the schedule: the button, the service, or the preset ``none``."""
        self.hand = None

    def expire(self, now: datetime) -> bool:
        """End a boost or override whose time is up. True if one ended."""
        if self.hand is not None and now >= self.hand.until:
            self.hand = None
            return True
        return False

    def on_schedule_change(self) -> None:
        """The room's schedule changed: an override ends, unless it waits for someone home."""
        if isinstance(self.hand, Override) and not self.hand.waiting_for_home:
            self.hand = None

    def on_presence(self, away: bool) -> None:
        """Once someone is home again, the next schedule change ends a waiting override."""
        if isinstance(self.hand, Override) and self.hand.waiting_for_home and not away:
            self.hand = Override(
                until=self.hand.until, target=self.hand.target, preset=self.hand.preset
            )

    def on_home_mode_by_hand(self, mode: HomeMode) -> None:
        """Home mode switched to away or off by hand ends every boost and override."""
        if mode in (HomeMode.AWAY, HomeMode.OFF):
            self.hand = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of the decision table for one room."""

    target: float  # the outcome of rows 4 to 7, shown also while the radiators are off
    preset: Preset
    reason: Reason
    heating: bool  # False: the radiators are off (rows 1 to 3)


def preset_temperature(preset: Preset, room: RoomState, away_temperature: float) -> float | None:
    """The temperature a schedule preset stands for."""
    if preset is Preset.COMFORT:
        return room.comfort
    if preset is Preset.ECO:
        return room.eco
    if preset is Preset.AWAY:
        return away_temperature
    return None


def decide(
    *,
    home_mode: HomeMode,
    room: RoomState,
    window_paused: bool,
    away: bool,
    schedule_is_on: bool,
    away_temperature: float,
) -> Decision:
    """Apply the decision table of concept section 5.3. The first matching row wins.

    ``away`` is row 5's condition: home mode ``away``, or presence reporting nobody home for
    longer than the away delay. ``schedule_is_on`` is True for a room without a schedule.
    """
    hand = room.hand
    if isinstance(hand, Boost):
        target, preset, reason = hand.temperature, Preset.BOOST, Reason.BOOST
    elif isinstance(hand, Override):
        preset = hand.preset or Preset.NONE
        chosen = preset_temperature(preset, room, away_temperature)
        target = chosen if chosen is not None else float(hand.target or room.comfort)
        reason = Reason.OVERRIDE
    elif away:
        target, preset, reason = away_temperature, Preset.AWAY, Reason.AWAY
    elif schedule_is_on:
        target, preset, reason = room.comfort, Preset.COMFORT, Reason.COMFORT
    else:
        target, preset, reason = room.eco, Preset.ECO, Reason.ECO

    if home_mode is HomeMode.OFF:
        return Decision(target, preset, Reason.SUMMER, heating=False)
    if room.mode is RoomMode.OFF:
        return Decision(target, preset, Reason.ROOM_OFF, heating=False)
    if window_paused:
        return Decision(target, preset, Reason.WINDOW, heating=False)
    return Decision(target, preset, reason, heating=True)


def is_away(home_mode: HomeMode, occupied: bool) -> bool:
    """Row 5's condition."""
    return home_mode is HomeMode.AWAY or not occupied


# --- Persistence ------------------------------------------------------------------------------


def hand_to_dict(hand: HandAction | None) -> dict[str, Any] | None:
    """Serialise a boost or override for the state file."""
    if isinstance(hand, Boost):
        return {"kind": "boost", "temperature": hand.temperature, "until": hand.until.isoformat()}
    if isinstance(hand, Override):
        return {
            "kind": "override",
            "until": hand.until.isoformat(),
            "target": hand.target,
            "preset": hand.preset.value if hand.preset else None,
            "waiting_for_home": hand.waiting_for_home,
        }
    return None


def hand_from_dict(data: object, now: datetime) -> HandAction | None:
    """Read a stored boost or override; one that has ended or cannot be read is dropped."""
    if not isinstance(data, dict):
        return None
    try:
        until = datetime.fromisoformat(data["until"])
        if until.tzinfo is None or until <= now:
            return None
        if data["kind"] == "boost":
            return Boost(temperature=float(data["temperature"]), until=until)
        if data["kind"] == "override":
            target = data.get("target")
            preset = data.get("preset")
            return Override(
                until=until,
                target=float(target) if target is not None else None,
                preset=Preset(preset) if preset is not None else None,
                waiting_for_home=bool(data.get("waiting_for_home", False)),
            )
    except KeyError, TypeError, ValueError:
        return None
    return None
