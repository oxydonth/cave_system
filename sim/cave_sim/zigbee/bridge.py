"""The Zigbee2MQTT stand-in: the MQTT side of Zigbee2MQTT 2.14.3, with simulated devices behind it.

What it publishes, and when, follows Zigbee2MQTT 2.14.3:

* ``<base>/bridge/state`` ``{"state":"online"}``, retained, at start; ``offline`` is the last will
  (``lib/mqtt.ts``).
* ``<base>/bridge/info``, retained (a subset of the real message, see ``sim/README.md``).
* Discovery messages, retained, below the discovery prefix (``lib/extension/homeassistant.ts``).
* ``<base>/<name>/availability`` ``{"state":"online"|"offline"}``, retained
  (``lib/extension/availability.ts``). All our devices run on batteries, which Zigbee2MQTT treats
  as "passive": a device is available while it was last heard less than
  ``availability.passive.timeout`` ago (1500 minutes by default, and in this project's
  configuration); every message from it makes it available again. A device that answers counts as
  heard all the time, as real devices check in regularly; a silent one turns unavailable the
  timeout after its last message. The timeout runs on the simulator's clock.
* ``<base>/<name>``: the device's whole cached state with ``last_seen`` and ``linkquality``, not
  retained, on every change (``lib/controller.ts`` ``publishEntityState`` with ``cache_state``).
* 30 seconds after Home Assistant announces itself on ``<discovery>/status`` with ``online``, the
  bridge state and every device's cached state again (``onMQTTMessage`` in homeassistant.ts).

It accepts ``<base>/<name>/set`` with a JSON object, ``<base>/<name>/set/<property>`` with a bare
value, the same for ``get`` (``lib/extension/publish.ts``), and answers a few bridge requests.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Final

from cave_sim.clock import Clock, Timer
from cave_sim.mqtt import MqttTransport
from cave_sim.zigbee.devices import ZigbeeDevice
from cave_sim.zigbee.discovery import ZIGBEE2MQTT_VERSION, Settings, bridge_messages, serialize

_LOGGER = logging.getLogger(__name__)

HOME_ASSISTANT_REPUBLISH_DELAY: Final = 30.0
STATE_VALUES: Final = ("on", "off", "toggle", "open", "close", "stop", "lock", "unlock")


def format_last_seen(timestamp: float, setting: str) -> str | float:
    """``formatDate`` in Zigbee2MQTT's ``lib/util/utils.ts`` for ``ISO_8601`` and ``epoch``."""
    if setting == "epoch":
        return round(timestamp * 1000)
    moment = datetime.fromtimestamp(timestamp, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class Zigbee2Mqtt:
    """Simulated Zigbee2MQTT with its devices."""

    def __init__(
        self, transport: MqttTransport, clock: Clock, settings: Settings | None = None
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.settings = settings or Settings()
        self.devices: dict[str, ZigbeeDevice] = {}
        self.log_level = "info"
        self.permit_join = False
        self._republish = Timer(clock, self.republish_states)
        self.passive_timeout = self.settings.passive_timeout
        self._last_heard: dict[str, float] = {}
        self._available: dict[str, bool] = {}
        self._availability_timers: dict[str, Timer] = {}
        base = re.escape(self.settings.base_topic)
        self._set_get = re.compile(rf"^{base}/(?!bridge)(.+?)/(get|set)(?:/(.+))?$")

    # --- set-up -----------------------------------------------------------------------------

    def add(self, device: ZigbeeDevice) -> None:
        """Add a device; call before :meth:`start`."""
        self.devices[device.friendly_name] = device

    def start(self) -> None:
        """Come online: subscribe, then publish the bridge, discovery, availability and states."""
        base = self.settings.base_topic
        self.transport.subscribe(f"{base}/#", self._on_message)
        self.transport.subscribe(f"{self.settings.discovery_topic}/status", self._on_ha_status)
        self.transport.publish(f"{base}/bridge/state", '{"state":"online"}', qos=1, retain=True)
        self.publish_bridge_info()
        for topic, payload in bridge_messages(self.settings).items():
            self._publish_discovery(topic, payload)
        for device in self.devices.values():
            for topic, payload in device.discovery_messages().items():
                self._publish_discovery(topic, payload)
        for device in self.devices.values():
            device.prime()
            self._last_heard[device.friendly_name] = self.clock.now()
            self._arm(device)
            self.publish_availability(device, True)
            self.publish_entity_state(device)

    def _publish_discovery(self, topic: str, payload: dict[str, Any]) -> None:
        self.transport.publish(
            f"{self.settings.discovery_topic}/{topic}", serialize(payload), qos=1, retain=True
        )

    def publish_bridge_info(self) -> None:
        """``<base>/bridge/info`` with the fields Home Assistant's bridge entities read."""
        info = {
            "version": ZIGBEE2MQTT_VERSION,
            "coordinator": {
                "ieee_address": self.settings.coordinator_ieee,
                "type": "EmberZNet",
                "meta": {"revision": "8.0.2.0"},
            },
            "log_level": self.log_level,
            "permit_join": self.permit_join,
            "restart_required": False,
        }
        self.transport.publish(
            f"{self.settings.base_topic}/bridge/info", serialize(info), qos=1, retain=True
        )

    # --- publishing -------------------------------------------------------------------------

    def publish_entity_state(
        self, device: ZigbeeDevice, changes: dict[str, Any] | None = None
    ) -> None:
        """Merge ``changes`` into Zigbee2MQTT's cache and publish the device's whole state."""
        if changes:
            device.cache.update(changes)
        message = dict(device.cache)
        if self.settings.last_seen != "disable":
            last_seen = self._last_heard.get(device.friendly_name, self.clock.now())
            message["last_seen"] = format_last_seen(last_seen, self.settings.last_seen)
        message["linkquality"] = device.linkquality
        if device.model.ota and (message.get("update") or {}).get("latest_version") is None:
            # adjustMessageBeforePublish in homeassistant.ts
            message["update"] = {
                **(message.get("update") or {}),
                "installed_version": -1,
                "latest_version": -1,
            }
        self.transport.publish(
            f"{self.settings.base_topic}/{device.friendly_name}", serialize(message)
        )

    def publish_availability(self, device: ZigbeeDevice, available: bool) -> None:
        """``<base>/<name>/availability``, retained, when it changed (``publishAvailability``)."""
        name = device.friendly_name
        if self._available.get(name) == available:
            return
        self._available[name] = available
        self.transport.publish(
            f"{self.settings.base_topic}/{name}/availability",
            serialize({"state": "online" if available else "offline"}),
            qos=1,
            retain=True,
        )

    def available(self, device: ZigbeeDevice) -> bool:
        """Whether Zigbee2MQTT shows the device as available."""
        return self._available.get(device.friendly_name, True)

    def device_heard(self, device: ZigbeeDevice, *, publish: bool = True) -> None:
        """A message from the device arrived (``onLastSeenChanged``)."""
        self._last_heard[device.friendly_name] = self.clock.now()
        self._arm(device)
        if publish:
            self.publish_availability(device, True)

    def make_unavailable(self, device: ZigbeeDevice) -> None:
        """Show the device as unavailable at once, as if its timeout had passed."""
        self.publish_availability(device, False)

    def set_passive_timeout(self, seconds: float) -> None:
        """Change ``availability.passive.timeout``; it applies from each device's last message."""
        self.passive_timeout = seconds
        for device in self.devices.values():
            self._check_availability(device)

    def _arm(self, device: ZigbeeDevice) -> None:
        """``resetTimer`` for a passive device: check again one second after the timeout."""
        name = device.friendly_name
        timer = self._availability_timers.get(name)
        if timer is None:
            timer = Timer(self.clock, lambda: self._check_availability(device))
            self._availability_timers[name] = timer
        remaining = self._last_heard[name] + self.passive_timeout - self.clock.now()
        timer.start(max(remaining, 0.0) + 1.0)

    def _check_availability(self, device: ZigbeeDevice) -> None:
        name = device.friendly_name
        if device.answering:
            # A device that answers is heard regularly (check-ins, reports), so it never times out.
            self._last_heard[name] = max(self._last_heard[name], self.clock.now() - 1.0)
        if self.clock.now() - self._last_heard[name] >= self.passive_timeout:
            self.publish_availability(device, False)
        else:
            self._arm(device)

    def republish_states(self) -> None:
        """What Zigbee2MQTT does 30 s after Home Assistant came online."""
        self.transport.publish(
            f"{self.settings.base_topic}/bridge/state", '{"state":"online"}', qos=1, retain=True
        )
        for device in self.devices.values():
            self.publish_entity_state(device)

    # --- receiving --------------------------------------------------------------------------

    def _on_ha_status(self, _topic: str, payload: bytes) -> None:
        if payload.decode(errors="replace").lower() == "online":
            self._republish.start(HOME_ASSISTANT_REPUBLISH_DELAY)

    def _on_message(self, topic: str, payload: bytes) -> None:
        bridge_request = f"{self.settings.base_topic}/bridge/request/"
        if topic.startswith(bridge_request):
            self._on_bridge_request(topic.removeprefix(bridge_request), payload)
            return
        match = self._set_get.match(topic)
        if match is None:
            return
        name, kind, attribute = match.groups()
        device = self.devices.get(name)
        if device is None:
            _LOGGER.error("Entity '%s' is unknown", name)
            return
        message = self._parse(payload.decode(errors="replace"), attribute)
        if message is None:
            _LOGGER.error("Invalid message '%s', skipping...", payload)
            return
        if kind == "set":
            device.handle_set(message)
        else:
            device.handle_get(message)

    @staticmethod
    def _parse(text: str, attribute: str | None) -> dict[str, Any] | None:
        """``parseMessage`` in publish.ts."""
        if attribute:
            try:
                return {attribute: json.loads(text)}
            except ValueError:
                return {attribute: text}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"state": text} if text.lower() in STATE_VALUES else None
        return parsed if isinstance(parsed, dict) else None

    def _on_bridge_request(self, request: str, payload: bytes) -> None:
        try:
            message = json.loads(payload) if payload else {}
        except ValueError:
            message = {}
        if not isinstance(message, dict):
            message = {"value": message}
        response: dict[str, Any]
        if request == "permit_join":
            self.permit_join = bool(message.get("time", message.get("value", 0)))
            response = {"data": {"time": message.get("time", 0)}, "status": "ok"}
            self.publish_bridge_info()
        elif request == "restart":
            response = {"data": {}, "status": "ok"}
            self.republish_states()
        elif request == "options":
            level = message.get("options", {}).get("advanced", {}).get("log_level")
            if isinstance(level, str):
                self.log_level = level
            response = {"data": {"restart_required": False}, "status": "ok"}
            self.publish_bridge_info()
        else:
            response = {
                "data": {},
                "status": "error",
                "error": f"Request '{request}' is not supported by the simulator",
            }
        if "transaction" in message:
            response["transaction"] = message["transaction"]
        self.transport.publish(
            f"{self.settings.base_topic}/bridge/response/{request}", serialize(response)
        )
