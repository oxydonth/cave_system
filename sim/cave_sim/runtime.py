"""A simulated home: rooms, the Zigbee2MQTT stand-in, the fake FRITZ!Box, and their control.

:class:`SimulatedHome` holds everything and offers the control functions. The HTTP control API
(:mod:`cave_sim.control_api`) and ``cave_sim.testing`` both call these; they are the one place where
the simulators are steered.
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Literal, get_args

from cave_sim.clock import Clock, PeriodicTask
from cave_sim.fritz.box import HKR_OFF, HKR_ON, FakeFritzBox, FritzThermostat, temperature_to_hkr
from cave_sim.home import HomeSpec, LockedCommand, WindowTarget
from cave_sim.mqtt import MqttTransport
from cave_sim.room import Room
from cave_sim.zigbee import fixtures
from cave_sim.zigbee.bridge import Zigbee2Mqtt
from cave_sim.zigbee.devices import (
    ContactSensor,
    SonoffTrv,
    Thermostat,
    ZigbeeDevice,
    create_device,
)
from cave_sim.zigbee.discovery import Settings
from cave_sim.zigbee.models import MODELS

STEP_SECONDS: Final = 60.0

type Device = ZigbeeDevice | FritzThermostat


class ControlError(ValueError):
    """A control request that cannot be carried out."""


class UnknownTarget(ControlError):
    """The room or device does not exist."""


class SimulatedHome:
    """The simulated devices of one home, on one clock."""

    def __init__(
        self,
        spec: HomeSpec,
        clock: Clock,
        transport: MqttTransport,
        *,
        fritz_username: str,
        fritz_password: str,
        settings: Settings | None = None,
        fixture_dir: Path = fixtures.FIXTURE_DIR,
        step_seconds: float = STEP_SECONDS,
        connected: Callable[[], bool] = lambda: True,
    ) -> None:
        self.spec = spec
        self.clock = clock
        self.outdoor_temperature = spec.outdoor_temperature
        self.step_seconds = step_seconds
        self.started = False
        self._connected = connected
        settings = dataclasses.replace(
            settings or Settings(), passive_timeout=spec.zigbee2mqtt.passive_timeout_minutes * 60
        )
        if spec.zigbee2mqtt.device_options is not None:
            settings = dataclasses.replace(
                settings, device_options=dict(spec.zigbee2mqtt.device_options)
            )
        self.zigbee = Zigbee2Mqtt(transport, clock, settings)
        self.fritzbox = FakeFritzBox(
            clock, fritz_username, fritz_password, report_delay=spec.fritzbox.report_delay
        )
        self.fritzbox.window_target = spec.fritzbox.window_target
        self.fritzbox.locked_command = spec.fritzbox.locked_command
        self.rooms: dict[str, Room] = {}
        self.devices: dict[str, Device] = {}
        loaded: dict[str, fixtures.Fixture] = {}
        for room_spec in spec.rooms:
            room = Room(
                room_spec.room_id,
                room_spec.name,
                room_spec.temperature,
                room_spec.humidity,
                room_spec.parameters,
            )
            self.rooms[room.room_id] = room
            for device_spec in room_spec.devices:
                device: Device
                if device_spec.family == "fritz":
                    device = self.fritzbox.add_thermostat(device_spec, room)
                else:
                    model = MODELS[device_spec.model]
                    if model.model not in loaded:
                        loaded[model.model] = fixtures.load(model.model, fixture_dir)
                    device = create_device(
                        self.zigbee, device_spec, model, loaded[model.model], room
                    )
                    self.zigbee.add(device)
                self.devices[device_spec.device_id] = device
        self._ticker = PeriodicTask(clock, step_seconds, self.step)

    # --- running ----------------------------------------------------------------------------

    def start(self) -> None:
        """Publish everything and start the room model."""
        self.fritzbox.tick()
        self.zigbee.start()
        self._ticker.start()
        self.started = True

    def healthy(self) -> tuple[bool, str]:
        """Whether the simulator has published its devices and is connected to the broker."""
        if not self.started:
            return False, "not started: waiting for the MQTT broker"
        if not self._connected():
            return False, "disconnected from the MQTT broker"
        return True, "ok"

    def stop(self) -> None:
        """Stop the room model."""
        self._ticker.stop()

    def step(self) -> None:
        """One step of the room model, after which every device measures and reports."""
        for room in self.rooms.values():
            room.step(self.step_seconds, self.outdoor_temperature)
        self.fritzbox.tick()
        for device in self.zigbee.devices.values():
            device.tick()

    def _refresh(self, room: Room) -> None:
        """Let the devices of ``room`` see a change at once instead of at the next step."""
        self.fritzbox.tick()
        for device in self.zigbee.devices.values():
            if device.room is room:
                device.tick()

    # --- lookup -----------------------------------------------------------------------------

    def room(self, room_id: str) -> Room:
        """The room with ``room_id``."""
        try:
            return self.rooms[room_id]
        except KeyError:
            raise UnknownTarget(f"no room {room_id!r}") from None

    def device(self, device_id: str) -> Device:
        """The device with ``device_id``."""
        try:
            return self.devices[device_id]
        except KeyError:
            raise UnknownTarget(f"no device {device_id!r}") from None

    def _typed[T](self, device_id: str, kind: type[T], what: str) -> T:
        device = self.device(device_id)
        if not isinstance(device, kind):
            raise ControlError(f"{device_id!r} is not {what}")
        return device

    # --- control ----------------------------------------------------------------------------

    def set_window(self, device_id: str, is_open: bool) -> None:
        """Open or close the window a contact sensor sits on."""
        self._typed(device_id, ContactSensor, "a window contact").set_window(is_open)

    def set_room_temperature(self, room_id: str, temperature: float) -> None:
        """Set a room's air temperature."""
        room = self.room(room_id)
        room.temperature = temperature
        self._refresh(room)

    def set_room_humidity(self, room_id: str, humidity: float) -> None:
        """Set a room's relative humidity."""
        room = self.room(room_id)
        room.humidity = humidity
        self._refresh(room)

    def set_outdoor_temperature(self, temperature: float) -> None:
        """Set the outdoor temperature the rooms lose heat to."""
        self.outdoor_temperature = temperature

    def set_online(self, device_id: str, online: bool) -> None:
        """Make a device unavailable at once (and silent), or bring it back.

        For a Zigbee device this is a shortcut past Zigbee2MQTT's availability timeout; use
        :meth:`set_silent` for a device that stops answering while it still shows as available.
        """
        device = self.device(device_id)
        if isinstance(device, FritzThermostat):
            device.present = online
            return
        device.set_answering(online)
        if not online:
            self.zigbee.make_unavailable(device)

    def set_silent(self, device_id: str, silent: bool) -> None:
        """Let a Zigbee device stop answering (dead battery, out of range), or answer again.

        A silent device stays available in Zigbee2MQTT until its passive timeout has passed since
        its last message; commands to it fail without a trace; it keeps working, and what changes
        on it meanwhile (a dial, a window) is reported when it answers again.
        """
        self._typed(device_id, ZigbeeDevice, "a Zigbee device").set_answering(not silent)

    def configure_zigbee2mqtt(self, *, passive_timeout_minutes: float | None = None) -> None:
        """Change Zigbee2MQTT's ``availability.passive.timeout``, counted from each last message."""
        if passive_timeout_minutes is not None:
            if passive_timeout_minutes <= 0:
                raise ControlError("passive_timeout_minutes must be positive")
            self.zigbee.set_passive_timeout(passive_timeout_minutes * 60)

    def set_device_mode(self, device_id: str, mode: str) -> None:
        """Change a Zigbee thermostat's mode on the device itself.

        TRV-ZBT and TRVZB: ``system_mode`` (off, auto, heat). BTH-RA: ``operating_mode`` (schedule,
        manual, pause), from which Home Assistant's climate mode is derived.
        """
        thermostat = self._typed(device_id, Thermostat, "a Zigbee thermostat")
        try:
            thermostat.set_mode(mode)
        except ValueError as error:
            raise ControlError(str(error)) from None

    def detect_open_window(self, device_id: str, minutes: float) -> None:
        """Let a FRITZ! thermostat's own detection start its window open mode for ``minutes``."""
        if minutes <= 0:
            raise ControlError("minutes must be positive")
        thermostat = self._typed(device_id, FritzThermostat, "a FRITZ! thermostat")
        self.fritzbox.detect_open_window(thermostat, minutes)

    def turn_dial(self, device_id: str, target: float | Literal["on", "off"]) -> None:
        """Turn a thermostat's dial: to a temperature, or for FRITZ! all the way to off or on."""
        device = self.device(device_id)
        if isinstance(device, FritzThermostat):
            if target == "off":
                value = HKR_OFF
            elif target == "on":
                value = HKR_ON
            else:
                value = temperature_to_hkr(float(target))
            self.fritzbox.dial(device, value)
        elif isinstance(device, Thermostat):
            if isinstance(target, str):
                raise ControlError(f"{device_id!r} has no dial position {target!r}")
            device.dial(float(target))
        else:
            raise ControlError(f"{device_id!r} is not a thermostat")

    def set_battery(self, device_id: str, level: int, low: bool | None = None) -> None:
        """Change a device's battery level."""
        device = self.device(device_id)
        if isinstance(device, FritzThermostat):
            device.battery = level
            if low is not None:
                device.battery_low = low
        else:
            device.set_battery(level, low)

    def configure_fritzbox(
        self,
        *,
        report_delay: float | None = None,
        summer: bool | None = None,
        holiday: bool | None = None,
        txbusy_while_pending: bool | None = None,
        window_target: WindowTarget | None = None,
        locked_command: LockedCommand | None = None,
    ) -> None:
        """Change the fake FRITZ!Box's behaviour; ``None`` leaves a setting as it is."""
        box = self.fritzbox
        if window_target is not None:
            if window_target not in get_args(WindowTarget.__value__):
                raise ControlError("window_target must be 'unchanged' or 'off'")
            box.window_target = window_target
        if locked_command is not None:
            if locked_command not in get_args(LockedCommand.__value__):
                raise ControlError("locked_command must be 'refuse' or 'ignore'")
            box.locked_command = locked_command
        if report_delay is not None:
            if report_delay < 0:
                raise ControlError("report_delay must not be negative")
            box.report_delay = report_delay
        if summer is not None:
            box.summer_active = summer
        if holiday is not None:
            box.holiday_active = holiday
        if txbusy_while_pending is not None:
            box.txbusy_while_pending = txbusy_while_pending

    def set_fritz_lock(
        self, device_id: str, *, lock: bool | None = None, device_lock: bool | None = None
    ) -> None:
        """Set a FRITZ! thermostat's key lock "via UI/API" and its lock on the device."""
        thermostat = self._typed(device_id, FritzThermostat, "a FRITZ! thermostat")
        if lock is not None:
            thermostat.lock = lock
        if device_lock is not None:
            thermostat.device_lock = device_lock

    def set_returns_on_feed(self, device_id: str, returns: bool) -> None:
        """Whether a Sonoff goes back to the external sensor by itself when values arrive again."""
        self._typed(device_id, SonoffTrv, "a Sonoff thermostat").returns_on_feed = returns

    # --- state ------------------------------------------------------------------------------

    def device_state(self, device_id: str) -> dict[str, Any]:
        """A device's state as the control API shows it."""
        device = self.device(device_id)
        if isinstance(device, FritzThermostat):
            return self._fritz_state(device)
        return device.snapshot()

    def _fritz_state(self, thermostat: FritzThermostat) -> dict[str, Any]:
        def temperature(raw: int) -> float | str:
            return {HKR_OFF: "off", HKR_ON: "on"}.get(raw, raw / 2)

        return {
            "id": thermostat.device_id,
            "family": "fritz",
            "model": thermostat.product_name,
            "name": thermostat.spec.name,
            "ain": thermostat.ain,
            "room": thermostat.room.room_id,
            "answering": thermostat.present,
            "available": thermostat.present,
            "state": {
                "target_temperature": temperature(thermostat.device_tsoll),
                "reported_target_temperature": temperature(thermostat.tsoll),
                "comfort_temperature": thermostat.komfort / 2,
                "eco_temperature": thermostat.absenk / 2,
                "temperature": round(thermostat.measured(), 2),
                "battery": thermostat.battery,
                "battery_low": thermostat.battery_low,
                "lock": self.fritzbox.locked(thermostat),
                "device_lock": thermostat.device_lock,
                "window_open_until": thermostat.window_open_end or None,
            },
            "valve_opening": round(thermostat.opening, 3),
            "set_commands": thermostat.set_commands,
        }

    def state(self) -> dict[str, Any]:
        """Everything: outdoor temperature, rooms, devices and the FRITZ!Box's settings."""
        return {
            "time": self.clock.now(),
            "outdoor_temperature": self.outdoor_temperature,
            "fritzbox": {
                "report_delay": self.fritzbox.report_delay,
                "summer": self.fritzbox.summer_active,
                "holiday": self.fritzbox.holiday_active,
                "txbusy_while_pending": self.fritzbox.txbusy_while_pending,
                "window_target": self.fritzbox.window_target,
                "locked_command": self.fritzbox.locked_command,
            },
            "zigbee2mqtt": {
                "passive_timeout_minutes": self.zigbee.passive_timeout / 60,
                "device_options": dict(self.zigbee.settings.device_options),
            },
            "rooms": {
                room.room_id: {
                    "name": room.name,
                    "temperature": round(room.temperature, 2),
                    "humidity": round(room.humidity, 2),
                    "open_windows": room.open_windows(),
                    "devices": [
                        device_id
                        for device_id, device in self.devices.items()
                        if device.room is room
                    ],
                }
                for room in self.rooms.values()
            },
            "devices": {device_id: self.device_state(device_id) for device_id in self.devices},
        }
