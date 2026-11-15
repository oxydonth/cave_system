"""Time for the simulators.

Everything timed in the simulators — reporting intervals, the room model, the FRITZ!Box's reporting
delay, the Sonoff falling back to its own sensor — is scheduled on a :class:`Clock`, never with
``sleep``. Three clocks exist:

* :class:`ManualClock` — time moves only when a test calls :meth:`ManualClock.advance`.
* :class:`AsyncioClock` — wall-clock time on an asyncio loop, used by ``python -m cave_sim``.
* ``cave_sim.testing.HassClock`` — Home Assistant's clock, so that ``async_fire_time_changed`` and
  the ``freezer`` fixture move the simulators together with Home Assistant.
"""

import asyncio
import heapq
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


class TimerHandle(Protocol):
    """A scheduled callback that can be cancelled."""

    def cancel(self) -> None:
        """Do not run the callback."""


class Clock(Protocol):
    """What the simulators need from time."""

    def now(self) -> float:
        """Return the current time as seconds since the Unix epoch."""

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle:
        """Run ``callback`` once, ``delay`` seconds from now."""


def utc_datetime(clock: Clock) -> datetime:
    """Return the clock's current time as an aware UTC datetime."""
    return datetime.fromtimestamp(clock.now(), tz=UTC)


@dataclass(order=True)
class _Scheduled:
    when: float
    sequence: int
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        self.cancelled = True


class ManualClock:
    """A clock that moves only when told to; callbacks run in time order during :meth:`advance`."""

    def __init__(self, start: float = 1_767_225_600.0) -> None:
        # The default start is 2026-01-01T00:00:00Z: a fixed, readable moment.
        self._now = start
        self._queue: list[_Scheduled] = []
        self._sequence = itertools.count()

    def now(self) -> float:
        """Return the current simulated time."""
        return self._now

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle:
        """Schedule ``callback`` at ``now() + delay``."""
        entry = _Scheduled(self._now + max(delay, 0.0), next(self._sequence), callback)
        heapq.heappush(self._queue, entry)
        return entry

    def advance(self, seconds: float) -> None:
        """Move time forward, running every callback that falls due on the way, in order."""
        target = self._now + seconds
        while self._queue and self._queue[0].when <= target:
            entry = heapq.heappop(self._queue)
            if entry.cancelled:
                continue
            self._now = max(self._now, entry.when)
            entry.callback()
        self._now = target

    def pending(self) -> int:
        """Return how many callbacks are waiting; for tests."""
        return sum(1 for entry in self._queue if not entry.cancelled)


class AsyncioClock:
    """Wall-clock time, with callbacks run by an asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def now(self) -> float:
        """Return the current wall-clock time."""
        return time.time()

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle:
        """Schedule ``callback`` on the loop."""
        return self._loop.call_later(max(delay, 0.0), callback)


class PeriodicTask:
    """Run a callback every ``interval`` seconds on a clock until stopped."""

    def __init__(self, clock: Clock, interval: float, callback: Callable[[], None]) -> None:
        self._clock = clock
        self._interval = interval
        self._callback = callback
        self._handle: TimerHandle | None = None

    def start(self) -> None:
        """Start ticking; the first tick is one interval from now."""
        if self._handle is None:
            self._handle = self._clock.call_later(self._interval, self._tick)

    def stop(self) -> None:
        """Stop ticking."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _tick(self) -> None:
        self._handle = self._clock.call_later(self._interval, self._tick)
        self._callback()


class Timer:
    """A restartable one-shot timer on a clock."""

    def __init__(self, clock: Clock, callback: Callable[[], None]) -> None:
        self._clock = clock
        self._callback = callback
        self._handle: TimerHandle | None = None

    @property
    def running(self) -> bool:
        """Whether the timer is armed."""
        return self._handle is not None

    def start(self, delay: float) -> None:
        """(Re)arm the timer to fire ``delay`` seconds from now."""
        self.cancel()
        self._handle = self._clock.call_later(delay, self._fire)

    def cancel(self) -> None:
        """Disarm the timer."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self) -> None:
        self._handle = None
        self._callback()
