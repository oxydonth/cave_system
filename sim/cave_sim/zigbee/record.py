"""Record a real device's messages into a fixture: ``python -m cave_sim.zigbee.record``.

Run it against the broker of a real Zigbee2MQTT with Home Assistant discovery enabled, for one
paired device::

    python -m cave_sim.zigbee.record --host 127.0.0.1 --username cave \\
        --password-env CAVE_MQTT_PASSWORD \\
        --friendly-name "Wohnzimmer Heizung Fenster" --model TRV-ZBT --seconds 120

It collects the device's retained discovery messages and the last state message Zigbee2MQTT
publishes for it within ``--seconds`` (touch the device, or publish ``{"state": ""}`` to its
``/get`` topic, to make it report), replaces the device's name and addresses by placeholders, and
writes ``fixtures/<model>.json`` with a ``source`` that starts with ``recorded``. From then on the
simulator publishes the recorded messages for that model, and ``python -m cave_sim.zigbee.fixtures``
leaves the file alone. The friendly name must contain a capital letter or a space (see
:class:`cave_sim.zigbee.fixtures.Identity`).
"""

import argparse
import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cave_sim.mqtt import MqttTransport, PahoTransport
from cave_sim.zigbee.fixtures import FIXTURE_DIR, Fixture, Identity, to_placeholders
from cave_sim.zigbee.models import MODELS

_LOGGER = logging.getLogger(__name__)


class Recorder:
    """Collects one device's messages from a transport."""

    def __init__(
        self,
        transport: MqttTransport,
        friendly_name: str,
        *,
        base_topic: str = "zigbee2mqtt",
        discovery_topic: str = "homeassistant",
    ) -> None:
        self.friendly_name = friendly_name
        self.discovery: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] | None = None
        self.state_received = threading.Event()
        self._lock = threading.Lock()
        self._discovery_topic = discovery_topic
        transport.subscribe(f"{discovery_topic}/+/+/+/config", self._on_discovery)
        transport.subscribe(f"{base_topic}/{friendly_name}", self._on_state)

    def _on_discovery(self, topic: str, payload: bytes) -> None:
        if not payload:
            return
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if (
            not isinstance(message, dict)
            or message.get("device", {}).get("name") != self.friendly_name
        ):
            return
        with self._lock:
            self.discovery[topic.removeprefix(f"{self._discovery_topic}/")] = message

    def _on_state(self, _topic: str, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if isinstance(message, dict):
            with self._lock:
                self.state = message
            self.state_received.set()

    def identity(self) -> Identity:
        """The device's and the coordinator's IEEE addresses, read from the discovery messages."""
        with self._lock:
            if not self.discovery:
                raise LookupError(f"no discovery message for {self.friendly_name!r}")
            topic, message = next(iter(self.discovery.items()))
        ieee = topic.split("/")[1]
        via = str(message.get("device", {}).get("via_device", ""))
        coordinator = via.removeprefix("zigbee2mqtt_bridge_")
        return Identity(self.friendly_name, ieee, coordinator)

    def fixture(self, model: str, origin: str) -> Fixture:
        """What was recorded, as a fixture of ``model``."""
        identity = self.identity()
        with self._lock:
            discovery = dict(self.discovery)
            state = dict(self.state or {})
        return Fixture(
            model=model,
            source=f"recorded: {origin}",
            discovery=to_placeholders(discovery, identity),
            state=to_placeholders(state, identity),
        )


def main(argv: list[str]) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m cave_sim.zigbee.record", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username")
    parser.add_argument("--password-env", help="name of the variable holding the broker password")
    parser.add_argument("--friendly-name", required=True)
    parser.add_argument("--model", required=True, choices=sorted(MODELS))
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--output-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    password = os.environ.get(args.password_env) if args.password_env else None
    transport = PahoTransport(
        args.host,
        args.port,
        dispatch=lambda callback: callback(),
        username=args.username,
        password=password,
        client_id="cave-sim-recorder",
    )
    recorder = Recorder(transport, args.friendly_name)
    transport.start()
    if not transport.connected.wait(timeout=10):
        _LOGGER.error("Could not connect to %s:%s", args.host, args.port)
        return 1
    recorder.state_received.wait(timeout=args.seconds)
    transport.stop()

    if not recorder.discovery:
        _LOGGER.error(
            "No discovery messages for %r: is Home Assistant discovery on?", args.friendly_name
        )
        return 1
    if recorder.state is None:
        _LOGGER.warning("No state message within %s s; the fixture's state is empty", args.seconds)
    origin = f"{args.host} on {datetime.now(tz=UTC).date().isoformat()}"
    fixture = recorder.fixture(args.model, origin)
    path = args.output_dir / f"{args.model}.json"
    path.write_text(fixture.to_json(), encoding="utf-8")
    _LOGGER.info("Wrote %s (%d discovery messages)", path, len(fixture.discovery))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
