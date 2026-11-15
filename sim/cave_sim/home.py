"""The description of a simulated home, as read from a ``home.yaml``.

::

    outdoor_temperature: 5.0          # optional, °C
    zigbee2mqtt:                      # optional
      passive_timeout: 1500           # minutes until a silent battery device is unavailable
    fritzbox:                         # optional
      report_delay: 0                 # seconds before a new setpoint is reported; 0 = at once
      window_target: unchanged        # what tsoll shows during window detection: unchanged or off
      locked_command: refuse          # sethkrtsoll while locked: refuse (HTTP 400) or ignore
    rooms:
      - id: living_room               # used by the control API
        name: Wohnzimmer
        temperature: 19.0             # optional start values
        humidity: 50
        heating_per_hour: 2.0         # optional room model rates, see cave_sim.room
        loss_per_hour: 0.1
        window_loss_per_hour: 2.0
        devices:
          - model: TRV-ZBT            # a Zigbee model, or "FRITZ!Smart Thermo 302"
            name: Wohnzimmer Heizung Fenster   # Zigbee2MQTT friendly name or FRITZ! device name
            ieee: "0x..."             # optional, Zigbee only; derived from the name otherwise
            ain: "13979 0000001"      # optional, FRITZ! only; derived from the name otherwise

A device's ID in the control API is its name in lower case with runs of whitespace replaced by an
underscore, which for a Zigbee device is also the start of its entity IDs.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from cave_sim.room import RoomParameters
from cave_sim.zigbee.models import MODELS

FRITZ_THERMO_302: Final = "FRITZ!Smart Thermo 302"

type Family = Literal["zigbee", "fritz"]
type WindowTarget = Literal["unchanged", "off"]
type LockedCommand = Literal["refuse", "ignore"]
WINDOW_TARGETS: Final = ("unchanged", "off")
LOCKED_COMMANDS: Final = ("refuse", "ignore")


class HomeError(ValueError):
    """The home description is invalid."""


def device_id_from_name(name: str) -> str:
    """The control-API ID of a device: Zigbee2MQTT's object ID rule applied to its name."""
    return re.sub(r"\s+", "_", name).lower()


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


@dataclass(frozen=True)
class DeviceSpec:
    """One device of the home."""

    model: str
    name: str
    ieee: str = ""
    ain: str = ""

    @property
    def family(self) -> Family:
        """``zigbee`` for the Zigbee2MQTT stand-in, ``fritz`` for the fake FRITZ!Box."""
        return "fritz" if self.model == FRITZ_THERMO_302 else "zigbee"

    @property
    def device_id(self) -> str:
        """The ID used by the control API."""
        return device_id_from_name(self.name)

    @property
    def ieee_address(self) -> str:
        """The IEEE address; derived from the name unless given."""
        return self.ieee or f"0x{_digest(self.name)[:16]}"

    @property
    def ain_or_default(self) -> str:
        """The FRITZ! actor identification number; derived from the name unless given."""
        if self.ain:
            return self.ain
        number = int(_digest(self.name)[:8], 16) % 10_000_000
        return f"13979 {number:07d}"


@dataclass(frozen=True)
class RoomSpec:
    """One room and its devices."""

    room_id: str
    name: str
    temperature: float = 19.0
    humidity: float = 50.0
    parameters: RoomParameters = field(default_factory=RoomParameters)
    devices: tuple[DeviceSpec, ...] = ()


@dataclass(frozen=True)
class FritzBoxSpec:
    """Behaviour of the fake FRITZ!Box."""

    report_delay: float = 0.0
    window_target: WindowTarget = "unchanged"
    locked_command: LockedCommand = "refuse"


@dataclass(frozen=True)
class Zigbee2MqttSpec:
    """Zigbee2MQTT settings of the stand-in."""

    passive_timeout_minutes: float = 1500.0


@dataclass(frozen=True)
class HomeSpec:
    """A whole simulated home."""

    rooms: tuple[RoomSpec, ...]
    outdoor_temperature: float = 5.0
    fritzbox: FritzBoxSpec = field(default_factory=FritzBoxSpec)
    zigbee2mqtt: Zigbee2MqttSpec = field(default_factory=Zigbee2MqttSpec)

    def devices(self) -> list[tuple[RoomSpec, DeviceSpec]]:
        """Every device with its room."""
        return [(room, device) for room in self.rooms for device in room.devices]


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HomeError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HomeError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HomeError(f"{where}: expected a mapping")
    return value


def _parse_device(raw: Any, where: str) -> DeviceSpec:
    data = _mapping(raw, where)
    unknown = set(data) - {"model", "name", "ieee", "ain"}
    if unknown:
        raise HomeError(f"{where}: unknown keys {sorted(unknown)}")
    model = _string(data.get("model"), f"{where}.model")
    if model != FRITZ_THERMO_302 and model not in MODELS:
        known = ", ".join([*MODELS, FRITZ_THERMO_302])
        raise HomeError(f"{where}.model: {model!r} is not simulated; known models: {known}")
    spec = DeviceSpec(
        model=model,
        name=_string(data.get("name"), f"{where}.name"),
        ieee=str(data.get("ieee", "")),
        ain=str(data.get("ain", "")),
    )
    if spec.ieee and not re.fullmatch(r"0x[0-9a-f]{16}", spec.ieee):
        raise HomeError(f"{where}.ieee: expected 0x and 16 lower-case hex digits")
    return spec


def _parse_room(raw: Any, where: str) -> RoomSpec:
    data = _mapping(raw, where)
    allowed = {
        "id",
        "name",
        "temperature",
        "humidity",
        "heating_per_hour",
        "loss_per_hour",
        "window_loss_per_hour",
        "devices",
    }
    unknown = set(data) - allowed
    if unknown:
        raise HomeError(f"{where}: unknown keys {sorted(unknown)}")
    room_id = _string(data.get("id"), f"{where}.id")
    if not re.fullmatch(r"[a-z0-9_]+", room_id):
        raise HomeError(f"{where}.id: use lower-case letters, digits and underscores")
    defaults = RoomParameters()
    parameters = RoomParameters(
        heating_per_hour=_number(
            data.get("heating_per_hour", defaults.heating_per_hour), f"{where}.heating_per_hour"
        ),
        loss_per_hour=_number(
            data.get("loss_per_hour", defaults.loss_per_hour), f"{where}.loss_per_hour"
        ),
        window_loss_per_hour=_number(
            data.get("window_loss_per_hour", defaults.window_loss_per_hour),
            f"{where}.window_loss_per_hour",
        ),
    )
    devices_raw = data.get("devices", [])
    if not isinstance(devices_raw, list):
        raise HomeError(f"{where}.devices: expected a list")
    return RoomSpec(
        room_id=room_id,
        name=_string(data.get("name", room_id), f"{where}.name"),
        temperature=_number(data.get("temperature", 19.0), f"{where}.temperature"),
        humidity=_number(data.get("humidity", 50.0), f"{where}.humidity"),
        parameters=parameters,
        devices=tuple(
            _parse_device(device, f"{where}.devices[{index}]")
            for index, device in enumerate(devices_raw)
        ),
    )


def parse_home(raw: Any) -> HomeSpec:
    """Validate and convert a parsed ``home.yaml``."""
    data = _mapping(raw, "home")
    unknown = set(data) - {"outdoor_temperature", "fritzbox", "zigbee2mqtt", "rooms"}
    if unknown:
        raise HomeError(f"home: unknown keys {sorted(unknown)}")
    rooms_raw = data.get("rooms", [])
    if not isinstance(rooms_raw, list):
        raise HomeError("home.rooms: expected a list")
    rooms = tuple(_parse_room(room, f"rooms[{index}]") for index, room in enumerate(rooms_raw))
    fritz_raw = _mapping(data.get("fritzbox", {}), "fritzbox")
    unknown = set(fritz_raw) - {"report_delay", "window_target", "locked_command"}
    if unknown:
        raise HomeError(f"fritzbox: unknown keys {sorted(unknown)}")
    window_target = fritz_raw.get("window_target", "unchanged")
    if window_target not in WINDOW_TARGETS:
        raise HomeError(f"fritzbox.window_target: one of {WINDOW_TARGETS}")
    locked_command = fritz_raw.get("locked_command", "refuse")
    if locked_command not in LOCKED_COMMANDS:
        raise HomeError(f"fritzbox.locked_command: one of {LOCKED_COMMANDS}")
    z2m_raw = _mapping(data.get("zigbee2mqtt", {}), "zigbee2mqtt")
    unknown = set(z2m_raw) - {"passive_timeout"}
    if unknown:
        raise HomeError(f"zigbee2mqtt: unknown keys {sorted(unknown)}")
    passive_timeout = _number(z2m_raw.get("passive_timeout", 1500), "zigbee2mqtt.passive_timeout")
    if passive_timeout <= 0:
        raise HomeError("zigbee2mqtt.passive_timeout: must be positive")
    home = HomeSpec(
        rooms=rooms,
        outdoor_temperature=_number(data.get("outdoor_temperature", 5.0), "outdoor_temperature"),
        fritzbox=FritzBoxSpec(
            report_delay=_number(fritz_raw.get("report_delay", 0.0), "fritzbox.report_delay"),
            window_target=window_target,
            locked_command=locked_command,
        ),
        zigbee2mqtt=Zigbee2MqttSpec(passive_timeout_minutes=passive_timeout),
    )
    _check_unique(home)
    return home


def _check_unique(home: HomeSpec) -> None:
    def duplicates(values: list[str]) -> list[str]:
        return sorted({value for value in values if values.count(value) > 1})

    devices = [device for _, device in home.devices()]
    checks = {
        "room IDs": [room.room_id for room in home.rooms],
        "device IDs (names)": [device.device_id for device in devices],
        "IEEE addresses": [d.ieee_address for d in devices if d.family == "zigbee"],
        "AINs": [d.ain_or_default for d in devices if d.family == "fritz"],
    }
    for what, values in checks.items():
        if repeated := duplicates(values):
            raise HomeError(f"{what} must be unique: {', '.join(repeated)}")


def load_home(path: Path) -> HomeSpec:
    """Read and validate a ``home.yaml``."""
    return parse_home(yaml.safe_load(path.read_text(encoding="utf-8")))
