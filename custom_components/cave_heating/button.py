"""``button.cave_<room>_resume_schedule`` (concept section 5.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity

if TYPE_CHECKING:
    from .runtime import CaveHeatingConfigEntry, HeatingRuntime, RoomRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CaveHeatingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a resume button per room."""
    runtime = entry.runtime_data

    def add(rooms: list[RoomRuntime]) -> None:
        async_add_entities(ResumeScheduleButton(runtime, room) for room in rooms)

    add(list(runtime.rooms.values()))
    runtime.register_room_adder(add)


class ResumeScheduleButton(RoomEntity, ButtonEntity):
    """Ends a boost or override."""

    _attr_translation_key = "resume_schedule"

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime) -> None:
        """Set the entity ID."""
        super().__init__(runtime, room, "button", "resume_schedule")

    async def async_press(self) -> None:
        """Resume the schedule."""
        if (room := self.room) is not None:
            self.runtime.resume_schedule(room)
