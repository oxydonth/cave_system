"""``cave watch`` against a local HTTP receiver: one alert per incident and one all-clear.

The server is the fake one of ``conftest.py`` (real state and Git, Docker faked). The watch runs
every five minutes; here each ``watch(...)`` call is such a run, ``minutes`` after the first one
(the wall clock is the fake clock). Each test is a row of the watch's table of exits in the pull
request of work package 11.
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading

import pytest

from cave_ops import alert
from cave_ops import lock as lock_module
from cave_ops.commands import restore
from cave_ops.lock import Lock, held_by

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
START = 1_800_000_000.0


@pytest.fixture
def receiver(ctx, alert_endpoint, clock):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url
    ctx.wall = lambda: START + clock.now
    return alert_endpoint


@pytest.fixture
def watch(run_cave, clock):
    """``watch(minute)``: a run of the watch that many minutes after the first one. The clock
    only goes forward: commands run in between (which wait on it) may have moved it on."""
    origin = []

    def run(minute):
        if not origin:
            origin.append(clock.now)
        clock.now = max(clock.now, origin[0] + minute * 60.0)
        result = run_cave("watch")
        assert result.code == 0, result.err
        return result

    return run


def every_five_minutes(watch, start, end):
    for minute in range(start, end + 1, 5):
        watch(minute)


def headlines(receiver):
    return [title.split(": ", 1)[1] for title in receiver.titles()]


# ----------------------------------------------------------------------------- nothing to do
def test_unset_the_watch_does_nothing(server, run_cave, alert_endpoint, ctx):
    server.stack.running = None
    calls = len(server.runner.fake.calls)

    result = run_cave("watch")

    assert result.code == 0
    assert "CAVE_ALERT_URL is not set" in result.out
    assert len(server.runner.fake.calls) == calls, "Docker or Git was asked"
    assert alert_endpoint.received == []
    assert not ctx.paths.watch_record.exists()


def test_a_healthy_stack_is_not_reported(server, receiver, watch):
    every_five_minutes(watch, 0, 60)

    assert receiver.received == []


# --------------------------------------------------------------------------------- incidents
def test_a_stack_stopped_for_fifteen_minutes_is_alerted_once_and_its_return_once(
    server, receiver, watch
):
    server.stack.running = None

    every_five_minutes(watch, 0, 10)
    assert receiver.received == [], "alerted before fifteen minutes"
    watch(15)
    every_five_minutes(watch, 20, 60)
    assert headlines(receiver) == ["the stack is stopped"]
    body = receiver.received[0].body
    assert "Nothing runs, and it was not stopped on purpose" in body
    assert "Stack: stopped: nothing runs" in body
    assert "Command: cave watch" in body

    server.stack.start(OLD)
    every_five_minutes(watch, 65, 90)

    assert headlines(receiver) == ["the stack is stopped", "healthy again"]
    assert "Stack: running and healthy" in receiver.received[1].body


def test_a_stack_that_runs_unhealthy_is_alerted(server, receiver, watch):
    server.stack.unhealthy.add(OLD)

    every_five_minutes(watch, 0, 30)

    assert headlines(receiver) == ["the stack is not healthy"]
    assert "Stack: running, but NOT healthy" in receiver.received[0].body


def test_an_interrupted_update_is_an_incident(server, run_cave, receiver, watch, killed):
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()

    every_five_minutes(watch, 0, 30)
    assert headlines(receiver) == ["an update was interrupted"]

    assert run_cave("update", "--recover").code == 2  # rolled back; that alerts by itself
    watch(35)
    watch(40)

    assert headlines(receiver) == [
        "an update was interrupted",
        "update rolled back",
        "healthy again",
    ]


def test_a_restore_that_stopped_half_way_is_an_incident(
    server,
    run_cave,
    receiver,
    watch,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 6:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()

    every_five_minutes(watch, 0, 30)

    assert headlines(receiver) == ["a restore stopped half-way"]


def test_a_command_s_alert_is_the_incident_s_alert_and_the_watch_sends_the_all_clear(
    server, run_cave, receiver, watch
):
    """An update that leaves nothing running alerts at once; the watch does not alert the same
    incident again, and says when the stack is healthy again."""
    server.release(NEW)
    server.stack.fail_up.update({NEW, OLD})
    assert run_cave("update").code == 3
    assert headlines(receiver) == ["update failed, act now"]
    assert "Stack: stopped: nothing runs" in receiver.received[0].body

    every_five_minutes(watch, 0, 60)
    assert headlines(receiver) == ["update failed, act now"]

    server.stack.fail_up.clear()
    assert run_cave("update", "--recover").code == 2
    watch(65)

    assert headlines(receiver) == ["update failed, act now", "update rolled back", "healthy again"]


# ------------------------------------------------------------------- stopped on purpose: quiet
def test_a_stack_stopped_with_down_is_not_reported_and_up_ends_that(
    server, run_cave, receiver, watch
):
    assert run_cave("down").code == 0
    assert server.stack.running is None

    every_five_minutes(watch, 0, 60)
    assert receiver.received == []

    assert run_cave("up").code == 0
    assert not server.data.joinpath(".cave", "stopped.json").exists()
    server.stack.running = None  # it stops by itself afterwards: that is an incident again
    every_five_minutes(watch, 65, 85)

    assert headlines(receiver) == ["the stack is stopped"]


@pytest.mark.parametrize("undo", [False, True])
def test_a_restore_or_undo_with_no_start_is_not_reported(
    server,
    run_cave,
    receiver,
    watch,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    undo,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    if undo:
        renames = []
        real_rename = os.rename

        def power_cut(source, target):
            renames.append(source)
            if len(renames) == 6:
                raise killed
            real_rename(source, target)

        monkeypatch.setattr(restore.os, "rename", power_cut)
        with pytest.raises(killed):
            run_cave("restore", str(archive))
        monkeypatch.undo()
        assert run_cave("restore", "--undo", "--no-start").code == 0
    else:
        assert run_cave("restore", str(archive), "--no-start").code == 0
    assert server.stack.running is None

    every_five_minutes(watch, 0, 60)

    assert receiver.received == []


def test_an_alerted_incident_stopped_on_purpose_is_closed_only_once_healthy(
    server, run_cave, receiver, watch
):
    server.stack.running = None
    every_five_minutes(watch, 0, 15)
    assert headlines(receiver) == ["the stack is stopped"]

    assert run_cave("down").code == 0
    every_five_minutes(watch, 20, 40)
    assert headlines(receiver) == ["the stack is stopped"]

    assert run_cave("up").code == 0
    watch(45)
    assert headlines(receiver) == ["the stack is stopped", "healthy again"]


# --------------------------------------------------------------------- a command is at work
def test_nothing_is_said_while_a_command_holds_the_lock(server, receiver, watch, ctx):
    server.stack.running = None  # an update or restore has stopped the stack

    with Lock(ctx.paths.lock_file, "update"):
        result = None
        for minute in range(0, 31, 5):
            result = watch(minute)
        assert "`cave update`" in result.out
    assert receiver.received == []

    # Once it has finished, the count starts afresh.
    every_five_minutes(watch, 35, 45)
    assert receiver.received == []
    watch(50)
    assert headlines(receiver) == ["the stack is stopped"]


def test_an_update_starting_while_the_watch_reads_the_lock_is_not_refused(
    server, run_cave, ctx, clock, monkeypatch
):
    """The watch reads who holds the lock with a shared lock for an instant. An update that
    tries to take it in exactly that instant keeps trying for a moment, and goes ahead."""
    server.release(NEW)
    ctx.paths.lock_file.touch()
    in_probe, probe_may_end = threading.Event(), threading.Event()
    real_flock = fcntl.flock

    def flock(fd, operation):
        real_flock(fd, operation)
        if operation == fcntl.LOCK_SH | fcntl.LOCK_NB and threading.current_thread().name == "w":
            in_probe.set()
            probe_may_end.wait(10)

    monkeypatch.setattr(lock_module.fcntl, "flock", flock)
    seen = []
    watcher = threading.Thread(
        target=lambda: seen.append(held_by(ctx.paths.lock_file)), name="w", daemon=True
    )
    watcher.start()
    assert in_probe.wait(10)

    def sleep(seconds):
        clock.sleep(seconds)
        probe_may_end.set()
        watcher.join(10)

    ctx.sleep = sleep

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.stack.running == NEW
    assert seen == [None]


# ------------------------------------------------------------------------------ time and delivery
def test_runs_far_apart_start_the_count_afresh(server, receiver, watch):
    """A power cut: the stack was down at the last run before it, and is still starting at the
    first run after it, hours later. That is not fifteen minutes of watching."""
    server.stack.running = None
    watch(0)
    watch(5)

    watch(180)
    assert receiver.received == []
    every_five_minutes(watch, 185, 190)
    assert receiver.received == []
    watch(195)
    assert headlines(receiver) == ["the stack is stopped"]


def test_an_alert_that_could_not_be_delivered_is_tried_again(server, receiver, watch):
    server.stack.running = None
    receiver.mode = "error"
    every_five_minutes(watch, 0, 20)
    assert len(receiver.received) == 2  # at 15 and 20 minutes, both refused

    receiver.mode = "ok"
    every_five_minutes(watch, 25, 40)

    assert headlines(receiver) == ["the stack is stopped"] * 3


def test_a_memory_that_cannot_be_written_is_said(server, run_cave, receiver, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(alert, "write_atomic", full)

    result = run_cave("watch")

    assert result.code == 1
    assert "cannot remember what it has alerted" in result.err


# ------------------------------------------------------------------------------ cut off, failed
def test_a_down_that_did_not_stop_the_stack_leaves_no_lasting_exemption(
    server, run_cave, receiver, watch
):
    """`down` records the stop first. If Docker then fails to stop the stack, the record must not
    keep the watch quiet for good: once containers still run an interval later, it is dropped."""
    server.stack.fail["down"] = 1
    assert run_cave("down").code != 0
    del server.stack.fail["down"]
    assert server.data.joinpath(".cave", "stopped.json").exists()

    every_five_minutes(watch, 0, 10)
    assert not server.data.joinpath(".cave", "stopped.json").exists()
    server.stack.running = None  # it stops later, for another reason
    every_five_minutes(watch, 15, 30)

    assert headlines(receiver) == ["the stack is stopped"]


def test_a_watch_cut_off_after_sending_sends_once_more(
    server, receiver, watch, monkeypatch, killed
):
    """Killed after the alert went out, before it was remembered: the next run sends it again.
    One repeat is the price of never losing an alert."""
    server.stack.running = None
    every_five_minutes(watch, 0, 10)
    real_save = alert.save_memory

    def cut(paths, memory):
        if memory.alerted is not None:
            raise killed
        real_save(paths, memory)

    monkeypatch.setattr(alert, "save_memory", cut)
    with pytest.raises(killed):
        watch(15)
    monkeypatch.undo()
    every_five_minutes(watch, 20, 40)

    assert headlines(receiver) == ["the stack is stopped", "the stack is stopped"]


def test_a_command_s_alert_that_could_not_be_remembered_is_alerted_by_the_watch(
    server, run_cave, receiver, watch, monkeypatch
):
    server.release(NEW)
    server.stack.fail_up.update({NEW, OLD})

    def full(paths, memory):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(alert, "save_memory", full)
    result = run_cave("update")
    monkeypatch.undo()
    assert result.code == 3
    assert "What was alerted could not be recorded" in result.err

    every_five_minutes(watch, 0, 20)

    assert headlines(receiver) == ["update failed, act now", "an update was interrupted"]
