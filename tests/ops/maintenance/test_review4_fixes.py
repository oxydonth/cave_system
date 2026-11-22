"""Tests added for the fourth review of pull request #10, beyond the reviewer's own
(test_restore_repeats.py, test_apply_lock_after_root.py): `cave restore --undo`, a failed repeat,
`.env` quoting, and links onto the directories that hold the commands' own records."""

from __future__ import annotations

import errno
import json
import os
import tarfile

import pytest

from cave_ops.commands import _server, restore
from cave_ops.envfile import load_env, parse_env, quote_value

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)
from .test_review3_fixes import make_archive

OLD = "v2026.11.14.1"


def cut_at(server, run_cave, archive, monkeypatch, killed, rename_number):
    """A restore killed at its ``rename_number``-th rename (aside first, then in)."""
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == rename_number:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    assert (server.data / ".cave/ops/restore.json").exists()


def comparable(tree):
    tree.pop(f"homeassistant/started-{OLD}", None)
    return tree


# ------------------------------------------------------------------------------------- --undo
def test_undo_after_attempts_cut_while_moving_in_discards_what_they_moved_in(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    state_tree,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    (server.data / "homeassistant/automations.yaml").write_text("- id: after the backup\n")
    (server.data / "only-here").mkdir()
    before = state_tree(server.data)
    entries = len(_server.state_entries(server.data))
    # The first attempt moves everything aside and one entry in; the repeat discards that one,
    # moves nothing aside, and is cut while moving in again.
    cut_at(server, run_cave, archive, monkeypatch, killed, entries + 2)
    cut_at(server, run_cave, archive, monkeypatch, killed, 2)

    result = run_cave("restore", "--undo")

    assert result.code == 0, result.err
    assert comparable(state_tree(server.data)) == comparable(before)


def test_undo_brings_back_an_env_an_attempt_had_already_replaced(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    env = server.root / ".env"
    env.write_text(env.read_text() + "TZ=Europe/Vienna\n")
    env_before = env.read_text()

    real_load = restore.load_env

    def power_cut(path):
        if list(server.root.glob(".env.before-restore-*")):
            raise killed  # right after the new .env was written, before the marker went
        return real_load(path)

    monkeypatch.setattr(restore, "load_env", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive), "--no-start")
    monkeypatch.undo()
    marker = json.loads((server.data / ".cave/ops/restore.json").read_text())
    assert marker["env_backup"].startswith(".env.before-restore-")
    assert "TZ=Europe/Vienna" not in env.read_text()  # the archive's .env is in place

    assert run_cave("restore", "--undo", "--no-start").code == 0
    assert env.read_text() == env_before


def test_there_is_always_a_env_naming_the_state_while_a_restore_runs(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    real_write = restore._server.write_atomic

    def power_cut(path, data, mode=0o644):
        if path.name == ".env":
            raise killed  # after the copy of the old .env, before the new one
        real_write(path, data, mode)

    monkeypatch.setattr(restore._server, "write_atomic", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive), "--no-start")
    monkeypatch.undo()

    assert load_env(server.root / ".env")["CAVE_DATA"] == str(server.data)
    assert run_cave("restore", "--undo", "--no-start").code == 0


def test_undo_without_an_unfinished_restore_changes_nothing(server, run_cave, state_tree):
    before = state_tree(server.data)

    result = run_cave("restore", "--undo")

    assert result.code == 1
    assert "no restore stopped half-way" in result.err
    assert state_tree(server.data) == before
    assert server.stack.ops == []


def test_undo_does_not_guess_for_a_marker_of_an_earlier_version(server, run_cave, state_tree):
    marker = server.data / ".cave/ops/restore.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"archive": "/x.tar.gz"}))
    before = state_tree(server.data)

    result = run_cave("restore", "--undo")

    assert result.code == 1
    assert "cannot be undone automatically" in result.err
    assert state_tree(server.data) == before


def test_an_archive_and_undo_together_are_refused(server, run_cave):
    result = run_cave("restore", "x.tar.gz", "--undo")

    assert result.code == 1
    assert "either an archive or --undo" in result.err


# ------------------------------------------------------------------------- a failed repeat (A2)
def test_a_failed_repeat_says_the_state_is_still_half_restored(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    notifications,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    cut_at(server, run_cave, archive, monkeypatch, killed, 2)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", disk_full)
    result = run_cave("restore", str(archive))

    assert result.code == 1
    message = notifications()[-1] if notifications() else result.err
    assert "still half restored" in result.err
    assert "the stack was NOT started" in result.err
    assert "sudo ./cave restore --undo" in result.err
    assert "started again unchanged" not in message
    assert "up" not in server.stack.names()


# ------------------------------------------------------------------------------- .env quoting
@pytest.mark.parametrize(
    "value",
    ["plain", "/var/lib/cave", "secret #1 $x", "two words", "it's", 'say "hi"', "", "a#b"],
)
def test_a_written_value_is_read_back_unchanged(value):
    assert parse_env(f"K={quote_value(value)}\n")["K"] == value


def test_a_value_that_cannot_be_written_unchanged_is_left_out_and_named(
    server,
    run_cave,
    backups,  # noqa: F811
    ctx,
    tmp_path,
):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz",
        'CAVE_DATA=/x\nCAVE_FRITZ_PASSWORD="it\'s $x"\n',
        [("homeassistant", tarfile.DIRTYPE)],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    assert load_env(server.root / ".env")["CAVE_FRITZ_PASSWORD"] == ""
    assert "CAVE_FRITZ_PASSWORD (its value cannot be written unchanged; set it again)" in (
        result.out
    )


# ------------------------------------------------- links onto what holds the commands' records
@pytest.mark.parametrize(
    "members",
    [
        [("homeassistant", tarfile.SYMTYPE, ".cave")],
        [("a", tarfile.SYMTYPE, ".")],
        [("x", tarfile.SYMTYPE, "."), ("homeassistant", tarfile.SYMTYPE, "x")],
    ],
)
def test_a_link_onto_a_directory_that_holds_the_records_is_refused(
    server, run_cave, tmp_path, state_tree, members
):
    archive = make_archive(tmp_path / "in" / "a.tar.gz", "CAVE_DATA=/x\n", members)
    before = state_tree(server.data)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert "or at a directory that contains it" in result.err
    assert state_tree(server.data) == before


def test_backup_leaves_out_a_link_onto_the_records_and_stays_restorable(
    server,
    run_cave,
    backups,  # noqa: F811
):
    (server.data / "zigbee2mqtt" / "records").symlink_to("../.cave")
    (server.data / "zigbee2mqtt" / "state").symlink_to(".")

    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert sorted(manifest["links_left_out"]) == [
        "zigbee2mqtt/records -> ../.cave",
    ]
    assert run_cave("restore", str(archive), "--no-start").code == 0
