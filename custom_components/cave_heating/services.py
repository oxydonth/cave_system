"""The services of concept section 5.2, registered once when the integration is set up."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import probatio as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

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
    from .runtime import HeatingRuntime, RoomRuntime


def _longer_than_zero(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise vol.Invalid("must last longer than zero")
    return value


def _hold_duration(value: timedelta) -> timedelta:
    value = _longer_than_zero(value)
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
BOOST_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(ATTR_DURATION): vol.All(cv.positive_time_period, _longer_than_zero),
        vol.Optional(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float), vol.Range(min=MIN_TEMPERATURE, max=MAX_TEMPERATURE)
        ),
    }
)
RESUME_SCHEMA = cv.make_entity_service_schema({})


def _runtime(hass: HomeAssistant) -> HeatingRuntime:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    runtime: HeatingRuntime = entries[0].runtime_data
    return runtime


def _target_rooms(
    hass: HomeAssistant, runtime: HeatingRuntime, call: ServiceCall
) -> list[RoomRuntime]:
    """The rooms a call targets, by their thermostats ``climate.cave_<room>``.

    An entity named directly that is not a room thermostat is refused, and so is a target that
    reaches no room thermostat at all, so that a mistaken call never passes silently.
    """
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    rooms: dict[str, RoomRuntime] = {}
    for entity_id in sorted(selected.referenced):
        room = runtime.room_for_climate(entity_id)
        if room is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_room",
                translation_placeholders={"entity": entity_id},
            )
        rooms[room.config.id] = room
    for entity_id in sorted(selected.indirectly_referenced):
        if (room := runtime.room_for_climate(entity_id)) is not None:
            rooms[room.config.id] = room
    if not rooms:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_room")
    return list(rooms.values())


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register ``reload``, ``hold``, ``release``, ``boost`` and ``resume_schedule``.

    ``boost`` and ``resume_schedule`` target room thermostats (``climate.cave_<room>``), so a
    room can be chosen by its entity, its device or its area.
    """

    async def reload(call: ServiceCall) -> None:
        # An invalid file fails the call, so `cave apply` can tell the user the new
        # configuration was not taken. As a validation error it reaches a REST caller as a 400
        # with this message, and Home Assistant does not log it as an error.
        result = await _runtime(hass).async_reload()
        if result.config is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="reload_failed",
                translation_placeholders={"errors": " ".join(result.errors)},
            )

    async def hold(call: ServiceCall) -> None:
        _runtime(hass).hold(call.data[ATTR_ENTITY_ID], call.data[ATTR_DURATION])

    async def release(call: ServiceCall) -> None:
        _runtime(hass).release(call.data[ATTR_ENTITY_ID])

    async def boost(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        rooms = _target_rooms(hass, runtime, call)
        for room in rooms:  # all or none: refused before any room is boosted
            runtime.refuse_if_off(room)
        for room in rooms:
            runtime.boost(room, call.data.get(ATTR_DURATION), call.data.get(ATTR_TEMPERATURE))

    async def resume_schedule(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        for room in _target_rooms(hass, runtime, call):
            runtime.resume_schedule(room)

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, reload)
    hass.services.async_register(DOMAIN, SERVICE_HOLD, hold, schema=HOLD_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELEASE, release, schema=RELEASE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_BOOST, boost, schema=BOOST_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_SCHEDULE, resume_schedule, schema=RESUME_SCHEMA
    )
