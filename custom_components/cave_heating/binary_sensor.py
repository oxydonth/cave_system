"""Window pause and problem per room, presence for the home (concept section 5.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_MESSAGES, ATTR_THERMOSTATS
from .entity import HomeEntity, RoomEntity

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime, RoomRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the occupancy sensor, and window and problem sensors per room."""
    runtime = entry.runtime_data

    def add(rooms: list[RoomRuntime]) -> None:
        async_add_entities(
            entity
            for room in rooms
            for entity in (WindowOpenSensor(runtime, room), ProblemSensor(runtime, room))
        )

    async_add_entities([OccupiedSensor(runtime)])
    add(list(runtime.rooms.values()))
    runtime.register_room_adder(add)


class WindowOpenSensor(RoomEntity, BinarySensorEntity):
    """``binary_sensor.cave_<room>_window_open``: on while the window pause is in effect.

    It has no device class: it is the pause, which starts after the open delay and ends after the
    close delay, not the window, so "Open" and "Closed" would contradict the contact's own row.
    The translations name its states "Paused" and "Not paused".
    """

    _attr_translation_key = "window_pause"

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, room, "binary_sensor", "window_open")

    @property
    def is_on(self) -> bool | None:
        """Whether the window pause is in effect."""
        room = self.room
        return room.windows.paused if room else None


class ProblemSensor(RoomEntity, BinarySensorEntity):
    """``binary_sensor.cave_<room>_problem``: on when something needs attention.

    A held thermostat is not a problem: ``cave device-check`` holds one on purpose.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "problem"

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, room, "binary_sensor", "problem")

    @property
    def is_on(self) -> bool | None:
        """Whether anything in the room needs attention."""
        room = self.room
        return bool(self.runtime.problems(room)) if room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """``thermostats``: the status of each; ``messages``: what is wrong, in words."""
        room = self.room
        if room is None:
            return {}
        return {
            ATTR_THERMOSTATS: self.runtime.thermostat_statuses(room),
            ATTR_MESSAGES: self.runtime.problems(room),
        }


class OccupiedSensor(HomeEntity, BinarySensorEntity):
    """``binary_sensor.cave_home_occupied``: presence after the away delay."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "home_occupied"

    def __init__(self, runtime: HeatingRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, "binary_sensor", "home_occupied")

    @property
    def is_on(self) -> bool:
        """Whether someone is home, after the away delay."""
        return self.runtime.occupied
