"""``cave restore`` and the record of a stack stopped on purpose: the items of issue #25.

The first test is the reviewer's, from the eighth review of pull request #10, under a proper name.
"""

from __future__ import annotations

import datetime
import errno
import json
import os
import types
from pathlib import Path

import pytest

from cave_ops.commands import restore

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


def _marker(server):
    return server.data / ".cave" / "ops" / "restore.json"


def _field(output, label):
    return [line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith(label)]


def _start_owed(server, run_cave, archive, killed):
    def die(current):
        if current == OLD:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    server.stack.before.clear()
    return json.loads(_marker(server).read_text())


def _paths(server):
    from cave_ops.paths import Paths

    return Paths(root=server.root, data=server.data)


def _disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


# ----------------------------------------------------------- --no-start, when unpacking fails
def test_no_start_is_honoured_when_a_second_restore_cannot_unpack(
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

    result = run_cave("restore", str(archive), "--no-start")

    assert server.stack.running is None, "started although --no-start was given"
    assert result.code == 1
    assert "The stack was not started again (--no-start)" in result.err
    assert "complete state of the restore before this one" in result.err
    # The boot does not start what the owner kept stopped; status says why it is stopped.
    monkeypatch.undo()
    run_cave("update", "--recover")
    assert server.stack.running is None
    assert not _marker(server).exists()
    said = " ".join(_field(run_cave("status").out, "NOT STARTED"))
    assert "failed while unpacking, run with --no-start" in said


def test_no_start_is_honoured_when_a_restore_of_a_running_stack_cannot_unpack(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    monkeypatch.setattr(restore._server, "extract", _disk_full)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert server.stack.running is None
    assert "Nothing was restored" in result.err
    assert "`sudo -u cave ./cave up` starts it" in result.err


def test_no_start_leaves_an_interrupted_update_for_its_own_way_back(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    monkeypatch.setattr(restore._server, "extract", _disk_full)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert server.stack.running is None
    assert server.record()["switch"] is not None
    assert "`sudo ./cave update --recover` finishes it" in result.err
    monkeypatch.undo()
    assert run_cave("update", "--recover").code == 2  # the boot: the update's way back
    assert server.stack.running == OLD


# ---------------------------------------------------- two restores within one second: two copies
class _Frozen(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 11, 22, 21, 0, 0, tzinfo=datetime.UTC)


def test_two_restores_within_one_second_keep_both_copies_of_env(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    env = server.root / ".env"
    env.write_text(env.read_text() + "CAVE_FRITZ_HOST=owner.box\n")
    owners = env.read_text()
    monkeypatch.setattr(
        restore, "datetime", types.SimpleNamespace(datetime=_Frozen, UTC=datetime.UTC)
    )

    assert run_cave("restore", str(archive)).code == 0
    first_restore = env.read_text()
    assert run_cave("restore", str(archive)).code == 0

    copies = sorted(p.name for p in server.root.glob(".env.before-restore-*"))
    assert copies == [
        ".env.before-restore-20261122T210000Z",
        ".env.before-restore-20261122T210000Z-2",
    ]
    assert (server.root / copies[0]).read_text() == owners, "the owner's .env was overwritten"
    assert (server.root / copies[1]).read_text() == first_restore


# ------------------------------- a repeat after a kill between the restored .env and the marker
def test_undo_after_repeats_cut_after_the_env_brings_back_the_owners_env(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    """A restore is cut right after it wrote the archive's .env, before its marker moved on; the
    same restore run again is cut at the same place; `--undo` must bring back the owner's .env,
    not the archive's that the first attempt left in place."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    env = server.root / ".env"
    env.write_text(env.read_text() + "CAVE_FRITZ_HOST=owner.box\n")
    owners = env.read_text()
    real_load_env = restore.load_env
    reads = []

    def cut_after_the_env(path):
        reads.append(path)
        if len(reads) == 2:  # the first chooses the target; the second reloads the new .env
            raise killed
        return real_load_env(path)

    monkeypatch.setattr(restore, "load_env", cut_after_the_env)
    for _attempt in range(2):
        reads.clear()
        with pytest.raises(killed):
            run_cave("restore", str(archive))
        assert env.read_text() != owners, "cut before the archive's .env was written"
        series = json.loads(_marker(server).read_text())
        assert not series.get("start_owed")
    monkeypatch.undo()
    assert (server.root / series["env_backup"]).read_text() == owners

    assert run_cave("restore", "--undo").code == 0

    assert env.read_text() == owners
    assert server.stack.running == OLD


# ------------------------------------------------------------- the stopped record and `up`
def test_up_ends_the_note_of_a_restore_with_no_start(
    server,
    run_cave,
    backups,  # noqa: F811
    ctx,
):
    """The note used to stay until an update saw the stack healthy: a stack started by hand that
    stayed unhealthy, or stopped later for another reason, still read "finished with
    --no-start"."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    assert run_cave("restore", str(archive), "--no-start").code == 0
    assert "finished with --no-start" in " ".join(_field(run_cave("status").out, "NOT STARTED"))
    server.stack.unhealthy.add(OLD)

    assert run_cave("up", "--timeout", "10").code == 1  # started, and not healthy

    assert _field(run_cave("status").out, "NOT STARTED") == []
    server.stack.running = None  # and later it stops for another reason
    assert _field(run_cave("status").out, "NOT STARTED") == []


def test_down_says_in_status_why_nothing_runs(server, run_cave):
    assert run_cave("down").code == 0

    said = " ".join(_field(run_cave("status").out, "NOT STARTED"))

    assert "`./cave down` stopped it" in said
    assert "`sudo -u cave ./cave up` starts it" in said


def test_the_note_of_the_previous_version_is_still_read_and_removed_by_up(server, run_cave):
    legacy = server.data / ".cave" / "ops" / "not-started.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"what": "the restore of cave-backup-old.tar.gz"}))
    server.stack.running = None

    said = " ".join(_field(run_cave("status").out, "NOT STARTED"))
    assert "the restore of cave-backup-old.tar.gz finished with --no-start" in said

    assert run_cave("up").code == 0
    assert not legacy.exists()


def test_the_records_of_the_commands_are_in_no_backup(server, run_cave, backups):  # noqa: F811
    assert run_cave("down").code == 0
    (server.data / ".cave" / "watch.json").write_text("{}\n")
    server.stack.start(OLD)

    assert run_cave("backup").code == 0

    [archive] = archives(backups)
    import tarfile

    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "data/.cave/mqtt-credentials" in names
    assert not [name for name in names if name.endswith(("stopped.json", "watch.json"))]
    assert os.path.exists(server.data / ".cave" / "stopped.json")


# ------------------------------------- the first review of #30: a refusal keeps the record
def test_a_restore_refused_for_space_keeps_the_record_of_down(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
):
    """The reviewer's test. "Nothing was changed", so the record of `./cave down` stays."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    assert run_cave("down").code == 0
    monkeypatch.setattr(restore._server, "free_bytes", lambda path: 0)

    result = run_cave("restore", str(archive))

    assert result.code != 0
    assert "Nothing was changed" in result.err
    assert server.data.joinpath(".cave", "stopped.json").exists(), (
        "a restore that changed nothing removed the record of `down`"
    )


def test_an_undo_with_nothing_to_undo_keeps_the_record_of_down(server, run_cave):
    """The reviewer's test."""
    assert run_cave("down").code == 0

    result = run_cave("restore", "--undo")

    assert result.code != 0
    assert "Nothing was changed" in result.err
    assert server.data.joinpath(".cave", "stopped.json").exists(), (
        "an --undo that changed nothing removed the record of `down`"
    )


# ----------------------------------------- the first review of #30: what it found untested
def test_a_restore_that_supersedes_an_update_names_the_files_it_removed(
    server,
    run_cave,
    backups,  # noqa: F811
    killed,
):
    """The update was cut off during its checkout, which had written one file of the new release.
    The restore checks out the release from before and removes that file, which is the release's
    own byte for byte: it says so."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    server.release(NEW, files={"tools/new_tool.py": "print('new')\n"})

    def half_done(argv):
        if "--force" not in argv:  # the switch's checkout
            (server.root / "tools").mkdir(exist_ok=True)
            (server.root / "tools" / "new_tool.py").write_text("print('new')\n")
            raise killed

    server.runner.git_before["checkout"] = half_done
    with pytest.raises(killed):
        run_cave("update")
    server.runner.git_before.clear()

    result = run_cave("restore", str(archive))

    assert result.code == 0, result.err
    assert not (server.root / "tools" / "new_tool.py").exists()
    assert f"Removed, because they are {NEW}'s own files, byte for byte" in result.err
    assert "tools/new_tool.py" in result.err


def test_the_recovery_s_start_ends_a_record_of_a_stop(server, run_cave, killed):
    """`down` while an update is in flight records the stop; the recovery's way back starts the
    stack, so the record would only mislead the watch and status afterwards."""
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    assert run_cave("down").code == 0
    assert server.data.joinpath(".cave", "stopped.json").exists()

    assert run_cave("update", "--recover").code == 2

    assert server.stack.running == OLD
    assert not server.data.joinpath(".cave", "stopped.json").exists()


def test_a_restore_ends_a_record_of_a_stop_once_it_changes_the_state(
    server,
    run_cave,
    backups,  # noqa: F811
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    assert run_cave("down").code == 0

    assert run_cave("restore", str(archive)).code == 0

    assert server.stack.running == OLD
    assert not server.data.joinpath(".cave", "stopped.json").exists()
    assert _field(run_cave("status").out, "NOT STARTED") == []


def test_a_restore_that_cannot_unpack_leaves_a_stack_stopped_on_purpose_stopped(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    assert run_cave("down").code == 0
    monkeypatch.setattr(restore._server, "extract", _disk_full)

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert server.stack.running is None
    assert "it had been stopped on purpose before the restore" in result.err
    assert "`./cave down` stopped it" in " ".join(_field(run_cave("status").out, "NOT STARTED"))


def test_an_up_whose_compose_fails_leaves_the_record_of_down(server, run_cave):
    assert run_cave("down").code == 0
    server.stack.fail_times["up"] = 1

    assert run_cave("up").code != 0

    assert server.data.joinpath(".cave", "stopped.json").exists()
    assert run_cave("up").code == 0
    assert not server.data.joinpath(".cave", "stopped.json").exists()


def test_a_start_by_the_service_user_overrides_the_note_it_cannot_remove(
    server, run_cave, monkeypatch
):
    """The note of v2026.11.22.1 lies in root's `.cave/ops`. The service user's `up` cannot
    remove it; it records the start instead, which counts more than the old note: a later stop is
    reported by the watch, and status no longer says "finished with --no-start"."""
    from cave_ops import records

    legacy = server.data / ".cave" / "ops" / "not-started.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"what": "the restore of cave-backup-old.tar.gz"}))
    server.stack.running = None
    real_unlink = Path.unlink

    def roots_directory(self, missing_ok=False):
        if self == legacy:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", roots_directory)

    assert run_cave("up").code == 0
    server.stack.running = None

    assert legacy.exists()
    assert records.stopped(_paths(server)) is None
    assert _field(run_cave("status").out, "NOT STARTED") == []


# ------------------------------------------ the second review of #30: what runs, not the record
def test_a_restore_after_a_start_without_cave_starts_the_stack_again_when_it_cannot_unpack(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
):
    """The reviewer's test. `./cave down`, then the owner starts the stack with Docker directly:
    the record of the stop is stale, the stack runs. A restore that cannot unpack stopped it, so it
    starts it again; the record does not decide that, what ran does."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    assert run_cave("down").code == 0
    server.stack.start(OLD)  # started without cave: the record of `down` is stale
    monkeypatch.setattr(restore._server, "extract", _disk_full)

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert server.stack.running == OLD, "a running stack was left stopped by a failed restore"
    assert "stopped on purpose" not in result.err


def test_an_undo_ends_a_record_of_a_stop_once_it_changes_the_state(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    """A restore stopped half-way, then `./cave down`, then `--undo`: the undo puts the state from
    before back and starts the stack, so the record of the stop goes."""
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
    assert run_cave("down").code == 0
    assert server.data.joinpath(".cave", "stopped.json").exists()

    assert run_cave("restore", "--undo").code == 0

    assert server.stack.running == OLD
    assert not server.data.joinpath(".cave", "stopped.json").exists()


def test_status_does_not_call_a_running_stack_stopped_on_purpose(server, run_cave):
    """`./cave down`, then a start without `cave` that does not become healthy: the record is
    stale, containers run. status does not say "NOT STARTED … on purpose"; the stack's line says
    what is wrong."""
    assert run_cave("down").code == 0
    server.stack.start(OLD)
    server.stack.unhealthy.add(OLD)

    result = run_cave("status")

    assert _field(result.out, "NOT STARTED") == []
    assert "not healthy" in " ".join(_field(result.out, "Stack"))
    server.stack.running = None  # and once nothing runs, the record speaks again
    assert "`./cave down` stopped it" in " ".join(_field(run_cave("status").out, "NOT STARTED"))
