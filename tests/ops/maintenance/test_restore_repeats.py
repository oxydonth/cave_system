"""Reviewer's fourth-round tests for PR #10 (tests/ops/maintenance/). Each asserts the correct
behaviour; a failure is a defect. Not for merging."""

from __future__ import annotations

import errno
import os

import pytest

from cave_ops.commands import restore
from cave_ops.envfile import load_env

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

ENTRIES = ("homeassistant", "mosquitto", "zigbee2mqtt", ".cave/mqtt-credentials")


def _cut_while_moving_aside(server, run_cave, archive, monkeypatch, killed):
    """A restore killed after it moved the first current entry aside."""
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 2:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    marker = server.data / ".cave" / "ops" / "restore.json"
    assert marker.exists()
    assert not (server.data / "homeassistant").exists()  # moved aside, nothing moved in yet
    return marker


# 1. The repeat of a half-done restore fails while unpacking (a full disk). It removes the marker
#    and starts the stack "again unchanged" -- on a state without homeassistant/, which Docker
#    then creates empty: Home Assistant starts from scratch, and update and backup no longer wait.
def test_a_failed_repeat_keeps_the_marker_and_does_not_start_a_half_restored_state(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    marker = _cut_while_moving_aside(server, run_cave, archive, monkeypatch, killed)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", disk_full)
    real_start = server.stack.start

    def start_like_docker(release):
        # Docker creates a missing bind-mount source as an empty directory.
        (server.data / "homeassistant").mkdir(exist_ok=True)
        real_start(release)

    monkeypatch.setattr(server.stack, "start", start_like_docker)
    result = run_cave("restore", str(archive))

    assert marker.exists(), "the marker was removed although the state is half restored"
    assert server.stack.running is None, (
        f"the stack was started on a state without homeassistant/: {result.err}"
    )


# 2. docs/operations.md, "A restore that cannot finish", way 3: "the oldest before-restore-*
#    holds" the state from before the first attempt. After two attempts cut while moving aside,
#    it holds only part of it; the rest is in the second one.
def test_the_oldest_before_restore_holds_the_whole_state_from_before(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _cut_while_moving_aside(server, run_cave, archive, monkeypatch, killed)
    _cut_while_moving_aside(server, run_cave, archive, monkeypatch, killed)

    asides = sorted((server.data / ".cave" / "ops").glob("before-restore-*"))
    oldest = asides[0]
    missing = [e for e in ENTRIES if not os.path.lexists(oldest / e)]
    assert missing == [], (
        f"{oldest.name} lacks {missing}; they are in {[a.name for a in asides[1:]]}"
    )


# 3. The restored .env is re-rendered without quotes. A value the owner had to quote (the docs
#    say: "In single quotes if it contains `$` or ` #`", e.g. the FRITZ!Box password) comes back
#    changed.
def test_a_quoted_value_survives_backup_and_restore(server, run_cave, backups, ctx):  # noqa: F811
    env = server.root / ".env"
    env.write_text(env.read_text() + "CAVE_FRITZ_PASSWORD='secret #1 $x'\n")
    ctx.env = load_env(env)
    ctx.env["CAVE_BACKUP_DIR"] = str(backups)
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    assert run_cave("restore", str(archive), "--no-start").code == 0

    assert load_env(env)["CAVE_FRITZ_PASSWORD"] == "secret #1 $x", env.read_text()
