"""The script ``cave device-check`` runs in the Home Assistant container, with the real loader.

``tests/ops`` cannot import Home Assistant or the integration, so the script that reads the
applied heating.yaml is run here, as the container runs it: ``python -c <script> <entity>
<file>`` with the directory holding ``custom_components`` as the working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from cave_ops.commands.device_check import CONFIG_SCRIPT, MARKER

REPO = Path(__file__).resolve().parents[2]


def _run(path: Path, entity: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", CONFIG_SCRIPT, entity, str(path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    line = next(line for line in result.stdout.splitlines() if line.startswith(MARKER))
    answer: dict[str, object] = json.loads(line.removeprefix(MARKER))
    return answer


def test_the_script_finds_the_thermostat_its_room_input_and_settings(tmp_path: Path) -> None:
    path = tmp_path / "heating.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "rooms": [
                    {
                        "id": "bedroom",
                        "name": "Schlafzimmer",
                        "trvs": [
                            {
                                "entity": "climate.schlafzimmer_heizung",
                                "external_temperature": (
                                    "number.schlafzimmer_heizung_remote_temperature"
                                ),
                                "settings": {"switch.schlafzimmer_heizung_window_detection": "off"},
                            }
                        ],
                        "temperature_sensor": "sensor.schlafzimmer_temperature",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert _run(path, "climate.schlafzimmer_heizung") == {
        "thermostat": {
            "room": "bedroom",
            "external_temperature": "number.schlafzimmer_heizung_remote_temperature",
            "settings": {"switch.schlafzimmer_heizung_window_detection": "off"},
            "temperature_sensor": "sensor.schlafzimmer_temperature",
        }
    }
    assert _run(path, "climate.elsewhere") == {"thermostat": None}


def test_the_script_reports_an_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "heating.yaml"
    path.write_text("version: 1\nrooms:\n  - id: Bad Room\n", encoding="utf-8")
    answer = _run(path, "climate.x")
    assert set(answer) == {"error"}
    assert "rooms[0]" in str(answer["error"])
