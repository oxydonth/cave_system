"""The narrow interface between the simulator logic and MQTT.

The Zigbee2MQTT stand-in only ever publishes and subscribes. :class:`MqttTransport` is that
interface; two implementations exist:

* :class:`InMemoryBroker` with its :class:`BrokerClient` — a small broker inside the process, with
  retained messages and topic wildcards. The simulator tests use it directly, and
  ``cave_sim.testing`` connects Home Assistant's MQTT integration to it.
* :class:`PahoTransport` — a real broker through ``paho-mqtt``, used by ``python -m cave_sim``.
"""

import functools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)

type MessageHandler = Callable[[str, bytes], None]


class MqttTransport(Protocol):
    """Publish and subscribe; nothing else."""

    def publish(
        self, topic: str, payload: bytes | str, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Publish a message."""

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Call ``handler(topic, payload)`` for every message matching ``topic_filter``."""


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Return whether ``topic`` matches ``topic_filter`` with MQTT's ``+`` and ``#`` wildcards."""
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    for index, level in enumerate(filter_levels):
        if level == "#":
            return True
        if index >= len(topic_levels):
            return False
        if level not in ("+", topic_levels[index]):
            return False
    return len(filter_levels) == len(topic_levels)


def _as_bytes(payload: bytes | str) -> bytes:
    return payload.encode() if isinstance(payload, str) else payload


@dataclass(frozen=True)
class Message:
    """A message as the broker saw it."""

    topic: str
    payload: bytes
    qos: int
    retain: bool
    sender: str


type Delivery = Callable[[str, bytes, int, bool], None]
type Dispatcher = Callable[[Callable[[], None]], None]


def _run_now(callback: Callable[[], None]) -> None:
    callback()


class InMemoryBroker:
    """An MQTT broker inside the process.

    Each client receives one copy of a message however many of its subscriptions match, as with a
    real broker. Retained messages are stored and delivered to later subscribers with the retain
    flag set; an empty retained message deletes the stored one. ``dispatch`` decides when a delivery
    runs: at once by default, or, for example, as a task on Home Assistant's event loop so that
    publishing never re-enters the publisher.
    """

    def __init__(self, dispatch: Dispatcher = _run_now) -> None:
        self._dispatch = dispatch
        self._clients: dict[str, Delivery] = {}
        self._filters: dict[str, list[str]] = {}
        self.retained: dict[str, Message] = {}
        self.log: list[Message] = []

    def client(self, client_id: str) -> BrokerClient:
        """Return a client of this broker implementing :class:`MqttTransport`."""
        return BrokerClient(self, client_id)

    def connect(self, client_id: str, deliver: Delivery) -> None:
        """Register a client and the function that receives its messages."""
        self._clients[client_id] = deliver
        self._filters.setdefault(client_id, [])

    def publish(
        self,
        sender: str,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        """Publish ``payload`` on ``topic`` from client ``sender``."""
        message = Message(topic, _as_bytes(payload), qos, retain, sender)
        self.log.append(message)
        if retain:
            if message.payload:
                self.retained[topic] = message
            else:
                self.retained.pop(topic, None)
        for client_id in self.subscribers(topic):
            self._deliver(self._clients[client_id], message, retain=False)

    def subscribe(
        self, client_id: str, topic_filter: str, retained_to: Delivery | None = None
    ) -> None:
        """Add a subscription and deliver the matching retained messages.

        Retained messages go to ``retained_to`` when given, otherwise to the client.
        """
        self._filters.setdefault(client_id, []).append(topic_filter)
        deliver = retained_to or self._clients[client_id]
        for message in list(self.retained.values()):
            if topic_matches(topic_filter, message.topic):
                self._deliver(deliver, message, retain=True)

    def unsubscribe(self, client_id: str, topic_filter: str) -> None:
        """Remove a client's subscriptions for ``topic_filter``."""
        self._filters[client_id] = [
            flt for flt in self._filters.get(client_id, []) if flt != topic_filter
        ]

    def subscriptions(self, client_id: str) -> list[str]:
        """The topic filters ``client_id`` is subscribed to."""
        return list(self._filters.get(client_id, []))

    def subscribers(self, topic: str) -> list[str]:
        """Return the clients that receive a message on ``topic``."""
        return [
            client_id
            for client_id, filters in self._filters.items()
            if client_id in self._clients and any(topic_matches(flt, topic) for flt in filters)
        ]

    def messages(self, topic_filter: str = "#") -> list[Message]:
        """Return every message published so far whose topic matches ``topic_filter``."""
        return [message for message in self.log if topic_matches(topic_filter, message.topic)]

    def _deliver(self, deliver: Delivery, message: Message, *, retain: bool) -> None:
        self._dispatch(lambda: deliver(message.topic, message.payload, message.qos, retain))


class BrokerClient:
    """A client of :class:`InMemoryBroker`; implements :class:`MqttTransport`."""

    def __init__(self, broker: InMemoryBroker, client_id: str) -> None:
        self._broker = broker
        self.client_id = client_id
        self._handlers: list[tuple[str, MessageHandler]] = []
        broker.connect(client_id, self._deliver)

    def publish(
        self, topic: str, payload: bytes | str, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Publish through the broker."""
        self._broker.publish(self.client_id, topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Subscribe through the broker; retained messages go to this handler only."""
        self._handlers.append((topic_filter, handler))

        def retained(topic: str, payload: bytes, _qos: int, _retain: bool) -> None:
            handler(topic, payload)

        self._broker.subscribe(self.client_id, topic_filter, retained)

    def _deliver(self, topic: str, payload: bytes, _qos: int, _retain: bool) -> None:
        for topic_filter, handler in list(self._handlers):
            if topic_matches(topic_filter, topic):
                handler(topic, payload)


class PahoTransport:
    """A connection to a real broker through ``paho-mqtt``.

    Messages arrive on paho's network thread and are handed to ``dispatch``, which must move them
    onto the simulator's own thread (``python -m cave_sim`` uses ``loop.call_soon_threadsafe``).
    Subscriptions are renewed after every reconnect.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        dispatch: Dispatcher,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "cave-sim",
        will: tuple[str, str] | None = None,
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        # Imported here so that the in-process simulators do not need paho at all.
        import paho.mqtt.client as paho

        self._dispatch = dispatch
        self._on_connected = on_connected
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self._client: Any = paho.Client(
            paho.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=paho.MQTTv311
        )
        if username:
            self._client.username_pw_set(username, password)
        if will is not None:
            self._client.will_set(will[0], will[1], qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._host = host
        self._port = port

    def start(self) -> None:
        """Connect in the background; paho reconnects by itself."""
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self, farewell: tuple[str, str] | None = None) -> None:
        """Publish ``farewell`` (topic, payload; retained) if connected, then disconnect."""
        if farewell is not None and self.connected.is_set():
            info = self._client.publish(farewell[0], farewell[1], qos=1, retain=True)
            info.wait_for_publish(timeout=2)
        self._client.disconnect()
        self._client.loop_stop()

    def publish(
        self, topic: str, payload: bytes | str, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Publish; paho queues the message while disconnected."""
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        """Subscribe now if connected, and again after every reconnect."""
        with self._lock:
            self._handlers.append((topic_filter, handler))
        if self.connected.is_set():
            self._client.subscribe(topic_filter, qos=1)

    def _on_connect(
        self, client: Any, _userdata: Any, _flags: Any, reason: Any, _props: Any
    ) -> None:
        if getattr(reason, "is_failure", False):
            _LOGGER.warning("MQTT connection refused: %s", reason)
            return
        _LOGGER.info("Connected to MQTT broker %s:%s", self._host, self._port)
        with self._lock:
            filters = sorted({flt for flt, _ in self._handlers})
        for topic_filter in filters:
            client.subscribe(topic_filter, qos=1)
        self.connected.set()
        if self._on_connected is not None:
            self._dispatch(self._on_connected)

    def _on_disconnect(self, *_args: Any) -> None:
        self.connected.clear()
        _LOGGER.warning("Disconnected from MQTT broker; paho reconnects")

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        topic: str = message.topic
        payload: bytes = bytes(message.payload)
        with self._lock:
            handlers = [handler for flt, handler in self._handlers if topic_matches(flt, topic)]
        for handler in handlers:
            self._dispatch(functools.partial(handler, topic, payload))
