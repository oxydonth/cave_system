"""Every restore and every `--undo` ends with the stack started, or with `--no-start` said by
`status`: the reviewer's test of the sixth review of pull request #10 and the rows added for it,
then the reviewer's test of the seventh review (a restore after an owed start) and its rows."""

from __future__ import annotations

import errno
import json
import os

import pytest

from cave_ops.commands import restore

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"


def _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed):
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 6:  # four entries aside, one moved in, then the cut
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()


# A plain restore (no update superseded) stops half-way; `--undo` puts the state back, marks the
# marker "undone", and is cut off before it starts the stack (here: while removing the emptied
# series directory). docs/operations.md: "once it had put the state back, the next `cave update`
# (at boot) also finishes it." The boot's `update --recover` removes the marker -- so `status`
# no longer says anything about a restore -- but starts nothing: the server comes up with
# nothing running, and nothing says why.
def test_an_undo_cut_before_the_start_is_finished_by_the_boot_with_the_stack_running(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)
    assert server.stack.running is None

    real_remove = restore._server.remove

    def cut(path):
        if path.name.startswith("before-restore-"):
            raise killed
        real_remove(path)

    monkeypatch.setattr(restore._server, "remove", cut)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    monkeypatch.undo()

    run_cave("update", "--recover")  # what the next boot does

    marker = server.data / ".cave" / "ops" / "restore.json"
    assert server.stack.running == OLD or marker.exists(), (
        "after the boot nothing runs, and the marker that would have said why is gone"
    )


# ------------------------------------------------------------------------- the rows added for C1
def field(output, label):
    return [line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith(label)]


def marker(server):
    return server.data / ".cave" / "ops" / "restore.json"


def cut_at_start(server, killed, release=OLD):
    """Kill the command at its `up` of ``release``: the state is in place, the start is owed."""

    def die(current):
        if current == release:
            raise killed

    server.stack.before["up"] = die


def test_a_restore_ends_with_the_stack_running(server, run_cave, backups):  # noqa: F811
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    assert run_cave("restore", str(archive)).code == 0

    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_a_restore_cut_before_its_start_is_started_by_the_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    cut_at_start(server, killed)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    server.stack.before.clear()
    assert server.stack.running is None
    assert "put the state in place but was cut off" in " ".join(
        field(run_cave("status").out, "NOT STARTED")
    )

    result = run_cave("update", "--recover")  # the boot

    assert server.stack.running == OLD, result.err
    assert not marker(server).exists()
    assert "had put the state in place but was cut off before it started the stack" in result.out


def test_a_restore_whose_start_fails_is_started_by_the_boot(
    server,
    run_cave,
    backups,  # noqa: F811
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    server.stack.fail_times["up"] = 1

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "tries again" in result.err
    assert marker(server).exists()
    run_cave("update", "--recover")
    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_a_half_restored_state_is_not_started_and_status_says_what_to_do(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    """The one row where the stack stays stopped after the boot: starting a half-restored state
    would start Home Assistant unconfigured (finding A2). status names the ways out."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)

    run_cave("update", "--recover")

    assert server.stack.running is None
    interrupted = " ".join(field(run_cave("status").out, "INTERRUPTED"))
    assert "`sudo ./cave restore --undo`" in interrupted


def test_a_restore_with_no_start_is_said_by_status_and_blocks_nothing(
    server,
    run_cave,
    backups,  # noqa: F811
    ctx,
    fake_ha,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    assert run_cave("restore", str(archive), "--no-start").code == 0

    assert server.stack.running is None
    assert not marker(server).exists()
    said = " ".join(field(run_cave("status").out, "NOT STARTED"))
    assert "finished with --no-start" in said
    assert "`sudo -u cave ./cave up`" in said
    # Nothing refuses because of it, and the boot does not start what the owner kept stopped.
    ctx.env["CAVE_BACKUP_DIR"] = str(backups)  # the restore reloaded .env from the archive
    assert run_cave("backup").code == 0
    run_cave("update", "--recover")
    assert server.stack.running is None
    # Once the stack runs, the note is gone from status and the next update removes it.
    server.stack.start(OLD)
    assert field(run_cave("status").out, "NOT STARTED") == []
    run_cave("update")
    assert not (server.data / ".cave/ops/not-started.json").exists()


def test_an_undo_ends_with_the_stack_running(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)

    assert run_cave("restore", "--undo").code == 0

    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_an_undo_cut_at_its_start_is_finished_by_running_it_again(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)
    cut_at_start(server, killed)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    server.stack.before.clear()

    assert run_cave("restore", "--undo").code == 0

    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_an_undo_cut_before_its_next_phase_is_finished_by_the_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)
    real = restore.complete_undo

    def die(ctx):
        raise killed

    monkeypatch.setattr(restore, "complete_undo", die)
    with pytest.raises(killed):
        run_cave("restore", "--undo")
    monkeypatch.setattr(restore, "complete_undo", real)

    run_cave("update", "--recover")

    assert server.stack.running == OLD
    assert not marker(server).exists()


@pytest.mark.parametrize("cut", [False, True])
def test_an_undo_with_no_start_is_said_by_status_also_when_cut(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    cut,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_in(server, run_cave, archive, monkeypatch, killed)
    if cut:
        monkeypatch.setattr(restore, "complete_undo", lambda ctx: (_ for _ in ()).throw(killed))
        with pytest.raises(killed):
            run_cave("restore", "--undo", "--no-start")
        monkeypatch.undo()
        run_cave("update", "--recover")  # the boot carries it to its end, without starting
    else:
        assert run_cave("restore", "--undo", "--no-start").code == 0

    assert server.stack.running is None
    assert not marker(server).exists()
    assert "finished with --no-start" in " ".join(field(run_cave("status").out, "NOT STARTED"))
    assert run_cave("backup").code == 0


# ---------------------------------------------- a restore after an owed start (seventh review, D1)
def _start_owed(server, run_cave, archive, killed):
    """A restore that put the state in place and was cut off at its start; its marker."""
    cut_at_start(server, killed)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    server.stack.before.clear()
    assert server.stack.running is None
    series = json.loads(marker(server).read_text())
    assert series["start_owed"] is True
    return series


def _cut_at_rename(monkeypatch, killed, number):
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == number:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)


def _state(server):
    """The state outside `.cave`: every file's content, every directory and link by name."""
    return {
        str(path.relative_to(server.data)): path.read_bytes() if path.is_file() else None
        for path in sorted(server.data.rglob("*"))
        if ".cave" not in path.relative_to(server.data).parts
    }


def _disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


# The reviewer's test. A restore puts the state in place and is cut off at its start. Before the
# boot the owner restores again; that restore took the marker for an earlier attempt of its own
# and copied `start_owed` into its marker before it moved anything. Cut while moving the state in,
# it left "only the start is owed" over a half-restored state: backup ran over it, and the boot
# started the stack on it (finding A2 of the fourth review, reopened).
def test_a_restore_cut_while_moving_in_after_an_owed_start_is_not_started_by_the_boot(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    ctx,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _start_owed(server, run_cave, archive, killed)

    _cut_at_rename(monkeypatch, killed, 2)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    half = sorted(p.name for p in server.data.iterdir() if p.name != ".cave")

    ctx.env["CAVE_BACKUP_DIR"] = str(backups)  # the restore reloaded .env from the archive
    backup_run = run_cave("backup")
    run_cave("update", "--recover")  # what the next boot does

    assert backup_run.code != 0, f"backup ran over a half-restored state: {backup_run.out}"
    assert server.stack.running is None, (
        f"the boot started the stack on a half-restored state holding only {half}"
    )
    assert "`sudo ./cave restore --undo`" in " ".join(field(run_cave("status").out, "INTERRUPTED"))


def test_a_restore_after_an_owed_start_is_a_new_restore_that_undo_takes_back(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    """It keeps its own copy of the state from before, which is the complete state of the restore
    whose start was owed: `--undo` brings exactly that back and starts the stack."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    first = _start_owed(server, run_cave, archive, killed)
    complete = _state(server)

    _cut_at_rename(monkeypatch, killed, 2)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    second = json.loads(marker(server).read_text())
    assert not second.get("start_owed")
    assert second["aside"] != first["aside"]
    assert _state(server) != complete

    result = run_cave("restore", "--undo")

    assert result.code == 0, result.err
    assert _state(server) == complete
    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_a_restore_that_cannot_unpack_after_an_owed_start_makes_the_start(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _start_owed(server, run_cave, archive, killed)
    complete = _state(server)
    monkeypatch.setattr(restore._server, "extract", _disk_full)

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "Nothing was restored." in result.err
    assert "half restored" not in result.err
    assert "started on the state of the restore before this one" in result.err
    assert _state(server) == complete
    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_a_restore_that_cannot_unpack_after_an_owed_start_still_owes_a_start_that_failed(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _start_owed(server, run_cave, archive, killed)
    monkeypatch.setattr(restore._server, "extract", _disk_full)
    server.stack.fail_times["up"] = 1

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "could NOT be started again" in result.err
    assert "tries again" in result.err
    assert server.stack.running is None
    assert json.loads(marker(server).read_text())["start_owed"] is True
    monkeypatch.undo()
    run_cave("update", "--recover")  # the boot
    assert server.stack.running == OLD
    assert not marker(server).exists()


def test_no_room_to_unpack_after_an_owed_start_changes_nothing_and_says_so(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    """The state is complete, not half restored, and the start stays owed."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    owed = _start_owed(server, run_cave, archive, killed)
    monkeypatch.setattr(restore._server, "free_bytes", lambda path: 0)

    result = run_cave("restore", str(archive))

    assert result.code != 0
    assert "Nothing was changed." in result.err
    assert "half restored" not in result.err
    assert json.loads(marker(server).read_text()) == owed
