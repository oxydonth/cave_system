"""The helper that records the shown notifications is updated in the event loop.

Home Assistant runs a dispatcher target that is not marked as a callback in a worker thread.
``Shown._update`` was such a target, so ``shown.current`` changed some time after the service
call that created or dismissed a notification had returned. A test that looked at once could look
too early: ``test_a_notification_the_person_dismissed_does_not_come_back`` failed that way now and
then, with ``assert {} == {}`` because the thread had run by the time the failure was printed
(run 45104152634 on ``main``).
"""

from __future__ import annotations

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
