"""``cave init``: creates everything, generates secrets, and changes nothing the second time."""

from __future__ import annotations

import base64
import hashlib
import re
import stat
import time

from cave_ops.envfile import NAMES, load_env


def test_creates_state_secrets_and_env(run_cave, fake_runner, checkout):
    result = run_cave("init")

    assert result.code == 0, result.err
    data = checkout / "data"
    ha = data / "homeassistant"
    for relative in (
        "secrets.yaml",
        "automations.yaml",
        "scripts.yaml",
        "scenes.yaml",
        "cave/heating.yaml",
        "cave/.applied/heating.yaml",
        "dashboards/heating.yaml",
        ".cave",
    ):
        assert (ha / relative).exists(), relative
    assert (data / "zigbee2mqtt" / "configuration.yaml").is_file()
    assert (data / "mosquitto" / "passwd").is_file()
    assert (data / ".cave").is_dir()
    # init is pure file work: it must not need Docker.
    assert fake_runner.calls == []


def test_env_has_every_variable_with_mode_0600_and_generated_secrets(run_cave, checkout):
    run_cave("init")

    env_file = checkout / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = load_env(env_file)
    assert set(values) == set(NAMES)
    assert values["CAVE_DATA"] == str(checkout / "data")
    for name in ("CAVE_MQTT_PASSWORD", "CAVE_HA_OWNER_PASSWORD", "CAVE_Z2M_FRONTEND_TOKEN"):
        assert re.fullmatch(r"[A-Za-z0-9]{32}", values[name]), name
    assert len({values[n] for n in ("CAVE_MQTT_PASSWORD", "CAVE_HA_OWNER_PASSWORD")}) == 2
    assert values["TZ"] == "Europe/Berlin"
    assert values["CAVE_AUTO_UPDATE"] == "1"
    assert values["CAVE_BACKUP_DIR"] == "/var/backups/cave"
    assert values["CAVE_BACKUP_KEEP"] == "14"


def test_private_files_are_not_readable_by_others(run_cave, checkout):
    run_cave("init")

    private = [
        checkout / ".env",
        checkout / "data/homeassistant/secrets.yaml",
        checkout / "data/mosquitto/passwd",
    ]
    for path in private:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE((checkout / "data/homeassistant/.cave").stat().st_mode) == 0o700


def test_starter_is_stored_as_edited_and_applied_copy(run_cave, checkout):
    run_cave("init")

    starter = (checkout / "homeassistant/heating.example.yaml").read_bytes()
    ha = checkout / "data/homeassistant"
    assert (ha / "cave/heating.yaml").read_bytes() == starter
    assert (ha / "cave/.applied/heating.yaml").read_bytes() == starter


def test_broker_password_file_matches_the_generated_password(run_cave, checkout):
    run_cave("init")

    values = load_env(checkout / ".env")
    line = (checkout / "data/mosquitto/passwd").read_text().strip()
    user, _, rest = line.partition(":")
    _, version, iterations, salt_b64, hash_b64 = rest.split("$")
    assert user == values["CAVE_MQTT_USER"]
    assert (version, iterations) == ("7", "101")
    expected = hashlib.pbkdf2_hmac(
        "sha512", values["CAVE_MQTT_PASSWORD"].encode(), base64.b64decode(salt_b64), 101
    )
    assert base64.b64decode(hash_b64) == expected


def test_second_run_changes_nothing_including_modification_times(run_cave, checkout, snapshot):
    run_cave("init")
    before = snapshot(checkout)
    time.sleep(0.05)  # a rewrite would now get a different modification time

    second = run_cave("init")

    assert second.code == 0
    assert "Nothing to do" in second.out
    assert snapshot(checkout) == before


def test_existing_values_and_files_are_kept(run_cave, checkout):
    run_cave("init")
    env_file = checkout / ".env"
    env_file.write_text(
        env_file.read_text().replace("TZ=Europe/Berlin", "TZ=Europe/London"), encoding="utf-8"
    )
    edited = checkout / "data/homeassistant/cave/heating.yaml"
    edited.write_text("version: 1\nrooms: []\n# my own edit\n", encoding="utf-8")

    run_cave("init")

    assert load_env(env_file)["TZ"] == "Europe/London"
    assert "# my own edit" in edited.read_text()


def test_missing_variable_is_appended_without_touching_the_rest(run_cave, checkout):
    run_cave("init")
    env_file = checkout / ".env"
    before = load_env(env_file)
    lines = [
        line
        for line in env_file.read_text().splitlines()
        if not line.startswith("CAVE_BACKUP_KEEP=")
    ]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run_cave("init")

    assert load_env(env_file) == before
    assert "CAVE_BACKUP_KEEP" in result.out
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_data_directory_from_option(run_cave, checkout, tmp_path):
    chosen = tmp_path / "elsewhere"

    run_cave("init", "--data", str(chosen))

    assert load_env(checkout / ".env")["CAVE_DATA"] == str(chosen.resolve())
    assert (chosen / "homeassistant" / "secrets.yaml").is_file()
    assert not (checkout / "data").exists()


def test_incomplete_checkout_is_reported_before_anything_is_written(run_cave, checkout):
    (checkout / "zigbee2mqtt/configuration.template.yaml").unlink()

    result = run_cave("init")

    assert result.code == 1
    assert "configuration.template.yaml" in result.err
    assert not (checkout / ".env").exists()


def test_unwritable_state_directory_gives_advice(run_cave, checkout):
    blocker = checkout / "blocked"
    blocker.write_text("a file where a directory is needed")

    result = run_cave("init", "--data", str(blocker / "state"))

    assert result.code == 1
    assert "provision-host.sh" in result.err
    assert "Traceback" not in result.err
    # A failed init must not leave a .env that names a directory that cannot exist.
    assert not (checkout / ".env").exists()


def test_init_does_not_depend_on_the_validator(run_cave, fake_runner):
    """The starter is stored as the applied copy directly: no container, no validator."""
    run_cave("init")

    assert not fake_runner.ran("docker")
