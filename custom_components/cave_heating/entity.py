"""Base classes for the entities of concept section 5.2.

Every entity ID is set explicitly from the room id (``<domain>.cave_<room>_<key>``) and every
unique ID is ``<room id>_<key>``; the home's entities use ``home`` in place of a room id. The
entities hold no state of their own: they show what the controller in ``runtime.py`` decided and
pass hand actions to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, HOME_DEVICE_ID, ROOM_DEVICE_PREFIX

if TYPE_CHECKING:
    from .runtime import HeatingRuntime, RoomRuntime

MANUFACTURER = "cave_system"


def room_device_info(room: RoomRuntime) -> DeviceInfo:
    """One device per room, placed in the Home Assistant area named like the room."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{ROOM_DEVICE_PREFIX}{room.config.id}")},
        name=room.config.name,
        suggested_area=room.config.name,
        manufacturer=MANUFACTURER,
        model="Room",
    )


HOME_DEVICE_INFO = DeviceInfo(
    identifiers={(DOMAIN, HOME_DEVICE_ID)},
    translation_key="home",
    manufacturer=MANUFACTURER,
    model="Home",
)


class CaveEntity(Entity):
    """Shows the controller's state; written after every evaluation."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: HeatingRuntime) -> None:
        """Keep the controller."""
        self.runtime = runtime

    async def async_added_to_hass(self) -> None:
        """Follow the controller."""
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.async_add_listener(self.async_write_ha_state))


class HomeEntity(CaveEntity):
    """An entity of the home device."""

    _attr_device_info = HOME_DEVICE_INFO

    def __init__(self, runtime: HeatingRuntime, domain: str, key: str) -> None:
        """Set ``<domain>.cave_<key>`` and the unique ID ``home_<key>``."""
        super().__init__(runtime)
        self.entity_id = f"{domain}.cave_{key}"
        self._attr_unique_id = f"{HOME_DEVICE_ID}_{key.removeprefix('home_')}"


class RoomEntity(CaveEntity):
    """An entity of a room's device."""

    def __init__(self, runtime: HeatingRuntime, room: RoomRuntime, domain: str, key: str) -> None:
        """Set ``<domain>.cave_<room>[_<key>]`` and the unique ID ``<room>_<key>``."""
        super().__init__(runtime)
        self.room_id = room.config.id
        suffix = "" if domain == "climate" else f"_{key}"
        self.entity_id = f"{domain}.cave_{self.room_id}{suffix}"
        self._attr_unique_id = f"{self.room_id}_{key}"
        self._attr_device_info = room_device_info(room)

    @property
    def room(self) -> RoomRuntime | None:
        """The room, or None once it has been removed from the file."""
        return self.runtime.rooms.get(self.room_id)

    @property
    def available(self) -> bool:
        """Room entities exist as long as the room does."""
        return self.room is not None
