"""Every restore and every `--undo` ends with the stack started, or with `--no-start` said by
`status`: the reviewer's test of the sixth review of pull request #10, and the rows added for it."""

from __future__ import annotations

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
