"""``sensor.cave_<room>_reason`` (concept section 5.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity
from .logic import Reason

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime, RoomRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a reason sensor per room."""
    runtime = entry.runtime_data

    def add(rooms: list[RoomRuntime]) -> None:
        async_add_entities(ReasonSensor(runtime, room) for room in rooms)

    add(list(runtime.rooms.values()))
    runtime.register_room_adder(add)


class ReasonSensor(RoomEntity, SensorEntity):
    """Why the room is at its current target: one of the reasons of section 5.3."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [reason.value for reason in Reason]
    _attr_translation_key = "reason"

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, room, "sensor", "reason")

    @property
    def native_value(self) -> str | None:
        """The reason."""
        room = self.room
        return room.decision.reason.value if room and room.decision else None
