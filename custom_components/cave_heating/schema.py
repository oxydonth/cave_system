"""The format of heating.yaml: loading, version conversion and validation (concept section 5.1).

This module imports nothing from Home Assistant, so the validator (``validate.py``) can use it
in a container where Home Assistant is not running, and the integration uses exactly the same
rules. Every message names the room and the field it is about.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import yaml

from .const import MAX_TEMPERATURE, MIN_TEMPERATURE, TEMPERATURE_STEP

CURRENT_VERSION: Final = 1

# Home Assistant's own rule for entity IDs (homeassistant.core.valid_entity_id).
_ENTITY_ID: Final = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
# A room id becomes part of entity IDs, so it follows the same rule as their object part.
_ROOM_ID: Final = re.compile(r"^(?!.*__)[a-z0-9](?:[a-z0-9_]*[a-z0-9])?$")
_DURATION: Final = re.compile(r"^(\d+):([0-5]?\d)(?::([0-5]?\d))?$")

type SettingValue = str | float


@dataclass(frozen=True, slots=True)
class Timing:
    """The ``timing:`` block: every timing value of concept sections 5.4 and 5.5."""

    debounce: timedelta = timedelta(seconds=2)
    confirm_timeout: timedelta = timedelta(minutes=5)
    confirm_timeout_slow: timedelta = timedelta(minutes=20)
    resend_attempts: int = 3
    retry_interval: timedelta = timedelta(minutes=30)
    manual_settle: timedelta = timedelta(seconds=45)
    command_budget_per_hour: int = 20
    budget_exhausted_hours: int = 3
    feed_threshold: float = 0.2
    feed_min_interval: timedelta = timedelta(minutes=10)
    feed_max_interval: timedelta = timedelta(minutes=25)
    sensor_stale: timedelta = timedelta(hours=3)
    window_unavailable_grace: timedelta = timedelta(minutes=30)
    presence_unavailable_grace: timedelta = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class ThermostatConfig:
    """One radiator thermostat of a room."""

    entity: str
    external_temperature: str | None = None
    settings: Mapping[str, SettingValue] = field(default_factory=dict)
    # None: decided at run time (thermostats of the FRITZ!Box integration are slow).
    slow: bool | None = None


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """One room, with the defaults already applied."""

    id: str
    name: str
    trvs: tuple[ThermostatConfig, ...]
    temperature_sensor: str | None = None
    humidity_sensor: str | None = None
    windows: tuple[str, ...] = ()
    schedule: str | None = None
    comfort_temperature: float = 21.0
    eco_temperature: float = 17.5
    boost_temperature: float = 25.0
    boost_duration: timedelta = timedelta(minutes=30)
    override_max: timedelta = timedelta(hours=6)
    window_open_delay: timedelta = timedelta(minutes=1)
    window_close_delay: timedelta = timedelta(minutes=2)

    def entities(self) -> Iterator[tuple[str, str]]:
        """Yield every entity the room names, with the field that names it."""
        for index, trv in enumerate(self.trvs):
            yield trv.entity, f"trvs[{index}].entity"
            if trv.external_temperature:
                yield trv.external_temperature, f"trvs[{index}].external_temperature"
            for setting in trv.settings:
                yield setting, f"trvs[{index}].settings"
        if self.temperature_sensor:
            yield self.temperature_sensor, "temperature_sensor"
        if self.humidity_sensor:
            yield self.humidity_sensor, "humidity_sensor"
        for index, window in enumerate(self.windows):
            yield window, f"windows[{index}]"
        if self.schedule:
            yield self.schedule, "schedule"


@dataclass(frozen=True, slots=True)
class HeatingConfig:
    """The whole home description."""

    version: int = CURRENT_VERSION
    presence: tuple[str, ...] = ()
    away_delay: timedelta = timedelta(minutes=30)
    away_temperature: float = 16.0
    rooms: tuple[RoomConfig, ...] = ()
    timing: Timing = field(default_factory=Timing)


def _sentence(text: str) -> str:
    """One complete sentence on one line: capitalised, single-spaced, ending with a full stop."""
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


class ConfigError(Exception):
    """The file cannot be used. ``errors`` lists every problem found, one sentence each."""

    def __init__(self, errors: list[str]) -> None:
        """Keep the list of problems."""
        self.errors = [_sentence(error) for error in errors]
        super().__init__(" ".join(self.errors))


# --- Reading the file -------------------------------------------------------------------------

_INT_TAG: Final = "tag:yaml.org,2002:int"
_FLOAT_TAG: Final = "tag:yaml.org,2002:float"


class _StrictLoader(yaml.SafeLoader):
    """YAML 1.1 as PyYAML reads it, with two traps removed.

    Duplicate keys are an error instead of the last one silently winning. And values such as
    ``12:00`` stay strings: YAML 1.1 reads them as base-60 numbers (720), which would turn a
    duration written without quotes into something else than what it says.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Build a mapping, refusing a key that appears twice."""
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, str | int | float | bool) and key in seen:
                raise yaml.constructor.ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    f"found the key {key!r} twice",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT_TAG, _FLOAT_TAG)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    _INT_TAG,
    re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$"),
    list("-+0123456789"),
)
_StrictLoader.add_implicit_resolver(
    _FLOAT_TAG,
    re.compile(
        r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
        r"|\.[0-9_]+(?:[eE][-+][0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)


def read_file(path: Path) -> object:
    """Parse the file as YAML. Raises ConfigError for a missing, unreadable or broken file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise ConfigError([f"the file {path} does not exist"]) from err
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError([f"the file {path} cannot be read: {err}"]) from err
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506  (a SafeLoader subclass)
    except RecursionError as err:
        raise ConfigError(["the file is nested too deeply to be read"]) from err
    except ValueError as err:
        # For example an integer with more digits than Python converts.
        raise ConfigError([f"the file contains a value that cannot be read: {err}"]) from err
    except yaml.MarkedYAMLError as err:
        where = ""
        if err.problem_mark is not None:
            where = f" (line {err.problem_mark.line + 1}, column {err.problem_mark.column + 1})"
        problem = " ".join(part for part in (err.context, err.problem) if part)
        raise ConfigError([f"the file is not valid YAML: {problem}{where}"]) from err
    except yaml.YAMLError as err:
        raise ConfigError([f"the file is not valid YAML: {err}"]) from err


def load_file(path: Path) -> HeatingConfig:
    """Read, convert and validate a heating.yaml. Raises ConfigError listing every problem."""
    data = read_file(path)
    try:
        return parse(data)
    except (OverflowError, RecursionError, ValueError) as err:
        # The validators guard against these; this keeps a file that slips past them a
        # validation error rather than a crash.
        raise ConfigError([f"the file contains a value that cannot be used: {err}"]) from err


# --- Versions ---------------------------------------------------------------------------------

# Converters from version N to N + 1, applied in turn until CURRENT_VERSION is reached. A release
# that changes the format adds one here and a stored example under
# tests/cave_heating/fixtures/versions/; older versions stay accepted forever (concept section
# 5.1). Version 1 is the first format, so there is nothing to convert yet.
MIGRATIONS: Final[dict[int, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a file of any supported version to the current one."""
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError([f'field "version": must be a whole number, but is {version!r}'])
    if version < 1 or version > CURRENT_VERSION:
        raise ConfigError(
            [
                f'field "version": version {version} is not supported; this release reads '
                f"versions 1 to {CURRENT_VERSION}"
            ]
        )
    while version < CURRENT_VERSION:
        data = MIGRATIONS[version](data)
        version += 1
        data["version"] = version
    return data


# --- Validation -------------------------------------------------------------------------------


class _Collector:
    """Collects problems, each prefixed with the room and field it belongs to."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self._room: str | None = None

    @contextmanager
    def room(self, label: str) -> Iterator[None]:
        previous, self._room = self._room, label
        try:
            yield
        finally:
            self._room = previous

    def add(self, path: str, message: str) -> None:
        parts = [self._room] if self._room is not None else []
        if path:
            parts.append(f'field "{path}"')
        self.errors.append(f"{', '.join(parts)}: {message}")


def _describe(value: object) -> str:
    return repr(value) if not isinstance(value, str) else f'"{value}"'


def _finite(value: object) -> bool:
    """A number that is neither infinite, nor NaN, nor too large for a float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _duration(err: _Collector, path: str, value: object, *, positive: bool) -> timedelta | None:
    """A duration: "HH:MM:SS" or "HH:MM" as a string, or a number of seconds."""
    result: timedelta | None = None
    try:
        if _finite(value) and value >= 0:  # type: ignore[operator]
            result = timedelta(seconds=value)  # type: ignore[arg-type]
        elif isinstance(value, str) and (match := _DURATION.match(value.strip())):
            hours, minutes, seconds = match.groups()
            result = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds or 0))
    except OverflowError:
        err.add(path, f"is too long to be a duration: {_describe(value)}")
        return None
    if result is None:
        err.add(
            path,
            f'must be a duration such as "00:05:00" (hours:minutes:seconds) or a number of '
            f"seconds, but is {_describe(value)}",
        )
        return None
    if positive and result <= timedelta(0):
        err.add(path, "must be longer than zero")
        return None
    return result


def _temperature(err: _Collector, path: str, value: object) -> float | None:
    if not _finite(value):
        err.add(path, f"must be a temperature in °C, but is {_describe(value)}")
        return None
    assert isinstance(value, int | float)  # noqa: S101  (checked by _finite)
    if not MIN_TEMPERATURE <= value <= MAX_TEMPERATURE:
        err.add(
            path,
            f"must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g} °C, but is {value:g}",
        )
        return None
    if not math.isclose(value / TEMPERATURE_STEP, round(value / TEMPERATURE_STEP)):
        err.add(path, f"must be a multiple of {TEMPERATURE_STEP:g} °C, but is {value:g}")
        return None
    return float(value)


def _entity(
    err: _Collector, path: str, value: object, domains: tuple[str, ...] | None
) -> str | None:
    if not isinstance(value, str) or not _ENTITY_ID.match(value):
        err.add(
            path, f"must be an entity ID such as climate.living_room, but is {_describe(value)}"
        )
        return None
    domain = value.split(".", 1)[0]
    if domains is not None and domain not in domains:
        allowed = " or ".join(domains)
        err.add(path, f'must be an entity of the domain {allowed}, but is "{value}"')
        return None
    return value


def _entity_list(
    err: _Collector, path: str, value: object, domains: tuple[str, ...] | None
) -> tuple[str, ...]:
    """One entity, or a list of entities."""
    items: list[object]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        err.add(path, f"must be an entity ID or a list of them, but is {_describe(value)}")
        return ()
    result: list[str] = []
    for index, item in enumerate(items):
        entity = _entity(err, f"{path}[{index}]", item, domains)
        if entity is not None:
            if entity in result:
                err.add(f"{path}[{index}]", f'"{entity}" is listed twice')
            else:
                result.append(entity)
    return tuple(result)


def _mapping(err: _Collector, path: str, value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        err.add(path, f"must be a mapping of keys to values, but is {_describe(value)}")
        return None
    return value


def _unknown_keys(err: _Collector, prefix: str, data: Mapping[str, Any], known: set[str]) -> None:
    for key in data:
        if key not in known:
            path = f"{prefix}{key}" if prefix else str(key)
            hint = ", ".join(sorted(known))
            err.add(path, f"is not a known field; the known fields are: {hint}")


_TIMING_DEFAULTS: Final = Timing()
_TIMING_COUNTS: Final = {
    "resend_attempts": 0,
    "command_budget_per_hour": 1,
    "budget_exhausted_hours": 1,
}


def _timing(err: _Collector, value: object) -> Timing:
    data = _mapping(err, "timing", value)
    if data is None:
        return _TIMING_DEFAULTS
    known = {f.name for f in fields(Timing)}
    _unknown_keys(err, "timing.", data, known)
    values: dict[str, Any] = {}
    for name in known & data.keys():
        raw = data[name]
        path = f"timing.{name}"
        if name in _TIMING_COUNTS:
            minimum = _TIMING_COUNTS[name]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
                err.add(
                    path, f"must be a whole number of at least {minimum}, but is {_describe(raw)}"
                )
            else:
                values[name] = raw
        elif name == "feed_threshold":
            if not _finite(raw) or raw <= 0:
                err.add(
                    path, f"must be a temperature difference above 0 K, but is {_describe(raw)}"
                )
            else:
                values[name] = float(raw)
        else:
            positive = name not in {
                "debounce",
                "manual_settle",
                "window_unavailable_grace",
                "presence_unavailable_grace",
            }
            duration = _duration(err, path, raw, positive=positive)
            if duration is not None:
                values[name] = duration
    timing = Timing(**values)
    if timing.feed_min_interval > timing.feed_max_interval:
        err.add("timing.feed_min_interval", "must not be longer than timing.feed_max_interval")
    return timing


_ROOM_DEFAULT_KEYS: Final = {
    "comfort_temperature",
    "eco_temperature",
    "boost_temperature",
    "boost_duration",
    "override_max",
    "window_open_delay",
    "window_close_delay",
}
_ROOM_KEYS: Final = _ROOM_DEFAULT_KEYS | {
    "id",
    "name",
    "trvs",
    "temperature_sensor",
    "humidity_sensor",
    "windows",
    "schedule",
}
_TRV_KEYS: Final = {"entity", "external_temperature", "settings", "slow"}
_TOP_KEYS: Final = {
    "version",
    "presence",
    "away_delay",
    "away_temperature",
    "defaults",
    "rooms",
    "timing",
}
_SETTING_DOMAINS: Final = ("select", "switch", "number")


def _room_defaults(err: _Collector, path: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the per-room values that the ``defaults`` block may also set."""
    values: dict[str, Any] = {}
    for key in _ROOM_DEFAULT_KEYS & data.keys():
        raw = data[key]
        full = f"{path}{key}"
        if key.endswith("_temperature"):
            temperature = _temperature(err, full, raw)
            if temperature is not None:
                values[key] = temperature
        else:
            positive = key in {"boost_duration", "override_max"}
            duration = _duration(err, full, raw, positive=positive)
            if duration is not None:
                values[key] = duration
    return values


def _setting(err: _Collector, path: str, entity: str, value: object) -> SettingValue | None:
    domain = entity.split(".", 1)[0]
    if domain == "number":
        if not _finite(value):
            err.add(path, f"must be a number for a number entity, but is {_describe(value)}")
            return None
        return float(value)  # type: ignore[arg-type]
    if isinstance(value, bool):
        # YAML 1.1 reads an unquoted on/off as true/false.
        return "on" if value else "off"
    if domain == "switch":
        if value not in ("on", "off"):
            err.add(path, f'must be "on" or "off" for a switch, but is {_describe(value)}')
            return None
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    if not isinstance(value, str) or not value:
        err.add(path, f"must be one of the options the entity offers, but is {_describe(value)}")
        return None
    return value


def _thermostat(err: _Collector, path: str, value: object) -> ThermostatConfig | None:
    data = _mapping(err, path, value)
    if data is None:
        return None
    _unknown_keys(err, f"{path}.", data, _TRV_KEYS)
    if "entity" not in data:
        err.add(f"{path}.entity", "is required")
        return None
    entity = _entity(err, f"{path}.entity", data["entity"], ("climate",))
    external: str | None = None
    if data.get("external_temperature") is not None:
        external = _entity(
            err,
            f"{path}.external_temperature",
            data["external_temperature"],
            ("number", "input_number"),
        )
    settings: dict[str, SettingValue] = {}
    if data.get("settings") is not None:
        raw_settings = _mapping(err, f"{path}.settings", data["settings"])
        for key, raw in (raw_settings or {}).items():
            setting_path = f"{path}.settings.{key}"
            setting_entity = _entity(err, setting_path, key, _SETTING_DOMAINS)
            if setting_entity is None:
                continue
            setting = _setting(err, setting_path, setting_entity, raw)
            if setting is not None:
                settings[setting_entity] = setting
    slow: bool | None = None
    if "slow" in data:
        if not isinstance(data["slow"], bool):
            err.add(f"{path}.slow", f"must be true or false, but is {_describe(data['slow'])}")
        else:
            slow = data["slow"]
    if entity is None:
        return None
    return ThermostatConfig(
        entity=entity, external_temperature=external, settings=settings, slow=slow
    )


def _room(
    err: _Collector, index: int, value: object, defaults: Mapping[str, Any]
) -> RoomConfig | None:
    path = f"rooms[{index}]"
    raw_id = value.get("id") if isinstance(value, dict) else None
    label = f'room "{raw_id}" ({path})' if isinstance(raw_id, str) else f"room {index + 1} ({path})"
    with err.room(label):
        data = _mapping(err, "", value)
        if data is None:
            return None
        _unknown_keys(err, "", data, _ROOM_KEYS)
        room_id: str | None = None
        if "id" not in data:
            err.add("id", "is required")
        elif not isinstance(data["id"], str) or not _ROOM_ID.match(data["id"]):
            err.add(
                "id",
                "must be lower-case ASCII letters, digits and single underscores, starting and "
                f"ending with a letter or digit, but is {_describe(data['id'])}",
            )
        else:
            room_id = data["id"]
        name: str | None = None
        if "name" not in data:
            err.add("name", "is required")
        elif not isinstance(data["name"], str) or not data["name"].strip():
            err.add("name", f"must be a non-empty text, but is {_describe(data['name'])}")
        else:
            name = data["name"].strip()
        trvs: list[ThermostatConfig] = []
        raw_trvs = data.get("trvs")
        if not isinstance(raw_trvs, list) or not raw_trvs:
            err.add("trvs", "must list at least one thermostat")
        else:
            for trv_index, raw in enumerate(raw_trvs):
                trv = _thermostat(err, f"trvs[{trv_index}]", raw)
                if trv is not None:
                    trvs.append(trv)
        optional: dict[str, Any] = {}
        for key, domains in (
            ("temperature_sensor", ("sensor",)),
            ("humidity_sensor", ("sensor",)),
            ("schedule", ("schedule", "input_boolean", "binary_sensor")),
        ):
            if data.get(key) is not None:
                optional[key] = _entity(err, key, data[key], domains)
        if data.get("windows") is not None:
            optional["windows"] = _entity_list(
                err, "windows", data["windows"], ("binary_sensor", "input_boolean")
            )
        values = {**defaults, **_room_defaults(err, "", data)}
        if room_id is None or name is None or not trvs or len(trvs) != len(raw_trvs or []):
            return None
        return RoomConfig(id=room_id, name=name, trvs=tuple(trvs), **optional, **values)


def _check_unique(err: _Collector, rooms: list[tuple[int, RoomConfig]]) -> None:
    room_ids: dict[str, int] = {}
    thermostats: dict[str, str] = {}
    inputs: dict[str, str] = {}
    # The room thermostats this file creates; none of them can be a radiator thermostat.
    room_climates = {f"climate.cave_{room.id}": room.id for _, room in rooms}
    for index, room in rooms:
        if room.id in room_ids:
            with err.room(f'room "{room.id}" (rooms[{index}])'):
                err.add("id", f"is already used by rooms[{room_ids[room.id]}]")
        room_ids.setdefault(room.id, index)
        for trv_index, trv in enumerate(room.trvs):
            with err.room(f'room "{room.id}" (rooms[{index}])'):
                if trv.entity in room_climates:
                    err.add(
                        f"trvs[{trv_index}].entity",
                        f'"{trv.entity}" is the room thermostat of room '
                        f'"{room_climates[trv.entity]}" that cave_heating creates, not a radiator '
                        "thermostat",
                    )
                if trv.entity in thermostats:
                    err.add(
                        f"trvs[{trv_index}].entity",
                        f'"{trv.entity}" is already a thermostat of {thermostats[trv.entity]}',
                    )
                thermostats.setdefault(trv.entity, f'room "{room.id}"')
                if trv.external_temperature:
                    if trv.external_temperature in inputs:
                        err.add(
                            f"trvs[{trv_index}].external_temperature",
                            f'"{trv.external_temperature}" is already the input of '
                            f"{inputs[trv.external_temperature]}",
                        )
                    inputs.setdefault(trv.external_temperature, trv.entity)


def parse(data: object) -> HeatingConfig:
    """Validate parsed YAML and build the configuration. Raises ConfigError on any problem."""
    if data is None:
        raise ConfigError(['the file is empty; it needs at least "version: 1"'])
    if not isinstance(data, dict):
        raise ConfigError([f"the file must be a mapping of keys to values, but is {data!r}"])
    data = migrate(dict(data))
    err = _Collector()
    _unknown_keys(err, "", data, _TOP_KEYS)

    presence: tuple[str, ...] = ()
    if data.get("presence") is not None:
        presence = _entity_list(err, "presence", data["presence"], None)
    top: dict[str, Any] = {}
    if "away_delay" in data:
        top["away_delay"] = _duration(err, "away_delay", data["away_delay"], positive=False)
    if "away_temperature" in data:
        top["away_temperature"] = _temperature(err, "away_temperature", data["away_temperature"])

    defaults: dict[str, Any] = {}
    if data.get("defaults") is not None:
        raw_defaults = _mapping(err, "defaults", data["defaults"])
        if raw_defaults is not None:
            _unknown_keys(err, "defaults.", raw_defaults, set(_ROOM_DEFAULT_KEYS))
            defaults = _room_defaults(err, "defaults.", raw_defaults)

    timing = _timing(err, data["timing"]) if data.get("timing") is not None else Timing()

    rooms: list[tuple[int, RoomConfig]] = []
    raw_rooms = data.get("rooms")
    if raw_rooms is not None and not isinstance(raw_rooms, list):
        err.add("rooms", f"must be a list of rooms, but is {_describe(raw_rooms)}")
    for index, raw in enumerate(raw_rooms if isinstance(raw_rooms, list) else []):
        room = _room(err, index, raw, defaults)
        if room is not None:
            rooms.append((index, room))
    _check_unique(err, rooms)

    if err.errors:
        raise ConfigError(err.errors)
    return HeatingConfig(
        version=data["version"],
        presence=presence,
        rooms=tuple(room for _, room in rooms),
        timing=timing,
        **{key: value for key, value in top.items() if value is not None},
    )
