"""What the tests of the notifications package share: loading it, and watching what it shows."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification as pn
from homeassistant.config import merge_packages_config
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import async_fire_time_changed

PACKAGE = Path(__file__).parents[2] / "homeassistant" / "packages" / "cave_notifications.yaml"

# The frozen clock every test of this directory starts on (the autouse fixture ``clock``):
# second 30 of a minute, as far as it gets from the minute's boundary. See ``check_clock``.
START = datetime(2026, 1, 14, 12, 0, 30, tzinfo=UTC)


def check_clock() -> None:
    """Fail unless the frozen clock is clear of the boundary of a minute.

    The time pattern that evaluates the sensors fires at second 0 of a minute plus a random
    offset of up to 0.5 s, and ``advance`` keeps the clock's second within the minute. Two
    seconds break it:

    - In the first second of a minute, past the offset, the first time the tracker computes
      while the package is set up is already over, so the sensors are evaluated during the set-up,
      before ``Shown`` listens, and the start-up case passes unseen.
    - In the last half second, ``async_fire_time_changed`` looks 0.5 s ahead, into second 0 but
      short of the offset. The tracker fires (a minute late), takes that time as past its own
      slot, schedules the slot of the following minute, and the next ``advance`` falls short of
      it: the sensors are evaluated in every other minute only.
    """
    second = dt_util.utcnow().second
    assert 1 <= second <= 58, f"the clock is at second {second} of a minute, see check_clock"


class Shown:
    """The persistent notifications that are currently shown in the Home Assistant UI."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.current: dict[str, pn.Notification] = {}
        self.removed: list[str] = []  # every id that was dismissed, in order
        pn.async_register_callback(hass, self._update)

    def _update(self, kind: pn.UpdateType, notifications: dict[str, pn.Notification]) -> None:
        if kind is pn.UpdateType.REMOVED:
            for notification_id in notifications:
                self.removed.append(notification_id)
                self.current.pop(notification_id, None)
        else:
            self.current.update(notifications)

    @property
    def titles(self) -> list[str]:
        return sorted(n["title"] or "" for n in self.current.values())

    @property
    def messages(self) -> list[str]:
        return sorted(n["message"] for n in self.current.values())


def _fail(hass: HomeAssistant, name: str, config: Any, message: str, domain_config: Any) -> None:
    raise AssertionError(f"package error: {name}: {message}")


async def load_package(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory | None = None, *, settle: bool = True
) -> Shown:
    """Merge the package into the configuration and set up what it contains.

    With a ``freezer`` (and ``settle``) a minute passes, so the sensors have made their first
    evaluation when this returns and what a test then changes is a change of the home. That
    first evaluation is the start-up case: what it finds is shown and not pushed. The restart
    tests run it themselves, with the sensors restored, and pass nothing.
    """
    check_clock()
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "system_log", {})
    package = load_yaml_dict(str(PACKAGE))
    config = await merge_packages_config(hass, {}, {"cave_notifications": package}, _fail)
    for domain in ("input_text", "script", "template", "automation"):
        assert await async_setup_component(hass, domain, config), domain
    await hass.async_block_till_done()
    shown = Shown(hass)
    if settle and freezer is not None:
        await advance(hass, freezer, 1)
    return shown


async def set_helper(hass: HomeAssistant, name: str, value: str) -> None:
    """Type a value into one of the package's helpers, ``input_text.<name>``."""
    await hass.services.async_call(
        "input_text",
        "set_value",
        {"entity_id": f"input_text.{name}", "value": value},
        blocking=True,
    )


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    """Move the clock forward one minute at a time, letting Home Assistant react to each."""
    for _ in range(minutes):
        check_clock()
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def in_area(hass: HomeAssistant, entity_id: str, area_name: str) -> None:
    """Register an entity and place it in an area (its state is set by the test)."""
    area = ar.async_get(hass).async_get_or_create(area_name)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(domain, "test", entity_id, suggested_object_id=object_id)
    er.async_get(hass).async_update_entity(entity_id, area_id=area.id)


def room(hass: HomeAssistant, area_name: str, slug: str, humidity: float | None = None) -> None:
    """A room as the integration makes it: a room thermostat placed in the area of its name."""
    in_area(hass, f"climate.cave_{slug}", area_name)
    set_humidity(hass, slug, humidity)


def set_humidity(hass: HomeAssistant, slug: str, humidity: float | str | None) -> None:
    """The room thermostat with the reading of the room's humidity sensor.

    The integration's ``current_humidity`` is the reading of the room's configured sensor, and
    the attribute is absent while there is no reading (no sensor, or it is unavailable).
    """
    attributes = {} if humidity is None else {"current_humidity": humidity}
    hass.states.async_set(f"climate.cave_{slug}", "heat", attributes)
