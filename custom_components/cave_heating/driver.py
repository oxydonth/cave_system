"""How thermostats are driven: concept section 5.5.

One ``ThermostatDriver`` per thermostat holds that thermostat's whole state explicitly: whether it
is available, the last report, what is wanted from it, the phase of the command exchange
(``ok``, ``pending``, ``unresponsive``, ``blocked``), the targets sent since the last
confirmation, the hold, and the command budget. It takes reports and the current time and hands
back the command to send, if any; ``runtime.py`` sends it through Home Assistant. Nothing here
imports Home Assistant, so every rule can be exercised directly with simulated time as well as
through the integration tests with fake thermostat entities.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum, StrEnum
from typing import Any, Final

from .schema import SettingValue, Timing

HEAT: Final = "heat"
OFF: Final = "off"
DEFAULT_STEP: Final = 0.5
# The command budget counts commands "within an hour" (concept section 5.5).
BUDGET_WINDOW: Final = timedelta(hours=1)
# Reported temperatures are compared to a hundredth of a degree.
_TOLERANCE: Final = 0.005


def same(first: float | None, second: float | None) -> bool:
    """Whether two temperatures are equal, allowing for float representation."""
    return first is not None and second is not None and abs(first - second) < _TOLERANCE


def round_target(value: float, step: float, minimum: float, maximum: float) -> float:
    """Round to the step, half up, then clamp to the range (concept section 5.5)."""
    if step <= 0:
        step = DEFAULT_STEP
    exact = Decimal(str(value)) / Decimal(str(step))
    rounded = exact.quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal(str(step))
    return float(min(max(rounded, Decimal(str(minimum))), Decimal(str(maximum))))


@dataclass(frozen=True, slots=True)
class Report:
    """What a thermostat reports, read from its Home Assistant state."""

    mode: str
    target: float | None
    current: float | None
    modes: tuple[str, ...]
    min_temp: float
    max_temp: float
    step: float
    action: str | None = None

    @property
    def heat_mode(self) -> str:
        """The mode the thermostat heats in: ``heat``, or its only heating mode."""
        if HEAT in self.modes:
            return HEAT
        heating = [mode for mode in self.modes if mode != OFF]
        return heating[0] if len(heating) == 1 else HEAT

    @property
    def has_off(self) -> bool:
        """Whether the thermostat has an ``off`` mode."""
        return OFF in self.modes


@dataclass(frozen=True, slots=True)
class Want:
    """What the room wants from its thermostats: off, or heat at the room's target."""

    heat: bool
    target: float


@dataclass(frozen=True, slots=True)
class Command:
    """A ``climate`` service call for one thermostat."""

    service: str
    data: Mapping[str, Any]
    target: float | None  # the target the command sends, if it sends one


class Availability(Enum):
    """Whether the thermostat's entity can be used."""

    UNSEEN = "unseen"  # not reported yet; Home Assistant may still be starting
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


class Phase(StrEnum):
    """The command exchange with an available thermostat."""

    OK = "ok"  # nothing waiting for confirmation
    PENDING = "pending"  # a command waits for confirmation
    UNRESPONSIVE = "unresponsive"  # resends exhausted; retried every retry_interval
    BLOCKED = "blocked"  # Home Assistant refused a command


class Status(StrEnum):
    """What the problem sensor's ``thermostats`` attribute shows for a thermostat."""

    OK = "ok"
    PENDING = "pending"
    UNRESPONSIVE = "unresponsive"
    BLOCKED = "blocked"
    HELD = "held"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"


PROBLEM_STATUSES: Final = frozenset(
    {Status.UNRESPONSIVE, Status.BLOCKED, Status.UNAVAILABLE, Status.MISSING}
)


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    """What a report meant."""

    returned: bool = False  # back from being unavailable or missing
    hand_target: float | None = None  # a hand change, with the new target


class ThermostatDriver:
    """The controller's view of one thermostat (concept section 5.5)."""

    def __init__(self, entity_id: str, timing: Timing, *, slow: bool) -> None:
        """Start before the first report."""
        self.entity_id = entity_id
        self.timing = timing
        self.slow = slow
        self.availability = Availability.UNSEEN
        self.report: Report | None = None
        self.want: Want | None = None
        self.phase = Phase.OK
        self.attempts = 0  # sends of the command now pending
        self.deadline: datetime | None = None  # confirmation timeout, or the next retry
        self.send_due: datetime | None = None  # a send waiting for the debounce or the budget
        self.resending = False
        self.sent: list[float] = []  # targets sent since the last confirmation, oldest first
        self.last_confirmed: float | None = None
        self.first_report = True
        self.hold_until: datetime | None = None
        self.budget: deque[datetime] = deque()
        self.budget_exhausted = False
        self.error: str | None = None

    # --- What is wanted ------------------------------------------------------------------------

    @property
    def confirm_timeout(self) -> timedelta:
        """How long a command may take to be confirmed."""
        return self.timing.confirm_timeout_slow if self.slow else self.timing.confirm_timeout

    @property
    def available(self) -> bool:
        """Whether the thermostat is available."""
        return self.availability is Availability.AVAILABLE

    def held(self, now: datetime) -> bool:
        """Whether a hold keeps the controller away from the thermostat."""
        return self.hold_until is not None and now < self.hold_until

    def wanted_target(self, report: Report | None = None) -> float | None:
        """The target wanted from the thermostat, rounded and clamped to what it accepts.

        None when the thermostat is wanted off and has an ``off`` mode.
        """
        report = report or self.report
        if self.want is None or report is None:
            return None
        if self.want.heat:
            return round_target(self.want.target, report.step, report.min_temp, report.max_temp)
        return None if report.has_off else report.min_temp

    def matches(self, report: Report | None = None) -> bool:
        """Whether the thermostat reports what is wanted (that is: a command is confirmed)."""
        report = report or self.report
        if self.want is None or report is None:
            return True
        if not self.want.heat and report.has_off:
            return report.mode == OFF
        return report.mode == report.heat_mode and same(report.target, self.wanted_target(report))

    def command(self) -> Command:
        """The command that brings the thermostat to what is wanted."""
        report = self.report
        assert report is not None  # noqa: S101  (only called with a report)
        if self.want is not None and not self.want.heat and report.has_off:
            return Command("set_hvac_mode", {"hvac_mode": OFF}, None)
        target = self.wanted_target()
        assert target is not None  # noqa: S101  (heat, or off by the minimum target)
        data: dict[str, Any] = {"temperature": target}
        if report.mode != report.heat_mode:
            # Together with the target: setting heat alone makes a FRITZ! thermostat apply the
            # FRITZ!Box's own temperatures.
            data["hvac_mode"] = report.heat_mode
        return Command("set_temperature", data, target)

    def set_want(self, want: Want, now: datetime) -> None:
        """What is wanted changed: send it, after the debounce, unless it already matches."""
        if want == self.want:
            return
        self.want = want
        if not self.available or self.held(now) or self.report is None:
            return
        if self.matches():
            self._confirm()
        elif self.phase is not Phase.BLOCKED:
            # A blocked thermostat is not sent into; it is retried on its own schedule.
            self.resending = False
            self._request_send(now)

    def reconcile(self, now: datetime) -> None:
        """Correct the thermostat if it differs and nothing is waiting for confirmation."""
        if not self.available or self.held(now) or self.report is None or self.want is None:
            return
        if self.matches():
            self._confirm()
        elif self.phase is Phase.OK:
            self._request_send(now)

    # --- Reports -------------------------------------------------------------------------------

    def on_report(
        self,
        report: Report | None,
        now: datetime,
        *,
        exists: bool = True,
        adopt: bool = False,
        initial: bool = False,
    ) -> ReportOutcome:
        """Take a report: the thermostat's new state, or None while it cannot be used.

        ``exists`` is False for an entity that does not exist. ``adopt`` is False while the
        room's radiators are off, so nothing is adopted. ``initial`` marks the state found when
        the controller starts, which is not a report that arrived: the first report after a
        start is still to come.
        """
        if report is None:
            self.availability = Availability.UNAVAILABLE if exists else Availability.MISSING
            self.phase = Phase.OK
            self.attempts = 0
            self.deadline = None
            self.send_due = None
            self.resending = False
            self.first_report = True
            return ReportOutcome()

        returned = self.availability is not Availability.AVAILABLE
        self.availability = Availability.AVAILABLE
        previous, self.report = self.report, report
        if returned:
            self.phase = Phase.OK
            self.attempts = 0
            self.deadline = None
        first = self.first_report
        if not initial:
            self.first_report = False
        if self.held(now) or self.want is None:
            return ReportOutcome(returned=returned)
        if self.matches():
            self._confirm()
            return ReportOutcome(returned=returned)
        if not first and adopt and self._is_hand_change(previous, report):
            return ReportOutcome(returned=returned, hand_target=report.target)
        if self.phase is Phase.BLOCKED:
            if (
                previous is None
                or report.mode != previous.mode
                or not same(report.target, previous.target)
            ):
                self._retry_now(now)
        elif self.phase is Phase.OK:
            self._request_send(now)
        return ReportOutcome(returned=returned)

    def _is_hand_change(self, previous: Report | None, report: Report) -> bool:
        """All of concept section 5.5's conditions for a change made on the thermostat."""
        if report.mode != report.heat_mode or report.target is None or previous is None:
            return False
        if previous.target is not None and same(previous.target, report.target):
            return False
        known = [self.wanted_target(report), self.last_confirmed, *self.sent]
        return not any(same(report.target, target) for target in known)

    def _confirm(self) -> None:
        target = self.wanted_target()
        if target is not None:
            # Forget the targets sent before the confirmed one; keep any sent after it.
            for index in range(len(self.sent) - 1, -1, -1):
                if same(self.sent[index], target):
                    del self.sent[: index + 1]
                    break
            self.last_confirmed = target
        self.phase = Phase.OK
        self.attempts = 0
        self.deadline = None
        self.send_due = None
        self.resending = False
        self.budget_exhausted = False
        self.error = None

    # --- Commands ------------------------------------------------------------------------------

    def _request_send(self, now: datetime) -> None:
        if self.send_due is None:
            self.send_due = now + self.timing.debounce

    def _retry_now(self, now: datetime) -> None:
        self.deadline = None
        self._request_send(now)

    def _budget_allows(self, now: datetime) -> bool:
        while self.budget and self.budget[0] <= now - BUDGET_WINDOW:
            self.budget.popleft()
        return len(self.budget) < self.timing.command_budget_per_hour

    def tick(self, now: datetime, *, may_send: bool = True) -> Command | None:
        """Advance to ``now`` and return the command to send now, if any.

        ``may_send`` is False while the room is collecting hand changes; a send that falls due
        then waits until they have settled.
        """
        if self.hold_until is not None and now >= self.hold_until:
            self.release(now)
        if not self.available or self.held(now) or self.report is None or self.want is None:
            return None
        if self.deadline is not None and now >= self.deadline:
            self.deadline = None
            if self.phase is Phase.PENDING and self.attempts > self.timing.resend_attempts:
                self.phase = Phase.UNRESPONSIVE
                self.deadline = now + self.timing.retry_interval
            else:
                # A resend (pending) or a retry (unresponsive, blocked).
                self.resending = self.phase is Phase.PENDING
                self.send_due = now
        if self.send_due is None or now < self.send_due:
            return None
        if self.matches():
            self._confirm()
            return None
        if not may_send:
            return None
        if not self._budget_allows(now):
            self.budget_exhausted = True
            self.send_due = self.budget[0] + BUDGET_WINDOW
            return None
        self.budget_exhausted = False
        command = self.command()
        self.send_due = None
        self.budget.append(now)
        if command.target is not None and not (self.sent and same(self.sent[-1], command.target)):
            self.sent.append(command.target)
        if self.phase is Phase.UNRESPONSIVE:
            self.deadline = now + self.timing.retry_interval
        else:
            resend = self.phase is Phase.PENDING and self.resending
            self.attempts = self.attempts + 1 if resend else 1
            self.phase = Phase.PENDING
            self.deadline = now + self.confirm_timeout
        self.resending = False
        return command

    def on_refused(self, now: datetime, error: str) -> None:
        """Home Assistant rejected a command: blocked, and retried every retry_interval."""
        self.phase = Phase.BLOCKED
        self.attempts = 0
        self.deadline = now + self.timing.retry_interval
        self.send_due = None
        self.resending = False
        self.error = error

    # --- Hold ----------------------------------------------------------------------------------

    def hold(self, now: datetime, duration: timedelta) -> None:
        """Keep the controller away from the thermostat for ``duration``."""
        self.hold_until = now + duration
        self.deadline = None
        self.send_due = None
        self.resending = False

    def release(self, now: datetime) -> None:
        """End a hold. The next report is not a hand change, and a differing state is corrected."""
        if self.hold_until is None:
            return
        self.hold_until = None
        self.first_report = True
        self.phase = Phase.OK
        self.attempts = 0
        self.deadline = None
        self.reconcile(now)

    # --- Status --------------------------------------------------------------------------------

    def status(self, now: datetime) -> Status:
        """What the problem sensor shows for this thermostat."""
        if self.availability is Availability.MISSING:
            return Status.MISSING
        if not self.available:
            return Status.UNAVAILABLE
        if self.held(now):
            return Status.HELD
        return Status(self.phase.value)

    def next_deadline(self, now: datetime) -> datetime | None:
        """When ``tick`` has something to do next."""
        candidates = [self.hold_until] if self.hold_until is not None else []
        if self.available and not self.held(now):
            candidates += [time for time in (self.deadline, self.send_due) if time is not None]
        future = [time for time in candidates if time > now]
        return min(future) if future else None

    def diagnostics(self, now: datetime) -> dict[str, Any]:
        """The driver's state, for the diagnostics download."""
        return {
            "status": self.status(now).value,
            "slow": self.slow,
            "want": None
            if self.want is None
            else {"heat": self.want.heat, "target": self.want.target},
            "phase": self.phase.value,
            "attempts": self.attempts,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "send_due": self.send_due.isoformat() if self.send_due else None,
            "sent": list(self.sent),
            "last_confirmed": self.last_confirmed,
            "hold_until": self.hold_until.isoformat() if self.hold_until else None,
            "commands_last_hour": len(self.budget),
            "budget_exhausted": self.budget_exhausted,
            "error": self.error,
        }


# --- Room temperature to the thermostat --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedStep:
    """A value to write to a thermostat's external temperature input."""

    value: float
    restarted: bool  # the feed starts again after a stop: device settings follow


@dataclass(slots=True)
class Feed:
    """The temperature feed of one thermostat (concept section 5.5)."""

    timing: Timing
    active: bool = False
    started: bool = False
    last_value: float | None = None
    last_at: datetime | None = None

    def _changed(self, reading: float) -> bool:
        return (
            self.last_value is not None
            and abs(reading - self.last_value) >= self.timing.feed_threshold - _TOLERANCE / 5
        )

    def step(
        self, now: datetime, reading: float | None, *, allowed: bool, paused: bool = False
    ) -> FeedStep | None:
        """The value to write now, if any.

        ``allowed`` is False while home mode or the room is off and while the room sensor is
        unavailable or stale: the feed stops. ``paused`` holds writes back without stopping it,
        while the thermostat is held or its input cannot be written.
        """
        if not allowed or reading is None:
            self.active = False
            return None
        if paused:
            return None
        if not self.active:
            restarted = self.started
            self.active = True
            self.started = True
            return FeedStep(reading, restarted)
        if self.last_at is None:
            return FeedStep(reading, False)
        elapsed = now - self.last_at
        if elapsed >= self.timing.feed_max_interval:
            return FeedStep(reading, False)
        if elapsed >= self.timing.feed_min_interval and self._changed(reading):
            return FeedStep(reading, False)
        return None

    def wrote(self, value: float, now: datetime) -> None:
        """Record a write (also a failed one, so a failing input is not hammered)."""
        self.last_value = value
        self.last_at = now

    def next_deadline(self, now: datetime, reading: float | None) -> datetime | None:
        """When the feed writes next without a new reading."""
        if not self.active or self.last_at is None:
            return None
        candidates = [self.last_at + self.timing.feed_max_interval]
        if reading is not None and self._changed(reading):
            candidates.append(self.last_at + self.timing.feed_min_interval)
        future = [time for time in candidates if time > now]
        return min(future) if future else None


# --- Device settings ---------------------------------------------------------------------------


def setting_matches(entity_id: str, wanted: SettingValue, state: str) -> bool:
    """Whether a settings entity is already at the configured value."""
    if entity_id.split(".", 1)[0] == "number":
        try:
            return same(float(state), float(wanted))
        except ValueError:
            return False
    return state == str(wanted)


def setting_call(entity_id: str, wanted: SettingValue) -> tuple[str, str, dict[str, Any]]:
    """The service call (domain, service, data) that writes a device setting."""
    domain = entity_id.split(".", 1)[0]
    if domain == "switch":
        return "switch", "turn_on" if wanted == "on" else "turn_off", {}
    if domain == "number":
        return "number", "set_value", {"value": wanted}
    return "select", "select_option", {"option": str(wanted)}
