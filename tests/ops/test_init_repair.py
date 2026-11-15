"""``cave init`` when ``.env`` and the broker's password file are out of step, or damaged.

The broker's health check logs in with the password in ``.env``, and Home Assistant waits for a
healthy broker. So each case below must end in a state that can start, or stop with a message,
and must never say "Nothing to do" when it changed something.
"""

from __future__ import annotations

import os
import stat

import pytest

from cave_ops.envfile import load_env
from cave_ops.mosquitto import accepts

SECRETS = ("CAVE_MQTT_PASSWORD", "CAVE_HA_OWNER_PASSWORD", "CAVE_Z2M_FRONTEND_TOKEN")


def broker_accepts_env(checkout):
    values = load_env(checkout / ".env")
    text = (checkout / "data/mosquitto/passwd").read_text()
    return accepts(text, values["CAVE_MQTT_USER"], values["CAVE_MQTT_PASSWORD"])


def assert_can_start(checkout):
    values = load_env(checkout / ".env")
    assert all(values[name] for name in SECRETS)
    assert broker_accepts_env(checkout)
    assert stat.S_IMODE((checkout / ".env").stat().st_mode) == 0o600


def edit_env(checkout, old, new):
    env_file = checkout / ".env"
    text = env_file.read_text()
    assert old in text
    env_file.write_text(text.replace(old, new))


def test_the_check_notices_a_wrong_password(run_cave, checkout):
    run_cave("init")
    edit_env(checkout, "CAVE_MQTT_USER=cave", "CAVE_MQTT_USER=home")

    assert not broker_accepts_env(checkout)


def test_deleted_env_with_kept_state_gets_a_matching_password_file(run_cave, checkout):
    run_cave("init")
    old_line = (checkout / "data/mosquitto/passwd").read_text()
    (checkout / ".env").unlink()

    result = run_cave("init")

    assert result.code == 0, result.err
    assert "Nothing to do" not in result.out
    assert (checkout / "data/mosquitto/passwd").read_text() != old_line
    assert "run `./cave up`" in result.out
    assert_can_start(checkout)


def test_removed_password_line_is_regenerated_and_the_broker_follows(run_cave, checkout):
    run_cave("init")
    env_file = checkout / ".env"
    kept = [
        line
        for line in env_file.read_text().splitlines()
        if not line.startswith("CAVE_MQTT_PASSWORD=")
    ]
    env_file.write_text("\n".join(kept) + "\n")

    result = run_cave("init")

    assert "Nothing to do" not in result.out
    assert "CAVE_MQTT_PASSWORD" in result.out
    assert_can_start(checkout)
    assert run_cave("init").out.startswith("Nothing to do")


def test_empty_secrets_count_as_missing_and_are_filled_in_place(run_cave, checkout):
    run_cave("init")
    env_file = checkout / ".env"
    before = load_env(env_file)
    for name in SECRETS:
        edit_env(checkout, f"{name}={before[name]}", f"{name}=")

    result = run_cave("init")

    after = load_env(env_file)
    assert "Nothing to do" not in result.out
    assert_can_start(checkout)
    assert env_file.read_text().count("CAVE_MQTT_PASSWORD=") == 1  # replaced, not appended
    assert {name for name in after if after[name] != before[name]} == set(SECRETS)


def test_env_copied_from_the_example_is_completed_and_made_private(run_cave, checkout, repo_root):
    example = (repo_root / ".env.example").read_text()
    env_file = checkout / ".env"
    env_file.write_text(
        example.replace("CAVE_DATA=/var/lib/cave", f"CAVE_DATA={checkout / 'data'}")
    )
    env_file.chmod(0o644)

    result = run_cave("init")

    assert result.code == 0, result.err
    assert "Nothing to do" not in result.out
    assert_can_start(checkout)
    assert load_env(env_file)["CAVE_MQTT_USER"] == "cave"  # the example's values stay


def test_an_env_with_a_wrong_mode_is_fixed_and_reported(run_cave, checkout):
    run_cave("init")
    (checkout / ".env").chmod(0o644)

    result = run_cave("init")

    assert "Nothing to do" not in result.out
    assert stat.S_IMODE((checkout / ".env").stat().st_mode) == 0o600


def test_a_changed_broker_user_rewrites_the_password_file(run_cave, checkout):
    run_cave("init")
    edit_env(checkout, "CAVE_MQTT_USER=cave", "CAVE_MQTT_USER=home")

    result = run_cave("init")

    assert "Nothing to do" not in result.out
    assert (checkout / "data/mosquitto/passwd").read_text().startswith("home:")
    assert broker_accepts_env(checkout)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_password_file_the_user_cannot_read_is_trusted_only_while_the_stamp_agrees(
    run_cave, checkout
):
    run_cave("init")
    passwd = checkout / "data/mosquitto/passwd"
    passwd.chmod(0o000)  # as after the broker's container took the file over
    try:
        assert run_cave("init").out.startswith("Nothing to do")

        edit_env(checkout, "CAVE_MQTT_USER=cave", "CAVE_MQTT_USER=home")
        changed = run_cave("init")  # the directory is ours, so the file can be replaced
        assert "Nothing to do" not in changed.out
    finally:
        passwd.chmod(0o600)
    assert passwd.read_text().startswith("home:")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_password_file_that_cannot_be_replaced_stops_with_the_repair_command(run_cave, checkout):
    run_cave("init")
    edit_env(checkout, "CAVE_MQTT_USER=cave", "CAVE_MQTT_USER=home")
    directory = checkout / "data/mosquitto"
    directory.chmod(0o555)  # as when the broker's container owns it
    try:
        result = run_cave("init")
    finally:
        directory.chmod(0o755)

    assert result.code == 1
    assert "sudo chown -R" in result.err
    assert str(directory) in result.err
    assert "Nothing to do" not in result.out


def test_data_option_that_contradicts_an_existing_env_is_refused(run_cave, checkout, tmp_path):
    run_cave("init")

    result = run_cave("init", "--data", str(tmp_path / "elsewhere"))

    assert result.code == 1
    assert "already sets CAVE_DATA" in result.err
    assert not (tmp_path / "elsewhere").exists()


def test_the_tools_mount_source_is_created_by_init_not_by_docker(run_cave, checkout):
    assert not (checkout / "tools").exists()

    run_cave("init")

    assert (checkout / "tools").is_dir()
