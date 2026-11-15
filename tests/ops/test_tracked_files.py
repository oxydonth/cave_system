"""Properties of the tracked runtime files that other components rely on.

Compose, Mosquitto, Zigbee2MQTT and Home Assistant themselves are exercised by the pipeline's
stack job; these tests catch a contract being broken before the pipeline runs.
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
import yaml

from cave_ops.mosquitto import password_line


class Loader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant's custom tags."""


for tag in ("!include", "!include_dir_named", "!include_dir_merge_named", "!secret", "!env_var"):
    Loader.add_constructor(tag, lambda loader, node: loader.construct_scalar(node))


def load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506


@pytest.fixture(scope="module")
def compose(repo_root):
    return load(repo_root / "compose.yaml")


def test_services_are_the_contract_of_concept_section_4_1(compose):
    assert set(compose["services"]) == {"homeassistant", "mosquitto", "zigbee2mqtt"}
    assert compose["services"]["zigbee2mqtt"]["profiles"] == ["zigbee"]
    assert "profiles" not in compose["services"]["homeassistant"]
    assert "profiles" not in compose["services"]["mosquitto"]
    assert compose["name"] == "cave"


@pytest.mark.parametrize("service", ["homeassistant", "mosquitto", "zigbee2mqtt"])
def test_every_service_has_a_health_check_restart_policy_and_log_rotation(compose, service):
    definition = compose["services"][service]

    assert definition["healthcheck"]["test"], "the Healthy contract (4.5) needs a health check"
    assert definition["healthcheck"]["interval"]
    assert definition["restart"] == "unless-stopped"
    assert definition["logging"]["options"]["max-size"]
    assert definition["logging"]["options"]["max-file"]


@pytest.mark.parametrize("service", ["homeassistant", "mosquitto", "zigbee2mqtt"])
def test_images_are_pinned_to_a_version(compose, service):
    image = compose["services"][service]["image"]

    assert re.search(r":\d+\.\d+\.\d+", image), image


def test_home_assistant_is_on_the_host_network_and_mounts_the_contract_paths(compose):
    ha = compose["services"]["homeassistant"]

    assert ha["network_mode"] == "host"
    assert "ports" not in ha
    volumes = ha["volumes"]
    assert any(v.endswith("/homeassistant:/config") for v in volumes)
    assert "./homeassistant/configuration.yaml:/config/configuration.yaml:ro" in volumes
    assert "./homeassistant/packages:/config/packages:ro" in volumes
    assert "./custom_components/cave_heating:/config/custom_components/cave_heating:ro" in volumes
    assert "./tools:/cave/tools:ro" in volumes
    # Code is read-only, and configuration.yaml is the only single-file mount.
    single_files = [v for v in volumes if v.split(":")[0].endswith(".yaml")]
    assert single_files == ["./homeassistant/configuration.yaml:/config/configuration.yaml:ro"]


def test_the_broker_is_published_on_localhost_only(compose):
    assert compose["services"]["mosquitto"]["ports"] == ["127.0.0.1:1883:1883"]


def test_zigbee_web_ui_is_published_and_takes_its_settings_from_the_environment(compose):
    z2m = compose["services"]["zigbee2mqtt"]

    assert "8080:8080" in z2m["ports"]
    environment = z2m["environment"]
    assert "CAVE_Z2M_SERIAL_PORT" in environment["ZIGBEE2MQTT_CONFIG_SERIAL_PORT"]
    assert "CAVE_Z2M_ADAPTER" in environment["ZIGBEE2MQTT_CONFIG_SERIAL_ADAPTER"]
    assert "CAVE_Z2M_FRONTEND_TOKEN" in environment["ZIGBEE2MQTT_CONFIG_FRONTEND_AUTH_TOKEN"]
    assert environment["ZIGBEE2MQTT_CONFIG_MQTT_SERVER"] == "mqtt://mosquitto:1883"


def test_mosquitto_requires_passwords_and_listens_where_the_contract_says(repo_root):
    config = (repo_root / "mosquitto/mosquitto.conf").read_text()

    assert "allow_anonymous false" in config
    assert re.search(r"^password_file /mosquitto/data/passwd$", config, re.MULTILINE)
    assert re.search(r"^persistence true$", config, re.MULTILINE)
    assert re.search(r"^listener 1883 ", config, re.MULTILINE)


def test_zigbee_template_has_discovery_availability_and_last_seen(repo_root):
    config = load(repo_root / "zigbee2mqtt/configuration.template.yaml")

    assert config["homeassistant"]["enabled"] is True
    assert config["homeassistant"]["discovery_topic"] == "homeassistant"
    assert config["availability"]["enabled"] is True
    assert config["availability"]["passive"]["timeout"] == 25 * 60  # minutes
    assert config["advanced"]["last_seen"] == "ISO_8601"
    assert "permit_join" not in config  # not a setting of Zigbee2MQTT 2.x
    # Secrets and the coordinator come from the environment, never from this file.
    assert "port" not in config["serial"]
    assert "auth_token" not in config["frontend"]
    assert "password" not in config["mqtt"]


def test_home_assistant_configuration_has_the_hooks_of_the_work_packages(repo_root):
    config = load(repo_root / "homeassistant/configuration.yaml")

    assert "cave_heating" in config
    assert config["cave_heating"] is None  # an empty entry
    assert config["homeassistant"]["packages"] == "packages"
    dashboard = config["lovelace"]["dashboards"]["cave-heating"]
    assert dashboard["mode"] == "yaml"
    assert dashboard["filename"] == "dashboards/heating.yaml"
    assert config["recorder"]["purge_keep_days"] > 0
    assert "default_config" not in config
    assert {"automation", "script", "scene"} <= set(config)
    # What the system needs on top of Home Assistant's core set, and nothing that phones home.
    assert {"history", "logbook", "sun", "mobile_app", "zeroconf", "ssdp"} <= set(config)
    assert not {"cloud", "homeassistant_alerts", "go2rtc", "bluetooth", "usb"} & set(config)
    # The stack job reads these two log lines.
    assert config["logger"]["logs"]["homeassistant.setup"] == "info"
    assert config["logger"]["logs"]["homeassistant.bootstrap"] == "info"


def test_heating_starter_has_the_defaults_of_concept_section_5_1_and_no_rooms(repo_root):
    starter = load(repo_root / "homeassistant/heating.example.yaml")

    assert starter["version"] == 1
    assert starter["away_delay"] == "00:30:00"
    assert starter["away_temperature"] == 16.0
    assert starter["defaults"] == {
        "comfort_temperature": 21.0,
        "eco_temperature": 17.5,
        "boost_temperature": 25.0,
        "boost_duration": "00:30:00",
        "override_max": "06:00:00",
        "window_open_delay": "00:01:00",
        "window_close_delay": "00:02:00",
    }
    assert starter["rooms"] == []


def test_password_file_format_is_what_mosquitto_passwd_writes():
    salt = bytes(range(12))

    line = password_line("cave", "not-a-real-password", salt=salt)

    assert re.fullmatch(r"cave:\$7\$101\$[A-Za-z0-9+/]{16}\$[A-Za-z0-9+/]{86}==\n", line)
    digest = hashlib.pbkdf2_hmac("sha512", b"not-a-real-password", salt, 101)
    assert line.strip().endswith("$" + base64.b64encode(digest).decode())
    assert password_line("cave", "x") != password_line("cave", "x")  # random salt


def test_zigbee_template_never_leads_to_the_published_default_network_key(repo_root):
    """Without GENERATE, Zigbee2MQTT 2.14.3 uses a network key, PAN ID and extended PAN ID that
    are in its source code. It replaces GENERATE at the first start and writes the values back
    (lib/zigbee.ts), but only for a file that says so, and init always creates the file."""
    advanced = load(repo_root / "zigbee2mqtt/configuration.template.yaml")["advanced"]

    for setting in ("network_key", "pan_id", "ext_pan_id"):
        assert advanced.get(setting) == "GENERATE", setting


def test_core_settings_are_not_in_yaml_because_that_locks_them_in_the_ui(repo_root):
    """Home Assistant 2026.10.4 core_config.py: any of them in YAML sets config_source=yaml."""
    core = load(repo_root / "homeassistant/configuration.yaml")["homeassistant"]

    assert set(core) == {"packages"}
