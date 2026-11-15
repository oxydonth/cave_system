"""An update killed, or cut off by a power failure, at any step: what the next run finds and does.

``killed`` stands for SIGKILL or a power cut: it is raised from inside a step and no handler of
``cave`` runs, so the disk is left exactly as the step left it. The next run is
``cave update --recover`` (systemd runs it at boot and after the update service stops) or the
next ``cave update``.
"""

from __future__ import annotations

import os
import signal

import pytest

from cave_ops.commands import _update_state
from cave_ops.lock import Lock

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


def kill_at(server, killed, operation, release=None, after=False):
    def hook(current):
        if release is None or current == release:
            raise killed

    (server.stack.after if after else server.stack.before)[operation] = hook


def test_nothing_to_recover_does_nothing(server, run_cave, fake_runner):
    result = run_cave("update", "--recover")

    assert result.code == 0
    assert "nothing to recover" in result.out
    assert fake_runner.calls == []


def test_killed_after_stopping_before_the_snapshot(server, run_cave, killed, notifications):
    server.release(NEW)
    kill_at(server, killed, "down", after=True)
    with pytest.raises(killed):
        run_cave("update")
    assert server.stack.running is None
    assert server.record()["switch"]["phase"] == "before-switch"
    server.stack.after.clear()

    result = run_cave("update", "--recover")

    assert result.code == 1
    assert server.stack.running == OLD
    assert server.checked_out() == OLD
    assert server.record()["switch"] is None
    assert NEW not in server.record()["failed"]
    assert "interrupted" in notifications()[-1]
    assert "Nothing was changed" in notifications()[-1]


def test_a_half_written_snapshot_is_discarded(server, run_cave, killed, monkeypatch, state_tree):
    before = state_tree(server.data)
    server.release(NEW)
    copies = []
    real_copy = _update_state._server.copy_entry

    def power_cut(source, target):
        copies.append(source)
        if len(copies) == 5:  # a few files copied, the rest not
            raise killed
        real_copy(source, target)

    # take_snapshot removes a partial copy when it fails; a power cut gives it no chance.
    monkeypatch.setattr(_update_state._server, "copy_entry", power_cut)
    monkeypatch.setattr(_update_state._server, "remove", lambda path: None)
    with pytest.raises(killed):
        run_cave("update")
    monkeypatch.undo()
    snapshots = server.data / ".cave/ops/snapshots"
    assert [p.name for p in snapshots.iterdir()] == ["0001-v2026.11.14.1-to-v2026.11.15.1.partial"]

    result = run_cave("update", "--recover")

    assert result.code == 1
    assert list(snapshots.iterdir()) == []
    assert server.stack.running == OLD
    after = state_tree(server.data)
    assert after.pop(f"homeassistant/started-{OLD}") == before.pop(f"homeassistant/started-{OLD}")
    assert after == before


def test_killed_after_the_checkout_before_the_start(
    server, run_cave, killed, notifications, state_tree
):
    before = state_tree(server.data)
    server.release(NEW)
    kill_at(server, killed, "up", release=NEW)
    with pytest.raises(killed):
        run_cave("update")
    assert server.checked_out() == NEW
    assert server.stack.running is None
    assert server.record()["switch"]["phase"] == "switching"
    server.stack.before.clear()

    result = run_cave("update", "--recover")

    assert result.code == 2
    assert server.checked_out() == OLD
    assert server.stack.running == OLD
    after = state_tree(server.data)
    after.pop(f"homeassistant/started-{OLD}")
    before.pop(f"homeassistant/started-{OLD}")
    assert after == before
    assert "interrupted (killed or a power cut) during the switch" in notifications()[-1]
    # Not tried again by itself: an update that died during the switch is suspect.
    assert NEW in server.record()["failed"]


def test_killed_while_the_new_release_runs(server, run_cave, killed):
    server.release(NEW)
    kill_at(server, killed, "up", release=NEW, after=True)
    with pytest.raises(killed):
        run_cave("update")
    assert server.stack.running == NEW
    assert server.schema() == NEW  # the new release has migrated the state

    result = run_cave("update", "--recover")

    assert result.code == 2
    assert server.stack.running == OLD
    assert server.schema() == OLD
    assert not (server.data / f"homeassistant/started-{NEW}").exists()


def test_killed_while_putting_the_snapshot_back(server, run_cave, killed, monkeypatch, state_tree):
    before = state_tree(server.data)
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 3:  # some entries moved away, none put back yet
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(_update_state.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("update")
    monkeypatch.undo()
    assert server.record()["switch"]["phase"] == "rolling-back"
    assert not (server.data / "homeassistant").exists()

    result = run_cave("update", "--recover")

    assert result.code == 2
    assert server.stack.running == OLD
    after = state_tree(server.data)
    after.pop(f"homeassistant/started-{OLD}")
    before.pop(f"homeassistant/started-{OLD}")
    assert after == before
    assert not (server.data / ".cave/ops/restoring").exists()
    assert not (server.data / ".cave/ops/discard").exists()


def test_the_next_ordinary_run_finishes_an_interrupted_update_first(server, run_cave, killed):
    server.release(NEW)
    kill_at(server, killed, "up", release=NEW)
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()

    result = run_cave("update", "--auto")

    assert result.code == 2
    assert "was interrupted (switching). Finishing it first." in result.err
    assert server.stack.running == OLD


def test_auto_update_off_does_not_stop_the_recovery(server, run_cave, killed, ctx):
    server.release(NEW)
    kill_at(server, killed, "up", release=NEW)
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    ctx.env["CAVE_AUTO_UPDATE"] = "0"

    assert run_cave("update", "--auto").code == 2
    assert server.stack.running == OLD


def test_recover_waits_for_a_backup_to_release_the_lock(server, run_cave, killed, ctx):
    server.release(NEW)
    kill_at(server, killed, "up", release=NEW)
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    backup = Lock(ctx.paths.lock_file, "backup")
    backup.acquire()
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        backup.release()  # the backup finishes while recover waits

    ctx.sleep = sleep

    result = run_cave("update", "--recover")

    assert waited[0] == 10
    assert result.code == 2
    assert server.stack.running == OLD


def test_sigterm_during_the_switch_rolls_back(server, run_cave):
    server.release(NEW)
    server.stack.before["up"] = lambda release: (
        os.kill(os.getpid(), signal.SIGTERM) if release == NEW else None
    )

    result = run_cave("update")

    assert result.code == 2
    assert "interrupted (SystemExit: 143)" in result.err
    assert server.stack.running == OLD
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
