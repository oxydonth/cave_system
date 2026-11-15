"""The shared validate-and-store function, with the validator replaced by the fake runner.

The validator itself ships with the ``cave_heating`` integration, a separate work package; these
tests pin how it is called and what is done with its verdict.
"""

from __future__ import annotations

import os
import time

import pytest

from cave_ops.heating import store_applied, validate, validate_and_store

VALID = "version: 1\nrooms: []\n"
VALIDATOR = ["docker", "compose", "run"]


@pytest.fixture
def home(ctx, run_cave):
    """An initialised state directory whose applied copy is the starter."""
    assert run_cave("init").code == 0
    return ctx.paths


def test_valid_file_is_stored_as_the_applied_copy(ctx, home, fake_runner):
    home.heating.write_text(VALID + "# edited\n")
    fake_runner.on(VALIDATOR, out="ok\n")

    result = validate_and_store(ctx)

    assert result.ok
    assert result.changed
    assert result.applied_path == home.heating_applied
    assert home.heating_applied.read_text() == VALID + "# edited\n"


def test_the_validator_runs_in_a_one_off_container_with_config_as_working_directory(
    ctx, home, fake_runner
):
    fake_runner.on(VALIDATOR)

    validate_and_store(ctx)

    (call,) = fake_runner.matching(*VALIDATOR)
    assert call.args == (
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--workdir",
        "/config",
        "--entrypoint",
        "python3",
        "homeassistant",
        "-m",
        "custom_components.cave_heating.validate",
        "/config/cave/.validating.yaml",
    )
    assert call.cwd == ctx.paths.root


def test_invalid_file_changes_nothing_and_the_messages_are_returned(ctx, home, fake_runner):
    before = home.heating_applied.read_bytes()
    mtime = home.heating_applied.stat().st_mtime_ns
    home.heating.write_text("version: 1\nrooms: [{id: x}]\n")
    fake_runner.on(VALIDATOR, rc=1, out="rooms[0] (x): field 'name' is required\n")

    result = validate_and_store(ctx)

    assert not result.ok
    assert not result.changed
    assert result.applied_path is None
    assert result.messages == ("rooms[0] (x): field 'name' is required",)
    assert home.heating_applied.read_bytes() == before
    assert home.heating_applied.stat().st_mtime_ns == mtime


def test_a_container_that_cannot_run_counts_as_invalid(ctx, home, fake_runner):
    fake_runner.on(VALIDATOR, rc=125, err="Cannot connect to the Docker daemon\n")

    result = validate_and_store(ctx)

    assert not result.ok
    assert "Cannot connect to the Docker daemon" in result.messages[0]


def test_a_file_outside_the_state_directory_is_staged_and_cleaned_up(
    ctx, home, fake_runner, tmp_path
):
    demo = tmp_path / "demo-heating.yaml"
    demo.write_text(VALID + "# demo\n")
    seen = []

    def spy(call):
        seen.append((home.ha_config / "cave" / ".validating.yaml").read_text())

    fake_runner.on(VALIDATOR, side_effect=spy)

    result = validate_and_store(ctx, demo)

    assert result.ok
    assert seen == [VALID + "# demo\n"]
    assert fake_runner.matching(*VALIDATOR)[0].args[-1] == "/config/cave/.validating.yaml"
    assert not (home.ha_config / "cave" / ".validating.yaml").exists()
    assert home.heating_applied.read_text() == VALID + "# demo\n"
    assert home.heating.read_text() != VALID + "# demo\n"  # the edited file is not touched


def test_the_staged_copy_is_removed_even_when_the_run_fails(ctx, home, fake_runner, tmp_path):
    demo = tmp_path / "demo-heating.yaml"
    demo.write_text(VALID)
    fake_runner.on(VALIDATOR, rc=1, out="bad\n")

    assert not validate_and_store(ctx, demo).ok
    assert not (home.ha_config / "cave" / ".validating.yaml").exists()


def test_storing_the_same_content_does_not_touch_the_file(ctx, home):
    target = home.heating_applied
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))

    assert store_applied(ctx, home.heating) is False

    assert target.stat().st_mtime_ns == 1_000_000_000


def test_storing_replaces_the_file_atomically_and_leaves_no_temporary_files(ctx, home, tmp_path):
    new = tmp_path / "new.yaml"
    new.write_text(VALID + "# new\n")
    time.sleep(0.01)

    assert store_applied(ctx, new) is True

    assert home.heating_applied.read_text() == VALID + "# new\n"
    assert os.listdir(home.heating_applied.parent) == ["heating.yaml"]
    assert oct(home.heating_applied.stat().st_mode & 0o777) == "0o644"


def test_validate_alone_does_not_store(ctx, home, fake_runner):
    home.heating.write_text(VALID + "# only validated\n")
    fake_runner.on(VALIDATOR)

    ok, _ = validate(ctx)

    assert ok
    assert "only validated" not in home.heating_applied.read_text()


def test_the_bytes_that_were_validated_are_the_bytes_that_are_stored(ctx, home, fake_runner):
    """The edited file may be saved again while the validator's container starts."""
    validated = VALID + "# the version that was validated\n"
    home.heating.write_text(validated)
    seen = []

    def save_again_while_validating(call):
        seen.append((home.ha_config / "cave" / ".validating.yaml").read_text())
        home.heating.write_text("version: 1\nrooms: [this was never validated]\n")

    fake_runner.on(VALIDATOR, side_effect=save_again_while_validating)

    result = validate_and_store(ctx)

    assert result.ok
    assert seen == [validated]
    assert home.heating_applied.read_text() == validated
    assert home.heating.read_text() != validated  # the later save is the edited file's business


def test_messages_name_the_edited_file_not_the_staged_copy(ctx, home, fake_runner):
    fake_runner.on(VALIDATOR, rc=1, out="/config/cave/.validating.yaml: rooms[0]: bad\n")

    result = validate_and_store(ctx)

    assert result.messages == (f"{home.heating}: rooms[0]: bad",)


def test_store_applied_accepts_bytes(ctx, home):
    assert store_applied(ctx, b"version: 1\nrooms: []\n# bytes\n") is True

    assert home.heating_applied.read_bytes().endswith(b"# bytes\n")
