"""Diagnostics download: the controller's state per room and thermostat.

The file names entities only, and no credentials pass through the integration. Presence is
redacted: the trackers' readings and since when nobody has been home say who is at home.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry

TO_REDACT = {"presence", "nobody_home_since", "occupied"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CaveHeatingConfigEntry
) -> dict[str, Any]:
    """Return the controller's state, without who is at home."""
    data: dict[str, Any] = async_redact_data(entry.runtime_data.diagnostics(), TO_REDACT)
    return data
