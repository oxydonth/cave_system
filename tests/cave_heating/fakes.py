"""Controllable stand-ins for the devices cave_heating drives, and helpers for the tests.

The real simulators are WP3's (``sim/``); these are small Home Assistant entities registered by
fake integrations, so that commands go through Home Assistant's real ``climate``, ``number``,
``select`` and ``switch`` services exactly as they would for a paired device.

A ``FakeThermostat`` can be given a step and range (or no step at all), report commands late,
refuse commands the way a FRITZ!Box does, ignore commands, go unavailable, and have its dial
turned. Every command it receives is recorded in ``commands``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant_custom_component.common import (
    MockModule,
    MockPlatform,
    async_fire_time_changed_exact,
    mock_integration,
    mock_platform,
)

from custom_components.cave_heating.const import DOMAIN

FAKE = "fake_trv"
FRITZ = "fritzbox"


class _Refusable:
    """Shared controls: refuse every command with a message, record every command."""

    refuse: str | None = None
    commands: list[tuple[str, dict[str, Any]]]

    def _received(self, name: str, data: dict[str, Any]) -> None:
        # Home Assistant passes the service call's entity_id through to set_temperature.
        data = {key: value for key, value in data.items() if key != "entity_id"}
        self.commands.append((name, data))
        if self.refuse is not None:
            raise HomeAssistantError(self.refuse)


class FakeThermostat(_Refusable, ClimateEntity):
    """A radiator thermostat."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        entity_id: str,
        *,
        modes: Iterable[str] = ("heat", "off"),
        mode: str = "heat",
        target: float | None = 20.0,
        current: float | None = 20.0,
        min_temp: float = 5.0,
        max_temp: float = 30.0,
        step: float | None = 0.5,
        precision: float | None = None,
        delay: timedelta = timedelta(0),
        unique_id: str | None = None,
    ) -> None:
        """Describe the thermostat. ``step=None`` means it has no ``target_temp_step``."""
        self.entity_id = entity_id
        self._attr_unique_id = unique_id
        self._attr_hvac_modes = [HVACMode(item) for item in modes]
        self._attr_hvac_mode = HVACMode(mode)
        self._attr_target_temperature = target
        self._attr_current_temperature = current
        self._attr_min_temp = min_temp
        self._attr_max_temp = max_temp
        self._attr_target_temperature_step = step
        if precision is not None:
            self._attr_precision = precision
        self._attr_hvac_action = None
        self.delay = delay
        self.deaf = False
        self.commands = []
        self._waiting: list[Callable[[], None]] = []

    # --- What Home Assistant calls ------------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Take a target (and a mode, when one is given)."""
        self._received("set_temperature", dict(kwargs))
        mode = kwargs.get(ATTR_HVAC_MODE)
        target = kwargs.get(ATTR_TEMPERATURE)

        def apply() -> None:
            if mode is not None:
                self._attr_hvac_mode = HVACMode(mode)
            if target is not None:
                self._attr_target_temperature = float(target)

        self._later(apply)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Take a mode."""
        self._received("set_hvac_mode", {"hvac_mode": hvac_mode})

        def apply() -> None:
            self._attr_hvac_mode = HVACMode(hvac_mode)

        self._later(apply)

    def _later(self, apply: Callable[[], None]) -> None:
        if self.deaf:
            return
        if not self.delay:
            apply()
            self.async_write_ha_state()
            return

        @callback
        def report(_now: datetime) -> None:
            apply()
            self.async_write_ha_state()

        self._waiting.append(async_call_later(self.hass, self.delay, report))

    # --- What a test does to it ---------------------------------------------------------------

    def turn_dial(self, target: float) -> None:
        """Someone turns the dial: the thermostat reports a new target at once."""
        self._attr_target_temperature = target
        self.async_write_ha_state()

    def report(
        self,
        *,
        mode: str | None = None,
        target: float | None = None,
        current: float | None = None,
        action: HVACAction | None = None,
    ) -> None:
        """The thermostat reports something by itself (a mode change, a reading)."""
        if mode is not None:
            self._attr_hvac_mode = HVACMode(mode)
        if target is not None:
            self._attr_target_temperature = target
        if current is not None:
            self._attr_current_temperature = current
        if action is not None:
            self._attr_hvac_action = action
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        """Go unavailable, or come back."""
        self._attr_available = available
        self.async_write_ha_state()

    @property
    def sent(self) -> list[tuple[str, dict[str, Any]]]:
        """The commands received so far."""
        return list(self.commands)


class FakeNumber(_Refusable, NumberEntity):
    """An external temperature input, or a numeric device setting."""

    _attr_should_poll = False

    def __init__(
        self,
        entity_id: str,
        value: float | None = None,
        *,
        minimum: float = -10.0,
        maximum: float = 50.0,
        step: float = 0.1,
    ) -> None:
        """Describe the input."""
        self.entity_id = entity_id
        self._attr_native_value = value
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self.commands = []

    @property
    def writes(self) -> list[float]:
        """The values written so far."""
        return [data["value"] for _, data in self.commands]

    async def async_set_native_value(self, value: float) -> None:
        """Take a value."""
        self._received("set_value", {"value": value})
        self._attr_native_value = value
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        """Go unavailable, or come back."""
        self._attr_available = available
        self.async_write_ha_state()


class FakeSelect(_Refusable, SelectEntity):
    """A device setting with options, such as a Sonoff's temperature sensor selection."""

    _attr_should_poll = False

    def __init__(self, entity_id: str, options: list[str], current: str) -> None:
        """Describe the setting."""
        self.entity_id = entity_id
        self._attr_options = options
        self._attr_current_option = current
        self.commands = []

    async def async_select_option(self, option: str) -> None:
        """Take an option."""
        self._received("select_option", {"option": option})
        self._attr_current_option = option
        self.async_write_ha_state()

    def change(self, option: str) -> None:
        """The device changes the setting by itself."""
        self._attr_current_option = option
        self.async_write_ha_state()


class FakeSwitch(_Refusable, SwitchEntity):
    """An on/off device setting, such as on-device window detection."""

    _attr_should_poll = False

    def __init__(self, entity_id: str, on: bool) -> None:
        """Describe the setting."""
        self.entity_id = entity_id
        self._attr_is_on = on
        self.commands = []

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch on."""
        self._received("turn_on", {})
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch off."""
        self._received("turn_off", {})
        self._attr_is_on = False
        self.async_write_ha_state()


class FakeHome:
    """Installs the fake integrations; sets states; starts cave_heating; moves time."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Nothing installed yet."""
        self.hass = hass
        self._adders: dict[tuple[str, str], Callable[[Iterable[Entity]], None]] = {}

    async def async_setup(self) -> None:
        """Register the fake integrations with the climate, number, select and switch domains."""
        for integration in (FAKE, FRITZ):
            mock_integration(self.hass, MockModule(integration))
        for integration, domain in (
            (FAKE, "climate"),
            (FRITZ, "climate"),
            (FAKE, "number"),
            (FAKE, "select"),
            (FAKE, "switch"),
        ):
            mock_platform(
                self.hass,
                f"{integration}.{domain}",
                MockPlatform(async_setup_platform=self._platform(integration, domain)),
            )
        platforms = {"climate": [FAKE, FRITZ], "number": [FAKE], "select": [FAKE], "switch": [FAKE]}
        for domain, names in platforms.items():
            assert await async_setup_component(
                self.hass, domain, {domain: [{"platform": name} for name in names]}
            )
        await self.hass.async_block_till_done()

    def _platform(self, integration: str, domain: str) -> Callable[..., Any]:
        async def setup(
            hass: HomeAssistant,
            config: dict[str, Any],
            async_add_entities: Callable[[Iterable[Entity]], None],
            discovery_info: Any = None,
        ) -> None:
            self._adders[(integration, domain)] = async_add_entities

        return setup

    async def add(self, *entities: Entity, integration: str = FAKE) -> None:
        """Add fake devices to Home Assistant."""
        for entity in entities:
            domain = entity.entity_id.split(".", 1)[0]
            self._adders[(integration, domain)]([entity])
        await self.hass.async_block_till_done()

    async def thermostat(self, entity_id: str, **kwargs: Any) -> FakeThermostat:
        """Add a fake thermostat (``integration=FRITZ`` for one of the FRITZ!Box integration)."""
        integration = kwargs.pop("integration", FAKE)
        thermostat = FakeThermostat(entity_id, **kwargs)
        await self.add(thermostat, integration=integration)
        return thermostat

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        """Set the state of a plain entity: a contact, a sensor, a schedule, a tracker."""
        self.hass.states.async_set(entity_id, state, attributes, force_update=True)

    async def settle(self) -> None:
        """Let everything that was triggered finish."""
        await self.hass.async_block_till_done()

    async def advance(
        self, freezer: FrozenDateTimeFactory, delta: timedelta, step: timedelta | None = None
    ) -> None:
        """Move time forward by ``delta``, stopping at every timer on the way.

        Time moves as it would for real: each timer fires at its own time, in order, and what
        it schedules in turn fires too, if it falls before the end. ``step`` additionally caps
        each move, for tests that change things between ticks.
        """
        end = dt_util.utcnow() + delta
        while (now := dt_util.utcnow()) < end:
            tick = end - now
            if step is not None:
                tick = min(tick, step)
            next_timer = self._next_timer()
            if next_timer is not None:
                tick = min(tick, max(next_timer, timedelta(microseconds=1)))
            freezer.tick(tick)
            async_fire_time_changed_exact(self.hass)
            await self.hass.async_block_till_done()

    def _next_timer(self) -> timedelta | None:
        """How far ahead of the simulated time the next scheduled timer is."""
        loop = self.hass.loop
        # The loop runs on the real monotonic clock; the simulated wall clock is ahead of the
        # real one by everything the freezer has ticked so far.
        ahead = dt_util.utcnow().timestamp() - time.time()
        delays = [
            handle.when() - loop.time() - ahead
            for handle in get_scheduled_timer_handles(loop)
            if not handle.cancelled()
        ]
        future = [delay for delay in delays if delay > 0]
        return timedelta(seconds=min(future)) if future else None

    def write_applied(self, config: dict[str, Any] | str | None) -> Path:
        """Write the applied heating.yaml (``None`` removes it)."""
        path = Path(self.hass.config.path("cave", ".applied", "heating.yaml"))
        if config is None:
            path.unlink(missing_ok=True)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        text = config if isinstance(config, str) else yaml.safe_dump(config, sort_keys=False)
        path.write_text(text, encoding="utf-8")
        return path

    async def start(self, config: dict[str, Any] | str | None) -> ConfigEntry:
        """Write the applied file and start cave_heating as configuration.yaml would."""
        await self.hass.async_add_executor_job(self.write_applied, config)
        assert await async_setup_component(self.hass, DOMAIN, {DOMAIN: None})
        await self.hass.async_block_till_done()
        entries = self.hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        return entries[0]

    async def reload(self, config: dict[str, Any] | str | None) -> None:
        """Write a new applied file and call cave_heating.reload."""
        await self.hass.async_add_executor_job(self.write_applied, config)
        await self.hass.services.async_call(DOMAIN, "reload", blocking=True)
        await self.hass.async_block_till_done()

    def state(self, entity_id: str) -> Any:
        """A state that must exist."""
        state = self.hass.states.get(entity_id)
        assert state is not None, f"{entity_id} does not exist"
        return state

    def runtime(self) -> Any:
        """The controller of the loaded config entry."""
        return self.hass.config_entries.async_entries(DOMAIN)[0].runtime_data

    @staticmethod
    def now() -> datetime:
        """The simulated time."""
        return dt_util.utcnow()


def room(room_id: str, *trvs: str | dict[str, Any], **fields: Any) -> dict[str, Any]:
    """A room for heating.yaml."""
    return {
        "id": room_id,
        "name": fields.pop("name", room_id.replace("_", " ").title()),
        "trvs": [trv if isinstance(trv, dict) else {"entity": trv} for trv in trvs],
        **fields,
    }


def config(*rooms: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """A heating.yaml."""
    return {"version": 1, **fields, "rooms": list(rooms)}
