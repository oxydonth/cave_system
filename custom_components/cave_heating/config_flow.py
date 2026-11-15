"""The config entry that represents the integration (concept section 5.2).

The integration creates its one entry itself, through the import step, the first time Home
Assistant starts with ``cave_heating:`` in its configuration. Rooms are described in
``cave/heating.yaml`` and applied with ``./cave apply``; there is nothing to set up in the UI, so a
flow started there ends with a message saying so.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN

TITLE = "Cave Heating"


class CaveHeatingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Creates the single entry; the rooms come from the file."""

    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry on first start."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=TITLE, data={})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Started from the UI: explain that the integration is configured through the file."""
        return self.async_abort(reason="configured_by_file")
