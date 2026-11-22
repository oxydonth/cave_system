"""Reviewer's second-round tests for PR #10. Each asserts the correct behaviour; a failure is a
defect. Not for merging."""

from __future__ import annotations

import io
import json
import os
import tarfile

import pytest

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


def _archive(path, data_dir, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:

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
        add("env", data=f"CAVE_DATA={data_dir}\n".encode())
        add("data", tarfile.DIRTYPE)
        for member in members:
            add(*member)


# A link whose target is lexically inside but passes through another link of the archive, created
# before that other link exists, so that the per-link realpath check sees a path that does not
# resolve yet. After the restore, CAVE_DATA/homeassistant points outside CAVE_DATA, and Docker
# bind-mounts that as /config into the root-run Home Assistant container.
def test_restore_leaves_no_link_that_resolves_outside_the_state(server, run_cave, tmp_path):
    archive = tmp_path / "in" / "cave-backup-20261115T000000Z.tar.gz"
    _archive(
        archive,
        server.data,
        [
            ("data/x", tarfile.DIRTYPE),
            ("data/homeassistant", tarfile.SYMTYPE, "x/s/x/s/../.."),
            ("data/x/s", tarfile.SYMTYPE, ".."),
        ],
    )

    result = run_cave("restore", str(archive), "--no-start")

    ha = server.data / "homeassistant"
    real = os.path.realpath(ha)
    assert result.code != 0 or real.startswith(os.path.realpath(server.data) + os.sep), (
        f"restore exited {result.code}; {ha} resolves to {real}, outside {server.data}"
    )


# The forward checkout fails because an untracked file of the owner is in the way of a file the
# release adds. The rollback then deletes the owner's file, which no checkout ever touched.
def test_rollback_does_not_delete_untracked_files_it_did_not_write(server, run_cave):
    server.release(NEW, files={"notes.txt": "from the release\n"})
    mine = server.root / "notes.txt"
    mine.write_text("the owner's notes\n")

    result = run_cave("update")

    assert result.code == 2, result.err
    assert mine.exists(), "the owner's untracked file was deleted by the rollback"


# Interrupted before the snapshot was complete: no checkout happened at all, yet the recovery
# deletes every path the new release would add.
def test_restart_of_the_previous_release_deletes_nothing(server, run_cave, killed):
    server.release(NEW, files={"compose.override.yaml": "services: {}\n"})
    override = server.root / "compose.override.yaml"
    override.write_text("# the owner's USB coordinator\nservices: {}\n")

    def die(release):
        raise killed

    server.stack.after["down"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()

    run_cave("update", "--recover")

    assert override.exists(), "the owner's compose.override.yaml was deleted"


# The record saying the snapshot was put back cannot be written (full disk), but the used-up
# snapshot is deleted anyway. Every later run then "fails" to put back a snapshot that no longer
# exists, keeps the update in flight (no backups), and stops and starts the stack each time.
def test_a_snapshot_put_back_is_not_put_back_forever(server, run_cave, monkeypatch, notifications):
    from cave_ops.commands import _update_state

    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    real_save = _update_state.save

    full = []

    def full_disk(ctx, record):
        # The disk fills up while the stack is down; from then on no write succeeds.
        if full or (record.switch is not None and record.switch.restored):
            full.append(1)
            raise OSError(28, "No space left on device")
        real_save(ctx, record)

    monkeypatch.setattr(_update_state, "save", full_disk)
    run_cave("update")
    monkeypatch.undo()
    assert server.schema() == OLD  # the snapshot is back

    downs_before = server.stack.names().count("down")
    result = run_cave("update", "--recover")

    assert server.record()["switch"] is None, result.err
    assert server.stack.names().count("down") == downs_before, "the stack was stopped again"


# A restore during an update interrupted after the switch: the restore checks out the previous
# release, then fails while unpacking, and starts the previous release on the state the new
# release migrated -- the combination the snapshot exists to prevent.
def test_a_failed_restore_does_not_start_the_old_release_on_the_new_state(
    server, run_cave, killed, monkeypatch, tmp_path, ctx
):
    from cave_ops.commands import restore

    ctx.env["CAVE_BACKUP_DIR"] = str(tmp_path / "backups")
    assert run_cave("backup").code == 0
    archive = next((tmp_path / "backups").glob("cave-backup-*.tar.gz"))
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.after["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()
    assert server.schema() == NEW

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", disk_full)
    run_cave("restore", str(archive))

    assert not (server.stack.running == OLD and server.schema() == NEW), (
        "the previous release runs on the state the new release migrated"
    )


# K6 with entries already put back: killed after the first entry came back and the second was
# set aside. (The author's test kills before any snapshot entry is moved, so the "already put
# back" branch is never run.)
@pytest.mark.parametrize("cut", [4, 5, 6, 7])
def test_killed_after_some_entries_were_put_back(
    server, run_cave, killed, monkeypatch, state_tree, cut
):
    from cave_ops.commands import _update_state

    before = state_tree(server.data)
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append((source, target))
        if len(renames) == cut:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(_update_state.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("update")
    monkeypatch.undo()

    result = run_cave("update", "--recover")

    assert result.code == 2, result.err
    after = state_tree(server.data)
    after.pop(f"homeassistant/started-{OLD}")
    before.pop(f"homeassistant/started-{OLD}")
    assert after == before
