"""A restore series that began by superseding an interrupted update ends, whatever exit it takes
and wherever it is cut off, in exactly one of two states, never a third:

S1 "restored": the archive's state on the release from before the update; the update forgotten.
S2 "from before": the update's own way back has put its snapshot (the state from before the
   update) and the release from before it back together; the update is finished and its release
   marked as failed.

Each test ends with what the next boot does (``cave update --recover``) and then checks the state.
The state is changed after the backup, so S1 (the archive's) and S2 (the snapshot's) differ.
"""

from __future__ import annotations

import os

import pytest

from cave_ops.commands import restore, update

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
ARCHIVE_AUTOMATIONS = b"[]\n"
LATER_AUTOMATIONS = b"- id: made after the backup\n"


def interrupted_update(server, run_cave, backups, killed):  # noqa: F811
    """A backup, a change, then an update killed after the new release migrated the state."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    (server.data / "homeassistant/automations.yaml").write_bytes(LATER_AUTOMATIONS)
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.after["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()
    assert server.schema() == NEW
    assert server.record()["switch"]["phase"] == "switching"
    return archive


def boot_and_state(server, run_cave):
    """What the next boot does, then which of the two states the server is in."""
    run_cave("update", "--recover")
    record = server.record()
    marker = server.data / ".cave/ops/restore.json"
    automations = (server.data / "homeassistant/automations.yaml").read_bytes()
    common = (
        server.checked_out() == OLD
        and server.schema() == OLD
        and record["switch"] is None
        and not marker.exists()
        and server.stack.running == OLD
    )
    if common and automations == ARCHIVE_AUTOMATIONS:
        return "S1"
    if common and automations == LATER_AUTOMATIONS and NEW in record["failed"]:
        return "S2"
    return (
        f"a third state: checkout {server.checked_out()}, schema {server.schema()}, switch "
        f"{record['switch']}, marker {marker.exists()}, running {server.stack.running}, "
        f"automations {automations!r}"
    )


def kill_once(monkeypatch, module, name, killed, when=lambda *a, **k: True):
    """Replace ``module.name`` by a function that is killed the first time ``when`` holds."""
    real = getattr(module, name)
    done = []

    def cut(*args, **kwargs):
        if not done and when(*args, **kwargs):
            done.append(1)
            raise killed
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, cut)


def cut_restore(server, run_cave, archive, monkeypatch, killed, module, name, when=None):
    kill_once(monkeypatch, module, name, killed, when or (lambda *a, **k: True))
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()


# ---------------------------------------------------------------- the exits of the first attempt
def test_success_restores_the_archive_and_forgets_the_update(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
):
    archive = interrupted_update(server, run_cave, backups, killed)

    assert run_cave("restore", str(archive)).code == 0

    assert boot_and_state(server, run_cave) == "S1"


def test_a_failed_unpack_finishes_the_updates_way_back(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    archive = interrupted_update(server, run_cave, backups, killed)

    def broken(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(restore._server, "extract", broken)
    assert run_cave("restore", str(archive)).code == 1
    monkeypatch.undo()

    assert boot_and_state(server, run_cave) == "S2"


def test_killed_after_unpacking_before_the_marker_the_update_is_still_in_flight(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(server, run_cave, archive, monkeypatch, killed, restore, "_write_marker")
    assert not (server.data / ".cave/ops/restore.json").exists()

    assert boot_and_state(server, run_cave) == "S2"


@pytest.mark.parametrize("then", ["repeat", "undo"])
def test_killed_after_the_marker_before_the_update_was_forgotten(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
    then,
):
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(server, run_cave, archive, monkeypatch, killed, restore, "save_update_record")
    # Both are recorded: the update in flight and the restore that supersedes it. The boot's
    # recovery waits for the restore; the owner repeats it or undoes it.
    assert server.record()["switch"] is not None
    assert run_cave("update", "--recover").code == 1

    if then == "repeat":
        assert run_cave("restore", str(archive)).code == 0
        assert boot_and_state(server, run_cave) == "S1"
    else:
        run_cave("restore", "--undo")
        assert boot_and_state(server, run_cave) == "S2"


@pytest.mark.parametrize("then", ["repeat", "undo"])
def test_killed_while_moving_the_state_in(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
    then,
):
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(
        server,
        run_cave,
        archive,
        monkeypatch,
        killed,
        restore.os,
        "rename",
        lambda source, target: "restore-staging" in str(source),  # aside done, moving in
    )

    if then == "repeat":
        assert run_cave("restore", str(archive)).code == 0
        assert boot_and_state(server, run_cave) == "S1"
    else:
        run_cave("restore", "--undo")
        assert boot_and_state(server, run_cave) == "S2"


@pytest.mark.parametrize("then", ["repeat", "undo"])
def test_killed_after_the_new_env_before_the_marker_went(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
    then,
):
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(
        server,
        run_cave,
        archive,
        monkeypatch,
        killed,
        restore,
        "load_env",
        lambda path: bool(list(server.root.glob(".env.before-restore-*"))),
    )
    assert (server.data / ".cave/ops/restore.json").exists()

    if then == "repeat":
        assert run_cave("restore", str(archive)).code == 0
        assert boot_and_state(server, run_cave) == "S1"
    else:
        run_cave("restore", "--undo")
        assert boot_and_state(server, run_cave) == "S2"


def test_a_failed_repeat_then_undo(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(server, run_cave, archive, monkeypatch, killed, restore.os, "rename")

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", broken)
    assert run_cave("restore", str(archive)).code == 1  # the marker stays, nothing starts
    monkeypatch.undo()
    run_cave("restore", "--undo")

    assert boot_and_state(server, run_cave) == "S2"


# ---------------------------------------------------------------------- --undo cut off anywhere
def series_cut_while_moving(server, run_cave, backups, killed, monkeypatch):  # noqa: F811
    archive = interrupted_update(server, run_cave, backups, killed)
    cut_restore(server, run_cave, archive, monkeypatch, killed, restore.os, "rename")
    return archive


@pytest.mark.parametrize("rename", [1, 2, 3])
def test_undo_cut_while_putting_back_is_finished_by_running_it_again(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
    rename,
):
    series_cut_while_moving(server, run_cave, backups, killed, monkeypatch)
    calls = []

    def nth(source, target):
        calls.append(source)
        return len(calls) == rename

    kill_once(monkeypatch, restore.os, "rename", killed, nth)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    monkeypatch.undo()
    run_cave("restore", "--undo")

    assert boot_and_state(server, run_cave) == "S2"


def test_undo_cut_after_the_state_was_back_is_finished_by_the_next_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    series_cut_while_moving(server, run_cave, backups, killed, monkeypatch)
    kill_once(monkeypatch, restore, "complete_undo", killed)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    monkeypatch.undo()
    # The state from before is back and the marker says so, but the update is not yet in flight
    # again: the next boot's update finishes what --undo began.

    assert boot_and_state(server, run_cave) == "S2"


def test_undo_cut_before_the_way_back_is_finished_by_the_next_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    series_cut_while_moving(server, run_cave, backups, killed, monkeypatch)
    kill_once(monkeypatch, update._Update, "finish_interrupted", killed)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    monkeypatch.undo()
    assert server.record()["switch"] is not None  # in flight again
    assert not (server.data / ".cave/ops/restore.json").exists()

    assert boot_and_state(server, run_cave) == "S2"


def test_undo_cut_during_the_way_back_is_finished_by_the_next_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
    monkeypatch,
):
    series_cut_while_moving(server, run_cave, backups, killed, monkeypatch)

    def die(release):
        if release == OLD:
            raise killed  # the way back is about to start the previous release

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    server.stack.before.clear()

    assert boot_and_state(server, run_cave) == "S2"


def test_os_is_unchanged_by_these_tests():
    """A guard that monkeypatches were undone (``os.rename`` is patched through modules)."""
    assert os.rename.__module__ in ("posix", "nt")
