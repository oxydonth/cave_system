"""Schedule temperatures per room and the home's away temperature (concept section 5.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_TEMPERATURE, MIN_TEMPERATURE, TEMPERATURE_STEP
from .entity import HomeEntity, RoomEntity
from .logic import Preset

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime, RoomRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the away temperature, and comfort and eco per room."""
    runtime = entry.runtime_data

    def add(rooms: list[RoomRuntime]) -> None:
        async_add_entities(
            RoomTemperature(runtime, room, preset)
            for room in rooms
            for preset in (Preset.COMFORT, Preset.ECO)
        )

    async_add_entities([AwayTemperature(runtime)])
    add(list(runtime.rooms.values()))
    runtime.register_room_adder(add)


class _Temperature(NumberEntity):
    """5 to 30 °C in 0.5 K steps."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = MIN_TEMPERATURE
    _attr_native_max_value = MAX_TEMPERATURE
    _attr_native_step = TEMPERATURE_STEP
    _attr_mode = NumberMode.BOX


class RoomTemperature(RoomEntity, _Temperature):
    """``number.cave_<room>_comfort_temperature`` and ``..._eco_temperature``."""

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime, preset: Preset) -> None:
        """Set the entity ID from the room and the preset."""
        key = f"{preset.value}_temperature"
        super().__init__(runtime, room, "number", key)
        self._preset = preset
        self._attr_translation_key = key

    @property
    def native_value(self) -> float | None:
        """The temperature the schedule uses."""
        room = self.room
        if room is None:
            return None
        return room.state.comfort if self._preset is Preset.COMFORT else room.state.eco

    async def async_set_native_value(self, value: float) -> None:
        """Set in the UI: from now on this value wins over the file."""
        if (room := self.room) is not None:
            self.runtime.set_schedule_temperature(room, self._preset, value)


class AwayTemperature(HomeEntity, _Temperature):
    """``number.cave_away_temperature``."""

    _attr_translation_key = "away_temperature"

    def __init__(self, runtime: HeatingRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, "number", "away_temperature")

    @property
    def native_value(self) -> float:
        """The setback temperature for the whole home."""
        return self.runtime.away_temperature

    async def async_set_native_value(self, value: float) -> None:
        """Set in the UI: from now on this value wins over the file."""
        self.runtime.set_away_temperature(value)
