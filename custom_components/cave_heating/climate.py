"""The room thermostat ``climate.cave_<room>`` (concept section 5.2)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_BOOST_UNTIL,
    ATTR_OVERRIDE_UNTIL,
    ATTR_REASON,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    TEMPERATURE_STEP,
)
from .entity import RoomEntity
from .logic import Boost, Override, Preset, RoomMode

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime, RoomRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a room thermostat per room, now and for rooms added on reload."""
    runtime = entry.runtime_data

    def add(rooms: list[RoomRuntime]) -> None:
        async_add_entities(RoomClimate(runtime, room) for room in rooms)

    add(list(runtime.rooms.values()))
    runtime.register_room_adder(add)


class RoomClimate(RoomEntity, ClimateEntity):
    """One thermostat for the whole room."""

    _attr_name = None
    _attr_translation_key = "room"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = [preset.value for preset in Preset]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMPERATURE
    _attr_max_temp = MAX_TEMPERATURE
    _attr_target_temperature_step = TEMPERATURE_STEP

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime) -> None:
        """Set ``climate.cave_<room>``."""
        super().__init__(runtime, room, "climate", "climate")

    def _room(self) -> RoomRuntime:
        room = self.room
        assert room is not None  # noqa: S101  (actions only reach an available entity)
        return room

    @property
    def available(self) -> bool:
        """Unavailable when no thermostat of the room is available (section 5.4)."""
        room = self.room
        return room is not None and room.available

    @property
    def hvac_mode(self) -> HVACMode:
        """Whether the room is switched on or off."""
        room = self.room
        return HVACMode.OFF if room and room.state.mode is RoomMode.OFF else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Off while the radiators are off; heating while a thermostat says it heats."""
        room = self.room
        if room is None or room.decision is None:
            return None
        if not room.decision.heating:
            return HVACAction.OFF
        return HVACAction.HEATING if room.heating_action else HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        """The outcome of rows 4 to 7, also while the radiators are off."""
        room = self.room
        return room.decision.target if room and room.decision else None

    @property
    def preset_mode(self) -> str | None:
        """The preset of the outcome."""
        room = self.room
        return room.decision.preset.value if room and room.decision else None

    @property
    def current_temperature(self) -> float | None:
        """The room sensor, or the average of the thermostats' own readings."""
        room = self.room
        return room.temperature if room else None

    @property
    def current_humidity(self) -> float | None:
        """The humidity sensor, when there is one and it is available."""
        room = self.room
        return room.humidity if room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``reason``, ``override_until`` and ``boost_until``."""
        room = self.room
        if room is None:
            return {}
        hand = room.state.hand
        return {
            ATTR_REASON: room.decision.reason.value if room.decision else None,
            ATTR_OVERRIDE_UNTIL: hand.until.isoformat() if isinstance(hand, Override) else None,
            ATTR_BOOST_UNTIL: hand.until.isoformat() if isinstance(hand, Boost) else None,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """A temperature set by hand: an override (and the mode, if one is given)."""
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(HVACMode(mode))
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self.runtime.set_temperature(self._room(), float(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch the room on or off."""
        mode = RoomMode.OFF if hvac_mode == HVACMode.OFF else RoomMode.HEAT
        self.runtime.set_room_mode(self._room(), mode)

    async def async_turn_on(self) -> None:
        """Switch the room on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Switch the room off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """A preset chosen by hand."""
        self.runtime.set_preset(self._room(), Preset(preset_mode))

    async def async_boost(
        self, duration: timedelta | None = None, temperature: float | None = None
    ) -> None:
        """``cave_heating.boost`` for this room."""
        self.runtime.boost(self._room(), duration, temperature)

    async def async_resume_schedule(self) -> None:
        """``cave_heating.resume_schedule`` for this room."""
        self.runtime.resume_schedule(self._room())
