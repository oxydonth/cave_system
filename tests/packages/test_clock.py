"""The clock these tests run on: every minute that ``advance`` moves is one the sensors see.

The sensors are evaluated by a time pattern (of the automation that pushes) at second 0 of every
minute plus an offset that Home Assistant draws at random between 50 and 500 ms. While the clock
was frozen wherever the real clock happened to be when a test started, a test that started in the
last half second of a minute saw an evaluation in every other minute only, and one that started in
the first second of a minute, past the offset, had its first evaluation while the package was being
set up, before anything listened (``check_clock`` in helpers.py). Both are checked here at both
ends of the offset.
"""

from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import RANDOM_MICROSECOND_MAX, RANDOM_MICROSECOND_MIN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import get_scheduled_timer_handles

from tests.packages.helpers import START, advance, check_clock, load_package

BATTERY = {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Hall contact"}
SENSOR = "sensor.cave_attention_battery"


@pytest.fixture(params=[RANDOM_MICROSECOND_MIN, RANDOM_MICROSECOND_MAX], ids=["earliest", "latest"])
def offset(request: pytest.FixtureRequest) -> Iterator[int]:
    """The offset after second 0 that the time pattern's tracker draws, pinned to one end."""
    with patch("homeassistant.helpers.event.randint", return_value=request.param):
        yield request.param


async def test_home_assistant_runs_on_the_frozen_clock_from_its_start(hass: HomeAssistant) -> None:
    """Every test starts at the same instant, and Home Assistant was set up on it.

    The frozen clock is read live, so the first two lines would hold even if ``hass`` had been
    set up before ``clock`` froze it. The timer Home Assistant schedules while it is set up (a
    daily one) shows when that was: set before the freeze, it would be on the real monotonic
    clock, far below ``START``, or gone, run when the clock jumped; either way this fails. Set up
    after ``freezer`` but before ``clock`` moved it, Home Assistant would have started on the
    frozen real date, long after ``START``, and its daily timer would be due long after the day
    after ``START``: the upper bound catches that.
    """
    assert dt_util.utcnow() == START
    assert hass.loop.time() == START.timestamp()
    timers = [h.when() for h in get_scheduled_timer_handles(hass.loop) if not h.cancelled()]
    assert timers, "no timer from the set-up of Home Assistant is left"
    assert min(timers) >= START.timestamp()
    assert max(timers) <= START.timestamp() + 2 * 86400


async def test_every_minute_is_one_evaluation_and_the_first_comes_after_the_set_up(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, offset: int
) -> None:
    """Nothing is evaluated while the package is set up; then each minute is evaluated once."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown = await load_package(hass)
    assert hass.states.get(SENSOR).state == "unknown", "evaluated while it was being set up"

    evaluated = []
    for _ in range(5):
        await advance(hass, freezer, 1)
        evaluated.append(hass.states.get(SENSOR).last_reported == dt_util.utcnow())

    assert evaluated == [True] * 5
    assert shown.titles == ["Low battery"]


@pytest.mark.parametrize("second", [0.6, 59.6])
def test_a_clock_near_the_boundary_of_a_minute_is_refused(
    freezer: FrozenDateTimeFactory, second: float
) -> None:
    """A test that moves the clock there fails at once instead of now and then."""
    check_clock()
    freezer.move_to(START.replace(second=0) + timedelta(seconds=second))
    with pytest.raises(AssertionError, match="see check_clock"):
        check_clock()


async def test_loading_the_package_near_the_boundary_of_a_minute_is_refused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """``load_package`` checks the clock before it sets anything up."""
    freezer.move_to(START.replace(second=0) + timedelta(seconds=0.6))
    with pytest.raises(AssertionError, match="see check_clock"):
        await load_package(hass)


async def test_a_step_of_advance_near_the_boundary_of_a_minute_is_refused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """``advance`` checks the clock before every minute it moves."""
    await load_package(hass)
    freezer.move_to(START + timedelta(seconds=29.6))
    with pytest.raises(AssertionError, match="see check_clock"):
        await advance(hass, freezer, 1)
