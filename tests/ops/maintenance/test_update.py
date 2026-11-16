"""``cave update``: concept section 9, one test per outcome, on a real checkout and state."""

from __future__ import annotations

import pytest

from cave_ops.commands import _server
from cave_ops.lock import Lock

NEW = "v2026.11.15.1"
NEWER = "v2026.11.16.1"
CHECKS = ["pull", "check_config", "validate"]


def stopped(server):
    """Whether the home's stack was stopped at any point."""
    return "down" in server.stack.names()


# ----------------------------------------------------------------------------- nothing to do
def test_no_new_release_changes_nothing_and_notifies_nobody(
    server, run_cave, notifications, journal, fake_runner
):
    result = run_cave("update")

    assert result.code == 0
    assert "Up to date: v2026.11.14.1 is installed" in result.out
    assert server.stack.ops == []
    assert notifications() == []
    assert any("Up to date" in line for line in journal)
    assert any(
        call.args == ("git", "fetch", "--prune", "--prune-tags", "--force", "--tags", "origin")
        for call in fake_runner.calls
    )
    assert server.record()["last_check"]["message"].startswith("Up to date")


def test_a_checkout_ahead_of_the_newest_release_is_up_to_date(server, run_cave):
    (server.origin / "NOTES").write_text("unreleased\n")
    server.release("not-a-release-tag")  # a newer commit, but no release
    server.release(NEW)
    run_cave("update")
    server.stack.ops.clear()

    result = run_cave("update")

    assert result.code == 0
    assert "Up to date" in result.out
    assert server.stack.ops == []


# ------------------------------------------------------------------------------------ success
def test_a_successful_update_validates_first_then_switches(
    server, run_cave, notifications, state_tree
):
    before = state_tree(server.data)
    server.release(NEW)

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.head() == server.commit_of(NEW)
    assert server.stack.running == NEW
    # Images pulled and both validations run with the new release's own files, before the stack
    # was stopped; then stop, start.
    assert server.stack.ops == [
        ("pull", NEW),
        ("check_config", NEW),
        ("validate", NEW),
        ("down", "v2026.11.14.1"),
        ("up", NEW),
    ]
    assert notifications() == [
        f"Installed {NEW} (was v2026.11.14.1); the stack is healthy. The snapshot of the state "
        "from before the update is kept as 0001-v2026.11.14.1-to-v2026.11.15.1."
    ]
    snapshot = server.data / ".cave/ops/snapshots/0001-v2026.11.14.1-to-v2026.11.15.1/data"
    assert state_tree(snapshot) == before
    record = server.record()
    assert record["switch"] is None
    assert record["last_result"]["outcome"] == "installed"
    assert not (server.data / ".cave/ops/tmp").exists()


def test_the_checks_use_the_exported_release_and_a_project_of_their_own(server, run_cave):
    server.release(NEW, {"compose.yaml": "name: cave\nservices: {new: {}}\n"})

    run_cave("update")

    checks = [
        call
        for call in server.runner.fake.calls
        if call.args[:2] == ("docker", "compose") and call.env
    ]
    assert {call.env["COMPOSE_PROJECT_NAME"] for call in checks} == {"cave-update"}
    exported = checks[0].env["COMPOSE_FILE"]
    assert exported.endswith(".cave/ops/tmp/release/compose.yaml")
    assert all(call.env["CAVE_DATA"] == str(server.data) for call in checks)
    validate = [call for call in checks if "custom_components.cave_heating.validate" in call.args]
    # Through the shared heating.validate(): a staged copy of the applied file's bytes.
    assert validate[0].args[-1].startswith("/config/cave/.validating-")


def test_only_the_last_two_snapshots_are_kept(server, run_cave):
    for tag in (NEW, NEWER, "v2026.11.17.1"):
        server.release(tag)
        assert run_cave("update").code == 0

    snapshots = sorted(p.name for p in (server.data / ".cave/ops/snapshots").iterdir())
    assert snapshots == [
        "0002-v2026.11.15.1-to-v2026.11.16.1",
        "0003-v2026.11.16.1-to-v2026.11.17.1",
    ]


# --------------------------------------------------------------------- before anything is touched
def test_an_unhealthy_stack_is_left_alone(server, run_cave, notifications):
    server.release(NEW)
    server.stack.unhealthy.add("v2026.11.14.1")

    result = run_cave("update")

    assert result.code == 1
    assert server.stack.ops == []
    assert server.checked_out() == "v2026.11.14.1"
    assert "the running stack is not healthy" in notifications()[0]
    assert NEW not in server.record()["failed"]


@pytest.mark.parametrize(
    ("failing", "remembered", "says"),
    [
        ("pull", False, "pulling its images failed"),
        ("check_config", True, "its Home Assistant rejects the configuration"),
        ("validate", True, "it does not accept the applied heating.yaml"),
    ],
)
def test_a_failed_check_stops_nothing(
    server, run_cave, notifications, state_tree, failing, remembered, says
):
    before = state_tree(server.data)
    server.release(NEW)
    server.stack.fail[failing] = 1

    result = run_cave("update")

    assert result.code == 1
    assert not stopped(server)
    assert server.stack.running == "v2026.11.14.1"
    assert server.checked_out() == "v2026.11.14.1"
    assert state_tree(server.data) == before
    assert says in notifications()[0]
    assert "Nothing was changed" in notifications()[0]
    assert (NEW in server.record()["failed"]) is remembered
    assert not (server.data / ".cave/ops/tmp").exists()


def test_the_validators_messages_reach_the_owner(server, run_cave, notifications):
    server.release(NEW)
    server.stack.fail["validate"] = 1

    run_cave("update")

    assert "the field 'thermostats' must list at least one thermostat" in notifications()[0]


def test_local_changes_in_the_checkout_are_refused(server, run_cave, notifications):
    server.release(NEW)
    (server.root / "compose.yaml").write_text("edited by hand\n")

    result = run_cave("update")

    assert result.code == 1
    assert server.stack.ops == []
    assert "has local changes" in notifications()[0]


def test_too_little_space_for_the_snapshot_is_refused(server, run_cave, monkeypatch):
    server.release(NEW)
    monkeypatch.setattr(_server, "free_bytes", lambda path: 1024)

    result = run_cave("update")

    assert result.code == 1
    # Measured after the pull, which fills the same disk; nothing was stopped.
    assert server.stack.names() == ["pull"]
    assert "MiB are free" in result.err


def test_a_failed_fetch_changes_nothing(server, run_cave, notifications):
    server.runner.git_fail["fetch"] = [(128, "fatal: Could not read from remote repository.\n")]

    result = run_cave("update")

    assert result.code == 1
    assert server.stack.ops == []
    assert "Could not fetch releases" in notifications()[0]


def test_without_a_script_token_the_timer_does_not_update(server, run_cave, notifications):
    server.release(NEW)
    (server.data / "homeassistant/.cave/token").unlink()

    automatic = run_cave("update", "--auto")
    assert automatic.code == 1
    assert server.stack.ops == []
    assert "cannot tell a Home Assistant in recovery mode" in automatic.err

    by_hand = run_cave("update")
    assert by_hand.code == 0
    assert "Warning: there is no script token" in by_hand.err
    assert server.stack.running == NEW


def test_auto_update_off_stops_timer_runs_only(server, run_cave, ctx):
    server.release(NEW)
    ctx.env["CAVE_AUTO_UPDATE"] = "0"

    timer = run_cave("update", "--auto")
    assert timer.code == 0
    assert "Automatic updates are off" in timer.out
    assert server.stack.ops == []

    by_hand = run_cave("update")
    assert by_hand.code == 0
    assert server.stack.running == NEW


def test_the_wrong_user_is_refused_before_anything(server, run_cave, monkeypatch, fake_runner):
    server.release(NEW)
    monkeypatch.setattr(_server, "current_uid", lambda: 4242)

    result = run_cave("update")

    assert result.code == 1
    assert "`cave update` must run as root" in result.err
    assert "sudo ./cave update" in result.err
    assert fake_runner.calls == []


# ------------------------------------------------------------------------------- the switch
def test_an_unhealthy_release_is_rolled_back_with_its_snapshot(
    server, run_cave, notifications, state_tree
):
    before = state_tree(server.data)
    old_commit = server.head()
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    result = run_cave("update")

    assert result.code == 2
    assert server.head() == old_commit
    assert server.stack.running == "v2026.11.14.1"
    # The state is the snapshot's: the release's migration and traces are gone.
    assert server.schema() == "v2026.11.14.1"
    assert not (server.data / f"homeassistant/started-{NEW}").exists()
    after = state_tree(server.data)
    after.pop("homeassistant/started-v2026.11.14.1")  # written again by the restart
    before.pop("homeassistant/started-v2026.11.14.1")
    assert after == before
    assert server.stack.names()[-3:] == ["up", "down", "up"]
    message = notifications()[-1]
    assert message.startswith(f"Update to {NEW} failed: {NEW} did not become healthy")
    assert "Rolled back: the previous release v2026.11.14.1" in message
    assert server.record()["failed"][NEW]["reason"].startswith(f"{NEW} did not become healthy")
    assert server.record()["switch"] is None


def test_a_half_done_checkout_is_undone_by_the_rollback(server, run_cave, notifications):
    server.release(NEW)

    def half_done(argv):
        if "--force" not in argv:  # the switch's checkout, not the rollback's
            (server.root / "RELEASE").write_text(NEW + "\n")

    server.runner.git_before["checkout"] = half_done
    server.runner.git_fail["checkout"] = [(1, "error: unable to unlink old 'tools/x'\n")]

    result = run_cave("update")

    assert result.code == 2
    assert f"{NEW} could not be checked out" in notifications()[-1]
    assert server.checked_out() == "v2026.11.14.1"
    assert server.stack.running == "v2026.11.14.1"
    assert server.schema() == "v2026.11.14.1"


def test_a_release_that_cannot_be_started_is_rolled_back(server, run_cave, notifications):
    server.release(NEW)
    server.stack.fail_up.add(NEW)

    result = run_cave("update")

    assert result.code == 2
    assert f"{NEW} could not be started" in notifications()[-1]
    assert server.stack.running == "v2026.11.14.1"
    assert server.schema() == "v2026.11.14.1"


def test_when_the_previous_release_cannot_be_checked_out_it_says_so(
    server, run_cave, notifications
):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    # The rollback's checkout and its fallback, reset --hard, both fail.
    server.runner.git_before["checkout"] = lambda argv: (
        server.runner.git_fail.setdefault("checkout", [(1, "fatal: index.lock exists\n")])
        if "--force" in argv
        else None
    )
    server.runner.git_fail["reset"] = [(128, "fatal: unable to write new index file\n")]

    result = run_cave("update")

    assert result.code == 3
    message = notifications()[-1]
    assert "the previous release v2026.11.14.1 could not be checked out" in message
    assert "whatever is checked out was started" in message


def test_a_rollback_that_is_unhealthy_leaves_the_previous_release_running(
    server, run_cave, notifications
):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    # Once the new release has run, the previous one does not come back healthy either.
    server.stack.after["up"] = lambda release: server.stack.unhealthy.add("v2026.11.14.1")

    result = run_cave("update")

    assert result.code == 3
    assert server.stack.running == "v2026.11.14.1"
    assert server.checked_out() == "v2026.11.14.1"
    message = notifications()[-1]
    assert "The previous release v2026.11.14.1 was started and left running" in message
    assert "NOT healthy" in message
    assert server.record()["last_result"]["status"] == 3


def test_when_nothing_can_be_started_it_says_so(server, run_cave, journal):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    server.stack.after["up"] = lambda release: server.stack.fail_up.add("v2026.11.14.1")

    result = run_cave("update")

    assert result.code == 3
    assert server.stack.running is None
    assert "NOTHING IS RUNNING" in result.err
    # Home Assistant is down, so only the journal and the record have it.
    assert any("NOTHING IS RUNNING" in line for line in journal)
    assert "NOTHING IS RUNNING" in server.record()["last_result"]["message"]
    # One retry of the start, then it gave up.
    assert server.stack.names()[-3:] == ["down", "up", "up"]


def test_a_stack_that_cannot_be_stopped_is_started_again_unchanged(server, run_cave, notifications):
    server.release(NEW)
    server.stack.fail["down"] = 1

    result = run_cave("update")

    assert result.code == 1
    assert server.checked_out() == "v2026.11.14.1"
    assert server.stack.running == "v2026.11.14.1"
    assert "could not be stopped" in notifications()[-1]
    assert "Nothing was changed" in notifications()[-1]
    assert NEW not in server.record()["failed"]
    assert not (server.data / ".cave/ops/snapshots").exists() or not list(
        (server.data / ".cave/ops/snapshots").iterdir()
    )


def test_a_failed_snapshot_leaves_no_partial_copy_and_starts_the_previous_release(
    server, run_cave, monkeypatch, notifications
):
    server.release(NEW)
    copied = []
    real_copy = _server.copy_entry

    def disk_full(source, target):
        if len(copied) == 2:
            raise OSError(28, "No space left on device")
        copied.append(source)
        real_copy(source, target)

    monkeypatch.setattr(_server, "copy_entry", disk_full)

    result = run_cave("update")

    assert result.code == 1
    assert list((server.data / ".cave/ops/snapshots").iterdir()) == []
    assert server.stack.running == "v2026.11.14.1"
    assert "No space left on device" in notifications()[-1]


def test_ctrl_c_during_the_switch_still_rolls_back(server, run_cave):
    server.release(NEW)

    def interrupt(release):
        if release == NEW:
            raise KeyboardInterrupt

    server.stack.before["up"] = interrupt

    result = run_cave("update")

    assert result.code == 2
    assert server.stack.running == "v2026.11.14.1"
    assert server.checked_out() == "v2026.11.14.1"
    assert "interrupted (KeyboardInterrupt" in result.err


# ------------------------------------------------------------------------ remembered failure
def test_a_failed_release_is_skipped_until_forced_or_superseded(server, run_cave, notifications):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    assert run_cave("update").code == 2
    server.stack.ops.clear()
    sent = len(notifications())

    night = run_cave("update", "--auto")
    assert night.code == 0
    assert f"{NEW} failed on" in night.out
    assert server.stack.ops == []
    assert len(notifications()) == sent

    server.stack.unhealthy.discard(NEW)
    forced = run_cave("update", "--force")
    assert forced.code == 0
    assert server.stack.running == NEW
    assert NEW not in server.record()["failed"]


def test_a_newer_release_replaces_a_failed_one(server, run_cave):
    server.release(NEW)
    server.stack.fail["check_config"] = 1
    assert run_cave("update").code == 1
    del server.stack.fail["check_config"]

    server.release(NEWER)
    assert run_cave("update").code == 0
    assert server.stack.running == NEWER


# ----------------------------------------------------------------------------------- the lock
def test_a_concurrent_run_is_refused(server, run_cave, ctx, fake_runner, notifications):
    server.release(NEW)
    with Lock(ctx.paths.lock_file, "backup"):
        result = run_cave("update")

    assert result.code == 1
    assert "`cave backup`" in result.err
    assert "is running" in result.err
    assert fake_runner.calls == []
    assert "Not updated" in notifications()[0]


def test_a_stale_lock_file_does_not_block(server, run_cave, ctx):
    ctx.paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.lock_file.write_text("`cave update` (pid 999999, since 2026-11-01 03:30:00)\n")
    server.release(NEW)

    assert run_cave("update").code == 0
    assert server.stack.running == NEW


def test_a_full_disk_on_the_way_back_does_not_keep_the_stack_stopped(server, run_cave, monkeypatch):
    from cave_ops.commands import _update_state

    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    real_save = _update_state.save

    def full_disk(ctx, record):
        if record.switch is None or record.switch.phase == "rolling-back":
            raise OSError(28, "No space left on device")
        real_save(ctx, record)

    monkeypatch.setattr(_update_state, "save", full_disk)

    result = run_cave("update")

    assert result.code == 2
    assert server.stack.running == "v2026.11.14.1"
    assert "The update record could not be written" in result.err
