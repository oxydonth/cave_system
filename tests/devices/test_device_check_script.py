"""The script ``cave device-check`` runs in the Home Assistant container, with the real loader.

``tests/ops`` cannot import Home Assistant or the integration, so the script that reads the
applied heating.yaml and the entity registry is run here as the container runs it:
``python -c <script> <entity> <file>`` in the configuration directory, with the integration
importable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from cave_ops.commands.device_check import CONFIG_SCRIPT, MARKER

REPO = Path(__file__).resolve().parents[2]
BOSCH = "climate.schlafzimmer_heizung"


def _run(config_dir: Path, entity: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", CONFIG_SCRIPT, entity, str(config_dir / "heating.yaml")],
        cwd=config_dir,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    line = next(line for line in result.stdout.splitlines() if line.startswith(MARKER))
    answer: dict[str, object] = json.loads(line.removeprefix(MARKER))
    return answer


def _write(config_dir: Path, data: object) -> None:
    (config_dir / "heating.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


ROOM = {
    "id": "bedroom",
    "name": "Schlafzimmer",
    "trvs": [
        {
            "entity": BOSCH,
            "external_temperature": "number.schlafzimmer_heizung_remote_temperature",
            "settings": {"switch.schlafzimmer_heizung_window_detection": "off"},
        }
    ],
    "temperature_sensor": "sensor.schlafzimmer_temperature",
}


def test_the_script_finds_the_thermostat_its_room_input_and_settings(tmp_path: Path) -> None:
    _write(tmp_path, {"version": 1, "rooms": [ROOM], "timing": {"sensor_stale": "02:00:00"}})
    assert _run(tmp_path, BOSCH) == {
        "thermostat": {
            "room": "bedroom",
            "external_temperature": "number.schlafzimmer_heizung_remote_temperature",
            "settings": {"switch.schlafzimmer_heizung_window_detection": "off"},
            "temperature_sensor": "sensor.schlafzimmer_temperature",
        },
        "platform": None,
        "rooms": ["bedroom"],
        "sensor_stale": 7200.0,
    }
    assert _run(tmp_path, "climate.elsewhere")["thermostat"] is None


def test_the_script_reads_the_platform_from_the_entity_registry(tmp_path: Path) -> None:
    """How device-check recognises cave_heating's own room thermostats, whatever their name."""
    _write(tmp_path, {"version": 1, "rooms": [ROOM]})
    storage = tmp_path / ".storage"
    storage.mkdir()
    registry = {
        "data": {
            "entities": [
                {"entity_id": "climate.cave_bedroom", "platform": "cave_heating"},
                {"entity_id": BOSCH, "platform": "mqtt"},
            ]
        }
    }
    (storage / "core.entity_registry").write_text(json.dumps(registry), encoding="utf-8")
    assert _run(tmp_path, "climate.cave_bedroom")["platform"] == "cave_heating"
    assert _run(tmp_path, BOSCH)["platform"] == "mqtt"


def test_the_script_reports_an_invalid_file(tmp_path: Path) -> None:
    (tmp_path / "heating.yaml").write_text(
        "version: 1\nrooms:\n  - id: Bad Room\n", encoding="utf-8"
    )
    answer = _run(tmp_path, "climate.x")
    assert set(answer) == {"error", "platform"}
    assert "rooms[0]" in str(answer["error"])
