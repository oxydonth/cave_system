"""The controller: reads Home Assistant, applies the heating logic, and drives the thermostats.

``logic.py`` decides what each room wants (concept sections 5.3 and 5.4) and ``driver.py`` holds
the state of each thermostat (section 5.5). This module is the only place that talks to Home
Assistant: it listens to the configured entities, keeps one timer for the earliest deadline of
anything, sends the commands the drivers hand back, writes the temperature feed and the device
settings, keeps the room and home state across restarts, and raises repair notices.

Everything runs in the event loop; reading the applied file happens in the executor. One room's
failure is contained: it is logged and reported on that room's problem sensor, and the other
rooms go on.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from homeassistant.components.climate import DATA_COMPONENT as CLIMATE_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    APPLIED_FILE_PARTS,
    DOMAIN,
    HOME_DEVICE_ID,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    ROOM_DEVICE_PREFIX,
    SLOW_PLATFORMS,
    STORAGE_SAVE_DELAY_SECONDS,
    TEMPERATURE_STEP,
)
from .driver import (
    DEFAULT_STEP,
    PROBLEM_STATUSES,
    Availability,
    Command,
    Feed,
    Report,
    Status,
    ThermostatDriver,
    Want,
    round_target,
    same,
    setting_call,
    setting_matches,
)
from .logic import (
    UNAVAILABLE_STATES,
    AwayTracker,
    Decision,
    HomeMode,
    Override,
    PresenceReadings,
    Preset,
    RoomMode,
    RoomState,
    ScheduleTracker,
    WindowPause,
    contact_of,
    decide,
    hand_from_dict,
    hand_to_dict,
    is_away,
    presence_of,
    room_temperature,
    schedule_on,
    sensor_fresh,
    someone_home,
)
from .schema import ConfigError, HeatingConfig, RoomConfig, ThermostatConfig, Timing, load_file

_LOGGER = logging.getLogger(__package__)

ISSUE_FILE_MISSING: Final = "applied_file_missing"
ISSUE_FILE_INVALID: Final = "applied_file_invalid"
ISSUE_RELOAD_INVALID: Final = "reload_invalid"

# Home Assistant's climate defaults, for a thermostat that does not report its range.
_CLIMATE_MIN: Final = 7.0
_CLIMATE_MAX: Final = 35.0
# A thermostat's external temperature input, when it does not report its step.
_FEED_STEP: Final = 0.1
# Only a missing or unavailable entity cannot be written; one that has no value yet can.
_NOT_WRITABLE: Final = "unavailable"


def applied_file_path(hass: HomeAssistant) -> Path:
    """Where the applied copy of heating.yaml lives (concept section 4.5)."""
    return Path(hass.config.path(*APPLIED_FILE_PARTS))


@dataclass(frozen=True, slots=True)
class FileResult:
    """The outcome of reading the applied copy."""

    config: HeatingConfig | None
    missing: bool = False
    errors: tuple[str, ...] = ()


def read_applied_file(path: Path) -> FileResult:
    """Read and validate the applied copy. Blocking: run it in the executor.

    Never raises: whatever goes wrong becomes a result with errors, so that the integration
    starts without rooms and a repair notice says why.
    """
    try:
        if not path.is_file():
            return FileResult(None, missing=True, errors=(f"The file {path} does not exist.",))
        return FileResult(load_file(path))
    except ConfigError as err:
        return FileResult(None, errors=tuple(err.errors))
    except Exception as err:  # reported as a repair notice, never raised
        _LOGGER.exception("Reading %s failed", path)
        return FileResult(None, errors=(f"The file {path} cannot be read: {err!r}.",))


def _float(value: object) -> float | None:
    """A finite number, or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError:
        return None
    return result if math.isfinite(result) else None


def _writable(state: State | None) -> bool:
    return state is not None and state.state != _NOT_WRITABLE


@dataclass(slots=True)
class Settle:
    """Hand changes being collected in a room (concept section 5.5)."""

    entity_id: str  # the thermostat that changed last
    target: float
    due: datetime


@dataclass(slots=True)
class RoomRuntime:
    """A room while the controller runs."""

    config: RoomConfig
    state: RoomState
    windows: WindowPause
    drivers: list[ThermostatDriver]
    schedule: ScheduleTracker = field(default_factory=ScheduleTracker)
    feeds: dict[str, Feed] = field(default_factory=dict)
    settings_due: dict[str, datetime] = field(default_factory=dict)
    # Settings whose entity could not be written at one of the three moments: setting entity
    # -> thermostat. Each is written once as soon as its entity can be.
    pending_settings: dict[str, str] = field(default_factory=dict)
    refused_settings: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)  # entity -> field naming it
    settle: Settle | None = None
    decision: Decision | None = None
    temperature: float | None = None
    sensor_fresh: bool = False
    humidity: float | None = None
    heating_action: bool = False
    error: str | None = None  # the controller failed on this room in its last evaluation

    @property
    def thermostats(self) -> Mapping[str, ThermostatConfig]:
        """The room's thermostats by entity ID."""
        return {trv.entity: trv for trv in self.config.trvs}

    @property
    def available(self) -> bool:
        """The room entity is unavailable when none of its thermostats is (section 5.4)."""
        return any(driver.available for driver in self.drivers)


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class HeatingRuntime:
    """One per config entry: the whole heating of the home."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        store: Store[dict[str, Any]],
        stored: Mapping[str, Any] | None,
    ) -> None:
        """Start without rooms, with the state kept from the last run."""
        self.hass = hass
        self.entry = entry
        self._store = store
        now = dt_util.utcnow()
        stored = stored if isinstance(stored, Mapping) else {}
        raw_home = stored.get("home")
        home: Mapping[str, Any] = raw_home if isinstance(raw_home, Mapping) else {}
        rooms = stored.get("rooms")
        self._stored_rooms: dict[str, Mapping[str, Any]] = (
            dict(rooms) if isinstance(rooms, Mapping) else {}
        )
        self.config = HeatingConfig()
        self.loaded = False
        try:
            self.home_mode = HomeMode(home.get("mode", HomeMode.AUTO))
        except ValueError:
            self.home_mode = HomeMode.AUTO
        stored_away = _float(home.get("away_temperature"))
        self._away_temperature_stored = stored_away is not None
        self.away_temperature = stored_away if stored_away is not None else 16.0
        self.away = AwayTracker(self.config.away_delay)
        # Restored, so that a restart neither restarts the away delay nor, while the trackers
        # are still loading, counts the home as occupied (concept section 5.5, start-up).
        self.away.nobody_home_since = _datetime(home.get("nobody_home_since"))
        self.presence = PresenceReadings(Timing().presence_unavailable_grace)
        self.presence.restore(home.get("presence"), now)
        self.rooms: dict[str, RoomRuntime] = {}
        self.drivers: dict[str, ThermostatDriver] = {}
        self._roles: dict[str, list[Callable[[State | None, datetime], None]]] = {}
        self._sensor_devices: dict[str, set[str]] = {}  # room sensor -> its device's entities
        self._sensors_without_device: set[str] = set()
        self._resubscribe = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._room_adders: list[Callable[[list[RoomRuntime]], None]] = []
        self._unsub_events: list[CALLBACK_TYPE] = []
        self._unsub_started: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._timer_at: datetime | None = None
        self._issues: set[str] = set()
        self._evaluating = False
        self._again = False
        self._started = False
        self._running = False
        self._saved: dict[str, Any] | None = None

    # --- Configuration -------------------------------------------------------------------------

    @callback
    def set_file_problem(self, result: FileResult, *, reload: bool = False) -> None:
        """Raise or clear the repair notices about the applied file."""
        path = str(applied_file_path(self.hass))
        errors = "\n".join(f"- {error}" for error in result.errors)
        for issue_id in (ISSUE_FILE_MISSING, ISSUE_FILE_INVALID, ISSUE_RELOAD_INVALID):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        if result.config is not None:
            return
        if reload:
            issue_id = ISSUE_RELOAD_INVALID
        else:
            issue_id = ISSUE_FILE_MISSING if result.missing else ISSUE_FILE_INVALID
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=issue_id,
            translation_placeholders={"path": path, "errors": errors},
        )

    def _initial_room_state(self, config: RoomConfig) -> RoomState:
        """A room's state from the state file, or from the file the first time it appears."""
        stored = self._stored_rooms.get(config.id)
        state = RoomState(comfort=config.comfort_temperature, eco=config.eco_temperature)
        if not isinstance(stored, Mapping):
            return state
        state.comfort = _float(stored.get("comfort_temperature")) or state.comfort
        state.eco = _float(stored.get("eco_temperature")) or state.eco
        if stored.get("mode") == RoomMode.OFF:
            state.mode = RoomMode.OFF
        state.hand = hand_from_dict(stored.get("hand"), dt_util.utcnow())
        return state

    def _log_file_differences(self) -> None:
        """Temperatures are adjusted in the UI; say so where the file says otherwise."""
        for room in self.rooms.values():
            for name, used, in_file in (
                ("comfort_temperature", room.state.comfort, room.config.comfort_temperature),
                ("eco_temperature", room.state.eco, room.config.eco_temperature),
            ):
                if not same(used, in_file):
                    _LOGGER.info(
                        "Room %s: %s is %s in heating.yaml but %s as set in the UI; the UI value "
                        "is kept (the file only sets the first value)",
                        room.config.id,
                        name,
                        in_file,
                        used,
                    )
        if not same(self.away_temperature, self.config.away_temperature):
            _LOGGER.info(
                "away_temperature is %s in heating.yaml but %s as set in the UI; the UI value is "
                "kept (the file only sets the first value)",
                self.config.away_temperature,
                self.away_temperature,
            )

    def _slow(self, trv: ThermostatConfig) -> bool:
        if trv.slow is not None:
            return trv.slow
        entry = er.async_get(self.hass).async_get(trv.entity)
        return entry is not None and entry.platform in SLOW_PLATFORMS

    def _new_room(
        self, room_config: RoomConfig, drivers: list[ThermostatDriver], previous: RoomRuntime | None
    ) -> RoomRuntime:
        timing = self.config.timing
        windows = WindowPause(
            room_config.window_open_delay,
            room_config.window_close_delay,
            timing.window_unavailable_grace,
            room_config.windows,
        )
        room = RoomRuntime(
            config=room_config,
            state=previous.state if previous else self._initial_room_state(room_config),
            windows=windows,
            drivers=drivers,
        )
        if previous is not None:
            # A reload keeps what the room was doing.
            windows.paused = previous.windows.paused
            room.decision = previous.decision
            room.refused_settings = dict(previous.refused_settings)
            room.pending_settings = {
                entity: trv
                for entity, trv in previous.pending_settings.items()
                if trv in room.thermostats
            }
            room.settings_due = {
                trv: due for trv, due in previous.settings_due.items() if trv in room.thermostats
            }
            if previous.config.schedule == room_config.schedule:
                room.schedule = previous.schedule
            if previous.settle and previous.settle.entity_id in room.thermostats:
                room.settle = previous.settle
        else:
            stored = self._stored_rooms.get(room_config.id)
            if isinstance(stored, Mapping) and stored.get("window_paused") is True:
                windows.paused = True
            if isinstance(room.state.hand, Override) and room.state.hand.schedule is not None:
                # A schedule change while Home Assistant was down still ends the override.
                room.schedule.last = room.state.hand.schedule
        for trv in room_config.trvs:
            if trv.external_temperature:
                old_feed = previous.feeds.get(trv.entity) if previous else None
                keep = (
                    old_feed is not None
                    and previous is not None
                    and previous.thermostats[trv.entity].external_temperature
                    == trv.external_temperature
                )
                feed = old_feed if keep and old_feed is not None else Feed(timing)
                feed.timing = timing
                room.feeds[trv.entity] = feed
        return room

    @callback
    def apply_config(self, config: HeatingConfig) -> None:
        """Make ``config`` the configuration in effect: at start-up and on reload."""
        now = dt_util.utcnow()
        old_rooms = self.rooms
        old_drivers = self.drivers
        if not self.loaded and not self._away_temperature_stored:
            self.away_temperature = config.away_temperature
        self.config = config
        self.loaded = True
        self.away.delay = config.away_delay
        self.presence.grace = config.timing.presence_unavailable_grace
        timing = config.timing

        rooms: dict[str, RoomRuntime] = {}
        drivers: dict[str, ThermostatDriver] = {}
        for room_config in config.rooms:
            room_drivers: list[ThermostatDriver] = []
            for trv in room_config.trvs:
                driver = old_drivers.get(trv.entity)
                if driver is None:
                    driver = ThermostatDriver(trv.entity, timing, slow=self._slow(trv), now=now)
                else:
                    driver.timing = timing
                    driver.slow = self._slow(trv)
                drivers[trv.entity] = driver
                room_drivers.append(driver)
            rooms[room_config.id] = self._new_room(
                room_config, room_drivers, old_rooms.get(room_config.id)
            )
        self.rooms = rooms
        self.drivers = drivers
        self._stored_rooms = {
            key: value for key, value in self._stored_rooms.items() if key in rooms
        }
        self._log_file_differences()

        added = [room for room_id, room in rooms.items() if room_id not in old_rooms]
        if self._room_adders:
            self.update_devices()
            for adder in self._room_adders:
                adder(added)
        if self._running:
            self._subscribe()
            self._read_all(now)
            for room in self.rooms.values():
                self._write_all_settings(room, now)
            self._evaluate()

    @callback
    def update_devices(self) -> None:
        """Remove the devices of rooms no longer in the file; keep the others' names current.

        Only called while a valid file is in effect, so a broken file never removes a room.
        Removing a device removes its entities too.
        """
        registry = dr.async_get(self.hass)
        wanted = {f"{ROOM_DEVICE_PREFIX}{room_id}": room for room_id, room in self.rooms.items()}
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
            if HOME_DEVICE_ID in identifiers:
                continue
            matching = [wanted[key] for key in identifiers if key in wanted]
            if not matching:
                registry.async_remove_device(device.id)
            elif device.name != matching[0].config.name:
                registry.async_update_device(device.id, name=matching[0].config.name)

    @callback
    def register_room_adder(self, adder: Callable[[list[RoomRuntime]], None]) -> None:
        """A platform adds the entities of rooms that appear on reload through this."""
        self._room_adders.append(adder)

    async def async_reload(self) -> FileResult:
        """Read the applied copy again (``cave_heating.reload``)."""
        result = await self.hass.async_add_executor_job(
            read_applied_file, applied_file_path(self.hass)
        )
        self.set_file_problem(result, reload=self.loaded)
        if result.config is not None:
            self.apply_config(result.config)
        return result

    # --- Start and stop ------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start listening and driving; called once the entities exist."""
        self._running = True
        self._started = self.hass.state is CoreState.running
        now = dt_util.utcnow()
        self._subscribe()
        self._read_all(now)
        for room in self.rooms.values():
            self._write_all_settings(room, now)
        self._evaluate()
        if not self._started:
            # Kept apart from the event subscriptions, which a reload renews.
            self._unsub_started = async_at_started(self.hass, self._on_started)

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._unsub_started = None
        self._started = True
        self._read_all(dt_util.utcnow())
        self._evaluate()

    async def async_stop(self) -> None:
        """Stop everything and write the state file."""
        self._running = False
        self._unsubscribe()
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set()
        await self._store.async_save(self._serialize())

    # --- Listening -----------------------------------------------------------------------------

    def _unsubscribe(self) -> None:
        while self._unsub_events:
            self._unsub_events.pop()()

    def _device_entities(self, entity_id: str) -> set[str] | None:
        """The entities of the device an entity belongs to; None if it has no known device."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None
        return {
            item.entity_id
            for item in er.async_entries_for_device(registry, entry.device_id)
            if item.entity_id != entity_id
        }

    @callback
    def _subscribe(self) -> None:
        self._unsubscribe()
        self._resubscribe = False
        self._roles = {}

        def add(entity_id: str, role: Callable[[State | None, datetime], None]) -> None:
            self._roles.setdefault(entity_id, []).append(role)

        for entity_id in self.config.presence:
            add(entity_id, self._presence_role(entity_id))
        for room in self.rooms.values():
            for entity_id, field_name in room.config.entities():
                add(entity_id, self._existence_role(room, entity_id, field_name))
            for driver in room.drivers:
                add(driver.entity_id, self._thermostat_role(room, driver))
            for window in room.config.windows:
                add(window, self._window_role(room, window))
            if room.config.schedule:
                add(room.config.schedule, self._schedule_role(room))
        if self._roles:
            self._unsub_events.append(
                async_track_state_change_event(self.hass, set(self._roles), self._on_state_event)
            )
        sensors = {
            room.config.temperature_sensor
            for room in self.rooms.values()
            if room.config.temperature_sensor
        }
        self._sensor_devices = {}
        self._sensors_without_device = set()
        for sensor in sensors:
            siblings = self._device_entities(sensor)
            if siblings is None:
                self._sensors_without_device.add(sensor)
            else:
                self._sensor_devices[sensor] = siblings
        reporters = sensors | {item for items in self._sensor_devices.values() for item in items}
        if reporters:
            self._unsub_events.append(
                async_track_state_report_event(self.hass, reporters, self._on_sensor_report)
            )
        siblings_only = reporters - set(self._roles)
        if siblings_only:
            self._unsub_events.append(
                async_track_state_change_event(self.hass, siblings_only, self._on_sensor_report)
            )

    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        now = dt_util.utcnow()
        state = event.data["new_state"]
        for role in self._roles.get(event.data["entity_id"], ()):
            role(state, now)
        self._evaluate()

    @callback
    def _on_sensor_report(
        self, event: Event[EventStateReportedData] | Event[EventStateChangedData]
    ) -> None:
        """A room sensor's device reported: only of interest while the sensor counts as stale."""
        if any(
            not room.sensor_fresh and room.config.temperature_sensor for room in self.rooms.values()
        ):
            self._evaluate()

    @callback
    def _read_all(self, now: datetime) -> None:
        """Read every configured entity as it is now (start-up and reload)."""
        for room in self.rooms.values():
            for entity_id, field_name in room.config.entities():
                self._existence_role(room, entity_id, field_name)(
                    self.hass.states.get(entity_id), now
                )
            for window in room.config.windows:
                self._window_role(room, window)(self.hass.states.get(window), now)
            if room.config.schedule:
                self._schedule_role(room)(self.hass.states.get(room.config.schedule), now)
            for driver in room.drivers:
                state = self.hass.states.get(driver.entity_id)
                if state is None and not self._started:
                    continue
                driver.on_report(
                    self._report(driver.entity_id, state), now, exists=state is not None
                )
        for entity_id in self.config.presence:
            self._presence_role(entity_id)(self.hass.states.get(entity_id), now)

    def _existence_role(
        self, room: RoomRuntime, entity_id: str, field_name: str
    ) -> Callable[[State | None, datetime], None]:
        def role(state: State | None, _now: datetime) -> None:
            if state is None and self._started:
                room.missing[entity_id] = field_name
            else:
                room.missing.pop(entity_id, None)
            if state is not None and entity_id in self._sensors_without_device:
                # The sensor appeared after the subscriptions were made: find its device now.
                self._resubscribe = True

        return role

    def _thermostat_role(
        self, room: RoomRuntime, driver: ThermostatDriver
    ) -> Callable[[State | None, datetime], None]:
        def role(state: State | None, now: datetime) -> None:
            if state is None and not self._started:
                return
            outcome = driver.on_report(
                self._report(driver.entity_id, state),
                now,
                exists=state is not None,
                adopt=room.decision is not None and room.decision.heating,
            )
            if outcome.returned:
                # A thermostat paired after the start is recognised as slow now.
                driver.slow = self._slow(room.thermostats[driver.entity_id])
                self._write_settings(room, room.thermostats[driver.entity_id], now)
            if outcome.hand_target is not None:
                room.settle = Settle(
                    driver.entity_id, outcome.hand_target, now + self.config.timing.manual_settle
                )

        return role

    def _window_role(
        self, room: RoomRuntime, entity_id: str
    ) -> Callable[[State | None, datetime], None]:
        def role(state: State | None, now: datetime) -> None:
            if state is None and not self._started:
                return  # not loaded yet: a restored pause waits for it
            room.windows.update(
                entity_id,
                contact_of(state.state if state else None),
                state.last_changed if state else now,
            )

        return role

    def _schedule_role(self, room: RoomRuntime) -> Callable[[State | None, datetime], None]:
        def role(state: State | None, _now: datetime) -> None:
            if room.schedule.update(state.state if state else None):
                room.state.on_schedule_change(room.schedule.last)

        return role

    def _presence_role(self, entity_id: str) -> Callable[[State | None, datetime], None]:
        def role(state: State | None, now: datetime) -> None:
            if state is None and not self._started:
                return  # not loaded yet: a restored reading stands meanwhile
            self.presence.update(
                entity_id, presence_of(entity_id, state.state if state else None), now
            )

        return role

    def _report(self, entity_id: str, state: State | None) -> Report | None:
        """Read a thermostat's state into a report; None while it cannot be used."""
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        attributes = state.attributes
        modes = tuple(str(mode) for mode in attributes.get("hvac_modes") or ())
        step = _float(attributes.get("target_temp_step"))
        if not step or step <= 0:
            component = self.hass.data.get(CLIMATE_COMPONENT)
            entity = component.get_entity(entity_id) if component is not None else None
            step = float(entity.precision) if entity is not None else DEFAULT_STEP
        minimum = _float(attributes.get("min_temp"))
        maximum = _float(attributes.get("max_temp"))
        action = attributes.get("hvac_action")
        return Report(
            mode=state.state,
            target=_float(attributes.get("temperature")),
            current=_float(attributes.get("current_temperature")),
            modes=modes,
            min_temp=minimum if minimum is not None else _CLIMATE_MIN,
            max_temp=maximum if maximum is not None else _CLIMATE_MAX,
            step=step,
            action=str(action) if action is not None else None,
        )

    # --- Evaluation ----------------------------------------------------------------------------

    @property
    def occupied(self) -> bool:
        """``binary_sensor.cave_home_occupied``."""
        return self.away.occupied(dt_util.utcnow())

    def _is_away(self, now: datetime) -> bool:
        return is_away(self.home_mode, self.away.occupied(now))

    @callback
    def _evaluate(self) -> None:
        """Bring everything up to date, send what is due, and set the next timer."""
        if not self._running:
            return
        if self._evaluating:
            self._again = True
            return
        self._evaluating = True
        now = dt_util.utcnow()
        try:
            if self._resubscribe:
                self._subscribe()
            self._again = True
            while self._again:
                self._again = False
                now = dt_util.utcnow()
                self._evaluate_once(now)
            self._save_if_changed()
            self._sync_issues(now)
            for listener in list(self._listeners):
                listener()
        finally:
            self._evaluating = False
            # Armed from the time the evaluation used, so a deadline that passed meanwhile is
            # handled at once rather than dropped.
            self._schedule_timer(now)

    def _decide(self, room: RoomRuntime, now: datetime, away: bool) -> Decision:
        schedule = self.hass.states.get(room.config.schedule) if room.config.schedule else None
        return decide(
            home_mode=self.home_mode,
            room=room.state,
            window_paused=room.windows.evaluate(now),
            away=away,
            schedule_is_on=schedule_on(schedule.state if schedule else None),
            away_temperature=self.away_temperature,
        )

    def _someone_home(self, now: datetime) -> bool:
        return someone_home(
            self.presence.effective(entity_id, now) for entity_id in self.config.presence
        )

    def _evaluate_once(self, now: datetime) -> None:
        self.away.update(self._someone_home(now), now)
        away = self._is_away(now)
        for room in self.rooms.values():
            try:
                self._evaluate_room(room, now, away)
            except Exception as err:  # one room must not stop the others
                _LOGGER.exception("Evaluating room %s failed", room.config.id)
                room.error = str(err) or type(err).__name__
            else:
                room.error = None

    def _evaluate_room(self, room: RoomRuntime, now: datetime, away: bool) -> None:
        room.state.on_presence(away)
        room.state.expire(now)
        decision = self._decide(room, now, away)
        if room.settle is not None and now >= room.settle.due:
            self._finish_settle(room, decision, now, away)
            decision = self._decide(room, now, away)
        room.decision = decision
        want = Want(heat=decision.heating, target=decision.target)
        # Nothing is sent while hand changes settle, nor while Home Assistant is still starting
        # and the room's other inputs may not be known yet.
        may_send = room.settle is None and self._started
        for driver in room.drivers:
            driver.set_want(want, now)
            if (command := driver.tick(now, may_send=may_send)) is not None:
                self._send(driver, command)

        sensor = self._sensor_value(room, now)
        readings = [
            driver.report.current
            for driver in room.drivers
            if driver.available and driver.report and driver.report.current is not None
        ]
        room.temperature, room.sensor_fresh = room_temperature(sensor, readings)
        room.humidity = self._number_state(room.config.humidity_sensor)
        room.heating_action = any(
            driver.available and driver.report and driver.report.action == "heating"
            for driver in room.drivers
        )
        self._feed(room, sensor, now)
        for entity_id, due in list(room.settings_due.items()):
            if now >= due:
                del room.settings_due[entity_id]
                self._write_settings(room, room.thermostats[entity_id], now)
        self._write_pending_settings(room, now)

    def _finish_settle(
        self, room: RoomRuntime, decision: Decision, now: datetime, away: bool
    ) -> None:
        """The hand changes have settled: the last one becomes the room's override.

        Nothing is adopted while the radiators are off, from a held thermostat, or from one
        that is back at what is wanted from it.
        """
        settle, room.settle = room.settle, None
        assert settle is not None  # noqa: S101
        driver = self.drivers.get(settle.entity_id)
        if decision.heating and driver is not None and not driver.held(now):
            report = driver.report if driver.available else None
            target = settle.target
            if report is not None and report.mode == report.heat_mode and report.target is not None:
                target = report.target
            if report is None or not driver.matches():
                target = round_target(target, TEMPERATURE_STEP, MIN_TEMPERATURE, MAX_TEMPERATURE)
                room.state.set_override(
                    now,
                    room.config.override_max,
                    away=away,
                    target=target,
                    schedule=room.schedule.last,
                )
        for item in room.drivers:
            item.reconcile(now)

    def _sensor_value(self, room: RoomRuntime, now: datetime) -> float | None:
        """The room sensor's reading, if it is available and fresh (section 5.4)."""
        if not room.config.temperature_sensor:
            return None
        state = self.hass.states.get(room.config.temperature_sensor)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        value = _float(state.state)
        last = self._sensor_last_report(room.config.temperature_sensor)
        if (
            value is None
            or last is None
            or not sensor_fresh(last, now, self.config.timing.sensor_stale)
        ):
            return None
        return value

    def _sensor_last_report(self, sensor: str) -> datetime | None:
        """When the sensor's device last reported: the newest report of any of its entities.

        Home Assistant does not record a repeated identical value as a new report, so a steady
        temperature alone would look like silence (concept section 5.4).
        """
        times = []
        for entity_id in {sensor, *self._sensor_devices.get(sensor, ())}:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in UNAVAILABLE_STATES:
                times.append(state.last_reported)
        return max(times) if times else None

    def _number_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        return _float(state.state)

    # --- Sending -------------------------------------------------------------------------------

    def _send(self, driver: ThermostatDriver, command: Command) -> None:
        _LOGGER.debug("%s: %s %s", driver.entity_id, command.service, dict(command.data))
        self.entry.async_create_task(
            self.hass,
            self._async_send(driver, command),
            f"{DOMAIN} command {driver.entity_id}",
            eager_start=False,
        )

    async def _async_send(self, driver: ThermostatDriver, command: Command) -> None:
        try:
            await self.hass.services.async_call(
                "climate",
                command.service,
                {ATTR_ENTITY_ID: driver.entity_id, **command.data},
                blocking=True,
            )
        except Exception as err:  # any refusal makes the thermostat blocked
            if self.drivers.get(driver.entity_id) is not driver:
                return
            if driver.on_refused(dt_util.utcnow(), str(err) or type(err).__name__, command):
                _LOGGER.warning("%s refused %s: %s", driver.entity_id, command.service, err)
            else:
                _LOGGER.info(
                    "%s: %s failed after the thermostat had confirmed: %s",
                    driver.entity_id,
                    command.service,
                    err,
                )
            self._evaluate()

    def _call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        on_result: Callable[[str | None], None] | None = None,
    ) -> None:
        async def call() -> None:
            try:
                await self.hass.services.async_call(domain, service, data, blocking=True)
            except Exception as err:  # reported, never raised into the loop
                _LOGGER.warning("%s.%s %s failed: %s", domain, service, data, err)
                if on_result is not None:
                    on_result(str(err) or type(err).__name__)
            else:
                if on_result is not None:
                    on_result(None)

        self.entry.async_create_task(
            self.hass, call(), f"{DOMAIN} {domain}.{service}", eager_start=False
        )

    def _feed(self, room: RoomRuntime, reading: float | None, now: datetime) -> None:
        """Room temperature to the thermostats that take it (section 5.5)."""
        allowed = (
            self.home_mode is not HomeMode.OFF
            and room.state.mode is not RoomMode.OFF
            and reading is not None
        )
        for trv in room.config.trvs:
            if not trv.external_temperature:
                continue
            feed = room.feeds[trv.entity]
            driver = self.drivers[trv.entity]
            target = self.hass.states.get(trv.external_temperature)
            paused = driver.held(now) or not _writable(target)
            step = feed.step(now, reading, allowed=allowed, paused=paused)
            if step is None or target is None:
                continue
            if step.restarted:
                room.settings_due[trv.entity] = now + self.config.timing.confirm_timeout
            if step.value is None:
                continue
            attributes = target.attributes
            minimum = _float(attributes.get("min"))
            maximum = _float(attributes.get("max"))
            value = round_target(
                step.value,
                _float(attributes.get("step")) or _FEED_STEP,
                minimum if minimum is not None else step.value,
                maximum if maximum is not None else step.value,
            )
            feed.wrote(value, now)
            domain = trv.external_temperature.split(".", 1)[0]
            self._call(domain, "set_value", {ATTR_ENTITY_ID: target.entity_id, "value": value})

    def _write_all_settings(self, room: RoomRuntime, now: datetime) -> None:
        for trv in room.config.trvs:
            self._write_settings(room, trv, now)

    def _write_settings(self, room: RoomRuntime, trv: ThermostatConfig, now: datetime) -> None:
        """One of the three moments: write each device setting that differs, once (5.5).

        A setting whose entity is missing or unavailable now, or whose thermostat is held, is
        remembered and written once as soon as it can be.
        """
        driver = self.drivers.get(trv.entity)
        if driver is None:
            return
        for entity_id in trv.settings:
            room.pending_settings[entity_id] = trv.entity
        self._write_pending_settings(room, now)

    def _write_pending_settings(self, room: RoomRuntime, now: datetime) -> None:
        for entity_id, trv_entity in list(room.pending_settings.items()):
            driver = self.drivers.get(trv_entity)
            state = self.hass.states.get(entity_id)
            if driver is None or driver.held(now) or not _writable(state):
                continue
            assert state is not None  # noqa: S101  (checked by _writable)
            del room.pending_settings[entity_id]
            wanted = room.thermostats[trv_entity].settings[entity_id]
            if setting_matches(entity_id, wanted, state.state):
                room.refused_settings.pop(entity_id, None)
                continue
            domain, service, data = setting_call(entity_id, wanted)

            def result(error: str | None, entity_id: str = entity_id) -> None:
                if error is None:
                    room.refused_settings.pop(entity_id, None)
                else:
                    room.refused_settings[entity_id] = error
                self._evaluate()

            self._call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, result)

    # --- Timer, storage, issues, entities --------------------------------------------------------

    def _deadlines(self, now: datetime) -> list[datetime]:
        deadlines: list[datetime] = []
        for deadline in (self.away.next_deadline(now), self.presence.next_deadline(now)):
            if deadline is not None:
                deadlines.append(deadline)
        stale = self.config.timing.sensor_stale
        for room in self.rooms.values():
            if room.state.hand is not None:
                deadlines.append(room.state.hand.until)
            if room.settle is not None:
                deadlines.append(room.settle.due)
            if (window := room.windows.next_deadline(now)) is not None:
                deadlines.append(window)
            deadlines.extend(room.settings_due.values())
            for driver in room.drivers:
                if (deadline := driver.next_deadline(now)) is not None:
                    deadlines.append(deadline)
            sensor = self._sensor_value(room, now)
            for feed in room.feeds.values():
                if (deadline := feed.next_deadline(now, sensor)) is not None:
                    deadlines.append(deadline)
            if sensor is not None and room.config.temperature_sensor:
                last = self._sensor_last_report(room.config.temperature_sensor)
                if last is not None:
                    deadlines.append(last + stale)
        return [deadline for deadline in deadlines if deadline > now]

    @callback
    def _schedule_timer(self, now: datetime) -> None:
        if not self._running:
            return
        deadlines = self._deadlines(now)
        next_at = min(deadlines) if deadlines else None
        if next_at == self._timer_at and self._unsub_timer is not None:
            return
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        self._timer_at = next_at
        if next_at is not None:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._on_timer, next_at)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        self._timer_at = None
        self._evaluate()

    def _serialize(self) -> dict[str, Any]:
        rooms = dict(self._stored_rooms)
        for room_id, room in self.rooms.items():
            rooms[room_id] = {
                "mode": room.state.mode.value,
                "comfort_temperature": room.state.comfort,
                "eco_temperature": room.state.eco,
                "hand": hand_to_dict(room.state.hand),
                "window_paused": room.windows.paused,
            }
        since = self.away.nobody_home_since
        return {
            "home": {
                "mode": self.home_mode.value,
                "away_temperature": self.away_temperature,
                "nobody_home_since": since.isoformat() if since else None,
                "presence": self.presence.to_dict(),
            },
            "rooms": rooms,
        }

    def _save_if_changed(self) -> None:
        data = self._serialize()
        if data != self._saved:
            self._saved = data
            self._store.async_delay_save(lambda: data, STORAGE_SAVE_DELAY_SECONDS)

    def _sync_issues(self, now: datetime) -> None:
        wanted: dict[str, tuple[str, dict[str, str]]] = {}
        for room in self.rooms.values():
            for entity_id, field_name in room.missing.items():
                wanted[f"missing_{room.config.id}_{entity_id}"] = (
                    "missing_entity",
                    {"room": room.config.name, "entity": entity_id, "field": field_name},
                )
            for driver in room.drivers:
                if driver.status(now) is Status.UNRESPONSIVE:
                    wanted[f"unresponsive_{driver.entity_id}"] = (
                        "disagreeing" if driver.disagreeing else "unresponsive",
                        {
                            "room": room.config.name,
                            "entity": driver.entity_id,
                            "hours": str(self.config.timing.budget_exhausted_hours),
                        },
                    )
        for issue_id in self._issues - wanted.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id in wanted.keys() - self._issues:
            translation_key, placeholders = wanted[issue_id]
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
        self._issues = set(wanted)

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``listener`` after every evaluation; returns the function that removes it."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # --- What the entities show ----------------------------------------------------------------

    def thermostat_statuses(self, room: RoomRuntime) -> dict[str, str]:
        """The problem sensor's ``thermostats`` attribute."""
        now = dt_util.utcnow()
        return {driver.entity_id: driver.status(now).value for driver in room.drivers}

    def problems(self, room: RoomRuntime) -> list[str]:
        """What needs attention in a room, in words (the problem sensor)."""
        now = dt_util.utcnow()
        messages: list[str] = []
        if room.error is not None:
            messages.append(f"the controller failed on this room: {room.error}")
        for driver in room.drivers:
            status = driver.status(now)
            if driver.availability is Availability.UNSEEN:
                continue
            if status in PROBLEM_STATUSES:
                if driver.disagreeing:
                    message = (
                        f"{driver.entity_id} kept differing from what is wanted for "
                        f"{self.config.timing.budget_exhausted_hours} hours; it is only tried "
                        "again from time to time"
                    )
                else:
                    message = _STATUS_MESSAGES[status].format(entity=driver.entity_id)
                if status is Status.BLOCKED and driver.error:
                    message += f" ({driver.error})"
                messages.append(message)
            if driver.budget_exhausted and not driver.held(now):
                messages.append(
                    f"{driver.entity_id} reached the limit of "
                    f"{self.config.timing.command_budget_per_hour} commands per hour; further "
                    "commands wait until the hour has passed"
                )
        thermostats = set(room.thermostats)
        for entity_id, field_name in sorted(room.missing.items()):
            if entity_id not in thermostats:
                messages.append(f"{entity_id} ({field_name}) does not exist")
        for entity_id in room.windows.silent_open(now):
            messages.append(
                f"window contact {entity_id} stopped reporting while open; the window pause "
                "has ended"
            )
        for entity_id, error in sorted(room.refused_settings.items()):
            messages.append(f"setting {entity_id} was refused: {error}")
        return messages

    def room_for_climate(self, entity_id: str) -> RoomRuntime | None:
        """The room whose thermostat entity ``climate.cave_<room>`` this is, if any."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform != DOMAIN or entry.domain != "climate":
            return None
        return self.rooms.get(entry.unique_id.removesuffix("_climate"))

    # --- Actions from entities and services ------------------------------------------------------

    def refuse_if_off(self, room: RoomRuntime) -> None:
        """Hand actions are refused while the radiators are off by mode (section 5.3)."""
        if self.home_mode is HomeMode.OFF:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="refused_summer",
                translation_placeholders={"room": room.config.name},
            )
        if room.state.mode is RoomMode.OFF:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="refused_room_off",
                translation_placeholders={"room": room.config.name},
            )

    @callback
    def set_temperature(self, room: RoomRuntime, temperature: float) -> None:
        """A temperature set by hand in the UI: an override."""
        self.refuse_if_off(room)
        now = dt_util.utcnow()
        target = round_target(temperature, TEMPERATURE_STEP, MIN_TEMPERATURE, MAX_TEMPERATURE)
        room.state.set_override(
            now,
            room.config.override_max,
            away=self._is_away(now),
            target=target,
            schedule=room.schedule.last,
        )
        room.settle = None
        self._evaluate()

    @callback
    def set_preset(self, room: RoomRuntime, preset: Preset) -> None:
        """A preset chosen by hand: boost, an override, or ``none`` to resume the schedule."""
        self.refuse_if_off(room)
        now = dt_util.utcnow()
        if preset is Preset.NONE:
            room.state.resume()
        elif preset is Preset.BOOST:
            room.state.start_boost(now, room.config.boost_duration, room.config.boost_temperature)
        else:
            room.state.set_override(
                now,
                room.config.override_max,
                away=self._is_away(now),
                preset=preset,
                schedule=room.schedule.last,
            )
        room.settle = None
        self._evaluate()

    @callback
    def boost(
        self, room: RoomRuntime, duration: timedelta | None, temperature: float | None
    ) -> None:
        """``cave_heating.boost``."""
        self.refuse_if_off(room)
        room.state.start_boost(
            dt_util.utcnow(),
            duration or room.config.boost_duration,
            temperature if temperature is not None else room.config.boost_temperature,
        )
        room.settle = None
        self._evaluate()

    @callback
    def resume_schedule(self, room: RoomRuntime) -> None:
        """The button or ``cave_heating.resume_schedule``: end a boost or override."""
        room.state.resume()
        room.settle = None
        self._evaluate()

    @callback
    def set_room_mode(self, room: RoomRuntime, mode: RoomMode) -> None:
        """Switch a room on (``heat``) or off; off discards hand changes still settling."""
        room.state.set_mode(mode)
        if mode is RoomMode.OFF:
            room.settle = None
        self._evaluate()

    @callback
    def set_schedule_temperature(self, room: RoomRuntime, preset: Preset, value: float) -> None:
        """The comfort or eco number entity."""
        if preset is Preset.COMFORT:
            room.state.comfort = value
        else:
            room.state.eco = value
        self._evaluate()

    @callback
    def set_home_mode(self, mode: HomeMode) -> None:
        """``select.cave_home_mode`` changed by hand.

        Away and off end every boost and override, and discard hand changes still settling.
        """
        if mode is not self.home_mode:
            for room in self.rooms.values():
                room.state.on_home_mode_by_hand(mode)
                if mode in (HomeMode.AWAY, HomeMode.OFF):
                    room.settle = None
        self.home_mode = mode
        self._evaluate()

    @callback
    def set_away_temperature(self, value: float) -> None:
        """``number.cave_away_temperature``."""
        self.away_temperature = value
        self._evaluate()

    def driver_for_service(self, entity_id: str) -> ThermostatDriver:
        """The driver of a configured thermostat, for hold and release."""
        driver = self.drivers.get(entity_id)
        if driver is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_thermostat",
                translation_placeholders={"entity": entity_id},
            )
        return driver

    @callback
    def hold(self, entity_id: str, duration: timedelta) -> None:
        """``cave_heating.hold``: also discards a dial change of that thermostat still settling."""
        self.driver_for_service(entity_id).hold(dt_util.utcnow(), duration)
        for room in self.rooms.values():
            if room.settle is not None and room.settle.entity_id == entity_id:
                room.settle = None
        self._evaluate()

    @callback
    def release(self, entity_id: str) -> None:
        """``cave_heating.release``."""
        self.driver_for_service(entity_id).release(dt_util.utcnow())
        self._evaluate()

    def diagnostics(self) -> dict[str, Any]:
        """The controller's state, for the diagnostics download."""
        now = dt_util.utcnow()
        stored = self._serialize()
        timing = self.config.timing
        return {
            "loaded": self.loaded,
            "started": self._started,
            "version": self.config.version,
            "home": stored["home"],
            "occupied": self.away.occupied(now),
            "timing": {item.name: str(getattr(timing, item.name)) for item in fields(timing)},
            "rooms": {
                room_id: {
                    "decision": None
                    if room.decision is None
                    else {
                        "target": room.decision.target,
                        "preset": room.decision.preset.value,
                        "reason": room.decision.reason.value,
                        "heating": room.decision.heating,
                    },
                    "state": stored["rooms"][room_id],
                    "temperature": room.temperature,
                    "temperature_from_sensor": room.sensor_fresh,
                    "settling": room.settle is not None,
                    "missing": dict(room.missing),
                    "pending_settings": dict(room.pending_settings),
                    "problems": self.problems(room),
                    "thermostats": {
                        driver.entity_id: driver.diagnostics(now) for driver in room.drivers
                    },
                    "feeds": {
                        entity_id: {
                            "active": feed.active,
                            "last_value": feed.last_value,
                            "last_at": feed.last_at.isoformat() if feed.last_at else None,
                        }
                        for entity_id, feed in room.feeds.items()
                    },
                }
                for room_id, room in self.rooms.items()
            },
        }


type CaveHeatingConfigEntry = ConfigEntry[HeatingRuntime]

_STATUS_MESSAGES: Final = {
    Status.UNRESPONSIVE: "{entity} does not confirm commands",
    Status.BLOCKED: "{entity} refuses commands",
    Status.UNAVAILABLE: "{entity} is unavailable",
    Status.MISSING: "{entity} does not exist",
}
