"""Diagnostics download: the controller's state per room and thermostat.

Nothing here is secret: the file names entities only, and no credentials pass through the
integration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CaveHeatingConfigEntry
) -> dict[str, Any]:
    """Return the controller's state."""
    return entry.runtime_data.diagnostics()
