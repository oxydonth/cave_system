"""What the tests of the notifications package share: loading it, and watching what it shows."""

from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification as pn
from homeassistant.config import merge_packages_config
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict

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
    package = load_yaml_dict(str(PACKAGE))
    config = await merge_packages_config(hass, {}, {"cave_notifications": package}, _fail)
    for domain in ("input_text", "script", "template", "automation"):
        assert await async_setup_component(hass, domain, config), domain
    await hass.async_block_till_done()
    return Shown(hass)
