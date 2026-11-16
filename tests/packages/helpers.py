"""What the tests of the notifications package share: loading it, and watching what it shows."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification as pn
from homeassistant.config import merge_packages_config
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import async_fire_time_changed

PACKAGE = Path(__file__).parents[2] / "homeassistant" / "packages" / "cave_notifications.yaml"


class Shown:
    """The persistent notifications that are currently shown in the Home Assistant UI."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.current: dict[str, pn.Notification] = {}
        pn.async_register_callback(hass, self._update)

    def _update(self, kind: pn.UpdateType, notifications: dict[str, pn.Notification]) -> None:
        if kind is pn.UpdateType.REMOVED:
            for notification_id in notifications:
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


async def load_package(hass: HomeAssistant) -> Shown:
    """Merge the package into the configuration and set up what it contains."""
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "system_log", {})
    package = load_yaml_dict(str(PACKAGE))
    config = await merge_packages_config(hass, {}, {"cave_notifications": package}, _fail)
    for domain in ("input_text", "script", "template", "automation"):
        assert await async_setup_component(hass, domain, config), domain
    await hass.async_block_till_done()
    return Shown(hass)


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
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def in_area(hass: HomeAssistant, entity_id: str, area_name: str) -> None:
    """Register an entity and place it in an area (its state is set by the test)."""
    area = ar.async_get(hass).async_get_or_create(area_name)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(domain, "test", entity_id, suggested_object_id=object_id)
    er.async_get(hass).async_update_entity(entity_id, area_id=area.id)


def room(hass: HomeAssistant, area_name: str, slug: str) -> None:
    """A room as the integration makes it: a room thermostat placed in the area of its name."""
    in_area(hass, f"climate.cave_{slug}", area_name)
    hass.states.async_set(f"climate.cave_{slug}", "heat")
