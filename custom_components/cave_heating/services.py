"""The services of concept section 5.2, registered once when the integration is set up."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import probatio as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import (
    ATTR_DURATION,
    DOMAIN,
    HOLD_DEFAULT_DURATION,
    HOLD_MAX_DURATION,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SERVICE_BOOST,
    SERVICE_HOLD,
    SERVICE_RELEASE,
    SERVICE_RELOAD,
    SERVICE_RESUME_SCHEDULE,
)

if TYPE_CHECKING:
    from .runtime import HeatingRuntime


def _hold_duration(value: timedelta) -> timedelta:
    if value > HOLD_MAX_DURATION:
        raise vol.Invalid(f"a hold lasts at most {HOLD_MAX_DURATION}")
    return value


HOLD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_domain(CLIMATE_DOMAIN),
        vol.Optional(ATTR_DURATION, default=HOLD_DEFAULT_DURATION): vol.All(
            cv.positive_time_period, _hold_duration
        ),
    }
)
RELEASE_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_domain(CLIMATE_DOMAIN)})
BOOST_SCHEMA: dict[str | vol.Marker, object] = {
    vol.Optional(ATTR_DURATION): cv.positive_time_period,
    vol.Optional(ATTR_TEMPERATURE): vol.All(
        vol.Coerce(float), vol.Range(min=MIN_TEMPERATURE, max=MAX_TEMPERATURE)
    ),
}


def _runtime(hass: HomeAssistant) -> HeatingRuntime:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    runtime: HeatingRuntime = entries[0].runtime_data
    return runtime


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register ``reload``, ``hold`` and ``release``, and ``boost`` and ``resume_schedule``.

    ``boost`` and ``resume_schedule`` target room thermostats (``climate.cave_<room>``), so a
    room can be chosen by its entity, its device or its area.
    """

    async def reload(call: ServiceCall) -> None:
        result = await _runtime(hass).async_reload()
        if result.config is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reload_failed",
                translation_placeholders={"errors": "; ".join(result.errors)},
            )

    async def hold(call: ServiceCall) -> None:
        _runtime(hass).hold(call.data[ATTR_ENTITY_ID], call.data[ATTR_DURATION])

    async def release(call: ServiceCall) -> None:
        _runtime(hass).release(call.data[ATTR_ENTITY_ID])

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, reload)
    hass.services.async_register(DOMAIN, SERVICE_HOLD, hold, schema=HOLD_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELEASE, release, schema=RELEASE_SCHEMA)
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_BOOST,
        entity_domain=CLIMATE_DOMAIN,
        schema=BOOST_SCHEMA,
        func="async_boost",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_RESUME_SCHEDULE,
        entity_domain=CLIMATE_DOMAIN,
        schema=None,
        func="async_resume_schedule",
    )
