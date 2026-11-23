"""``cave update``: the items of issues #25 and #24 that change what it does (work package 11)."""

from __future__ import annotations

import fcntl
import json
import os

import pytest

from cave_ops.lock import Lock

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
NEWER = "v2026.11.16.1"
PROBLEM = "binary_sensor.cave_bad_problem"


def _marker(server):
    return server.data / ".cave" / "ops" / "restore.json"


# ------------------------------------------------- the newest release failed: the one before it
def test_when_the_newest_release_failed_the_newest_that_did_not_is_installed(
    server, run_cave, notifications
):
    server.release(NEW)
    server.release(NEWER)
    server.stack.unhealthy.add(NEWER)
    assert run_cave("update").code == 2  # NEWER was tried, failed and is remembered

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.stack.running == NEW
    assert server.checked_out() == NEW
    assert (
        f"{NEWER} failed before and was skipped; {NEW} is the newest release"
        in (notifications()[-1])
    )
    # Next night: nothing newer that has not failed.
    again = run_cave("update")
    assert again.code == 0
    assert f"{NEWER} failed on" in again.out
    assert server.stack.running == NEW


def test_force_still_takes_the_newest_release(server, run_cave):
    server.release(NEW)
    server.release(NEWER)
    server.stack.unhealthy.add(NEWER)
    assert run_cave("update").code == 2
    server.stack.unhealthy.discard(NEWER)

    assert run_cave("update", "--force").code == 0

    assert server.stack.running == NEWER


# ------------------------------------------------------- a thermostat held by `cave device-check`
def test_the_switch_waits_while_device_check_holds_a_thermostat(
    server, run_cave, fake_ha, notifications
):
    server.release(NEW)
    fake_ha.states[PROBLEM] = {
        "state": "off",
        "attributes": {"thermostats": {"climate.bad_heizung": "held", "climate.bad_2": "ok"}},
    }

    result = run_cave("update")

    assert result.code == 1
    assert f"Not updating to {NEW} now: `cave device-check` holds climate.bad_heizung" in result.err
    assert "the next run tries again" in notifications()[-1]
    assert "down" not in server.stack.names(), "the stack was not stopped"
    assert server.checked_out() == OLD
    assert NEW not in server.record()["failed"]

    fake_ha.states[PROBLEM]["attributes"]["thermostats"]["climate.bad_heizung"] = "ok"
    assert run_cave("update").code == 0
    assert server.stack.running == NEW


# ------------------------------------------- a refusal because of a restore changes nothing
def test_a_refusal_because_of_a_restore_keeps_the_update_in_flight(server, run_cave, killed):
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    switch = server.record()["switch"]
    assert switch is not None
    _marker(server).write_text(
        json.dumps(
            {
                "archive": "cave-backup-x.tar.gz",
                "aside": "before-restore-x",
                "original_entries": [],
                "env_backup": None,
            }
        )
    )

    refused = run_cave("update")

    assert refused.code == 1
    assert "a restore of cave-backup-x.tar.gz was interrupted" in refused.err
    assert server.record()["switch"] == switch, "the refusal forgot the update in flight"


# --------------------------------------------------- what the clean-up after a checkout removed
def test_the_way_back_names_the_files_it_removed(server, run_cave, killed, notifications):
    server.release(NEW, files={"tools/new_tool.py": "print('new')\n"})

    def half_done(argv):
        if "--force" not in argv:  # the switch's checkout: it wrote one file, then the power went
            (server.root / "tools").mkdir(exist_ok=True)
            (server.root / "tools" / "new_tool.py").write_text("print('new')\n")
            raise killed

    server.runner.git_before["checkout"] = half_done
    with pytest.raises(killed):
        run_cave("update")
    server.runner.git_before.clear()

    result = run_cave("update", "--recover")

    assert result.code == 2
    assert not (server.root / "tools" / "new_tool.py").exists()
    message = notifications()[-1]
    assert f"removed, because they are {NEW}'s own files, byte for byte" in message
    assert "tools/new_tool.py" in message


# ------------------------------------------------- the lock: a probe of `held_by` refuses nothing
def test_a_shared_probe_of_the_lock_does_not_refuse_an_update(server, run_cave, ctx, clock):
    """``status`` and ``cave watch`` read who holds the lock with a shared lock for an instant.
    An update that starts in exactly that instant keeps trying for a moment instead of refusing."""
    server.release(NEW)
    ctx.paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.lock_file.touch()
    probe = os.open(ctx.paths.lock_file, os.O_RDONLY)
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    tries = []

    def sleep(seconds):
        tries.append(seconds)
        clock.sleep(seconds)
        if len(tries) == 1:
            os.close(probe)  # the probe ends

    ctx.sleep = sleep

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.stack.running == NEW
    assert tries[0] < 1


def test_a_command_that_really_holds_the_lock_still_refuses(server, run_cave, ctx):
    server.release(NEW)
    with Lock(ctx.paths.lock_file, "backup"):
        result = run_cave("update")

    assert result.code == 1
    assert "`cave backup`" in result.err
    assert server.stack.ops == []


# ----------------------------------------- the first review of #30: what it found untested
def test_when_home_assistant_cannot_say_whether_a_thermostat_is_held_the_update_goes_ahead(
    server, run_cave, ctx, fake_ha
):
    """It was healthy a moment before, with the same token. Holding every update back because
    one list could not be read would stop updates for good; a check that the switch spoils only
    has to be run again."""
    server.release(NEW)

    def states_fail(method, url, headers, body, timeout):
        if url.endswith("/api/states"):
            return 500, b'{"message": "internal error"}'
        return fake_ha(method, url, headers, body, timeout)

    ctx.transport = states_fail

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.stack.running == NEW
    assert "Could not ask Home Assistant whether a thermostat is held" in result.err


@pytest.mark.parametrize("error", [OSError(5, "I/O error"), RuntimeError("a bug")])
def test_a_failure_while_removing_old_images_never_changes_the_exit_status(
    server, run_cave, monkeypatch, error
):
    from cave_ops.commands import _images

    def broken(ctx):
        raise error

    monkeypatch.setattr(_images, "prune", broken)
    server.release(NEW)

    result = run_cave("update")

    assert result.code == 0
    assert server.stack.running == NEW
    assert "Old images were not removed" in result.err


def test_a_probe_that_lasts_a_second_still_refuses_no_update(server, run_cave, ctx, clock):
    """The patience is meant for an instant's probe by `status` or the watch, which a busy server
    may stretch: a second is well inside it."""
    server.release(NEW)
    ctx.paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.lock_file.touch()
    probe = os.open(ctx.paths.lock_file, os.O_RDONLY)
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        clock.sleep(seconds)
        if sum(waited) >= 1.0 and probe not in released:
            released.append(probe)
            os.close(probe)

    released = []
    ctx.sleep = sleep

    result = run_cave("update")

    assert result.code == 0, result.err
    assert sum(waited) >= 1.0
