"""How each simulated Zigbee device behaves.

A device keeps its own registers (``state``), separate from what Zigbee2MQTT has cached for it
(``cache``), and reports changes the way its attribute reporting is configured
(:mod:`cave_sim.zigbee.models`). A device that has gone silent keeps working and keeps changing its
registers (a dial turned by hand, a window opened), but nothing it says arrives; when it answers
again it reports what changed. Commands arrive as Zigbee2MQTT ``/set``
messages and are handled the way Zigbee2MQTT 2.14.3 ``lib/extension/publish.ts`` handles them: each
key goes to its converter; a key without one is logged and skipped; a converter that fails (for
example because the device refuses the value or does not answer) is logged and leaves the state
alone; otherwise the new value is published at once ("optimistic"), and the device's own report
follows where what it stored differs from what was sent.

Behaviour that goes beyond the sources is marked "Assumption" and listed in ``sim/README.md``.
"""

import logging
import math
from typing import TYPE_CHECKING, Any, Final

from cave_sim.clock import Timer
from cave_sim.home import DeviceSpec
from cave_sim.room import PROPORTIONAL_BAND, RadiatorBias, Room
from cave_sim.zigbee.exposes import GET, STATE, Binary, Climate, Enum, Numeric, Text
from cave_sim.zigbee.fixtures import Fixture, Identity
from cave_sim.zigbee.models import ZigbeeModel

if TYPE_CHECKING:
    from cave_sim.zigbee.bridge import Zigbee2Mqtt

_LOGGER = logging.getLogger(__name__)

# Sonoff: "Fallback to internal sensor appears to occur after 2 hours of silence from the external
# sensor, but reverts to external sensor as soon as the next update is received."
# (zigbee-herdsman-converters pull request 8873, by the author of the external sensor support.)
SONOFF_EXTERNAL_TIMEOUT: Final = 2 * 3600.0
# Bosch BTH-RA: remote_temperature is "Required at least every 30 minutes to prevent fallback to the
# internal sensor!" (src/lib/bosch.ts, remoteTemperature()).
BOSCH_REMOTE_TIMEOUT: Final = 30 * 60.0
# Bosch BTH-RA: boost_heating "opens TRV for 5 minutes" (src/lib/bosch.ts, boostHeating()).
BOSCH_BOOST_DURATION: Final = 5 * 60.0
# Assumption: frost protection opens the valve below this temperature (models without a setting).
DEFAULT_FROST_PROTECTION: Final = 5.0


class CommandRejected(Exception):
    """The device (or its converter) refused a write; Zigbee2MQTT logs it and changes nothing."""


def js_round_half(value: float) -> float:
    """``tz.thermostat_occupied_heating_setpoint``: ``Math.round(value * 2) / 2``."""
    return math.floor(value * 2 + 0.5) / 2


def _matches(candidate: Any, value: Any) -> bool:
    if isinstance(candidate, str) and isinstance(value, str):
        return candidate.lower() == value.lower()
    return bool(candidate == value and type(candidate) is type(value))


class ZigbeeDevice:
    """A Zigbee device behind the Zigbee2MQTT stand-in."""

    def __init__(
        self,
        network: Zigbee2Mqtt,
        spec: DeviceSpec,
        model: ZigbeeModel,
        fixture: Fixture,
        room: Room,
    ) -> None:
        self.network = network
        self.spec = spec
        self.model = model
        self.fixture = fixture
        self.room = room
        self.clock = network.clock
        # The device's own registers, and what Zigbee2MQTT has cached for it.
        self.state: dict[str, Any] = fixture.initial_state()
        self.cache: dict[str, Any] = dict(self.state)
        self.answering = True
        self.linkquality = 120
        self._unsent: set[str] = set()
        self._reported: dict[str, tuple[float, Any]] = {}
        self._deferred: dict[str, Timer] = {}

    # --- identity -------------------------------------------------------------------------

    @property
    def friendly_name(self) -> str:
        """The Zigbee2MQTT friendly name."""
        return self.spec.name

    @property
    def device_id(self) -> str:
        """The control-API ID."""
        return self.spec.device_id

    def identity(self) -> Identity:
        """The values behind the fixture's placeholders."""
        return Identity(
            self.friendly_name, self.spec.ieee_address, self.network.settings.coordinator_ieee
        )

    def discovery_messages(self) -> dict[str, dict[str, Any]]:
        """This device's discovery messages, keyed by topic below the discovery prefix."""
        return self.fixture.render_discovery(self.identity())

    # --- measuring and reporting --------------------------------------------------------------

    def measure(self) -> dict[str, Any]:
        """The values the device measures right now; subclasses add theirs."""
        return {}

    def prime(self) -> None:
        """Before the first publication: measure, and count everything as reported now."""
        self.state.update(self.measure())
        self.cache = dict(self.state)
        now = self.clock.now()
        self._reported = {key: (now, value) for key, value in self.state.items()}

    def report(self, changes: dict[str, Any]) -> None:
        """The device sends a message; Zigbee2MQTT merges it into its cache and publishes it.

        A silent device changes its registers all the same, and reports the changes when it
        answers again.
        """
        if not changes:
            return
        self.state.update(changes)
        now = self.clock.now()
        for key, value in changes.items():
            self._reported[key] = (now, value)
        if not self.answering:
            self._unsent.update(changes)
            return
        self.network.device_heard(self)
        self.network.publish_entity_state(self, changes)

    def report_if_due(self, prop: str, value: Any) -> None:
        """Report ``prop`` if its reporting configuration says so."""
        config = self.model.reporting.get(prop)
        last = self._reported.get(prop)
        if last is None or config is None:
            if last is None or last[1] != value:
                self.report({prop: value})
            return
        elapsed = self.clock.now() - last[0]
        numeric = isinstance(value, int | float) and isinstance(last[1], int | float)
        moved = numeric and abs(value - last[1]) >= config.change - 1e-9
        if (elapsed >= config.min_interval and moved) or elapsed >= config.max_interval:
            self.report({prop: value})
        elif moved and prop not in self._deferred:
            # Changed enough, but too soon after the last report: the device reports when the
            # minimum interval is over, with whatever it measures then.
            def deferred() -> None:
                del self._deferred[prop]
                self.report_if_due(prop, self.measure().get(prop, value))

            self._deferred[prop] = Timer(self.clock, deferred)
            self._deferred[prop].start(config.min_interval - elapsed)

    def tick(self) -> None:
        """Called every simulation step: report what was measured per the reporting rules."""
        for prop, value in self.measure().items():
            self.report_if_due(prop, value)

    # --- commands ---------------------------------------------------------------------------

    def handle_set(self, message: dict[str, Any]) -> None:
        """A ``/set`` message for this device."""
        optimistic: dict[str, Any] = {}
        for key, value in message.items():
            if not self.model.settable(key):
                _LOGGER.error("No converter available for '%s' on '%s'", key, self.friendly_name)
                continue
            if not self.answering:
                # Zigbee2MQTT's write times out; it logs, publishes nothing and leaves the
                # availability alone (publish.ts).
                _LOGGER.error(
                    "Publish 'set' '%s' to '%s' failed: 'Timeout'", key, self.friendly_name
                )
                continue
            try:
                optimistic.update(self.write(key, value))
            except CommandRejected as error:
                _LOGGER.error(
                    "Publish 'set' '%s' to '%s' failed: '%s'", key, self.friendly_name, error
                )
        if optimistic:
            self.network.publish_entity_state(self, optimistic)
        self.after_command()

    def handle_get(self, message: dict[str, Any]) -> None:
        """A ``/get`` message: the device answers with the current values, if it answers."""
        if not self.answering:
            _LOGGER.error("Publish 'get' to '%s' failed: 'Timeout'", self.friendly_name)
            return
        answer = {}
        for key in message:
            expose = self.model.find(key)
            gettable = (
                expose is not None and not isinstance(expose, Climate) and expose.access & GET
            )
            if gettable and key in self.state:
                answer[key] = self.state[key]
        self.report(answer)

    def write(self, key: str, value: Any) -> dict[str, Any]:
        """Check and write one property; return the state Zigbee2MQTT publishes optimistically.

        What the device stores goes into its registers here. Zigbee2MQTT's ``numeric`` converter
        does no range check (modernExtend.ts ``numeric``); a value outside the range is published
        optimistically and ignored by the device, as zigbee-herdsman-converters PR 8873 observed
        for the TRVZB's external temperature (assumed for every numeric property).
        """
        expose = self.model.find(key)
        stored = True
        if isinstance(expose, Numeric):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CommandRejected(f"Value '{value}' is not a number")
            stored = (expose.value_min is None or value >= expose.value_min) and (
                expose.value_max is None or value <= expose.value_max
            )
        elif isinstance(expose, Binary):
            allowed = (expose.value_on, expose.value_off)
            match = next((v for v in allowed if _matches(v, value)), None)
            if match is None:
                raise CommandRejected(f"Value '{value}' is not allowed, expected one of {allowed}")
            value = match
        elif isinstance(expose, Enum):
            match = next((v for v in expose.values if _matches(v, value)), None)
            if match is None:
                raise CommandRejected(f"Value '{value}' is not allowed, expected {expose.values}")
            value = match
        elif isinstance(expose, Text) and not isinstance(value, str):
            raise CommandRejected(f"Value '{value}' is not a string")
        if expose is None or isinstance(expose, Climate) or not expose.access & STATE:
            return {}  # a set-only property, such as a button: the device acts, nothing is stored
        if stored:
            self.state[key] = value
        return {key: value}

    def after_command(self) -> None:
        """Called after a ``/set`` message: device reports that follow the write."""

    # --- control ----------------------------------------------------------------------------

    def set_answering(self, answering: bool) -> None:
        """Let the device fall silent, or answer again and report what changed meanwhile."""
        if answering == self.answering:
            return
        if not answering:
            # Until now it was heard regularly (check-ins); the timeout counts from here.
            self.network.device_heard(self, publish=False)
        self.answering = answering
        if answering:
            changes = {key: self.state[key] for key in sorted(self._unsent)}
            self._unsent.clear()
            changes.update(self.measure())
            self.report(changes or {"linkquality": self.linkquality})

    def set_battery(self, level: int, low: bool | None = None) -> None:
        """Report a battery level and, for models that have it, the low-battery flag."""
        changes: dict[str, Any] = {"battery": level}
        if low is not None and self.model.find("battery_low") is not None:
            changes["battery_low"] = low
        self.report(changes)

    def snapshot(self) -> dict[str, Any]:
        """What the control API shows for this device."""
        return {
            "id": self.device_id,
            "family": "zigbee",
            "model": self.model.model,
            "name": self.friendly_name,
            "ieee": self.spec.ieee_address,
            "room": self.room.room_id,
            "answering": self.answering,
            "available": self.network.available(self),
            "state": dict(self.state),
            "zigbee2mqtt_state": dict(self.cache),
        }


class Thermostat(ZigbeeDevice):
    """A radiator thermostat: a proportional valve on a regulation temperature."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.opening = 0.0
        self.radiator = RadiatorBias(self.clock.now())
        self._written_setpoint: float | None = None
        self.state.setdefault("occupied_heating_setpoint", 20.0)
        self.state.setdefault("running_state", "idle")

    def setpoint_range(self) -> tuple[float, float, float]:
        """Minimum, maximum and step of ``occupied_heating_setpoint``."""
        expose = self.model.find("occupied_heating_setpoint")
        if not isinstance(expose, Numeric) or expose.value_min is None or expose.value_max is None:
            raise TypeError(f"{self.model.model} has no bounded occupied_heating_setpoint")
        return expose.value_min, expose.value_max, expose.value_step or 0.5

    def internal_temperature(self) -> float:
        """What the thermostat's own sensor measures, next to the radiator."""
        calibration = float(self.state.get("local_temperature_calibration") or 0)
        return self.room.temperature + self.radiator.bias + calibration

    def regulation_temperature(self) -> float:
        """The temperature the valve regulates on, reported as ``local_temperature``."""
        return self.internal_temperature()

    def heating_enabled(self) -> bool:
        """Whether the thermostat heats to its setpoint (rather than only protecting from frost)."""
        return True

    def forced_opening(self) -> float | None:
        """An opening that overrides regulation, such as a boost."""
        return None

    def frost_protection(self) -> float:
        """Below this temperature the valve opens even when heating is off."""
        return DEFAULT_FROST_PROTECTION

    def compute_opening(self) -> float:
        """The valve opening for the current state, between 0 and 1."""
        forced = self.forced_opening()
        if forced is not None:
            return forced
        temperature = self.regulation_temperature()
        if not self.heating_enabled():
            return 1.0 if temperature < self.frost_protection() else 0.0
        target = float(self.state["occupied_heating_setpoint"])
        return min(max((target - temperature) / PROPORTIONAL_BAND, 0.0), 1.0)

    def measure(self) -> dict[str, Any]:
        """The temperature the thermostat reports."""
        return {"local_temperature": round(self.regulation_temperature(), 2)}

    def tick(self) -> None:
        """Regulate, then report temperature and running state."""
        self.radiator.update(self.clock.now(), self.opening)
        self.opening = self.compute_opening()
        super().tick()
        running = "heat" if self.opening > 0 else "idle"
        if running != self.state.get("running_state"):
            self.report({"running_state": running})

    def write(self, key: str, value: Any) -> dict[str, Any]:
        """Setpoints are rounded the way Zigbee2MQTT's converter rounds them before writing."""
        if key != "occupied_heating_setpoint":
            return super().write(key, value)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CommandRejected(f"'{key}' is not a number, got {type(value).__name__} ({value})")
        value_min, value_max, _ = self.setpoint_range()
        device_value = js_round_half(float(value))
        if not value_min <= device_value <= value_max:
            raise CommandRejected("Status 'INVALID_VALUE'")  # the device's answer to the write
        self._written_setpoint = device_value
        self.state[key] = device_value
        return {key: value}

    def after_command(self) -> None:
        """The device reports the setpoint it stored where it differs from the value sent."""
        written, self._written_setpoint = self._written_setpoint, None
        if written is not None and self.cache.get("occupied_heating_setpoint") != written:
            self.report({"occupied_heating_setpoint": written})

    MODE_PROPERTY = "system_mode"

    def set_mode(self, mode: str) -> None:
        """Change the mode on the device itself (its buttons or app), which it then reports."""
        expose = self.model.find(self.MODE_PROPERTY)
        if not isinstance(expose, Enum) or mode not in expose.values:
            values = expose.values if isinstance(expose, Enum) else ()
            raise ValueError(f"{self.MODE_PROPERTY} must be one of {list(values)}, got {mode!r}")
        self.report({self.MODE_PROPERTY: mode})

    def dial(self, target: float) -> None:
        """Turn the dial on the device to ``target``: rounded to its step, within its range."""
        value_min, value_max, step = self.setpoint_range()
        value = min(max(round(round(target / step) * step, 2), value_min), value_max)
        self.report({"occupied_heating_setpoint": value})

    def snapshot(self) -> dict[str, Any]:
        """Add the valve and the temperatures behind it."""
        data = super().snapshot()
        data["valve_opening"] = round(self.opening, 3)
        data["internal_temperature"] = round(self.internal_temperature(), 2)
        data["regulation_temperature"] = round(self.regulation_temperature(), 2)
        return data


class SonoffTrv(Thermostat):
    """Sonoff TRVZB and TRV-ZBT: an external temperature input that times out after two hours."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.external_received: float | None = None
        # Whether the device goes back to the external sensor by itself when values arrive again
        # after a fallback. Observed in zigbee-herdsman-converters PR 8873; not yet confirmed on our
        # own hardware (concept section 12), hence switchable through the control API.
        self.returns_on_feed = True
        self._feed_resumed = False
        self._fallback = Timer(self.clock, self._external_timed_out)

    def accepted_value(self, key: str, value: Any) -> bool:
        """Whether the device stored ``value`` (it ignores values outside the range)."""
        return bool(self.state.get(key) == value)

    def _uses_external(self) -> bool:
        return (
            self.state.get("temperature_sensor_select") == "remote_temperature"
            and self.external_received is not None
            and self.clock.now() - self.external_received < SONOFF_EXTERNAL_TIMEOUT
        )

    def regulation_temperature(self) -> float:
        """The external value while it is selected and fresh, the own sensor otherwise."""
        if self._uses_external():
            return float(self.state.get("external_temperature_input", 0))
        return self.internal_temperature()

    def heating_enabled(self) -> bool:
        """``off`` stops heating; ``auto`` heats to the current setpoint (no schedules)."""
        return self.state.get("system_mode") != "off"

    def frost_protection(self) -> float:
        """The device's frost protection setting."""
        return float(self.state.get("frost_protection_temperature", DEFAULT_FROST_PROTECTION))

    def write(self, key: str, value: Any) -> dict[str, Any]:
        """Track the external input and the sensor selection."""
        result = super().write(key, value)
        now = self.clock.now()
        if key == "external_temperature_input" and self.accepted_value(key, value):
            self.external_received = now
            self._feed_resumed = (
                self.state.get("temperature_sensor_select") == "remote_source_offline"
            )
            self._fallback.start(SONOFF_EXTERNAL_TIMEOUT)
        elif key == "temperature_sensor_select" and value == "remote_temperature":
            since = now - (self.external_received if self.external_received is not None else now)
            self._fallback.start(max(SONOFF_EXTERNAL_TIMEOUT - since, 0.0))
        return result

    def after_command(self) -> None:
        """A fresh external value brings the device back from its fallback, and is reported."""
        super().after_command()
        if self._feed_resumed and self.returns_on_feed:
            # The device reports 0x03, which the converter maps to "remote_temperature".
            self.report({"temperature_sensor_select": "remote_temperature"})
        self._feed_resumed = False
        self.tick()

    def _external_timed_out(self) -> None:
        if self.state.get("temperature_sensor_select") == "remote_temperature":
            self.report({"temperature_sensor_select": "remote_source_offline"})
            self.tick()

    def snapshot(self) -> dict[str, Any]:
        """Add when the external value last arrived."""
        data = super().snapshot()
        data["external_received"] = self.external_received
        data["returns_on_feed"] = self.returns_on_feed
        return data


class BoschTrv(Thermostat):
    """Bosch BTH-RA: remote temperature valid for 30 minutes, operating modes, window and boost."""

    # The climate entity's mode comes from operating_mode (overrideHaDiscoveryPayload in bosch.ts).
    MODE_PROPERTY = "operating_mode"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.remote_received: float | None = None
        self._source: str | None = None
        self._boost_end = Timer(self.clock, self._boost_over)
        self.state.setdefault("pi_heating_demand", 0)

    def regulation_temperature(self) -> float:
        """The remote value while it is fresh, the own sensor otherwise."""
        if (
            self.remote_received is not None
            and self.clock.now() - self.remote_received < BOSCH_REMOTE_TIMEOUT
        ):
            return float(self.state.get("remote_temperature", 0))
        return self.internal_temperature()

    def heating_enabled(self) -> bool:
        """``pause`` and an active window open mode stop heating."""
        return (
            self.state.get("operating_mode") != "pause"
            and self.state.get("window_detection") != "ON"
        )

    def forced_opening(self) -> float | None:
        """Boost opens the valve fully."""
        return 1.0 if self.state.get("boost_heating") == "ON" else None

    def write(self, key: str, value: Any) -> dict[str, Any]:
        """Track the remote temperature, boost, valve adaptation and setpoint source."""
        if key == "valve_adapt_process" and self.state.get("valve_adapt_status") not in (
            "ready_to_calibrate",
            "error",
        ):
            raise CommandRejected("Valve adaptation process not possible right now!")
        result = super().write(key, value)
        if key == "remote_temperature" and self.state.get(key) == value:
            self.remote_received = self.clock.now()
        elif key == "boost_heating" and result.get(key) == "ON":
            self._boost_end.start(BOSCH_BOOST_DURATION)
        elif key == "occupied_heating_setpoint":
            # Assumption: a setpoint written over Zigbee is reported with the source "externally".
            self._source = "externally"
        return result

    def after_command(self) -> None:
        """Report the setpoint's source and the effect on the measured temperature."""
        super().after_command()
        source, self._source = self._source, None
        if source is not None:
            self.report({"setpoint_change_source": source})
        self.tick()

    def _boost_over(self) -> None:
        self.report({"boost_heating": "OFF"})

    def tick(self) -> None:
        """Also report the heating demand, which this model reports on every change."""
        super().tick()
        demand = round(self.opening * 100)
        if demand != self.state.get("pi_heating_demand"):
            self.report({"pi_heating_demand": demand})

    def dial(self, target: float) -> None:
        """A dial change is reported with the source ``manual``."""
        super().dial(target)
        self.report({"setpoint_change_source": "manual"})

    def snapshot(self) -> dict[str, Any]:
        """Add when the remote temperature last arrived."""
        data = super().snapshot()
        data["remote_received"] = self.remote_received
        return data


class TemperatureHumiditySensor(ZigbeeDevice):
    """Sonoff SNZB-02P: measures the room and adds its calibration offsets."""

    def measure(self) -> dict[str, Any]:
        """Temperature and humidity as the device reports them."""
        temperature = self.room.temperature + float(self.state.get("temperature_calibration") or 0)
        humidity = self.room.humidity + float(self.state.get("humidity_calibration") or 0)
        return {
            "temperature": round(temperature, 2),
            "humidity": round(min(max(humidity, 0.0), 100.0), 2),
        }

    def after_command(self) -> None:
        """A calibration change shows in the next reading."""
        self.tick()


class ContactSensor(ZigbeeDevice):
    """Sonoff SNZB-04P on a window: ``contact`` is ``true`` while the window is closed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.window_open = False
        self.room.window_states.append(lambda: self.window_open)

    def measure(self) -> dict[str, Any]:
        """The contact."""
        return {"contact": not self.window_open}

    def set_window(self, is_open: bool) -> None:
        """Open or close the window; the IAS zone reports a change at once."""
        self.window_open = is_open
        self.report({"contact": not is_open})


DEVICE_CLASSES: Final[dict[str, type[ZigbeeDevice]]] = {
    "sonoff_trv": SonoffTrv,
    "bosch_trv": BoschTrv,
    "temperature_humidity": TemperatureHumiditySensor,
    "contact": ContactSensor,
}


def create_device(
    network: Zigbee2Mqtt, spec: DeviceSpec, model: ZigbeeModel, fixture: Fixture, room: Room
) -> ZigbeeDevice:
    """Create the device object for ``model`` and connect it to its room."""
    device = DEVICE_CLASSES[model.kind](network, spec, model, fixture, room)
    if isinstance(device, Thermostat):
        thermostat = device
        room.valve_openings.append(lambda: thermostat.opening)
    return device
