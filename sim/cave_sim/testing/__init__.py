"""Simulated homes inside Home Assistant's test harness.

This is the interface the simulator tests, the demo test (WP6) and the device tests (WP7) use. It
wires a :class:`~cave_sim.runtime.SimulatedHome` into a Home Assistant instance from
``pytest-homeassistant-custom-component``:

* The Zigbee2MQTT stand-in talks to an in-process MQTT broker
  (:class:`~cave_sim.mqtt.InMemoryBroker`). Home Assistant's real MQTT integration is connected to
  the same broker through the test library's mocked paho client, so discovery, state and commands
  flow both ways as with Mosquitto, including retained messages.
* The fake FRITZ!Box serves HTTP on ``127.0.0.1`` (the only address the test library lets sockets
  reach) and Home Assistant's real ``fritzbox`` integration is set up against it with its config
  flow.
* The simulators run on :class:`HassClock`, Home Assistant's clock. Move time with
  :func:`async_advance`, or with ``freezer.tick`` and ``async_fire_time_changed`` yourself.

Usage, with the fixture from :mod:`cave_sim.testing.fixtures` imported into a ``conftest.py``::

    async def test_window(hass, simulated_home, freezer):
        sim = await simulated_home(DEMO_HOME)
        sim.home.set_window("wohnzimmer_fenster", True)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.wohnzimmer_fenster_contact").state == "on"

Without the fixture, call :func:`async_start_simulated_home` with the ``mqtt_client_mock`` and
``mqtt_mock_entry`` fixtures and with sockets enabled (the ``socket_enabled`` fixture), and await
:meth:`SimulatedHomeHandle.async_stop` at the end.

This module imports Home Assistant and its test library. Nothing in the simulator's runtime path
(``python -m cave_sim``) imports it.
"""

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from unittest.mock import Mock

from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockMqttReasonCode,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from cave_sim.clock import TimerHandle
from cave_sim.fritz import server as fritz_server
from cave_sim.home import HomeSpec, load_home
from cave_sim.mqtt import InMemoryBroker
from cave_sim.runtime import SimulatedHome

DEMO_HOME: Final = Path(__file__).resolve().parents[2] / "demo" / "home.yaml"
HOME_ASSISTANT_CLIENT: Final = "homeassistant"
SIMULATOR_CLIENT: Final = "cave-sim"
# Obviously fake credentials for the fake FRITZ!Box.
FRITZ_USERNAME: Final = "cave-test"
FRITZ_PASSWORD: Final = "not-a-real-password"  # noqa: S105


class HassClock:
    """Home Assistant's clock: ``dt_util.utcnow()`` and the event loop's timers.

    Timers scheduled here fire when the tests move time the way Home Assistant's own tests do, with
    ``freezer.tick`` and ``async_fire_time_changed``.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._handles: list[asyncio.TimerHandle] = []

    def now(self) -> float:
        """Home Assistant's current time."""
        return dt_util.utcnow().timestamp()

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle:
        """Run ``callback`` on the event loop ``delay`` seconds from now."""
        self._handles = [handle for handle in self._handles if not handle.cancelled()]
        handle = self._hass.loop.call_later(max(delay, 0.0), callback)
        self._handles.append(handle)
        return handle

    def cancel_all(self) -> None:
        """Cancel every timer still pending, so that none lingers after the test."""
        for handle in self._handles:
            handle.cancel()
        self._handles.clear()


def hass_dispatcher(hass: HomeAssistant) -> Callable[[Callable[[], None]], None]:
    """Deliver broker messages as tracked tasks, so ``async_block_till_done`` waits for them."""

    async def run(callback: Callable[[], None]) -> None:
        callback()

    def dispatch(callback: Callable[[], None]) -> None:
        hass.async_create_task(run(callback), eager_start=False)

    return dispatch


@dataclass
class _PublishInfo:
    mid: int
    rc: int = 0


def connect_hass_to_broker(
    hass: HomeAssistant, broker: InMemoryBroker, mqtt_client_mock: Any
) -> None:
    """Route Home Assistant's mocked paho client through ``broker``.

    ``mqtt_client_mock`` is the test library's fixture of that name. Home Assistant's subscriptions
    become broker subscriptions (and receive retained messages, as from a real broker), its
    publications go to the broker, and the broker's deliveries are fed into Home Assistant's MQTT
    client. Call before setting up the MQTT config entry.
    """

    def deliver(topic: str, payload: bytes, qos: int, retain: bool) -> None:
        async_fire_mqtt_message(hass, topic, payload, qos, retain)

    broker.connect(HOME_ASSISTANT_CLIENT, deliver)
    original_subscribe = mqtt_client_mock.subscribe.side_effect
    original_unsubscribe = mqtt_client_mock.unsubscribe.side_effect
    mids = itertools.count(1_000_000)

    def topics(topic_or_list: Any) -> list[str]:
        if isinstance(topic_or_list, str):
            return [topic_or_list]
        return [item[0] if isinstance(item, tuple) else item for item in topic_or_list]

    def subscribe(topic_or_list: Any, qos: int = 0, **kwargs: Any) -> Any:
        result = original_subscribe(topic_or_list, qos, **kwargs)
        for topic in topics(topic_or_list):
            broker.subscribe(HOME_ASSISTANT_CLIENT, topic)
        return result

    def unsubscribe(topic_or_list: Any, **kwargs: Any) -> Any:
        result = original_unsubscribe(topic_or_list, **kwargs)
        for topic in topics(topic_or_list):
            broker.unsubscribe(HOME_ASSISTANT_CLIENT, topic)
        return result

    def publish(
        topic: str, payload: Any = None, qos: int = 0, retain: bool = False, properties: Any = None
    ) -> _PublishInfo:
        broker.publish(HOME_ASSISTANT_CLIENT, topic, payload or b"", qos=qos, retain=retain)
        mid = next(mids)
        hass.loop.call_soon(mqtt_client_mock.on_publish, Mock(), 0, mid, MockMqttReasonCode(), None)
        return _PublishInfo(mid)

    mqtt_client_mock.subscribe.side_effect = subscribe
    mqtt_client_mock.unsubscribe.side_effect = unsubscribe
    mqtt_client_mock.publish.side_effect = publish


class MqttEntrySetup(Protocol):
    """The ``mqtt_mock_entry`` fixture: sets up Home Assistant's MQTT integration."""

    def __call__(self) -> Awaitable[Any]:
        """Set up the MQTT config entry."""


@dataclass
class SimulatedHomeHandle:
    """A running simulated home inside a Home Assistant test."""

    hass: HomeAssistant
    home: SimulatedHome
    broker: InMemoryBroker
    clock: HassClock
    mqtt_client_mock: Any
    fritz_url: str
    fritz_username: str = FRITZ_USERNAME
    fritz_password: str = FRITZ_PASSWORD
    fritz_entry: ConfigEntry | None = None
    _runner: web.AppRunner | None = field(default=None, repr=False)

    async def async_setup_fritzbox(self) -> ConfigEntry:
        """Set up Home Assistant's ``fritzbox`` integration with its config flow, once."""
        if self.fritz_entry is not None:
            return self.fritz_entry
        result = await self.hass.config_entries.flow.async_init(
            "fritzbox",
            context={"source": "user"},
            data={
                "url": self.fritz_url,
                "username": self.fritz_username,
                "password": self.fritz_password,
                "verify_ssl": False,
            },
        )
        if result.get("type") != "create_entry":
            raise RuntimeError(f"fritzbox config flow did not create an entry: {result}")
        await self.hass.async_block_till_done()
        self.fritz_entry = result["result"]
        return self.fritz_entry

    async def async_refresh_fritzbox(self) -> None:
        """Make Home Assistant poll the FRITZ!Box now instead of waiting for its 30 s interval."""
        if self.fritz_entry is None:
            raise RuntimeError("the fritzbox integration is not set up")
        await self.fritz_entry.runtime_data.async_refresh()
        await self.hass.async_block_till_done()

    async def async_flush_subscriptions(self) -> None:
        """Let Home Assistant send the MQTT subscriptions it has queued, and wait for the replies.

        Home Assistant's MQTT client subscribes after a cooldown of 0.5 s of real time; only then
        does the broker deliver retained messages (discovery, availability) to it, as a real broker
        would. Rather than wait, this runs the client's subscribe job at once (its debouncer,
        ``homeassistant/components/mqtt/util.py`` ``EnsureJobAfterCooldown`` in 2026.10.4), repeated
        until discovery has stopped creating entities that subscribe to further topics. Call it
        after anything that makes Home Assistant subscribe, such as reloading the MQTT entry.
        """
        mqtt_client = self.hass.data["mqtt"].client
        client = getattr(mqtt_client, "_mock_wraps", None) or mqtt_client
        for _ in range(10):
            before = self.broker.subscriptions(HOME_ASSISTANT_CLIENT)
            await client._subscribe_debouncer.async_execute()
            await self.hass.async_block_till_done()
            if self.broker.subscriptions(HOME_ASSISTANT_CLIENT) == before:
                return
        raise RuntimeError("Home Assistant kept subscribing to new MQTT topics")

    async def async_stop(self) -> None:
        """Stop the simulators, their timers and the fake FRITZ!Box's HTTP server.

        Also closes the mocked MQTT connection's socket, which stops the MQTT client's periodic
        timer; the test library's mock opens that socket but never closes it.
        """
        self.home.stop()
        self.clock.cancel_all()
        on_socket_close = getattr(self.mqtt_client_mock, "on_socket_close", None)
        if callable(on_socket_close) and self.hass.data.get("mqtt") is not None:
            on_socket_close(self.mqtt_client_mock, None, Mock(fileno=Mock(return_value=-1)))
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def async_start_simulated_home(
    hass: HomeAssistant,
    home: HomeSpec | Path | str,
    *,
    mqtt_client_mock: Any,
    mqtt_mock_entry: MqttEntrySetup,
    setup_fritzbox: bool = True,
) -> SimulatedHomeHandle:
    """Start a simulated home and connect Home Assistant's MQTT and FRITZ! integrations to it.

    ``home`` is a :class:`~cave_sim.home.HomeSpec` or the path of a ``home.yaml``. The MQTT
    integration is set up through ``mqtt_mock_entry``; the ``fritzbox`` integration is set up when
    the home has a FRITZ! thermostat and ``setup_fritzbox`` is true. When this returns, every
    entity exists and shows the devices' current state: the states are published once more after
    Home Assistant has processed discovery, as Zigbee2MQTT does 30 s after Home Assistant's birth
    message.
    """
    spec = home if isinstance(home, HomeSpec) else load_home(Path(home))
    broker = InMemoryBroker(hass_dispatcher(hass))
    connect_hass_to_broker(hass, broker, mqtt_client_mock)
    clock = HassClock(hass)
    simulated = SimulatedHome(
        spec,
        clock,
        broker.client(SIMULATOR_CLIENT),
        fritz_username=FRITZ_USERNAME,
        fritz_password=FRITZ_PASSWORD,
    )
    runner = web.AppRunner(fritz_server.create_app(simulated.fritzbox), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    handle = SimulatedHomeHandle(
        hass,
        simulated,
        broker,
        clock,
        mqtt_client_mock,
        fritz_url=f"http://127.0.0.1:{port}",
        _runner=runner,
    )
    try:
        simulated.start()
        await mqtt_mock_entry()
        await hass.async_block_till_done()
        await handle.async_flush_subscriptions()
        simulated.zigbee.republish_states()
        await hass.async_block_till_done()
        if setup_fritzbox and simulated.fritzbox.thermostats:
            await handle.async_setup_fritzbox()
    except BaseException:
        await handle.async_stop()
        raise
    return handle


async def async_advance(
    hass: HomeAssistant, freezer: Any, seconds: float, step: float = 60.0
) -> None:
    """Move Home Assistant's and the simulators' time forward by ``seconds``, in ``step`` steps.

    ``freezer`` is the ``freezer`` fixture. Each step fires every timer that has fallen due, so
    periodic work (the room model, reporting, the FRITZ! integration's polling) runs once per step.
    """
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        freezer.tick(timedelta(seconds=delta))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= delta
