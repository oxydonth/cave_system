"""``python -m cave_sim``: its configuration, and that it stays clear of Home Assistant."""

import subprocess
import sys
from pathlib import Path

import pytest

from cave_sim.__main__ import DEFAULT_HOME, Config, ConfigError

SIM_DIR = Path(__file__).resolve().parents[2] / "sim"


def test_the_runtime_path_imports_neither_home_assistant_nor_pytest() -> None:
    """The image installs neither, so nothing ``python -m cave_sim`` loads may need them."""
    code = (
        "import sys\n"
        "import cave_sim.__main__, cave_sim.zigbee.record, cave_sim.zigbee.fixtures\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('homeassistant', 'pytest', 'pytest_homeassistant_custom_component', '_pytest'))\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SIM_DIR)!r})\n{code}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_the_configuration_has_defaults_for_everything_but_the_fritz_password() -> None:
    config = Config.from_env({"CAVE_FRITZ_PASSWORD": "not-a-real-password"})
    assert config.home == DEFAULT_HOME
    assert DEFAULT_HOME.exists()
    assert (config.mqtt_host, config.mqtt_port) == ("mosquitto", 1883)
    assert (config.fritz_port, config.control_port) == (8480, 8481)
    assert config.fritz_user == "cave"
    assert config.mqtt_user is None


def test_the_configuration_is_read_from_the_environment() -> None:
    config = Config.from_env(
        {
            "CAVE_FRITZ_PASSWORD": "not-a-real-password",
            "CAVE_FRITZ_USER": "someone",
            "CAVE_MQTT_USER": "cave",
            "CAVE_MQTT_PASSWORD": "also-not-real",
            "CAVE_SIM_MQTT_HOST": "127.0.0.1",
            "CAVE_SIM_MQTT_PORT": "1884",
            "CAVE_SIM_HOME": "/tmp/home.yaml",  # noqa: S108  (only a path, never opened)
        }
    )
    assert (config.mqtt_host, config.mqtt_port, config.mqtt_user) == ("127.0.0.1", 1884, "cave")
    assert config.fritz_user == "someone"
    assert config.home == Path("/tmp/home.yaml")  # noqa: S108


@pytest.mark.parametrize(
    "env",
    [{}, {"CAVE_FRITZ_PASSWORD": "x", "CAVE_SIM_FRITZ_PORT": "eighty"}],
)
def test_an_invalid_configuration_names_the_variable(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="CAVE_"):
        Config.from_env(env)
