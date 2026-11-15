"""The simulated Zigbee models, described as data.

Every expose list below is transcribed from zigbee-herdsman-converters v26.125.0, the version
Zigbee2MQTT 2.14.3 pins, in the order Zigbee2MQTT 2.14.3's ``Device.exposes()`` returns them: the
definition's ``exposes`` first, then each ``extend`` in order (``src/index.ts``
``processExtensions``), then the link quality Zigbee2MQTT appends (``lib/model/device.ts``). The
source of each model is named next to it. Adding a model is mostly adding a description here.

``reporting`` holds the attribute reporting each definition's ``configure`` sets up
(``src/lib/reporting.ts`` and the modern extends' ``reporting`` arguments): a property is reported
when it changed by at least ``change`` and ``min_interval`` has passed, and in any case after
``max_interval``. The simulated devices follow it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cave_sim.zigbee.exposes import (
    ALL,
    SET,
    STATE,
    STATE_GET,
    AnyExpose,
    Binary,
    Climate,
    Composite,
    Enum,
    Numeric,
    Text,
    linkquality,
    local_temperature,
    local_temperature_calibration,
    modern_battery,
    modern_battery_low,
    modern_battery_voltage,
    pi_heating_demand,
    preset_battery,
    running_state,
    setpoint,
    system_mode,
)

type Kind = Literal["sonoff_trv", "bosch_trv", "temperature_humidity", "contact"]
type DiscoveryOverride = Callable[[dict[str, Any]], None]

HOUR = 3600.0


@dataclass(frozen=True)
class Reporting:
    """Attribute reporting configuration of one property, in the property's own unit."""

    min_interval: float
    max_interval: float
    change: float


@dataclass(frozen=True)
class ZigbeeModel:
    """One Zigbee model as Zigbee2MQTT knows it, plus what the simulator needs to behave like it."""

    model: str
    vendor: str
    description: str
    kind: Kind
    exposes: tuple[AnyExpose, ...]
    source: str
    ota: bool = True
    reporting: Mapping[str, Reporting] = field(default_factory=dict)
    initial_state: Mapping[str, Any] = field(default_factory=dict)
    discovery_override: DiscoveryOverride | None = None

    def find(self, prop: str) -> AnyExpose | None:
        """Return the expose (or climate feature) whose property is ``prop``."""
        for expose in self.exposes:
            if isinstance(expose, Climate):
                feature = next((f for f in expose.features if f.property == prop), None)
                if feature is not None:
                    return feature
            elif expose.property == prop:
                return expose
        return None

    def settable(self, prop: str) -> bool:
        """Whether Zigbee2MQTT accepts ``prop`` in a ``/set`` command for this model."""
        expose = self.find(prop)
        return expose is not None and not isinstance(expose, Climate) and bool(expose.access & SET)


def _numeric(name: str, access: int = ALL, **kwargs: Any) -> Numeric:
    return Numeric(name=name, access=access, **kwargs)


def _binary(name: str, value_on: Any, value_off: Any, access: int = ALL, **kwargs: Any) -> Binary:
    return Binary(name=name, access=access, value_on=value_on, value_off=value_off, **kwargs)


def _enum(name: str, values: tuple[str, ...], access: int = ALL, **kwargs: Any) -> Enum:
    return Enum(name=name, access=access, values=values, **kwargs)


# Sonoff: src/devices/sonoff.ts in zigbee-herdsman-converters v26.125.0

SONOFF_TEMPERATURE_SENSOR_SELECT = (
    "local_temperature",
    "remote_temperature",
    "remote_source_offline",
)
_SUNDAY_FIRST = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


def _sonoff_external_temperature_input() -> Numeric:
    return _numeric(
        "external_temperature_input",
        label="External temperature",
        category="config",
        value_min=0,
        value_max=99.9,
        value_step=0.1,
        unit="°C",
    )


def _sonoff_steps_and_voltages() -> tuple[Numeric, ...]:
    return (
        _numeric("idle_steps", STATE_GET, category="diagnostic"),
        _numeric("closing_steps", STATE_GET, category="diagnostic"),
        _numeric("valve_opening_limit_voltage", STATE_GET, category="diagnostic", unit="mV"),
        _numeric("valve_closing_limit_voltage", STATE_GET, category="diagnostic", unit="mV"),
        _numeric("valve_motor_running_voltage", STATE_GET, category="diagnostic", unit="mV"),
    )


TRVZB = ZigbeeModel(
    model="TRVZB",
    vendor="SONOFF",
    description="Zigbee thermostatic radiator valve",
    kind="sonoff_trv",
    source="zigbee-herdsman-converters v26.125.0, src/devices/sonoff.ts, definition 'TRVZB'",
    exposes=(
        Climate(
            features=(
                setpoint("occupied_heating_setpoint", 4, 35, 0.5),
                local_temperature(),
                local_temperature_calibration(-12.7, 12.7, 0.2),
                system_mode(("off", "auto", "heat")),
                running_state(("idle", "heat")),
            )
        ),
        preset_battery(),
        _numeric(
            "timer_mode_target_temp",
            category="config",
            value_min=4,
            value_max=35,
            value_step=0.5,
            unit="°C",
        ),
        _numeric(
            "temporary_mode_duration",
            category="config",
            value_min=0,
            value_max=1440,
            value_step=1,
            unit="minutes",
        ),
        _enum(
            "temporary_mode_select", ("boost", "timer"), label="Temporary mode ", category="config"
        ),
        _binary("child_lock", "LOCK", "UNLOCK", category="config"),
        _binary("open_window", "ON", "OFF", category="config"),
        _numeric(
            "frost_protection_temperature",
            category="config",
            value_min=4,
            value_max=35,
            value_step=0.5,
            unit="°C",
        ),
        _enum(
            "temperature_sensor_select",
            SONOFF_TEMPERATURE_SENSOR_SELECT,
            label="Temperature sensor",
        ),
        _sonoff_external_temperature_input(),
        *_sonoff_steps_and_voltages(),
        _numeric(
            "valve_opening_degree",
            category="config",
            value_min=0,
            value_max=100,
            value_step=1,
            unit="%",
        ),
        _numeric(
            "valve_closing_degree",
            category="config",
            value_min=0,
            value_max=100,
            value_step=1,
            unit="%",
        ),
        _numeric(
            "temperature_accuracy",
            category="config",
            value_min=-1,
            value_max=-0.2,
            value_step=0.2,
            unit="°C",
        ),
        # The weekly schedule of sonoffExtend.weeklySchedule, access STATE_SET
        *(
            Text(name=f"weekly_schedule_{day}", access=STATE | SET, category="config")
            for day in _SUNDAY_FIRST
        ),
        _binary("smart_temperature_control", True, False),
        linkquality(),
    ),
    reporting={
        # The definition's configure sets up local temperature reporting with 0 s, 1 h and
        # 0.1 °C (reporting.thermostatTemperature), and setpoint and system mode reporting.
        "local_temperature": Reporting(0, HOUR, 0.1),
    },
    initial_state={
        "battery": 100,
        "child_lock": "UNLOCK",
        "closing_steps": 1210,
        "external_temperature_input": 0,
        "frost_protection_temperature": 7,
        "idle_steps": 1270,
        "local_temperature_calibration": 0,
        "open_window": "ON",
        "smart_temperature_control": False,
        "system_mode": "heat",
        "temperature_accuracy": -1,
        "temperature_sensor_select": "local_temperature",
        "valve_closing_degree": 100,
        "valve_closing_limit_voltage": 2200,
        "valve_motor_running_voltage": 2000,
        "valve_opening_degree": 100,
        "valve_opening_limit_voltage": 2200,
    },
)


TRV_ZBT = ZigbeeModel(
    model="TRV-ZBT",
    vendor="SONOFF",
    description="Zigbee thermostatic radiator valve",
    kind="sonoff_trv",
    source="zigbee-herdsman-converters v26.125.0, src/devices/sonoff.ts, definition 'TRV-ZBT'",
    exposes=(
        Climate(
            features=(
                # sonoffTrvzbtTargetTemperatureRange, sonoffTrvzbtLocalTemperatureCalibrationRange
                setpoint("occupied_heating_setpoint", 5, 30, 0.5),
                local_temperature(),
                local_temperature_calibration(-10, 10, 0.2),
                system_mode(("off", "auto", "heat")),
                running_state(("idle", "heat")),
            )
        ),
        modern_battery(),
        # Exposes of the extend sonoffExtend.trvzbtFaultCode
        Text(name="fault_status", access=STATE_GET, label="Fault status", category="diagnostic"),
        _enum("screen_direction", ("0", "90", "180", "270"), category="config"),
        _binary("child_lock", "LOCK", "UNLOCK", category="config"),
        _binary("open_window", "ON", "OFF", category="config"),
        # sonoffTrvzbtFrostProtectionTemperatureRange
        _numeric(
            "frost_protection_temperature",
            category="config",
            value_min=5,
            value_max=15,
            value_step=0.5,
            unit="°C",
        ),
        _enum(
            "temperature_sensor_select",
            SONOFF_TEMPERATURE_SENSOR_SELECT,
            label="Temperature sensor",
        ),
        _sonoff_external_temperature_input(),
        *_sonoff_steps_and_voltages(),
        _numeric(
            "heating_valve_position",
            category="config",
            value_min=0,
            value_max=100,
            value_step=1,
            unit="%",
        ),
        _numeric(
            "idle_valve_position",
            category="config",
            value_min=0,
            value_max=100,
            value_step=1,
            unit="%",
        ),
        _numeric(
            "temperature_accuracy",
            category="config",
            value_min=-1,
            value_max=-0.2,
            value_step=0.2,
            unit="°C",
        ),
        _binary("smart_temperature_control", "ON", "OFF", category="config"),
        # Exposes of the extend sonoffExtend.trvzbtTemporaryMode
        _enum("temporary_mode_mode", ("disabled", "timer", "boost")),
        _numeric(
            "temporary_mode_duration", value_min=1, value_max=1440, value_step=1, unit="minutes"
        ),
        _numeric(
            "temporary_mode_target_temperature",
            value_min=5,
            value_max=30,
            value_step=0.5,
            unit="°C",
        ),
        _enum("low_battery_valve_state", ("close", "open_30"), category="config"),
        # Exposes of the extend sonoffExtend.trvzbtWeeklySchedule
        _enum("schedule_active_group", ("1", "2", "3"), category="config"),
        _enum("schedule_group_to_edit", ("1", "2", "3"), category="config"),
        *(
            Text(name=f"weekly_schedule_{day}", access=ALL, category="config")
            for day in _SUNDAY_FIRST
        ),
        # Exposes of the extend sonoffExtend.trvzbtRemoteTemperatureLinkage
        _binary(
            "remote_temperature_linkage",
            "ON",
            "OFF",
            label="Thermostat Linkage",
            category="config",
        ),
        _numeric(
            "remote_setpoint_temperature",
            label="Thermostat Linkage Target Temperature",
            value_min=5,
            value_max=30,
            value_step=0.5,
            unit="°C",
            category="config",
        ),
        # Exposes of the extend sonoffExtend.trvzbtHvacNotification
        _binary("open_window_detected", True, False, access=STATE),
        _numeric(
            "heat_percentage_hour",
            STATE_GET,
            category="diagnostic",
            value_min=0,
            value_max=100,
            value_step=1,
            unit="%",
        ),
        _enum("valve_travel_calibration", ("calibrate",), category="config"),
        # Exposes of the extend sonoffExtend.motorTravelCalibrationStatus
        Text(name="motor_travel_calibration_status", access=STATE_GET, category="diagnostic"),
        # Exposes of the extend sonoffExtend.trvzbtBluetoothPairing
        _enum("bluetooth_pairing", ("start", "stop"), access=SET),
        _enum("bluetooth_pairing_status", ("success", "fail"), access=STATE),
        # Exposes of the extend sonoffExtend.trvzbtTemperatureControlHistory
        Composite(name="read_temperature_control_history", access=SET),
        Text(name="temperature_control_history", access=STATE, category="diagnostic"),
        linkquality(),
    ),
    reporting={
        # The definition's configure: thermostatTemperature with the change 50, i.e. 0.5 °C
        "local_temperature": Reporting(0, HOUR, 0.5),
    },
    initial_state={
        "battery": 100,
        "child_lock": "UNLOCK",
        "closing_steps": 1210,
        "external_temperature_input": 0,
        "fault_status": "",
        "frost_protection_temperature": 7,
        "heat_percentage_hour": 0,
        "heating_valve_position": 100,
        "idle_steps": 1270,
        "idle_valve_position": 0,
        "local_temperature_calibration": 0,
        "low_battery_valve_state": "close",
        "open_window": "ON",
        "open_window_detected": False,
        "remote_temperature_linkage": "OFF",
        "schedule_active_group": "1",
        "screen_direction": "0",
        "smart_temperature_control": "ON",
        "system_mode": "heat",
        "temperature_accuracy": -1,
        "temperature_sensor_select": "local_temperature",
        "temporary_mode_mode": "disabled",
        "valve_closing_limit_voltage": 2200,
        "valve_motor_running_voltage": 2000,
        "valve_opening_limit_voltage": 2200,
    },
)


SNZB_02P = ZigbeeModel(
    model="SNZB-02P",
    vendor="SONOFF",
    description="Temperature and humidity sensor",
    kind="temperature_humidity",
    source="zigbee-herdsman-converters v26.125.0, src/devices/sonoff.ts, definition 'SNZB-02P'",
    exposes=(
        modern_battery(),
        # The extends temperature and humidity of modernExtend.ts, numeric with access STATE_GET
        _numeric("temperature", STATE_GET, unit="°C"),
        _numeric("humidity", STATE_GET, unit="%"),
        _numeric(
            "temperature_calibration",
            category="config",
            value_min=-50,
            value_max=50,
            value_step=0.1,
            unit="°C",
        ),
        _numeric(
            "humidity_calibration",
            category="config",
            value_min=-50,
            value_max=50,
            value_step=0.1,
            unit="%",
        ),
        linkquality(),
    ),
    reporting={
        # modernExtend.ts temperature and humidity report with min 10 s, max 1 h, change 100
        # in units of 0.01, that is 1 °C and 1 %.
        "temperature": Reporting(10, HOUR, 1.0),
        "humidity": Reporting(10, HOUR, 1.0),
    },
    initial_state={"battery": 100, "humidity_calibration": 0, "temperature_calibration": 0},
)


SNZB_04P = ZigbeeModel(
    model="SNZB-04P",
    vendor="SONOFF",
    description="Contact sensor",
    kind="contact",
    source="zigbee-herdsman-converters v26.125.0, src/devices/sonoff.ts, definition 'SNZB-04P'",
    exposes=(
        # The contact zone of modernExtend.ts iasZoneAlarm with alarm_1 and battery_low
        _binary("contact", False, True, access=STATE),
        _binary("battery_low", True, False, access=STATE, category="diagnostic"),
        _binary("tamper", True, False, access=STATE_GET),
        # The battery of ewelink.ts ewelinkBattery, with voltage
        modern_battery(),
        modern_battery_voltage(),
        linkquality(),
    ),
    initial_state={"battery": 100, "battery_low": False, "tamper": False, "voltage": 3000},
)


def _bosch_discovery_override(payload: dict[str, Any]) -> None:
    """``meta.overrideHaDiscoveryPayload`` of the BTH-RA definition, verbatim in effect."""
    topic = payload.get("mode_command_topic")
    if isinstance(topic, str) and topic.endswith("/system_mode"):
        payload["mode_command_topic"] = topic[: topic.rindex("/system_mode")]
        payload["mode_command_template"] = (
            "{% set values = { 'auto':'schedule','heat':'manual','off':'pause'} %}"
            '{"operating_mode": "{{ values[value] if value in values.keys() else \'pause\' }}"}'
        )
        payload["mode_state_template"] = (
            "{% set values = {'schedule':'auto','manual':'heat','pause':'off'} %}"
            "{% set value = value_json.operating_mode %}"
            "{{ values[value] if value in values.keys() else 'off' }}"
        )
        payload["modes"] = ["off", "heat", "auto"]


BTH_RA = ZigbeeModel(
    model="BTH-RA",
    vendor="Bosch",
    description="Radiator thermostat II",
    kind="bosch_trv",
    source=(
        "zigbee-herdsman-converters v26.125.0, src/devices/bosch.ts, definition 'BTH-RA', "
        "with the extends in src/lib/bosch.ts (boschThermostatExtend, boschGeneralExtend)"
    ),
    exposes=(
        # The climate of boschThermostatExtend.raThermostat (modernExtend.ts thermostat)
        Climate(
            features=(
                local_temperature(),
                local_temperature_calibration(-5, 5, 0.1),
                setpoint("occupied_heating_setpoint", 5, 30, 0.5),
                system_mode(("heat",)),
                running_state(("idle", "heat")),
                pi_heating_demand(ALL),
            )
        ),
        _enum(
            "setpoint_change_source",
            ("manual", "schedule", "externally"),
            access=STATE_GET,
            category="diagnostic",
        ),
        _enum("operating_mode", ("schedule", "manual", "pause"), category="config"),
        _binary("window_detection", "ON", "OFF"),
        _binary("boost_heating", "ON", "OFF", label="Activate boost heating"),
        _numeric(
            "remote_temperature",
            category="config",
            value_min=0,
            value_max=35,
            value_step=0.2,
            unit="°C",
        ),
        _binary("child_lock", "LOCK", "UNLOCK"),
        _numeric(
            "display_brightness",
            category="config",
            value_min=0,
            value_max=100,
            value_step=10,
            unit="%",
        ),
        _numeric(
            "display_switch_on_duration",
            label="Display switch-on duration",
            category="config",
            value_min=5,
            value_max=30,
            unit="s",
        ),
        _enum(
            "display_orientation",
            ("standard_arrangement", "rotated_by_180_degrees"),
            category="config",
        ),
        _enum(
            "displayed_temperature",
            ("set_temperature", "measured_temperature"),
            category="config",
        ),
        # Exposes of the extend boschThermostatExtend.valveAdaptation
        _enum(
            "valve_adapt_status",
            ("none", "ready_to_calibrate", "calibration_in_progress", "error", "success"),
            access=STATE_GET,
            label="Valve adaptation status",
            category="diagnostic",
        ),
        _binary(
            "automatic_valve_adapt",
            True,
            False,
            access=STATE_GET,
            label="Automatic valve adaptation requested",
            category="diagnostic",
        ),
        _enum(
            "valve_adapt_process",
            ("adapt",),
            access=SET,
            label="Trigger adaptation process",
            category="config",
        ),
        # Exposes of the extend boschThermostatExtend.errorState
        Text(name="error_state", access=STATE_GET, category="diagnostic"),
        # Exposes of the extend boschGeneralExtend.batteryWithPercentageAndLowStatus
        modern_battery(),
        modern_battery_low(),
        # The schedule texts of boschThermostatExtend.raWeeklySchedule, Monday first
        *(
            Text(name=f"weekly_schedule_{day}", access=ALL, category="config")
            for day in (*_SUNDAY_FIRST[1:], _SUNDAY_FIRST[0])
        ),
        linkquality(),
    ),
    reporting={
        # raThermostat reports the local temperature with min 1 min, max 1 h, change 0.1 °C
        "local_temperature": Reporting(60, HOUR, 0.1),
    },
    initial_state={
        "automatic_valve_adapt": False,
        "battery": 100,
        "battery_low": False,
        "boost_heating": "OFF",
        "child_lock": "UNLOCK",
        "display_brightness": 70,
        "display_orientation": "standard_arrangement",
        "display_switch_on_duration": 10,
        "displayed_temperature": "set_temperature",
        "error_state": "ok",
        "local_temperature_calibration": 0,
        "operating_mode": "manual",
        "setpoint_change_source": "manual",
        "system_mode": "heat",
        "valve_adapt_status": "success",
        "window_detection": "OFF",
    },
    discovery_override=_bosch_discovery_override,
)


MODELS: Mapping[str, ZigbeeModel] = {
    model.model: model for model in (TRV_ZBT, TRVZB, BTH_RA, SNZB_02P, SNZB_04P)
}
