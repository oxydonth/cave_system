"""``cave restore`` and the record of a stack stopped on purpose: the items of issue #25.

The first test is the reviewer's, from the eighth review of pull request #10, under a proper name.
"""

from __future__ import annotations

import datetime
import errno
import json
import os
import types

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
