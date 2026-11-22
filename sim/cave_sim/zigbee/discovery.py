"""Home Assistant discovery messages exactly as Zigbee2MQTT 2.14.3 builds them.

A transcription of ``lib/extension/homeassistant.ts`` at Zigbee2MQTT tag 2.14.3, limited to what our
models need: ``exposeToConfig`` for the expose types ``climate``, ``binary``, ``numeric``,
``enum``, ``text`` and ``composite`` of a device without endpoints; ``getConfigs`` with the
``last_seen`` sensor and the ``update`` entity; ``discover`` with topics, entity IDs
(``object_id``/``default_entity_id``), unique IDs, device and availability; ``getDevicePayload``;
and the bridge's own entities from ``getBridgeEntity``. Groups, scenes, lights, covers, fans,
locks, events and device triggers are not transcribed. The lookup tables hold only the entries the
models use, copied verbatim. Zigbee2MQTT serialises with sorted keys (``lib/util/stringify.ts``),
and so does :func:`serialize`.
"""

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from cave_sim.zigbee.exposes import (
    SET,
    STATE,
    AnyExpose,
    Binary,
    Climate,
    Composite,
    Enum,
    Expose,
    Numeric,
    Text,
)
from cave_sim.zigbee.models import ZigbeeModel

ZIGBEE2MQTT_VERSION: Final = "2.14.3"
ORIGIN: Final = {
    "name": "Zigbee2MQTT",
    "sw": ZIGBEE2MQTT_VERSION,
    "url": "https://www.zigbee2mqtt.io",
}


class _Undefined:
    """JavaScript's ``undefined``: a key holding it is left out of the serialised payload."""

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED: Final = _Undefined()

# homeassistant.ts lookup tables: the entries our models touch, copied verbatim.
CONFIG_SWITCH_DISCOVERY_LOOKUP: Final[dict[str, dict[str, Any]]] = {
    "window_detection": {"entity_category": "config", "icon": "mdi:window-open-variant"},
}
BINARY_DISCOVERY_LOOKUP: Final[dict[str, dict[str, Any]]] = {
    "battery_low": {"entity_category": "diagnostic", "device_class": "battery"},
    "child_lock": {"entity_category": "config", "icon": "mdi:account-lock"},
    "contact": {"device_class": "door"},
    "tamper": {"device_class": "tamper"},
    "window_detection": {"entity_category": "config", "icon": "mdi:window-open-variant"},
}
NUMERIC_DISCOVERY_LOOKUP: Final[dict[str, dict[str, Any]]] = {
    "battery": {"device_class": "battery", "state_class": "measurement"},
    "external_temperature_input": {"device_class": "temperature", "icon": "mdi:thermometer"},
    "humidity": {"device_class": "humidity", "state_class": "measurement"},
    "humidity_calibration": {"entity_category": "config", "icon": "mdi:wrench-clock"},
    "linkquality": {
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "icon": "mdi:signal",
        "state_class": "measurement",
    },
    "local_temperature": {"device_class": "temperature", "state_class": "measurement"},
    "temperature": {"device_class": "temperature", "state_class": "measurement"},
    "temperature_calibration": {"entity_category": "config", "icon": "mdi:wrench-clock"},
}
ENUM_DISCOVERY_LOOKUP: Final[dict[str, dict[str, Any]]] = {
    "temperature_sensor_select": {"entity_category": "config", "icon": "mdi:home-thermometer"},
}
LIST_DISCOVERY_LOOKUP: Final[dict[str, dict[str, Any]]] = {}

_ACCESS_STATE: Final = STATE
_ACCESS_SET: Final = SET


@dataclass
class DiscoveryEntry:
    """``DiscoveryEntry`` in homeassistant.ts."""

    type: str
    object_id: str
    discovery_payload: dict[str, Any]


@dataclass(frozen=True)
class DeviceInfo:
    """What ``getDevicePayload`` reads from a Zigbee2MQTT device."""

    friendly_name: str
    ieee_address: str
    model: ZigbeeModel
    software_build_id: str | None = None
    hardware_version: int | None = None


def project_device_options() -> dict[str, Any]:
    """The ``device_options`` this project gives Zigbee2MQTT (a fresh copy).

    ``compose.yaml`` sets them through ``ZIGBEE2MQTT_CONFIG_DEVICE_OPTIONS``; a test keeps the two
    equal. They enable the ``last_seen`` sensor of every device in Home Assistant, which changes
    with every message the device sends, so that ``cave_heating`` can tell a live room sensor in
    a steady room from a dead one (concept section 5.4).
    """
    return {"homeassistant": {"last_seen": {"enabled_by_default": True}}}


@dataclass(frozen=True)
class Settings:
    """The Zigbee2MQTT settings the stand-in follows: this project's, by default."""

    base_topic: str = "zigbee2mqtt"
    discovery_topic: str = "homeassistant"
    last_seen: str = "ISO_8601"
    availability_enabled: bool = True
    frontend_url: str = ""
    coordinator_ieee: str = "0x00124b0000000001"
    # availability.passive.timeout, in seconds: 1500 minutes, the default of Zigbee2MQTT 2.14.3
    # (lib/util/settings.schema.json) and the value in zigbee2mqtt/configuration.template.yaml.
    passive_timeout: float = 1500 * 60.0
    # ``device_options``: options applied to every device, below its own (``devices.<ieee>``).
    # Zigbee2MQTT's default is empty; see project_device_options.
    device_options: Mapping[str, Any] = field(default_factory=project_device_options, hash=False)


def js_number(value: float) -> float:
    """Return ``value`` as JavaScript would print it: integral numbers without a fraction."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def js_string(value: Any) -> str:
    """JavaScript's ``value.toString()`` for the scalars that occur in exposes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float | int):
        return str(js_number(value))
    return str(value)


def state_value_template(prop: str, body: str | None = None) -> str:
    """``stateValueTemplate``."""
    if body is None:
        body = f'{{{{ value_json["{prop}"] }}}}'
    return f'{{% if "{prop}" in value_json %}}{body}{{% endif %}}'


def clean_name(name: str) -> str:
    """``cleanName``."""
    return re.sub(r"^(?:analog_in_|analog_out_)", "", name)


def _expose_to_config(
    expose: AnyExpose, all_exposes: tuple[AnyExpose, ...]
) -> list[DiscoveryEntry]:
    """``exposeToConfig`` for one expose of a device without endpoints."""
    entries: list[DiscoveryEntry] = []
    if isinstance(expose, Climate):
        entries = _climate_to_config(expose, all_exposes)
    elif isinstance(expose, Binary):
        entries = [_binary_to_config(expose)]
    elif isinstance(expose, Numeric):
        entries = [_numeric_to_config(expose)]
    elif isinstance(expose, Enum):
        entries = _enum_to_config(expose)
    elif isinstance(expose, Text | Composite):
        entries = _text_to_config(expose)

    category = expose.category
    if category in ("config", "diagnostic"):
        for entry in entries:
            entry.discovery_payload["entity_category"] = category

    for entry in entries:
        payload = entry.discovery_payload
        if entry.type in ("binary_sensor", "sensor") and payload.get("entity_category") == "config":
            payload["entity_category"] = "diagnostic"
        # None of our exposes carries `homeassistant.name`, so a device class always drops the name.
        if payload.get("device_class"):
            payload.pop("name", None)
    return entries


def _climate_to_config(expose: Climate, all_exposes: tuple[AnyExpose, ...]) -> list[DiscoveryEntry]:
    numerics = [f for f in expose.features if isinstance(f, Numeric)]
    enums = [f for f in expose.features if isinstance(f, Enum)]
    heating = next(
        (
            f
            for f in numerics
            if f.name in ("occupied_heating_setpoint", "current_heating_setpoint")
        ),
        None,
    )
    if heating is None or heating.value_min is None or heating.value_max is None:
        raise ValueError("No setpoint found or it is missing value_min/max")
    temperature = expose.feature("local_temperature")
    if temperature is None:
        raise ValueError("No temperature found")

    climate: dict[str, Any] = {
        "name": None,
        "state_topic": False,
        "temperature_unit": "C",
        "temp_step": js_number(heating.value_step) if heating.value_step is not None else UNDEFINED,
        "min_temp": js_string(heating.value_min),
        "max_temp": js_string(heating.value_max),
        "current_temperature_topic": True,
        "current_temperature_template": state_value_template(temperature.property),
        "command_topic_prefix": UNDEFINED,
    }
    entries: list[DiscoveryEntry] = []

    mode = next((f for f in enums if f.name == "system_mode"), None)
    if mode is not None:
        values = [v for v in mode.values if v != "sleep"]
        climate["mode_state_topic"] = True
        climate["mode_state_template"] = state_value_template(mode.property)
        climate["modes"] = values
        climate["mode_command_topic"] = True

    state = expose.feature("running_state")
    if state is not None:
        climate["action_topic"] = True
        climate["action_template"] = state_value_template(
            state.property,
            "{% set values = {None:None,'idle':'idle','heat':'heating','cool':'cooling',"
            f"'fan_only':'fan'}} %}}{{{{ values[value_json[\"{state.property}\"]] }}}}",
        )

    climate["temperature_command_topic"] = heating.name
    climate["temperature_state_template"] = state_value_template(heating.property)
    climate["temperature_state_topic"] = True

    calibration = next((f for f in numerics if f.name == "local_temperature_calibration"), None)
    if calibration is not None:
        payload: dict[str, Any] = {
            "name": calibration.label,
            "value_template": state_value_template(calibration.property),
            "command_topic": True,
            "command_topic_prefix": UNDEFINED,
            "command_topic_postfix": calibration.property,
            "device_class": "temperature_delta",
            "entity_category": "config",
        }
        if calibration.unit:
            payload["unit_of_measurement"] = calibration.unit
        if calibration.value_min is not None:
            payload["min"] = js_number(calibration.value_min)
        if calibration.value_max is not None:
            payload["max"] = js_number(calibration.value_max)
        if calibration.value_step is not None:
            payload["step"] = js_number(calibration.value_step)
        entries.append(DiscoveryEntry("number", calibration.name, payload))

    demand = next((f for f in numerics if f.name == "pi_heating_demand"), None)
    if demand is not None:
        payload = {
            "name": demand.label,
            "value_template": state_value_template(demand.property),
            "icon": "mdi:radiator",
        }
        if demand.unit:
            payload["unit_of_measurement"] = demand.unit
        if demand.access & _ACCESS_SET:
            payload["command_topic"] = True
            payload["command_topic_prefix"] = UNDEFINED
            payload["command_topic_postfix"] = demand.property
            payload["min"] = (
                js_number(demand.value_min) if demand.value_min is not None else UNDEFINED
            )
            payload["max"] = (
                js_number(demand.value_max) if demand.value_max is not None else UNDEFINED
            )
            entries.append(DiscoveryEntry("number", demand.name, payload))
        else:
            payload["entity_category"] = "diagnostic"
            entries.append(DiscoveryEntry("sensor", demand.name, payload))

    top_level_numeric = [e for e in all_exposes if isinstance(e, Numeric)]
    has_temperature_sensor = any(
        e.name == "temperature" and e.access & _ACCESS_STATE for e in top_level_numeric
    )
    has_local_temperature_sensor = any(
        e.name == "local_temperature" and e.access & _ACCESS_STATE for e in top_level_numeric
    )
    if (
        isinstance(temperature, Numeric)
        and not has_temperature_sensor
        and not has_local_temperature_sensor
    ):
        payload = {
            "name": temperature.label,
            "value_template": state_value_template(temperature.property),
            "device_class": "temperature",
            "state_class": "measurement",
        }
        if temperature.unit:
            payload["unit_of_measurement"] = temperature.unit
        entries.append(DiscoveryEntry("sensor", temperature.name, payload))

    humidity = next(
        (e for e in top_level_numeric if e.name == "humidity" and e.access & _ACCESS_STATE),
        None,
    )
    if humidity is not None:
        climate["current_humidity_template"] = state_value_template(humidity.property)
        climate["current_humidity_topic"] = True

    entries.append(DiscoveryEntry("climate", "climate", climate))
    return entries


def _binary_to_config(expose: Binary) -> DiscoveryEntry:
    lookup = BINARY_DISCOVERY_LOOKUP.get(expose.name, {})
    if expose.access & _ACCESS_SET:
        body = (
            f'{{% if value_json["{expose.property}"] %}}true{{% else %}}false{{% endif %}}'
            if isinstance(expose.value_on, bool)
            else None
        )
        payload: dict[str, Any] = {
            "name": expose.label,
            "value_template": state_value_template(expose.property, body),
            "payload_on": js_string(expose.value_on),
            "payload_off": js_string(expose.value_off),
            "command_topic": True,
            "command_topic_prefix": UNDEFINED,
            "command_topic_postfix": expose.property,
            **lookup,
        }
        return DiscoveryEntry("switch", f"switch_{expose.name}", payload)
    payload = {
        "name": expose.label,
        "value_template": state_value_template(expose.property),
        "payload_on": expose.value_on,
        "payload_off": expose.value_off,
        **lookup,
    }
    return DiscoveryEntry("binary_sensor", expose.name, payload)


def _numeric_to_config(expose: Numeric) -> DiscoveryEntry:
    if expose.access & _ACCESS_SET:
        payload: dict[str, Any] = {
            "name": expose.label,
            "value_template": state_value_template(expose.property),
            "command_topic": True,
            "command_topic_prefix": UNDEFINED,
            "command_topic_postfix": expose.property,
        }
        if expose.unit:
            payload["unit_of_measurement"] = expose.unit
        if expose.value_step:
            payload["step"] = js_number(expose.value_step)
        payload.update(NUMERIC_DISCOVERY_LOOKUP.get(clean_name(expose.name), {}))
        if NUMERIC_DISCOVERY_LOOKUP.get(expose.name, {}).get("device_class") == "temperature":
            payload["device_class"] = "temperature"
        else:
            payload.pop("device_class", None)
        if expose.value_min is not None:
            payload["min"] = js_number(expose.value_min)
        if expose.value_max is not None:
            payload["max"] = js_number(expose.value_max)
        return DiscoveryEntry("number", expose.name, payload)

    extra: dict[str, Any] = {}
    if expose.unit in ("Wh", "kWh"):
        extra = {"device_class": "energy", "state_class": "total_increasing"}
    elif expose.unit in ("A", "mA"):
        extra = {"device_class": "current", "state_class": "measurement"}
    elif expose.unit in ("mW", "W", "kW"):
        extra = {"device_class": "power", "state_class": "measurement"}
    payload = {
        "name": expose.label,
        "value_template": state_value_template(expose.property),
        "enabled_by_default": True,
    }
    if expose.unit:
        payload["unit_of_measurement"] = expose.unit
    payload.update(NUMERIC_DISCOVERY_LOOKUP.get(clean_name(expose.name), {}))
    payload.update(extra)
    if payload.get("device_class") and not payload.get("unit_of_measurement"):
        payload.pop("device_class")
    if payload.get("entity_category") == "config":
        payload["entity_category"] = "diagnostic"
    return DiscoveryEntry("sensor", expose.name, payload)


def _enum_to_config(expose: Enum) -> list[DiscoveryEntry]:
    lookup = ENUM_DISCOVERY_LOOKUP.get(expose.name, {})
    value_template: Any = (
        state_value_template(expose.property) if expose.access & _ACCESS_STATE else UNDEFINED
    )
    if expose.access & _ACCESS_SET and len(expose.values) == 1:
        button: dict[str, Any] = {
            "name": expose.label,
            "state_topic": False,
            "command_topic_prefix": UNDEFINED,
            "command_topic": True,
            "command_topic_postfix": expose.property,
            "payload_press": js_string(expose.values[0]),
            **lookup,
        }
        return [DiscoveryEntry("button", expose.property, button)]
    if expose.access & _ACCESS_SET:
        select: dict[str, Any] = {
            "name": expose.label,
            "value_template": value_template,
            "state_topic": bool(expose.access & _ACCESS_STATE),
            "command_topic_prefix": UNDEFINED,
            "command_topic": True,
            "command_topic_postfix": expose.property,
            "options": [js_string(v) for v in expose.values],
            **lookup,
        }
        return [DiscoveryEntry("select", expose.property, select)]
    if expose.access & _ACCESS_STATE:
        sensor: dict[str, Any] = {"name": expose.label, "value_template": value_template, **lookup}
        return [DiscoveryEntry("sensor", expose.property, sensor)]
    return []


def _text_to_config(expose: Expose) -> list[DiscoveryEntry]:
    lookup = LIST_DISCOVERY_LOOKUP.get(expose.name, {})
    if isinstance(expose, Text) and expose.access & _ACCESS_SET:
        text: dict[str, Any] = {
            "name": expose.label,
            "state_topic": expose.access & _ACCESS_STATE,
            "value_template": state_value_template(expose.property),
            "command_topic_prefix": UNDEFINED,
            "command_topic": True,
            "command_topic_postfix": expose.property,
            **lookup,
        }
        return [DiscoveryEntry("text", expose.property, text)]
    if expose.access & _ACCESS_STATE:
        body = (
            f"{{{{ value_json[\"{expose.property}\"] | default('',True) | string "
            "| truncate(254, True, '', 0) }}"
        )
        sensor: dict[str, Any] = {
            "name": expose.label,
            "value_template": state_value_template(expose.property, body),
            **lookup,
        }
        return [DiscoveryEntry("sensor", expose.property, sensor)]
    return []


def config_entries(model: ZigbeeModel, settings: Settings) -> list[DiscoveryEntry]:
    """``getConfigs`` for a device of ``model``."""
    configs: list[DiscoveryEntry] = []
    for expose in model.exposes:
        configs.extend(_expose_to_config(expose, model.exposes))
    if settings.last_seen != "disable":
        last_seen: dict[str, Any] = {
            "name": "Last seen",
            "value_template": state_value_template("last_seen"),
            "icon": "mdi:clock",
            "enabled_by_default": False,
            "entity_category": "diagnostic",
        }
        if settings.last_seen.startswith("ISO_8601"):
            last_seen["device_class"] = "timestamp"
        configs.append(DiscoveryEntry("sensor", "last_seen", last_seen))
    if model.ota:
        update_template = state_value_template(
            "update",
            "{\"latest_version\":\"{{ value_json['update']['latest_version'] }}\","
            "\"installed_version\":\"{{ value_json['update']['installed_version'] }}\","
            "\"update_percentage\":{{ value_json['update'].get('progress', 'null') }},"
            "\"in_progress\":{{ (value_json['update'].get('state') == 'updating')|lower }}}",
        )
        configs.append(
            DiscoveryEntry(
                "update",
                "update",
                {
                    "name": None,
                    "entity_picture": "https://github.com/Koenkk/zigbee2mqtt/raw/master/images/logo.png",
                    "state_topic": True,
                    "device_class": "firmware",
                    "entity_category": "config",
                    "command_topic": (
                        f"{settings.base_topic}/bridge/request/device/ota_update/update"
                    ),
                    "payload_install": "{" + '"id": "' + "{ieee}" + '"}',
                    "value_template": update_template,
                },
            )
        )
    return configs


def device_payload(device: DeviceInfo, settings: Settings) -> dict[str, Any]:
    """``getDevicePayload`` for a device."""
    payload: dict[str, Any] = {
        "identifiers": [f"zigbee2mqtt_{device.ieee_address}"],
        "name": device.friendly_name,
        "sw_version": f"Zigbee2MQTT {ZIGBEE2MQTT_VERSION}",
        "model": device.model.description,
        "model_id": device.model.model,
        "manufacturer": device.model.vendor,
    }
    if device.software_build_id is not None:
        payload["sw_version"] = device.software_build_id
    if device.hardware_version is not None:
        payload["hw_version"] = str(device.hardware_version)
    if settings.frontend_url:
        payload["configuration_url"] = (
            f"{settings.frontend_url}/#/device/{device.ieee_address}/info"
        )
    payload["via_device"] = f"zigbee2mqtt_bridge_{settings.coordinator_ieee}"
    return payload


def object_id_for(friendly_name: str, entry: DiscoveryEntry) -> str:
    """The ``object_id`` Zigbee2MQTT suggests, which Home Assistant turns into the entity ID."""
    object_id = re.sub(r"\s+", "_", friendly_name).lower()
    if entry.object_id.startswith(entry.type) and "_" in entry.object_id:
        object_id += "_" + entry.object_id.split("_", 1)[1]
    elif not entry.object_id.startswith(entry.type):
        object_id += "_" + entry.object_id
    return object_id


def discover_device(device: DeviceInfo, settings: Settings) -> dict[str, dict[str, Any]]:
    """``discover`` for a device: discovery topic (below the discovery prefix) to payload."""
    messages: dict[str, dict[str, Any]] = {}
    base = f"{settings.base_topic}/{device.friendly_name}"
    for entry in config_entries(device.model, settings):
        payload = dict(entry.discovery_payload)
        state_topic = payload.get("state_topic", UNDEFINED)
        if state_topic is UNDEFINED or state_topic:
            payload["state_topic"] = base
        else:
            payload.pop("state_topic", None)

        object_id = object_id_for(device.friendly_name, entry)
        payload["object_id"] = object_id
        payload["default_entity_id"] = f"{entry.type}.{object_id}"
        payload["unique_id"] = f"{device.ieee_address}_{entry.object_id}_{settings.base_topic}"
        payload["device"] = device_payload(device, settings)
        payload["origin"] = dict(ORIGIN)

        availability = [{"topic": f"{settings.base_topic}/bridge/state"}]
        if settings.availability_enabled:
            payload["availability_mode"] = "all"
            availability.append({"topic": f"{base}/availability"})
        for item in availability:
            item["value_template"] = "{{ value_json.state }}"
        payload["availability"] = availability

        prefix = payload.pop("command_topic_prefix", UNDEFINED)
        postfix = payload.pop("command_topic_postfix", UNDEFINED)
        prefix_part = f"{prefix}/" if isinstance(prefix, str) and prefix else ""
        postfix_part = f"/{postfix}" if isinstance(postfix, str) and postfix else ""
        command_topic = f"{base}/{prefix_part}set{postfix_part}"
        if payload.get("command_topic") and not isinstance(payload["command_topic"], str):
            payload["command_topic"] = command_topic
        for key in (
            "mode_state_topic",
            "current_temperature_topic",
            "temperature_state_topic",
            "action_topic",
            "current_humidity_topic",
        ):
            if payload.get(key):
                payload[key] = base
        if payload.get("mode_command_topic"):
            payload["mode_command_topic"] = f"{base}/{prefix_part}set/system_mode"
        if payload.get("temperature_command_topic"):
            payload["temperature_command_topic"] = (
                f"{base}/{prefix_part}set/{payload['temperature_command_topic']}"
            )
        if "payload_install" in payload:
            payload["payload_install"] = payload["payload_install"].replace(
                "{ieee}", device.ieee_address
            )

        if device.model.discovery_override is not None:
            device.model.discovery_override(payload)

        topic = f"{entry.type}/{device.ieee_address}/{entry.object_id}/config"
        messages[topic] = _drop_undefined(payload)
    return messages


def entity_options(settings: Settings, own: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``Device.options`` in ``lib/model/device.ts``: ``{...device_options, ...deviceOptions}``.

    A shallow merge: a device whose own options have a ``homeassistant`` key does not see the
    ``homeassistant`` key of ``device_options`` at all.
    """
    return {**copy.deepcopy(dict(settings.device_options)), **copy.deepcopy(dict(own or {}))}


def apply_options(
    messages: Mapping[str, Mapping[str, Any]], options: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The user's ``homeassistant`` options on a device's discovery messages.

    Zigbee2MQTT applies them in homeassistant.ts: ``discover`` publishes nothing for a device
    whose ``homeassistant`` option is set and falsy; ``getConfigs`` drops each entity whose own
    entry is ``null``; and ``discover``, after the converter's ``overrideHaDiscoveryPayload``,
    copies the scalar and list values of ``homeassistant`` into every payload (``name`` excepted)
    and then those of ``homeassistant.<object id>`` into that entity's payload (``add``): a
    ``null`` deletes the key, and a ``device`` object is merged into the device payload. Renaming
    an entity through ``type`` or ``object_id`` in its entry (also ``getConfigs``) is not
    transcribed, and refused. ``messages`` are keyed ``<component>/<ieee>/<object id>/config``.
    """
    if "homeassistant" not in options:
        return {topic: copy.deepcopy(dict(payload)) for topic, payload in messages.items()}
    homeassistant = options["homeassistant"]
    if not homeassistant:
        return {}
    if not isinstance(homeassistant, Mapping):
        raise ValueError(f"homeassistant option must be an object, got {homeassistant!r}")
    result: dict[str, dict[str, Any]] = {}
    for topic, original in messages.items():
        object_id = topic.split("/")[2]
        own = homeassistant.get(object_id, UNDEFINED)
        if own is None:
            continue
        if isinstance(own, Mapping) and ("type" in own or "object_id" in own):
            raise ValueError(
                f"homeassistant.{object_id}: renaming an entity is not transcribed by the stand-in"
            )
        payload = copy.deepcopy(dict(original))
        _add(payload, homeassistant, ignore_name=True)
        if isinstance(own, Mapping):
            _add(payload, own, ignore_name=False)
        result[topic] = payload
    return result


def _add(payload: dict[str, Any], values: Mapping[str, Any], *, ignore_name: bool) -> None:
    """``add`` inside ``discover``."""
    for key, value in values.items():
        if key in ("type", "object_id") or (ignore_name and key == "name"):
            continue
        if isinstance(value, bool | int | float | str | list):
            payload[key] = copy.deepcopy(value)
        elif value is None:
            payload.pop(key, None)
        elif key == "device" and isinstance(value, Mapping):
            payload["device"] = {**payload.get("device", {}), **copy.deepcopy(dict(value))}


def bridge_messages(settings: Settings) -> dict[str, dict[str, Any]]:
    """The bridge's own entities (``getBridgeEntity``) and their discovery payloads."""
    base = f"{settings.base_topic}/bridge"
    entries = [
        DiscoveryEntry(
            "binary_sensor",
            "connection_state",
            {
                "name": "Connection state",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "state_topic": True,
                "state_topic_postfix": "state",
                "value_template": "{{ value_json.state }}",
                "payload_on": "online",
                "payload_off": "offline",
                "availability": False,
            },
        ),
        DiscoveryEntry(
            "binary_sensor",
            "restart_required",
            {
                "name": "Restart required",
                "device_class": "problem",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "state_topic": True,
                "state_topic_postfix": "info",
                "value_template": "{{ value_json.restart_required }}",
                "payload_on": True,
                "payload_off": False,
            },
        ),
        DiscoveryEntry(
            "button",
            "restart",
            {
                "name": "Restart",
                "device_class": "restart",
                "state_topic": False,
                "command_topic": f"{base}/request/restart",
                "payload_press": "",
            },
        ),
        DiscoveryEntry(
            "sensor",
            "version",
            {
                "name": "Version",
                "icon": "mdi:zigbee",
                "entity_category": "diagnostic",
                "state_topic": True,
                "state_topic_postfix": "info",
                "value_template": "{{ value_json.version }}",
            },
        ),
        DiscoveryEntry(
            "switch",
            "permit_join",
            {
                "name": "Permit join",
                "icon": "mdi:human-greeting-proximity",
                "state_topic": True,
                "state_topic_postfix": "info",
                "value_template": "{{ value_json.permit_join | lower }}",
                "command_topic": f"{base}/request/permit_join",
                "state_on": "true",
                "state_off": "false",
                "payload_on": '{"time": 254}',
                "payload_off": '{"time": 0}',
            },
        ),
    ]
    device = {
        "identifiers": [f"zigbee2mqtt_bridge_{settings.coordinator_ieee}"],
        "name": "Zigbee2MQTT Bridge",
        "sw_version": ZIGBEE2MQTT_VERSION,
        "model": "Bridge",
        "manufacturer": "Zigbee2MQTT",
        "hw_version": "EmberZNet 8.0.2.0",
    }
    messages: dict[str, dict[str, Any]] = {}
    for entry in entries:
        payload = dict(entry.discovery_payload)
        postfix = payload.pop("state_topic_postfix", None)
        if payload.get("state_topic"):
            payload["state_topic"] = f"{base}/{postfix}" if postfix else base
        else:
            payload.pop("state_topic", None)
        object_id = f"zigbee2mqtt_bridge_{entry.object_id}"
        payload["object_id"] = object_id
        payload["default_entity_id"] = f"{entry.type}.{object_id}"
        payload["unique_id"] = (
            f"bridge_{settings.coordinator_ieee}_{entry.object_id}_{settings.base_topic}"
        )
        payload["device"] = device
        payload["origin"] = dict(ORIGIN)
        if payload.get("availability", True):
            payload["availability"] = [
                {"topic": f"{base}/state", "value_template": "{{ value_json.state }}"}
            ]
            payload["availability_mode"] = "all"
        else:
            payload.pop("availability")
        key = f"{_encoded_base_topic(settings)}_{settings.coordinator_ieee}"
        messages[f"{entry.type}/{key}/{entry.object_id}/config"] = payload
    return messages


def _encoded_base_topic(settings: Settings) -> str:
    """``getEncodedBaseTopic``: the base topic's character codes, concatenated."""
    return "".join(str(ord(char)) for char in settings.base_topic)


def _drop_undefined(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_undefined(v) for k, v in value.items() if v is not UNDEFINED}
    if isinstance(value, list):
        return [None if v is UNDEFINED else _drop_undefined(v) for v in value]
    return value


def serialize(payload: Any) -> str:
    """``stringify`` in Zigbee2MQTT's ``lib/util/stringify.ts``: compact, keys sorted, UTF-8."""
    return json.dumps(
        _normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        return js_number(value)
    return value
