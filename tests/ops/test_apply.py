"""``cave apply``: validate and store, regenerate the dashboard, reload ``cave_heating``.

The validator ships with the ``cave_heating`` integration and the dashboard generator runs in a
Home Assistant container, so both are replaced by the fake runner here. One test runs the real
generator (``tools/render_dashboard.py``, which needs only PyYAML) in place of the container, to
prove that ``apply`` hands it the right paths and that its output is what Home Assistant loads.
Nothing here imports Home Assistant.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

GENERATOR_PATH = "/cave/tools/render_dashboard.py"
CONTAINER = ["docker", "compose", "run"]
VALID = (
    "version: 1\nrooms:\n  - id: office\n    name: Office\n    trvs:\n      - entity: climate.a\n"
)
OLD_DASHBOARD = "# the dashboard before this apply\n"


@pytest.fixture
def home(ctx, run_cave, fake_ha):
    """An initialised state directory, with a dashboard from an earlier apply."""
    assert run_cave("init").code == 0
    ctx.paths.dashboard.write_text(OLD_DASHBOARD)
    fake_ha.services["cave_heating"] = ["reload"]
    return ctx.paths


def containers(
    fake_runner, home, *, validator=(0, ""), generator=(0, ""), real_generator=None, log=None
):
    """Declare the two one-off containers `apply` starts: the validator, then the generator.

    ``validator`` and ``generator`` are ``(status, output)``. A generator that succeeds writes a
    marker dashboard, or, with ``real_generator`` (a path), runs that script instead. ``log`` is
    a list that gets ``"validate"`` and ``"generate"`` when the containers run.
    """

    def generate(call):
        if call.args[-3] != GENERATOR_PATH:
            if log is not None:
                log.append("validate")
            return
        if log is not None:
            log.append("generate")
        source = home.heating_applied  # /config/cave/.applied/heating.yaml
        assert call.args[-2] == "/config/cave/.applied/heating.yaml"
        assert call.args[-1] == "/config/dashboards/heating.yaml"
        if generator[0] != 0:
            return
        if real_generator is not None:
            done = subprocess.run(
                [sys.executable, str(real_generator), str(source), str(home.dashboard)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode == 0, done.stderr
        else:
            home.dashboard.write_text(f"# generated from {source.read_text()!r}\n")

    fake_runner.on(
        CONTAINER,
        sequence=[(validator[0], validator[1], ""), (generator[0], generator[1], "")],
        side_effect=generate,
    )


def files(snapshot, root):
    """Every file below ``root`` with its mode, size and time. Directories are left out: the
    validator stages a copy of the edited file in ``cave/`` for its run and removes it again,
    which touches that directory's own modification time and nothing else."""
    return {
        name: info
        for name, info in snapshot(root).items()
        if (root / name).is_file() and name != ".cave/lock"  # the lock file names its holder
    }


def edit(home, text=VALID):
    home.heating.write_text(text)


def test_a_valid_file_is_stored_the_dashboard_regenerated_and_cave_heating_reloaded(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home)

    result = run_cave("apply")

    assert result.code == 0, result.err
    assert home.heating_applied.read_text() == VALID
    assert home.dashboard.read_text() == f"# generated from {VALID!r}\n"
    assert fake_ha.service_calls == [("cave_heating", "reload", {})]
    assert "Applied copy:    updated" in result.out
    assert "Dashboard:       regenerated" in result.out
    assert "Home Assistant:  cave_heating reloaded" in result.out
    assert result.err == ""


def test_the_steps_run_in_order_validate_then_generate_then_reload(
    ctx, run_cave, home, fake_runner, fake_ha, write_token
):
    """One log for the containers and for Home Assistant's API, so that the order is the order."""
    edit(home)
    write_token()
    log = []
    containers(fake_runner, home, log=log)

    def recording(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/api/services/cave_heating/reload"):
            log.append("reload")
        return fake_ha(method, url, headers, body, timeout)

    ctx.transport = recording

    assert run_cave("apply").code == 0

    assert log == ["validate", "generate", "reload"]
    validator, generator = fake_runner.matching(*CONTAINER)
    assert validator.args[-2] == "custom_components.cave_heating.validate"
    assert validator.args[-1].startswith("/config/cave/")  # the bytes of the edited file
    assert generator.args[-4:] == (
        "homeassistant",
        GENERATOR_PATH,
        "/config/cave/.applied/heating.yaml",
        "/config/dashboards/heating.yaml",
    )
    assert generator.args[:6] == ("docker", "compose", "run", "--rm", "--no-deps", "-T")


def test_nothing_after_validation_runs_when_the_file_is_invalid(
    ctx, run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home, "rooms: [")
    write_token()
    log = []
    containers(fake_runner, home, validator=(1, "line 1: not valid YAML\n"), log=log)

    assert run_cave("apply").code == 1

    assert log == ["validate"]


def test_the_generator_receives_the_applied_copy_not_the_edited_file(
    run_cave, home, fake_runner, write_token
):
    edit(home, VALID)
    write_token()
    containers(fake_runner, home)

    run_cave("apply")
    edit(home, VALID + "# edited again, not applied\n")

    assert home.heating_applied.read_text() == VALID
    assert "again" not in home.dashboard.read_text()


def test_an_invalid_file_changes_nothing_and_shows_the_errors(
    run_cave, home, fake_runner, fake_ha, write_token, snapshot
):
    edit(home, "version: 1\nrooms:\n  - id: office\n")
    write_token()
    containers(
        fake_runner, home, validator=(1, "room office: field `name` is required\nrooms[1]: x\n")
    )
    before = files(snapshot, home.data)

    result = run_cave("apply")

    assert result.code == 1
    assert "room office: field `name` is required" in result.err
    assert "rooms[1]: x" in result.err
    assert "Nothing was changed" in result.err
    assert files(snapshot, home.data) == before
    assert home.dashboard.read_text() == OLD_DASHBOARD
    assert len(fake_runner.matching(*CONTAINER)) == 1  # the generator never ran
    assert fake_ha.requests == []  # and Home Assistant was not asked anything
    assert result.out == ""


def test_an_invalid_file_does_not_replace_a_good_applied_copy(run_cave, home, fake_runner):
    edit(home, VALID)
    containers(fake_runner, home)
    assert run_cave("apply").code == 0
    edit(home, "rooms: [oops")
    fake_runner.calls.clear()
    containers(fake_runner, home, validator=(1, "line 1: not valid YAML\n"))

    result = run_cave("apply")

    assert result.code == 1
    assert home.heating_applied.read_text() == VALID


def test_without_a_token_home_assistant_has_to_be_restarted_instead(
    run_cave, home, fake_runner, fake_ha
):
    edit(home)
    containers(fake_runner, home)

    result = run_cave("apply")

    assert result.code == 0
    assert home.heating_applied.read_text() == VALID
    assert home.dashboard.read_text().startswith("# generated from")
    assert fake_ha.requests == []
    assert "no script token yet" in result.out
    assert "has to be restarted instead" in result.out
    assert "./cave restart homeassistant" in result.out


def test_applying_an_unchanged_file_still_regenerates_and_reloads(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home, VALID)
    write_token()
    containers(fake_runner, home)
    run_cave("apply")
    fake_runner.calls.clear()
    fake_ha.service_calls.clear()
    home.dashboard.write_text(OLD_DASHBOARD)  # the dashboard went missing or stale
    containers(fake_runner, home)

    result = run_cave("apply")

    assert result.code == 0
    assert "Applied copy:    unchanged" in result.out
    assert home.dashboard.read_text().startswith("# generated from")
    assert fake_ha.service_calls == [("cave_heating", "reload", {})]


# ---------------------------------------------------------------- what is left after a failure
def test_a_dashboard_that_cannot_be_generated_leaves_the_old_one_and_still_reloads(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(
        fake_runner,
        home,
        generator=(1, "render_dashboard: /config/cave/.applied/heating.yaml: rooms[0]: broken\n"),
    )

    result = run_cave("apply")

    assert result.code == 1
    # The applied copy is stored: that cannot be undone, and the heating follows it.
    assert home.heating_applied.read_text() == VALID
    assert fake_ha.service_calls == [("cave_heating", "reload", {})]
    # The dashboard is as it was, and the person is told so, with the generator's own message.
    assert home.dashboard.read_text() == OLD_DASHBOARD
    assert "render_dashboard: /config/cave/.applied/heating.yaml: rooms[0]: broken" in result.err
    assert "Applied copy:    updated" in result.out
    assert "Dashboard:       NOT regenerated" in result.out
    assert "still the previous one" in result.out
    assert "Home Assistant:  cave_heating reloaded" in result.out


def test_a_generator_that_cannot_run_at_all_is_reported_the_same_way(
    run_cave, home, fake_runner, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home, generator=(125, "Cannot connect to the Docker daemon\n"))

    result = run_cave("apply")

    assert result.code == 1
    assert "Cannot connect to the Docker daemon" in result.err
    assert "NOT regenerated (exit status 125)" in result.out
    assert home.heating_applied.read_text() == VALID


def test_a_reload_that_fails_leaves_everything_else_done_and_says_to_restart(
    ctx, run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home)

    def refuse_reload(method, url, headers, body, timeout):
        if method == "POST":
            return 500, b"Internal Server Error"
        return fake_ha(method, url, headers, body, timeout)

    ctx.transport = refuse_reload

    result = run_cave("apply")

    assert result.code == 1
    assert home.heating_applied.read_text() == VALID
    assert home.dashboard.read_text().startswith("# generated from")
    assert "Applied copy:    updated" in result.out
    assert "Dashboard:       regenerated" in result.out
    assert "Home Assistant answered 500: Internal Server Error" in result.err
    assert "reload refused (answer 500, shown above)" in result.out
    assert "loaded when Home Assistant restarts" in result.out
    assert "./cave restart homeassistant" in result.out


def test_a_reload_that_refuses_the_applied_copy_shows_what_home_assistant_answered(
    ctx, run_cave, home, fake_runner, fake_ha, write_token
):
    """The integration answers 400 with the problems in `message`; they reach the person."""
    edit(home)
    write_token()
    containers(fake_runner, home)
    problems = (
        'cave/.applied/heating.yaml is not valid (2 problems): Room "office" (rooms[0]), '
        'field "trvs": at least one thermostat is required. Room "bath" (rooms[1]), field '
        '"humidity_sensor": "x" is not an entity ID.'
    )

    def refuse_reload(method, url, headers, body, timeout):
        if method == "POST":
            return 400, json.dumps({"message": problems}).encode()
        return fake_ha(method, url, headers, body, timeout)

    ctx.transport = refuse_reload

    result = run_cave("apply")

    assert result.code == 1
    assert f"Home Assistant answered 400: {problems}" in result.err
    assert "{" not in result.err  # the message, not the JSON around it
    assert "the configuration that was running stays in effect" in result.out
    assert "Applied copy:    updated" in result.out
    assert "Dashboard:       regenerated" in result.out


def test_a_home_assistant_that_is_down_loads_the_applied_copy_when_it_starts(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home)
    fake_ha.down = True

    result = run_cave("apply")

    assert result.code == 1
    assert home.heating_applied.read_text() == VALID
    assert "Dashboard:       regenerated" in result.out
    assert "not told: cannot reach Home Assistant" in result.out
    assert "loads the applied copy when it starts" in result.out
    assert "./cave up" in result.out


def test_an_integration_that_is_not_loaded_in_the_running_home_assistant_is_reported(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home)
    del fake_ha.services["cave_heating"]

    result = run_cave("apply")

    assert result.code == 1
    assert fake_ha.service_calls == []
    assert "no cave_heating.reload" in result.out
    assert "./cave restart homeassistant" in result.out


def test_a_failed_dashboard_and_a_failed_reload_are_both_reported(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    containers(fake_runner, home, generator=(1, "boom\n"))
    fake_ha.down = True

    result = run_cave("apply")

    assert result.code == 1
    assert "Dashboard:       NOT regenerated" in result.out
    assert "Home Assistant:  not told" in result.out
    assert home.heating_applied.read_text() == VALID


# ------------------------------------------------------------------------------ preconditions
def test_apply_before_init_says_so(run_cave):
    result = run_cave("apply")

    assert result.code == 1
    assert "run `./cave init` first" in result.err


def test_apply_without_an_edited_file_says_so(run_cave, home):
    home.heating.unlink()

    result = run_cave("apply")

    assert result.code == 1
    assert "heating.yaml does not exist; run `./cave init` first" in result.err


def test_apply_is_listed_by_help(run_cave):
    result = run_cave("help")

    assert "apply" in result.out


# ------------------------------------------------------------------------------------- the lock
def test_apply_holds_the_lock_of_update_backup_and_restore_for_its_whole_run(
    ctx, run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token()
    seen = []

    def holder(call):
        seen.append(ctx.paths.lock_file.read_text())

    fake_runner.on(CONTAINER, side_effect=holder)

    run_cave("apply")

    assert len(seen) == 2  # during the validator and during the generator
    assert all(text.startswith("`cave apply`") for text in seen)
    ctx.lock("update").acquire()  # and it was let go of afterwards


def test_a_busy_lock_is_reported_with_its_holder_and_nothing_is_changed(
    ctx, run_cave, home, fake_runner, fake_ha, write_token, snapshot
):
    edit(home, VALID + "# edited while an update runs\n")
    write_token()
    before = files(snapshot, home.data)
    update = ctx.lock("update")
    update.acquire()

    result = run_cave("apply")

    update.release()
    assert result.code == 1
    assert "`cave update`" in result.err
    assert "is running" in result.err
    assert "Nothing was changed" in result.err
    assert fake_runner.calls == []  # not even the validator ran
    assert fake_ha.requests == []
    assert files(snapshot, home.data) == before


def test_an_apply_blocks_an_update_while_it_runs_and_not_afterwards(
    ctx, run_cave, home, fake_runner, write_token
):
    edit(home)
    write_token()
    from cave_ops.lock import LockBusy

    def try_update(call):
        with pytest.raises(LockBusy):
            ctx.lock("update").acquire()

    fake_runner.on(CONTAINER, side_effect=try_update)

    assert run_cave("apply").code == 0

    lock = ctx.lock("update")
    lock.acquire()
    lock.release()


# ------------------------------------------------------------------- file system and the token
def test_a_state_directory_that_cannot_be_written_gives_one_line_not_a_traceback(
    ctx, run_cave, home, fake_runner, write_token, monkeypatch
):
    edit(home)
    write_token()
    containers(fake_runner, home)
    applied_dir = home.heating_applied.parent

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(applied_dir))

    monkeypatch.setattr("cave_ops.heating.tempfile.mkstemp", refuse)

    result = run_cave("apply")

    assert result.code == 1
    assert "Traceback" not in result.err
    assert "Permission denied" in result.err
    assert str(applied_dir) in result.err
    assert "Nothing was changed" in result.err
    assert result.err.count("\n") == 2
    assert home.dashboard.read_text() == OLD_DASHBOARD
    ctx.lock("update").acquire()  # the lock was let go of


def test_an_edited_file_that_cannot_be_read_gives_one_line_not_a_traceback(
    run_cave, home, fake_runner, write_token
):
    edit(home)
    write_token()
    home.heating.chmod(0o000)
    containers(fake_runner, home)
    try:
        result = run_cave("apply")
    finally:
        home.heating.chmod(0o644)

    if result.code == 0:
        pytest.skip("this user can read a file with no permissions (root)")
    assert result.code == 1
    assert "Traceback" not in result.err
    assert "Nothing was changed" in result.err


def test_a_token_that_home_assistant_does_not_accept_is_reported_as_that(
    run_cave, home, fake_runner, fake_ha, write_token
):
    edit(home)
    write_token("a-token-that-was-revoked")
    containers(fake_runner, home)

    result = run_cave("apply")

    assert result.code == 1
    assert "did not accept the script token" in result.out
    assert "./cave bootstrap" in result.out
    assert "reload refused" not in result.out
    assert "stays in effect" not in result.out
    assert "Applied copy:    updated" in result.out
    assert "Dashboard:       regenerated" in result.out


# ----------------------------------------------------------- with the real dashboard generator
def test_the_real_generator_turns_the_applied_copy_into_the_dashboard_home_assistant_loads(
    run_cave, home, fake_runner, write_token, repo_root
):
    """The paths `apply` passes work for the real tool, and what it writes is the dashboard."""
    yaml = pytest.importorskip("yaml")
    edit(home, VALID)
    write_token()
    containers(fake_runner, home, real_generator=repo_root / "tools" / "render_dashboard.py")

    result = run_cave("apply")

    assert result.code == 0, result.err
    dashboard = yaml.safe_load(home.dashboard.read_text())
    rooms_view, maintenance = dashboard["views"]
    headings = [
        card["heading"]
        for section in rooms_view["sections"]
        for card in section["cards"]
        if card["type"] == "heading"
    ]
    assert headings == ["Home", "Office"]
    assert maintenance["path"] == "maintenance"
    assert home.dashboard.read_text().startswith("# Generated by tools/render_dashboard.py")
