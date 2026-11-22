"""What the real Zigbee2MQTT does with the environment compose.yaml gives it (issue #21).

Zigbee2MQTT 2.14.3 applies its ``ZIGBEE2MQTT_CONFIG_*`` variables by writing them into its own
``configuration.yaml`` at every start (``lib/util/onboarding.ts`` calls ``settings.write()``, which
applies them in ``lib/util/settings.ts``). That file is state: it holds the network key and the
paired devices. These tests start the pinned image from ``compose.yaml`` with its environment on a
configuration like one on a running server, and check that the only changes are the keys the
environment names, ``device_options`` among them. Zigbee2MQTT then fails to reach its coordinator
and stops, which does not matter here: the file is written before it tries.

They need Docker and pull the image, so they run in the pipeline and are skipped where Docker is
missing, as on the development machine.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
TIMEOUT = 300  # seconds for one start, the image pull included
# What `./cave init` and the owner would put into .env; values only for this test.
ENVIRONMENT = {
    "CAVE_MQTT_USER": "cave",
    "CAVE_MQTT_PASSWORD": "test-password",
    "CAVE_Z2M_FRONTEND_TOKEN": "test-token",
    "CAVE_Z2M_SERIAL_PORT": "tcp://127.0.0.1:6638",
    "CAVE_Z2M_ADAPTER": "ember",
    "TZ": "Europe/Berlin",
}
# A configuration as Zigbee2MQTT leaves it on a server after its first start and some pairing.
NETWORK_KEY = [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 13]
DEVICES = {
    "0x00158d0000000103": {"friendly_name": "Wohnzimmer"},
    "0x00158d0000000101": {"friendly_name": "Wohnzimmer Heizung Fenster", "retain": False},
}


def _docker() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason="needs Docker (runs in the pipeline)")


def _interpolate(value: str) -> str:
    """Compose's ``${VAR}``, ``${VAR:-default}`` and ``${VAR:?message}`` with ENVIRONMENT."""

    def replace(match: re.Match[str]) -> str:
        name, operator, rest = match.group(1), match.group(2), match.group(3)
        if name in ENVIRONMENT and ENVIRONMENT[name]:
            return ENVIRONMENT[name]
        if operator == ":-":
            return rest or ""
        raise AssertionError(f"{name} is required by compose.yaml")

    return re.sub(r"\$\{([A-Z0-9_]+)(?:(:-|:\?)([^}]*))?\}", replace, value)


def _service() -> tuple[str, dict[str, str]]:
    compose = yaml.safe_load((REPOSITORY / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["zigbee2mqtt"]
    environment = {key: _interpolate(str(value)) for key, value in service["environment"].items()}
    return service["image"], environment


def _configuration() -> dict[str, Any]:
    template = yaml.safe_load(
        (REPOSITORY / "zigbee2mqtt" / "configuration.template.yaml").read_text(encoding="utf-8")
    )
    template["advanced"] |= {
        "network_key": NETWORK_KEY,
        "pan_id": 6754,
        "ext_pan_id": [221, 221, 221, 221, 221, 221, 221, 221],
    }
    template["devices"] = copy.deepcopy(DEVICES)
    return dict(template)


def _start(data: Path) -> None:
    """Start Zigbee2MQTT once on ``data`` with compose.yaml's image and environment."""
    image, environment = _service()
    name = f"cave-z2m-settings-{uuid.uuid4().hex[:8]}"
    command = ["docker", "run", "--rm", "--name", name, "--network", "none"]
    command += ["-v", f"{data}:/app/data"]
    for key, value in {**environment, "Z2M_ONBOARD_NO_SERVER": "1"}.items():
        command += ["-e", f"{key}={value}"]
    command.append(image)
    try:
        subprocess.run(command, capture_output=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _read(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _paths(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every leaf of a mapping, by its path."""
    if not isinstance(value, dict) or not value:
        return {prefix: value}
    leaves: dict[tuple[str, ...], Any] = {}
    for key, item in value.items():
        leaves |= _paths(item, (*prefix, str(key)))
    return leaves


# The settings compose.yaml sets through the environment, by path.
FROM_ENVIRONMENT = {
    ("mqtt", "server"),
    ("mqtt", "user"),
    ("mqtt", "password"),
    ("serial", "port"),
    ("serial", "adapter"),
    ("frontend", "auth_token"),
}


def _changed_outside_environment(
    before: dict[str, Any], after: dict[str, Any]
) -> set[tuple[str, ...]]:
    old, new = _paths(before), _paths(after)
    changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
    return {
        path
        for path in changed
        if path[:2] not in FROM_ENVIRONMENT and path[:1] != ("device_options",)
    }


@pytest.fixture
def data(tmp_path: Path) -> Path:
    directory = tmp_path / "zigbee2mqtt"
    directory.mkdir()
    os.chmod(directory, 0o777)  # the container may run as another user
    return directory


def test_the_device_options_are_written_and_nothing_else_of_the_state_changes(
    data: Path,
) -> None:
    """The network key, the PAN IDs and the paired devices stay as they were."""
    before = _configuration()
    (data / "configuration.yaml").write_text(yaml.safe_dump(before), encoding="utf-8")
    _start(data)
    after = _read(data / "configuration.yaml")
    _image, environment = _service()
    assert after["device_options"] == json.loads(environment["ZIGBEE2MQTT_CONFIG_DEVICE_OPTIONS"])
    assert after["advanced"]["network_key"] == NETWORK_KEY
    assert after["advanced"]["pan_id"] == 6754
    assert after["devices"] == DEVICES
    assert _changed_outside_environment(before, after) == set()

    _start(data)
    assert _read(data / "configuration.yaml") == after, "a second start changes nothing"


def test_device_options_set_elsewhere_are_replaced(data: Path) -> None:
    """compose.yaml owns ``device_options``: one set in the web UI is gone at the next start."""
    before = _configuration() | {"device_options": {"retain": True}}
    (data / "configuration.yaml").write_text(yaml.safe_dump(before), encoding="utf-8")
    _start(data)
    after = _read(data / "configuration.yaml")
    assert after["device_options"] == {
        "homeassistant": {"last_seen": {"enabled_by_default": True}}
    }
    assert _changed_outside_environment(before, after) == set()
