"""A smoke test of a running simulator: ``python -m cave_sim.smoke``.

Used by the pipeline's ``sim`` job against the image and a real Mosquitto. It checks that

* the bridge is online and every Zigbee device of the home has published its discovery messages
  and its availability;
* a command sent the way Home Assistant sends it changes the device, which reports the new state;
* the control API answers and the fake FRITZ!Box accepts the PBKDF2 login.

Configured through the same environment variables as ``python -m cave_sim``. Exits non-zero with a
message on the first failure.
"""

import json
import logging
import os
import sys
import threading
import urllib.parse
import urllib.request
from typing import Any
from xml.etree import ElementTree

from cave_sim.__main__ import Config
from cave_sim.fritz.box import pbkdf2_response
from cave_sim.home import load_home
from cave_sim.mqtt import PahoTransport

_LOGGER = logging.getLogger("cave_sim.smoke")
TIMEOUT = 30.0


class SmokeError(Exception):
    """A check failed."""


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310  (http to the simulator)
        body: bytes = response.read()
    return body.decode()


def check_mqtt(config: Config, host: str) -> None:
    """Discovery, availability, and a command answered with a state report."""
    home = load_home(config.home)
    zigbee = [device for _, device in home.devices() if device.family == "zigbee"]
    discovered: set[str] = set()
    available: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    lock = threading.Condition()

    def on_message(topic: str, payload: bytes) -> None:
        with lock:
            if topic.startswith("homeassistant/") and payload:
                discovered.add(topic.split("/")[2])
            elif topic.endswith("/availability") and json.loads(payload) == {"state": "online"}:
                available.add(topic.split("/")[1])
            elif topic.startswith("zigbee2mqtt/") and topic.count("/") == 1:
                states[topic.split("/", 1)[1]] = json.loads(payload)
            lock.notify_all()

    transport = PahoTransport(
        host,
        config.mqtt_port,
        dispatch=lambda callback: callback(),
        username=config.mqtt_user,
        password=config.mqtt_password,
        client_id="cave-sim-smoke",
    )
    transport.subscribe("homeassistant/+/+/+/config", on_message)
    transport.subscribe("zigbee2mqtt/#", on_message)
    transport.start()
    try:
        expected = {device.ieee_address for device in zigbee}
        names = {device.name for device in zigbee}
        with lock:
            if not lock.wait_for(lambda: expected <= discovered and names <= available, TIMEOUT):
                raise SmokeError(
                    f"missing discovery for {sorted(expected - discovered)}, "
                    f"availability for {sorted(names - available)}"
                )
        thermostat = next(d for d in zigbee if d.model in ("TRVZB", "TRV-ZBT", "BTH-RA"))
        transport.publish(f"zigbee2mqtt/{thermostat.name}/set/occupied_heating_setpoint", "23.5")
        with lock:
            changed = lock.wait_for(
                lambda: states.get(thermostat.name, {}).get("occupied_heating_setpoint") == 23.5,
                TIMEOUT,
            )
        if not changed:
            raise SmokeError(f"{thermostat.name} did not report the new setpoint")
        _LOGGER.info(
            "MQTT: %d devices discovered and online; %s accepted a setpoint",
            len(zigbee),
            thermostat.name,
        )
    finally:
        transport.stop()


def check_http(config: Config, host: str) -> None:
    """The control API's health and a FRITZ!Box login."""
    control = f"http://{host}:{config.control_port}"
    if json.loads(_get(f"{control}/api/health")) != {"status": "ok"}:
        raise SmokeError("the control API is not healthy")
    box = f"http://{host}:{config.fritz_port}/login_sid.lua?version=2"
    # The XML comes from our own simulator.
    challenge = ElementTree.fromstring(_get(box)).findtext("Challenge") or ""  # noqa: S314
    response = pbkdf2_response(challenge, config.fritz_password)
    query = urllib.parse.urlencode({"username": config.fritz_user, "response": response})
    sid = ElementTree.fromstring(_get(f"{box}&{query}")).findtext("SID")  # noqa: S314
    if not sid or sid == "0000000000000000":
        raise SmokeError("the fake FRITZ!Box refused the login")
    _LOGGER.info("HTTP: control API healthy; FRITZ!Box login accepted")


def main() -> int:
    """Run the checks against ``CAVE_SIM_MQTT_HOST`` and ``CAVE_SIM_SMOKE_HTTP_HOST``."""
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env(os.environ)
    http_host = os.environ.get("CAVE_SIM_SMOKE_HTTP_HOST", "127.0.0.1")
    try:
        check_http(config, http_host)
        check_mqtt(config, config.mqtt_host)
    except (SmokeError, OSError) as error:
        _LOGGER.error("Smoke test failed: %s", error)
        return 1
    _LOGGER.info("Smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
