"""Per-room heating control for the cave_system home.

This is the WP1 skeleton: Home Assistant loads the integration and accepts any
``cave_heating:`` configuration without validating it. The configuration schema,
entities and heating logic are added by the ``cave_heating`` work package.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

# Accept anything under the domain key, including an empty ``cave_heating:``.
CONFIG_SCHEMA = vol.Schema({DOMAIN: object}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML and keep the raw configuration."""
    hass.data[DOMAIN] = config.get(DOMAIN)
    return True
