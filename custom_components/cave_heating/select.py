"""``select.cave_home_mode`` (concept section 5.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HomeEntity
from .logic import HomeMode

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the home mode."""
    async_add_entities([HomeModeSelect(entry.runtime_data)])


class HomeModeSelect(HomeEntity, SelectEntity):
    """``auto`` (normal), ``away`` (forced setback, for holidays), ``off`` (summer)."""

    _attr_translation_key = "home_mode"
    _attr_options = [mode.value for mode in HomeMode]

    def __init__(self, runtime: HeatingRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, "select", "home_mode")

    @property
    def current_option(self) -> str:
        """The home mode."""
        return self.runtime.home_mode.value

    async def async_select_option(self, option: str) -> None:
        """Switched by hand: away and off end every boost and override."""
        self.runtime.set_home_mode(HomeMode(option))
