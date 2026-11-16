"""A simulated home with ``cave_heating`` on top, for the device integration tests.

The devices are the simulators of ``sim/``: Zigbee thermostats and sensors reach Home Assistant's
real MQTT integration through the Zigbee2MQTT stand-in, FRITZ! thermostats reach its real
``fritzbox`` integration through the fake FRITZ!Box (``cave_sim.testing``). ``cave_heating`` is set
up from an applied ``heating.yaml`` exactly as on the server. Nothing here is a stand-in entity.

What the devices receive is read where it arrives: the MQTT messages Home Assistant published to
``zigbee2mqtt/<name>/set...``, and the ``sethkrtsoll`` requests the fake FRITZ!Box answered. Both
are normalised to ``("target", 21.0)`` and ``("mode", "off")``, so a test can count commands the
same way for every model; a Zigbee heat command with a mode is two radio writes (Home Assistant's
MQTT climate publishes the mode, then the setpoint), a FRITZ! one is one request.

Time is simulated: :meth:`Rig.advance` moves Home Assistant's clock, which the simulators share,
from timer to timer, as ``tests/cave_heating/fakes.py`` does. Nothing sleeps.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import yaml
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

from cave_sim.home import parse_home
from cave_sim.testing import HOME_ASSISTANT_CLIENT, SimulatedHomeHandle
from custom_components.cave_heating.const import DOMAIN

ROOM: Final = "living_room"
ROOM_CLIMATE: Final = f"climate.cave_{ROOM}"
PROBLEM: Final = f"binary_sensor.cave_{ROOM}_problem"
WINDOW_OPEN: Final = f"binary_sensor.cave_{ROOM}_window_open"
COMFORT: Final = f"number.cave_{ROOM}_comfort_temperature"
ECO: Final = f"number.cave_{ROOM}_eco_temperature"
HOME_MODE: Final = "select.cave_home_mode"
SCHEDULE: Final = "schedule.living_room_heating"
PRESENCE: Final = "input_boolean.someone_home"

SEC: Final = 1.0
MIN: Final = 60.0
HOUR: Final = 3600.0

type Command = tuple[str, Any]


def object_id(name: str) -> str:
    """Zigbee2MQTT's (and Home Assistant's) entity ID for a device name."""
    return "_".join(name.lower().split())


@dataclass(frozen=True)
class ThermostatModel:
    """A thermostat model as the tests use it, with the demo home's device name."""

    key: str
    sim_model: str
    name: str
    family: str  # zigbee or fritz
    min_temp: float
    max_temp: float
    step: float | None  # target_temp_step; None where the entity has none (FRITZ!)
    hvac_modes: tuple[str, ...]
    external_suffix: str | None = None
    settings_suffixes: dict[str, str] = field(default_factory=dict)
    entities: tuple[str, ...] = ()  # the entities the heating logic and the dashboard use

    @property
    def device_id(self) -> str:
        """The simulator's control-API ID."""
        return object_id(self.name)

    @property
    def climate(self) -> str:
        """The climate entity."""
        return f"climate.{self.device_id}"

    @property
    def battery(self) -> str:
        """The battery sensor."""
        return f"sensor.{self.device_id}_battery"

    @property
    def external(self) -> str | None:
        """The external temperature input, if the model has one."""
        if self.external_suffix is None:
            return None
        return f"number.{self.device_id}_{self.external_suffix}"

    @property
    def settings(self) -> dict[str, str]:
        """The ``settings`` block of concept section 5.6 for this model."""
        return {
            f"{entity.split('.')[0]}.{self.device_id}_{entity.split('.')[1]}": value
            for entity, value in self.settings_suffixes.items()
        }

    @property
    def zigbee(self) -> bool:
        """Whether it is a Zigbee thermostat."""
        return self.family == "zigbee"

    def trv(self, *, external: bool = True, settings: bool = True) -> dict[str, Any]:
        """Its entry in ``heating.yaml``."""
        entry: dict[str, Any] = {"entity": self.climate}
        if external and self.external:
            entry["external_temperature"] = self.external
        if settings and self.settings:
            entry["settings"] = dict(self.settings)
        return entry


def _sonoff(key: str, name: str, minimum: float, maximum: float) -> ThermostatModel:
    device = object_id(name)
    return ThermostatModel(
        key=key,
        sim_model=key,
        name=name,
        family="zigbee",
        min_temp=minimum,
        max_temp=maximum,
        step=0.5,
        hvac_modes=("off", "auto", "heat"),
        external_suffix="external_temperature_input",
        settings_suffixes={
            "select.temperature_sensor_select": "remote_temperature",
            "switch.open_window": "off",
        },
        entities=(
            f"climate.{device}",
            f"number.{device}_external_temperature_input",
            f"select.{device}_temperature_sensor_select",
            f"switch.{device}_open_window",
            f"sensor.{device}_battery",
        ),
    )


# The device names are those of the demo home (sim/demo/home.yaml), so the entity IDs are the ones
# sim/README.md documents.
TRV_ZBT: Final = _sonoff("TRV-ZBT", "Wohnzimmer Heizung Fenster", 5.0, 30.0)
TRVZB: Final = _sonoff("TRVZB", "Wohnzimmer Heizung Tuer", 4.0, 35.0)
BTH_RA: Final = ThermostatModel(
    key="BTH-RA",
    sim_model="BTH-RA",
    name="Schlafzimmer Heizung",
    family="zigbee",
    min_temp=5.0,
    max_temp=30.0,
    step=0.5,
    hvac_modes=("off", "heat", "auto"),
    external_suffix="remote_temperature",
    settings_suffixes={"switch.window_detection": "off"},
    entities=(
        "climate.schlafzimmer_heizung",
        "number.schlafzimmer_heizung_remote_temperature",
        "switch.schlafzimmer_heizung_window_detection",
        "select.schlafzimmer_heizung_operating_mode",
        "sensor.schlafzimmer_heizung_battery",
        "binary_sensor.schlafzimmer_heizung_battery_low",
    ),
)
FRITZ_302: Final = ThermostatModel(
    key="FRITZ-302",
    sim_model="FRITZ!Smart Thermo 302",
    name="Bad Heizung",
    family="fritz",
    min_temp=8.0,
    max_temp=28.0,
    step=None,
    hvac_modes=("heat", "off"),
    entities=(
        "climate.bad_heizung",
        "sensor.bad_heizung_temperature",
        "sensor.bad_heizung_battery",
        "binary_sensor.bad_heizung_summer_mode",
        "binary_sensor.bad_heizung_holiday_mode",
        "binary_sensor.bad_heizung_button_lock_via_ui",
        "binary_sensor.bad_heizung_button_lock_on_device",
        "binary_sensor.bad_heizung_open_window_detected",
    ),
)
MODELS: Final[dict[str, ThermostatModel]] = {
    model.key: model for model in (TRV_ZBT, TRVZB, BTH_RA, FRITZ_302)
}
FRITZ_AIN: Final = "13979 0000001"

# The room sensor and the window contact.
SENSOR_NAME: Final = "Wohnzimmer"
CONTACT_NAME: Final = "Wohnzimmer Fenster"
SENSOR_ID: Final = object_id(SENSOR_NAME)
CONTACT_ID: Final = object_id(CONTACT_NAME)
TEMPERATURE: Final = f"sensor.{SENSOR_ID}_temperature"
HUMIDITY: Final = f"sensor.{SENSOR_ID}_humidity"
CONTACT: Final = f"binary_sensor.{CONTACT_ID}_contact"

# Bosch BTH-RA: the climate entity's mode is the device's operating_mode (sim/README.md).
_BOSCH_MODES: Final = {"manual": "heat", "pause": "off", "schedule": "auto"}
_COMMAND_PROPERTIES: Final = ("system_mode", "operating_mode", "occupied_heating_setpoint")
HKR_OFF: Final = 253
HKR_ON: Final = 254


def heat(model: ThermostatModel, target: float, *, with_mode: bool = False) -> list[Command]:
    """What a device receives for heat at ``target``.

    A Zigbee thermostat gets the mode (when the command carries one) and the setpoint as two
    writes. Home Assistant's FRITZ! integration turns set_temperature with a target into one
    ``sethkrtsoll`` and ignores the mode, so a FRITZ! thermostat gets one request either way.
    """
    if model.zigbee and with_mode:
        return [("mode", "heat"), ("target", target)]
    return [("target", target)]


OFF: Final = [("mode", "off")]


class Rig:
    """The simulated home, the heating on top, and what the devices received."""

    def __init__(self, hass: HomeAssistant, freezer: Any, factory: Any) -> None:
        """Nothing started yet."""
        self.hass = hass
        self.freezer = freezer
        self._factory: Callable[..., Awaitable[SimulatedHomeHandle]] = factory
        self._sim: SimulatedHomeHandle | None = None
        self.models: list[ThermostatModel] = []
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self._fritz_commands: list[tuple[float, int]] = []
        self._marks: dict[str, int] = {}

    # --- building the home ------------------------------------------------------------------

    @property
    def sim(self) -> SimulatedHomeHandle:
        """The running simulated home."""
        assert self._sim is not None, "call rig.start() first"
        return self._sim

    async def start(
        self,
        *models: ThermostatModel,
        sensor: bool = True,
        contact: bool = True,
        schedule: bool = False,
        presence: bool = False,
        external: bool = True,
        settings: bool = True,
        fritzbox: dict[str, Any] | None = None,
        zigbee2mqtt: dict[str, Any] | None = None,
        timing: dict[str, Any] | None = None,
        room: dict[str, Any] | None = None,
        before_heating: Callable[[Rig], Awaitable[None]] | None = None,
        heating: bool = True,
    ) -> None:
        """Start the simulated home with these devices in one room, then cave_heating.

        ``schedule`` moves the clock to 07:58 local time first and adds a schedule helper that is
        on from 08:00 to 09:00 every day. ``presence`` adds ``input_boolean.someone_home`` (on).
        ``before_heating`` runs after the devices exist and before cave_heating starts.
        """
        self.models = list(models)
        if schedule:
            self._move_to_local_time(7, 58)
        devices: list[dict[str, Any]] = [
            {"model": model.sim_model, "name": model.name}
            | ({"ain": FRITZ_AIN} if not model.zigbee else {})
            for model in models
        ]
        if sensor:
            devices.append({"model": "SNZB-02P", "name": SENSOR_NAME})
        if contact:
            devices.append({"model": "SNZB-04P", "name": CONTACT_NAME})
        spec = parse_home(
            {
                "fritzbox": fritzbox or {},
                "zigbee2mqtt": zigbee2mqtt or {},
                "rooms": [
                    {
                        "id": ROOM,
                        "name": "Wohnzimmer",
                        "temperature": 19.0,
                        "humidity": 50,
                        "devices": devices,
                    }
                ],
            }
        )
        self._sim = await self._factory(spec)
        self._record_fritz_commands()
        self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_service_call)
        if schedule:
            day = [{"from": "08:00:00", "to": "09:00:00"}]
            weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
            assert await async_setup_component(
                self.hass,
                "schedule",
                {
                    "schedule": {
                        "living_room_heating": {
                            "name": "Living room heating",
                            **dict.fromkeys((*weekdays, "sunday"), day),
                        }
                    }
                },
            )
        if presence:
            assert await async_setup_component(
                self.hass, "input_boolean", {"input_boolean": {"someone_home": {"initial": True}}}
            )
        await self.hass.async_block_till_done()
        if before_heating is not None:
            await before_heating(self)
        if not heating:
            return
        room_entry: dict[str, Any] = {
            "id": ROOM,
            "name": "Wohnzimmer",
            "trvs": [model.trv(external=external, settings=settings) for model in models],
        }
        if sensor:
            room_entry |= {"temperature_sensor": TEMPERATURE, "humidity_sensor": HUMIDITY}
        if contact:
            room_entry["windows"] = [CONTACT]
        if schedule:
            room_entry["schedule"] = SCHEDULE
        room_entry |= room or {}
        config: dict[str, Any] = {"version": 1, "rooms": [room_entry]}
        if presence:
            config["presence"] = [PRESENCE]
        if timing:
            config["timing"] = timing
        await self.start_heating(config)

    async def start_heating(self, config: dict[str, Any]) -> None:
        """Write the applied heating.yaml and start cave_heating as configuration.yaml does."""
        path = Path(self.hass.config.path("cave", ".applied", "heating.yaml"))

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        await self.hass.async_add_executor_job(write)
        assert await async_setup_component(self.hass, DOMAIN, {DOMAIN: None})
        await self.hass.async_block_till_done()
        await self.advance(3 * SEC)

    def _move_to_local_time(self, hour: int, minute: int) -> None:
        """Move the clock forward (never back) to the next ``hour:minute`` local time."""
        now = dt_util.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        self.freezer.move_to(target)

    def _record_fritz_commands(self) -> None:
        box = self.sim.home.fritzbox
        original = box.homeautoswitch

        def recording(params: dict[str, str]) -> Any:
            response = original(params)
            if params.get("switchcmd") == "sethkrtsoll":
                self._fritz_commands.append((response.status, int(params.get("param", "0"))))
            return response

        box.homeautoswitch = recording  # type: ignore[method-assign]

    def _on_service_call(self, event: Event[Any]) -> None:
        data = event.data
        self.service_calls.append(
            (data["domain"], data["service"], dict(data.get("service_data") or {}))
        )

    # --- time ---------------------------------------------------------------------------------

    async def advance(self, seconds: float, step: float = MIN) -> None:
        """Move time forward, stopping at every timer on the way and at least every ``step``."""
        end = dt_util.utcnow() + timedelta(seconds=seconds)
        while (now := dt_util.utcnow()) < end:
            tick = min(end - now, timedelta(seconds=step))
            next_timer = self._next_timer()
            if next_timer is not None:
                tick = min(tick, max(next_timer, timedelta(microseconds=1)))
            self.freezer.tick(tick)
            async_fire_time_changed_exact(self.hass)
            await self.hass.async_block_till_done(wait_background_tasks=True)

    def _next_timer(self) -> timedelta | None:
        loop = self.hass.loop
        ahead = dt_util.utcnow().timestamp() - time.time()
        delays = [
            handle.when() - loop.time() - ahead
            for handle in get_scheduled_timer_handles(loop)
            if not handle.cancelled()
        ]
        future = [delay for delay in delays if delay > 0]
        return timedelta(seconds=min(future)) if future else None

    async def past_start_window(self) -> None:
        """No report is a hand change for confirm_timeout (5 min) after a start: let it pass."""
        await self.advance(5 * MIN + 5 * SEC)

    @staticmethod
    def now() -> datetime:
        """The simulated time."""
        return dt_util.utcnow()

    # --- what the devices received ----------------------------------------------------------

    def _zigbee_messages(self, name: str) -> list[dict[str, Any]]:
        messages = []
        for message in self.sim.broker.messages(f"zigbee2mqtt/{name}/set/#"):
            if message.sender != HOME_ASSISTANT_CLIENT:
                continue
            prop = message.topic.removeprefix(f"zigbee2mqtt/{name}/set").strip("/")
            text = message.payload.decode()
            if prop:
                try:
                    value = json.loads(text)
                except ValueError:
                    value = text
                messages.append({prop: value})
            else:
                messages.append(json.loads(text))
        return messages

    def all_commands(self, model: ThermostatModel) -> list[Command]:
        """Every mode and target command the device received since the start, normalised."""
        commands: list[Command] = []
        if model.zigbee:
            for message in self._zigbee_messages(model.name):
                for prop, value in message.items():
                    if prop == "occupied_heating_setpoint":
                        commands.append(("target", float(value)))
                    elif prop == "system_mode":
                        commands.append(("mode", value))
                    elif prop == "operating_mode":
                        commands.append(("mode", _BOSCH_MODES[value]))
            return commands
        for _status, param in self._fritz_commands:
            if param == HKR_OFF:
                commands.append(("mode", "off"))
            elif param == HKR_ON:
                commands.append(("mode", "on"))
            else:
                commands.append(("target", param / 2))
        return commands

    def all_writes(self, model_name: str, prop: str) -> list[Any]:
        """Every value Home Assistant wrote to a Zigbee device's property since the start."""
        return [message[prop] for message in self._zigbee_messages(model_name) if prop in message]

    def mark(self) -> None:
        """From now on, :meth:`commands` and :meth:`writes` count only what follows."""
        for model in self.models:
            self._marks[model.key] = len(self.all_commands(model))
        self._marks["__writes__"] = len(self.sim.broker.log)

    def commands(self, model: ThermostatModel) -> list[Command]:
        """The mode and target commands the device received since the last :meth:`mark`."""
        return self.all_commands(model)[self._marks.get(model.key, 0) :]

    def writes(self, name: str, prop: str) -> list[Any]:
        """Values written to a Zigbee device's property since the last :meth:`mark`."""
        start = self._marks.get("__writes__", 0)
        values = []
        for message in self.sim.broker.log[start:]:
            if message.sender != HOME_ASSISTANT_CLIENT:
                continue
            if message.topic == f"zigbee2mqtt/{name}/set/{prop}":
                values.append(json.loads(message.payload))
            elif message.topic == f"zigbee2mqtt/{name}/set":
                body = json.loads(message.payload)
                if prop in body:
                    values.append(body[prop])
        return values

    def fritz_answers(self) -> list[int]:
        """The HTTP status the FRITZ!Box gave each ``sethkrtsoll``, since the start."""
        return [status for status, _ in self._fritz_commands]

    def controller_calls(self, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
        """The climate services called for ``entity_id`` (by the controller), since the start."""
        return [
            (service, {key: value for key, value in data.items() if key != "entity_id"})
            for domain, service, data in self.service_calls
            if domain == "climate" and data.get("entity_id") in (entity_id, [entity_id])
        ]

    # --- reading Home Assistant -------------------------------------------------------------

    def state(self, entity_id: str) -> State:
        """A state that must exist."""
        state = self.hass.states.get(entity_id)
        assert state is not None, f"{entity_id} does not exist"
        return state

    def room(self) -> tuple[float | None, str | None, str | None]:
        """The room thermostat's target, preset and reason."""
        attributes = self.state(ROOM_CLIMATE).attributes
        return (
            attributes.get("temperature"),
            attributes.get("preset_mode"),
            attributes.get("reason"),
        )

    def statuses(self) -> dict[str, str]:
        """The problem sensor's ``thermostats`` attribute."""
        return dict(self.state(PROBLEM).attributes["thermostats"])

    def status(self, model: ThermostatModel) -> str:
        """The controller's status of one thermostat."""
        return self.statuses()[model.climate]

    def problem(self) -> bool:
        """Whether the room's problem sensor is on."""
        return self.state(PROBLEM).state == "on"

    def messages(self) -> list[str]:
        """The problem sensor's messages."""
        return list(self.state(PROBLEM).attributes["messages"])

    # --- acting in the UI ---------------------------------------------------------------------

    async def call(self, domain: str, service: str, **data: Any) -> None:
        """A blocking service call, as the UI makes it."""
        await self.hass.services.async_call(domain, service, data, blocking=True)
        await self.hass.async_block_till_done()

    async def set_comfort(self, value: float) -> None:
        """Change the room's comfort temperature in the UI."""
        await self.call("number", "set_value", entity_id=COMFORT, value=value)

    async def set_eco(self, value: float) -> None:
        """Change the room's eco temperature in the UI."""
        await self.call("number", "set_value", entity_id=ECO, value=value)

    async def room_off(self) -> None:
        """Switch the room off."""
        await self.call("climate", "turn_off", entity_id=ROOM_CLIMATE)

    async def room_on(self) -> None:
        """Switch the room on."""
        await self.call("climate", "turn_on", entity_id=ROOM_CLIMATE)

    async def home_mode(self, mode: str) -> None:
        """Switch the home mode by hand."""
        await self.call("select", "select_option", entity_id=HOME_MODE, option=mode)

    async def boost(self, **data: Any) -> None:
        """``cave_heating.boost`` on the room."""
        await self.call(DOMAIN, "boost", entity_id=ROOM_CLIMATE, **data)

    async def hold(self, model: ThermostatModel, duration: str | None = None) -> None:
        """``cave_heating.hold``."""
        data = {"entity_id": model.climate} | ({"duration": duration} if duration else {})
        await self.call(DOMAIN, "hold", **data)

    async def release(self, model: ThermostatModel) -> None:
        """``cave_heating.release``."""
        await self.call(DOMAIN, "release", entity_id=model.climate)

    # --- acting on the devices ----------------------------------------------------------------

    async def refresh_fritzbox(self) -> None:
        """Let Home Assistant poll the FRITZ!Box now, if the home has one."""
        if self.sim.fritz_entry is not None:
            await self.sim.async_refresh_fritzbox()

    async def device(self, action: Callable[[], None]) -> None:
        """Do something to a device through the simulator, and let Home Assistant see it.

        A FRITZ! change reaches Home Assistant with its next poll (every 30 s); this polls now.
        """
        action()
        await self.hass.async_block_till_done()
        await self.refresh_fritzbox()

    def cached(self, model: ThermostatModel) -> dict[str, Any]:
        """What Zigbee2MQTT shows for a Zigbee device (its cache)."""
        return dict(self.sim.home.device_state(model.device_id)["zigbee2mqtt_state"])

    def registers(self, device_id: str) -> dict[str, Any]:
        """A device's own state, as the control API shows it."""
        return dict(self.sim.home.device_state(device_id)["state"])


def unique(values: Iterable[Any]) -> list[Any]:
    """The values in order, without repeats."""
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
