"""Zigbee2MQTT's description of a device's properties ("exposes"), as data.

A Python transcription of the expose classes in zigbee-herdsman-converters v26.125.0, the version
Zigbee2MQTT 2.14.3 pins in its ``package.json``: ``src/lib/exposes.ts`` (``Base``, ``Binary``,
``Numeric``, ``Enum``, ``Text``, ``Composite``, ``Climate``, the ``access`` bit mask and the presets
our models use) and ``getLabelFromName`` in ``src/lib/utils.ts``. Only what decides the Home
Assistant discovery messages and the command handling is kept.
"""

from dataclasses import dataclass
from typing import Literal

# exposes.ts `access`: bit 0 = in the published state, bit 1 = /set, bit 2 = /get.
STATE = 0b001
SET = 0b010
GET = 0b100
STATE_SET = STATE | SET
STATE_GET = STATE | GET
ALL = STATE | SET | GET

type Category = Literal["config", "diagnostic"] | None
type Scalar = str | int | float | bool


def label_from_name(name: str) -> str:
    """utils.ts ``getLabelFromName``: underscores to spaces, first letter upper case."""
    label = name.replace("_", " ")
    return label[:1].upper() + label[1:]


@dataclass(frozen=True, kw_only=True)
class Expose:
    """Fields common to every expose (``Base``)."""

    name: str
    access: int
    label: str = ""
    property: str = ""
    category: Category = None

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", label_from_name(self.name))
        if not self.property:
            object.__setattr__(self, "property", self.name)
        # Base.validateCategory
        if self.category == "config" and not self.access & SET:
            raise ValueError(f"config expose {self.name} must be settable")
        if self.category == "diagnostic" and self.access & SET:
            raise ValueError(f"diagnostic expose {self.name} must not be settable")


@dataclass(frozen=True, kw_only=True)
class Numeric(Expose):
    """``Numeric``."""

    unit: str | None = None
    value_min: float | None = None
    value_max: float | None = None
    value_step: float | None = None


@dataclass(frozen=True, kw_only=True)
class Binary(Expose):
    """``Binary``: ``value_on``/``value_off`` are what appears in the state and in commands."""

    value_on: Scalar
    value_off: Scalar


@dataclass(frozen=True, kw_only=True)
class Enum(Expose):
    """``Enum``."""

    values: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Text(Expose):
    """``Text``."""


@dataclass(frozen=True, kw_only=True)
class Composite(Expose):
    """``Composite``; our models carry only set-only composites, which get no entity."""


@dataclass(frozen=True, kw_only=True)
class Climate:
    """``Climate``: a group of features, discovered as one ``climate`` entity and helpers."""

    features: tuple[Expose, ...]
    name: str = "climate"
    access: int = STATE
    category: Category = None

    def feature(self, name: str) -> Expose | None:
        """Return the feature called ``name``."""
        return next((f for f in self.features if f.name == name), None)


type AnyExpose = Expose | Climate


# --- exposes.ts `Climate` builders -----------------------------------------------------------


def setpoint(prop: str, value_min: float, value_max: float, step: float) -> Numeric:
    """``Climate.withSetpoint`` (access ``ALL``)."""
    return Numeric(
        name=prop, access=ALL, value_min=value_min, value_max=value_max, value_step=step, unit="°C"
    )


def local_temperature() -> Numeric:
    """``Climate.withLocalTemperature`` (access ``STATE_GET``)."""
    return Numeric(name="local_temperature", access=STATE_GET, unit="°C")


def local_temperature_calibration(value_min: float, value_max: float, step: float) -> Numeric:
    """``Climate.withLocalTemperatureCalibration`` (access ``ALL``)."""
    return Numeric(
        name="local_temperature_calibration",
        access=ALL,
        value_min=value_min,
        value_max=value_max,
        value_step=step,
        unit="°C",
    )


def system_mode(modes: tuple[str, ...]) -> Enum:
    """``Climate.withSystemMode`` (access ``ALL``)."""
    return Enum(name="system_mode", access=ALL, values=modes)


def running_state(states: tuple[str, ...]) -> Enum:
    """``Climate.withRunningState`` (access ``STATE_GET``)."""
    return Enum(name="running_state", access=STATE_GET, values=states)


def pi_heating_demand(access: int) -> Numeric:
    """``Climate.withPiHeatingDemand``."""
    return Numeric(
        name="pi_heating_demand",
        access=access,
        label="PI heating demand",
        value_min=0,
        value_max=100,
        unit="%",
    )


# --- exposes.ts `presets`, modernExtend.ts helpers and Zigbee2MQTT's link quality ------------


def preset_battery() -> Numeric:
    """``presets.battery`` (exposes.ts): access ``STATE``."""
    return Numeric(
        name="battery", access=STATE, unit="%", value_min=0, value_max=100, category="diagnostic"
    )


def modern_battery() -> Numeric:
    """modernExtend.ts ``battery()`` percentage expose: access ``STATE_GET``."""
    return Numeric(
        name="battery",
        access=STATE_GET,
        unit="%",
        value_min=0,
        value_max=100,
        category="diagnostic",
    )


def modern_battery_voltage() -> Numeric:
    """modernExtend.ts ``battery({voltage: true})`` voltage expose."""
    return Numeric(name="voltage", access=STATE_GET, unit="mV", category="diagnostic")


def modern_battery_low() -> Binary:
    """modernExtend.ts ``battery({lowStatus: true})`` expose."""
    return Binary(
        name="battery_low", access=STATE, value_on=True, value_off=False, category="diagnostic"
    )


def linkquality() -> Numeric:
    """Zigbee2MQTT 2.14.3 ``lib/model/device.ts`` ``LINKQUALITY``, appended to every device."""
    return Numeric(
        name="linkquality",
        access=STATE,
        unit="lqi",
        value_min=0,
        value_max=255,
        category="diagnostic",
    )
