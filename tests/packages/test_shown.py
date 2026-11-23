"""The helper that records the shown notifications is updated in the event loop.

Home Assistant runs a dispatcher target that is not marked as a callback in a worker thread.
``Shown._update`` was such a target, so ``shown.current`` changed some time after the service
call that created or dismissed a notification had returned. A test that looked at once could look
too early: ``test_a_notification_the_person_dismissed_does_not_come_back`` failed that way now and
then, with ``assert {} == {}`` because the thread had run by the time the failure was printed
(run 45104152634 on ``main``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from homeassistant.components import persistent_notification as pn
from homeassistant.core import HassJobType, HomeAssistant, get_hassjob_callable_job_type
from homeassistant.setup import async_setup_component

from tests.packages.helpers import Shown


def test_home_assistant_calls_the_update_of_shown_in_the_event_loop(hass: HomeAssistant) -> None:
    """What decides it: the kind of job Home Assistant makes of the target."""
    shown = Shown(hass)

    assert get_hassjob_callable_job_type(shown._update) is HassJobType.Callback


async def test_shown_follows_a_notification_without_any_waiting(hass: HomeAssistant) -> None:
    """Created and dismissed with nothing awaited in between, and ``Shown`` has followed.

    This shows the behaviour the tests rely on. It does not prove the cause on its own: a worker
    thread that happens to run at once would pass it too, which is why the test above looks at
    the kind of job."""
    assert await async_setup_component(hass, "persistent_notification", {})
    shown = Shown(hass)

    pn.async_create(hass, "The message", title="The title", notification_id="one")
    assert shown.titles == ["The title"]

    pn.async_dismiss(hass, "one")
    assert shown.current == {}
    assert shown.removed == ["one"]


class _HoldsBackShownUpdates(ThreadPoolExecutor):
    """An executor that does not run the jobs of ``Shown._update`` until it is told to.

    Whatever else is submitted runs at once, so Home Assistant behaves as it does. A job of
    ``Shown._update`` is kept back, which is the worst case of a worker thread that has not run
    yet, made certain instead of left to the scheduler.
    """

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.held: list[tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]] = []

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if "Shown._update" in _names(fn):
            outcome: Future[Any] = Future()
            self.held.append((outcome, fn, args))
            return outcome
        return super().submit(fn, *args, **kwargs)

    def release(self) -> None:
        """Run what was held back, now."""
        held, self.held = self.held, []
        for outcome, fn, args in held:
            try:
                outcome.set_result(fn(*args))
            except BaseException as err:
                outcome.set_exception(err)


def _names(fn: Any) -> str:
    """The qualified names of a function and of what it wraps, so a wrapper does not hide it."""
    names = []
    while fn is not None:
        names.append(getattr(fn, "__qualname__", ""))
        fn = getattr(fn, "__wrapped__", None) or getattr(fn, "func", None)
    return " ".join(names)


async def test_shown_has_followed_a_notification_even_when_no_worker_thread_has_run(
    hass: HomeAssistant,
) -> None:
    """Deterministic: the jobs of ``Shown._update`` are held back, and ``Shown`` still has followed.

    Without ``@callback`` on ``Shown._update`` Home Assistant hands the update to the default
    executor, this one holds it, and ``shown`` is empty right after ``async_create``. With it the
    update runs in the event loop at once. This fails every time without the mark.
    """
    assert await async_setup_component(hass, "persistent_notification", {})
    executor = _HoldsBackShownUpdates()
    previous = hass.loop._default_executor
    # Home Assistant makes the loop's own set_default_executor raise; the class's does not.
    asyncio.BaseEventLoop.set_default_executor(hass.loop, executor)  # type: ignore[arg-type]
    try:
        shown = Shown(hass)

        pn.async_create(hass, "The message", title="The title", notification_id="one")
        assert shown.titles == ["The title"], "the update waited for a worker thread"
        assert executor.held == []

        pn.async_dismiss(hass, "one")
        assert shown.removed == ["one"]
        assert shown.current == {}
    finally:
        executor.release()
        asyncio.BaseEventLoop.set_default_executor(hass.loop, previous)  # type: ignore[arg-type]
        executor.shutdown()
