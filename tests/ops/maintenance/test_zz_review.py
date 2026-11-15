"""Reviewer's scratch tests for PR #10. Each test asserts the CORRECT behaviour, so a failing test
demonstrates a defect. Not for merging."""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import tarfile

import pytest

from cave_ops.commands import _server, restore, update

from .conftest import git

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


def archives(directory):
    from cave_ops.commands import _archives

    return [directory / name for name in _archives.archives(directory)]


@pytest.fixture
def backups(ctx, tmp_path):
    directory = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(directory)
    return directory


# 1. A power cut during `git checkout` leaves .git/index.lock; the recovery cannot check out the
#    previous release and starts whatever half-checked-out tree is there.
def test_power_cut_during_checkout_is_recovered(server, run_cave, killed):
    server.release(NEW)
    calls = []

    def power_cut(argv):
        calls.append(argv)
        if len(calls) == 1:  # the release's checkout, not the rollback's
            (server.root / ".git" / "index.lock").write_text("")
            (server.root / "RELEASE").write_text(NEW + "\n")  # half the files written
            raise killed

    server.runner.git_before["checkout"] = power_cut
    with pytest.raises(killed):
        run_cave("update")
    server.runner.git_before.clear()

    result = run_cave("update", "--recover")

    assert server.checked_out() == OLD, result.err
    assert result.code == 2, result.err


# 2. A restore while an update is interrupted is silently undone by the next recovery.
def test_restore_is_not_undone_by_a_pending_update_recovery(
    server, run_cave, killed, backups, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    wanted = state_tree(server.data)
    (server.data / "homeassistant/automations.yaml").write_text("- id: damaged later\n")
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.after["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()

    assert run_cave("restore", str(archive), "--no-start").code == 0
    assert state_tree(server.data)["homeassistant/automations.yaml"] == b"[]\n"

    run_cave("update", "--recover")  # what cave-recover.service does at the next boot

    assert (
        state_tree(server.data)["homeassistant/automations.yaml"]
        == wanted["homeassistant/automations.yaml"]
    ), "the restored state was replaced by the update's snapshot"


# 3. The free-space check reserves one copy, but the rollback needs a second (staging) copy.
def test_space_checked_before_the_switch_suffices_for_the_rollback(server, run_cave, monkeypatch):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    big = server.data / "homeassistant/home-assistant_v2.db"
    big.write_bytes(b"x" * 4_000_000)
    entries = _server.state_entries(server.data)
    size = sum(_server.tree_size(server.data / e) for e in entries)
    budget = {"free": int(size * 1.1)}
    monkeypatch.setattr(update, "DISK_MARGIN", 0)
    monkeypatch.setattr(update._server, "free_bytes", lambda path: budget["free"])
    real_copy2 = shutil.copy2

    def copy2(src, dst, **kw):
        n = src.stat().st_size if hasattr(src, "stat") else 0
        if n > budget["free"]:
            raise OSError(errno.ENOSPC, "No space left on device", str(dst))
        budget["free"] -= n
        return real_copy2(src, dst, **kw)

    monkeypatch.setattr(_server.shutil, "copy2", copy2)

    result = run_cave("update")

    assert server.schema() == OLD, (
        f"exit {result.code}: the previous release runs on the new release's state: {result.err}"
    )


# 4. A corrupt SQLite database makes the whole backup fail: nothing else is archived either.
def test_a_corrupt_recorder_database_does_not_stop_the_backup(server, run_cave, backups):
    db = server.data / "homeassistant/home-assistant_v2.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00\x10" + b"garbage" * 1000)

    result = run_cave("backup")

    assert archives(backups), f"no archive at all: {result.err}"


# 5. The archive check lets a chain of symbolic links write outside the target.
def test_restore_cannot_write_outside_the_state(server, run_cave, backups, ctx, tmp_path):
    archive = backups / "cave-backup-20261115T000000Z.tar.gz"
    backups.mkdir()
    with tarfile.open(archive, "w:gz") as tar:

        def add(name, kind=tarfile.REGTYPE, link="", data=b""):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            info.size = len(data)
            info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            info.uid, info.gid = os.getuid(), os.getgid()
            tar.addfile(info, io.BytesIO(data) if data else None)

        manifest = {"format": 1, "release_tag": None, "release": "x", "databases": []}
        add("manifest.json", data=json.dumps(manifest).encode())
        add("env", data=f"CAVE_DATA={server.data}\n".encode())
        add("data", tarfile.DIRTYPE)
        add("data/a", tarfile.SYMTYPE, ".")
        add("data/a/a/a/a/l", tarfile.SYMTYPE, "../../../..")
        add("data/l/escaped.txt", data=b"written outside\n")

    result = run_cave("restore", str(archive), "--no-start")

    # staging is CAVE_DATA/.cave/ops/restore-staging: four levels up is tmp_path
    assert not (tmp_path / "escaped.txt").exists(), (result.code, result.out, result.err)


# 6. A restore that fails after stopping the stack leaves it stopped.
def test_a_failed_restore_starts_the_stack_again(server, run_cave, backups, monkeypatch):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", disk_full)
    # Changed by the author: the restore reports the failure as a cave error (exit 1, message),
    # instead of letting the OSError escape as a traceback. See the pull request.
    result = run_cave("restore", str(archive))
    assert result.code == 1
    assert "No space left on device" in result.err

    assert server.stack.running == OLD, "nothing runs after a failed restore"


# 7. A rollback whose `down` failed forgets the switch: the snapshot is never put back later.
def test_a_rollback_that_could_not_put_the_snapshot_back_is_retried(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def docker_hiccup(release):
        if release == NEW:
            server.stack.fail["down"] = 1

    server.stack.after["up"] = docker_hiccup
    result = run_cave("update")
    assert result.code == 3
    assert server.schema() == NEW  # previous release on the new release's state
    server.stack.after.clear()
    server.stack.fail.clear()

    again = run_cave("update", "--recover")  # next boot, docker fine again

    assert server.schema() == OLD, f"never retried: {again.out}{again.err}"


# 8. A tag that is not on main (pushed by hand) is installed.
def test_a_tag_not_on_main_is_not_installed(server, run_cave):
    git("checkout", "-q", "-b", "experiment", cwd=server.origin)
    server.release(NEW)  # tags the experiment branch
    git("checkout", "-q", "main", cwd=server.origin)

    run_cave("update")

    assert server.checked_out() == OLD


# 9. A release tag that was moved on GitHub blocks every later update.
def test_a_moved_tag_does_not_block_later_releases(server, run_cave):
    server.release(NEW)
    assert run_cave("update").code == 0
    git("tag", "-d", NEW, cwd=server.origin)
    git("commit", "-q", "--allow-empty", "-m", "fix", cwd=server.origin)
    git("tag", NEW, cwd=server.origin)  # re-created on another commit
    server.release("v2026.11.16.1")

    result = run_cave("update")

    assert server.checked_out() == "v2026.11.16.1", result.err


# 10. A Docker hiccup while running the validator marks the release as failed for good.
@pytest.mark.parametrize("status", [124, 125])
def test_a_validator_that_could_not_run_does_not_mark_the_release_failed(server, run_cave, status):
    server.release(NEW)
    server.stack.fail["validate"] = status

    run_cave("update")

    assert NEW not in server.record()["failed"]


# 11. Retention by name: one archive named by a clock that was once ahead deletes every new one.
def test_retention_keeps_the_archive_just_written(server, run_cave, backups, ctx):
    ctx.env["CAVE_BACKUP_KEEP"] = "2"
    backups.mkdir()
    for name in ("cave-backup-20991231T000000Z.tar.gz", "cave-backup-20991231T000001Z.tar.gz"):
        (backups / name).write_bytes(b"")

    result = run_cave("backup")

    assert any(not p.name.startswith("cave-backup-2099") for p in backups.iterdir()), result.out


# 12. Snapshots are pruned only after a success: every rollback leaves another full copy.
def test_rollbacks_do_not_accumulate_snapshots(server, run_cave):
    for day in (15, 16, 17):
        tag = f"v2026.11.{day}.1"
        server.release(tag)
        server.stack.unhealthy.add(tag)
        assert run_cave("update").code == 2

    snapshots = list((server.data / ".cave/ops/snapshots").iterdir())
    assert len(snapshots) <= 2, sorted(p.name for p in snapshots)
