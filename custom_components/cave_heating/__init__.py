"""Per-room heating control for the cave_system home (concept section 5).

``configuration.yaml`` holds only an empty ``cave_heating:`` entry. The integration then creates
its config entry itself and reads the applied copy of the home description,
``cave/.applied/heating.yaml`` in the configuration directory, at start-up and on
``cave_heating.reload``. A missing or invalid file means no rooms and a repair notice; it never
keeps Home Assistant from starting.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .runtime import CaveHeatingConfigEntry, HeatingRuntime, applied_file_path, read_applied_file
from .services import async_setup_services

# Nothing is configured in configuration.yaml: rooms come from the applied heating.yaml. Content
# under the key is logged as an error and otherwise ignored, so it cannot stop Home Assistant.
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and create the config entry on first start."""
    async_setup_services(hass)
    if not hass.config_entries.async_entries(DOMAIN, include_ignore=True):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={}),
            f"{DOMAIN} create config entry",
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CaveHeatingConfigEntry) -> bool:
    """Load the applied file and start the controller."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored = await store.async_load()
    result = await hass.async_add_executor_job(read_applied_file, applied_file_path(hass))
    runtime = HeatingRuntime(hass, entry, store, stored)
    runtime.set_file_problem(result)
    if result.config is not None:
        runtime.apply_config(result.config)
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if runtime.loaded:
        runtime.update_devices()
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CaveHeatingConfigEntry) -> bool:
    """Stop the controller and keep its state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
