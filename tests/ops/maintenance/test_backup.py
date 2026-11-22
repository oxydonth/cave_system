"""``cave backup`` and ``cave restore``, with real files and a real SQLite database."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from cave_ops.commands import _archives, _server, restore
from cave_ops.envfile import load_env, parse_env
from cave_ops.lock import Lock
from cave_ops.paths import Paths

DB = "homeassistant/home-assistant_v2.db"
INITIAL_ROWS = 500


@pytest.fixture
def backups(ctx, tmp_path):
    directory = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(directory)
    return directory


def archives(directory):
    """The archives in ``directory``, oldest first."""
    return [directory / name for name in _archives.archives(directory)]


def members(archive):
    with tarfile.open(archive) as tar:
        return {m.name: m for m in tar.getmembers()}


def make_database(path):
    """A WAL database like Home Assistant's, with ``INITIAL_ROWS`` rows in the main file."""
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE states (n INTEGER PRIMARY KEY, payload BLOB)")
    con.executemany(
        "INSERT INTO states VALUES (?, ?)",
        ((n, os.urandom(512)) for n in range(1, INITIAL_ROWS + 1)),
    )
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


class Writer(threading.Thread):
    """Home Assistant's recorder: commits one row after another, never checkpointing, so that
    everything new is only in the -wal file."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.written = INITIAL_ROWS
        self.stop = threading.Event()
        self.started_writing = threading.Event()

    def run(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA wal_autocheckpoint=0")
        while not self.stop.is_set():
            con.execute("INSERT INTO states VALUES (?, ?)", (self.written + 1, os.urandom(512)))
            con.commit()
            self.written += 1
            self.started_writing.set()
        con.close()


def rows(path):
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return [n for (n,) in con.execute("SELECT n FROM states ORDER BY n")]
    finally:
        con.close()


# ------------------------------------------------------------------------------------- backup
def test_a_backup_holds_the_state_and_env_and_stops_nothing(
    server, run_cave, backups, notifications
):
    result = run_cave("backup")

    assert result.code == 0, result.err
    assert server.stack.ops == []
    [archive] = archives(backups)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    names = members(archive)
    assert {"manifest.json", "env", "data/homeassistant/.storage/core.config_entries"} <= set(names)
    assert "data/.cave/mqtt-credentials" in names
    assert not any(name.startswith(("data/.cave/ops", "data/.cave/lock")) for name in names)
    assert names["data/homeassistant/.cave/token"].mode == 0o600
    assert notifications()[-1].startswith(f"Backup {archive.name} written to {backups}")


def test_retention_keeps_the_newest_archives(server, run_cave, backups, ctx):
    ctx.env["CAVE_BACKUP_KEEP"] = "3"
    made = []
    for _ in range(5):
        assert run_cave("backup").code == 0
        made.append(archives(backups)[-1].name)

    assert [p.name for p in archives(backups)] == made[-3:]
    assert len(set(made)) == 5


def test_an_invalid_retention_is_refused(server, run_cave, backups, ctx):
    ctx.env["CAVE_BACKUP_KEEP"] = "0"

    result = run_cave("backup")

    assert result.code == 1
    assert "CAVE_BACKUP_KEEP=0" in result.err
    assert not backups.exists()


def test_a_copy_goes_to_the_second_location_with_the_same_retention(
    server, run_cave, backups, ctx, tmp_path
):
    second = tmp_path / "nas"
    second.mkdir()
    ctx.env["CAVE_BACKUP_COPY_TO"] = str(second)
    ctx.env["CAVE_BACKUP_KEEP"] = "2"
    for _ in range(3):
        assert run_cave("backup").code == 0

    assert [p.name for p in archives(second)] == [p.name for p in archives(backups)]
    assert len(archives(second)) == 2
    assert archives(second)[-1].read_bytes() == archives(backups)[-1].read_bytes()


def test_a_missing_second_location_is_reported_and_not_created(
    server, run_cave, backups, ctx, tmp_path, notifications
):
    unmounted = tmp_path / "nas"
    ctx.env["CAVE_BACKUP_COPY_TO"] = str(unmounted)

    result = run_cave("backup")

    assert result.code == 1
    assert len(archives(backups)) == 1
    assert not unmounted.exists()
    assert "FAILED: it is not a directory (not mounted?)" in notifications()[-1]


def test_a_failed_backup_leaves_no_partial_archive(
    server, run_cave, backups, monkeypatch, notifications
):
    make_database(server.data / DB)
    assert run_cave("backup").code == 0
    before = [p.name for p in backups.iterdir()]

    real_add = _archives.add_file
    added = []

    def disk_full(archive, name, source, info):
        added.append(name)
        if len(added) == 5:
            raise OSError(28, "No space left on device")
        real_add(archive, name, source, info)

    monkeypatch.setattr(_archives, "add_file", disk_full)

    result = run_cave("backup")

    assert result.code == 1
    assert [p.name for p in backups.iterdir()] == before
    assert "The backup failed: [Errno 28] No space left on device" in notifications()[-1]


def test_a_damaged_database_is_archived_as_it_is_and_named(
    server, run_cave, backups, notifications
):
    damaged = "homeassistant/home-assistant_v2.db.corrupt.2026-11-15T03:00:00"
    (server.data / damaged).write_bytes(b"SQLite format 3\x00" + b"garbage" * 100)
    make_database(server.data / DB)

    result = run_cave("backup")

    assert result.code == 0
    [archive] = archives(backups)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert tar.extractfile(f"data/{damaged}").read() == (server.data / damaged).read_bytes()
    assert manifest["databases"] == [DB]
    assert manifest["unreadable_databases"] == [damaged]
    assert f"WARNING: {damaged} could not be read" in notifications()[-1]


def test_backup_and_update_never_overlap(server, run_cave, backups, ctx, notifications):
    with Lock(ctx.paths.lock_file, "update"):
        result = run_cave("backup")

    assert result.code == 1
    assert "`cave update`" in result.err
    assert not backups.exists() or archives(backups) == []
    assert notifications()[-1].startswith("No backup was made")


def test_backup_as_the_wrong_user_is_refused(server, run_cave, backups, monkeypatch):
    monkeypatch.setattr(_server, "current_uid", lambda: 4242)

    result = run_cave("backup")

    assert result.code == 1
    assert "`cave backup` must run as root" in result.err
    assert not backups.exists()


# ----------------------------------------------------------------------- round trip (restore)
def new_host(server, ctx, tmp_path):
    """A freshly provisioned server: a clone of the repository, no .env, no state."""
    root = tmp_path / "new-host" / "checkout"
    root.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(server.origin), str(root)],
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    data = tmp_path / "new-host" / "state"
    ctx.paths = Paths(root=root, data=root / "data")
    ctx.env = {}
    server.stack.running = None
    server.stack.data = data
    return root, data


def test_round_trip_onto_a_new_server_with_a_database_being_written(
    server, run_cave, backups, ctx, tmp_path, state_tree, notifications
):
    database = server.data / DB
    make_database(database)
    writer = Writer(database)
    writer.start()
    assert writer.started_writing.wait(10)
    during = writer.written
    try:
        result = run_cave("backup")
    finally:
        writer.stop.set()
        writer.join(10)
    assert result.code == 0, result.err
    assert writer.written > during, "the database was written to while the backup ran"
    [archive] = archives(backups)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["databases"] == [DB]
    assert f"data/{DB}-wal" not in members(archive)
    old_env = (server.root / ".env").read_text()
    old_state = state_tree(server.data)

    root, data = new_host(server, ctx, tmp_path)
    result = run_cave("restore", str(archive), "--data", str(data))

    assert result.code == 0, result.err
    restored = rows(data / DB)
    # A consistent moment: every row up to some point, none missing, more than the main file
    # held, so the rows that were only in the -wal file at that moment are there too.
    assert restored == list(range(1, len(restored) + 1))
    assert len(restored) > INITIAL_ROWS
    assert len(restored) <= writer.written
    new_state = state_tree(data)
    for name in (DB, f"{DB}-wal", f"{DB}-shm"):
        old_state.pop(name, None)
        new_state.pop(name, None)
    assert new_state == old_state
    # The restored .env holds the archive's values, with CAVE_DATA the directory actually used.
    restored_env = load_env(root / ".env")
    expected = {**parse_env(old_env), "CAVE_DATA": str(data)}
    assert {k: restored_env[k] for k in expected} == expected
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    token = data / "homeassistant/.cave/token"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert (root / "tools").is_dir()
    assert server.stack.running == "v2026.11.14.1"
    assert "The stack is healthy." in notifications()[-1]


def test_restore_over_a_running_server_keeps_what_it_replaced(
    server, run_cave, backups, ctx, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    backed_up = state_tree(server.data)
    (server.data / "homeassistant/automations.yaml").write_text("- id: added later\n")
    (server.data / "zigbee2mqtt/new-device.json").write_text("{}\n")
    replaced = state_tree(server.data)

    result = run_cave("restore", str(archive))

    assert result.code == 0, result.err
    assert server.stack.names() == ["down", "up"]
    assert state_tree(server.data) == backed_up
    [aside] = (server.data / ".cave/ops").glob("before-restore-*")
    assert _tree(aside) == replaced
    assert list(server.root.glob(".env.before-restore-*"))


def _tree(path: Path):
    return {
        str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()
    }


def test_an_interrupted_restore_is_finished_by_running_it_again(
    server, run_cave, backups, ctx, monkeypatch, killed, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    backed_up = state_tree(server.data)
    (server.data / "homeassistant/automations.yaml").write_text("- id: added later\n")
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 4:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive), "--no-start")
    monkeypatch.undo()

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    assert state_tree(server.data) == backed_up


def test_restore_refuses_an_archive_from_a_newer_release(
    server, run_cave, backups, ctx, tmp_path, state_tree
):
    server.release("v2026.11.15.1")
    assert run_cave("update").code == 0
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    root, data = new_host(server, ctx, tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "v2026.11.14.1"],
        cwd=root,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
    )

    refused = run_cave("restore", str(archive), "--data", str(data))
    assert refused.code == 1
    assert "taken with v2026.11.15.1, but the checkout has v2026.11.14.1" in refused.err
    assert not data.exists()

    forced = run_cave("restore", str(archive), "--data", str(data), "--force", "--no-start")
    assert forced.code == 0


def test_an_unsafe_archive_is_refused_before_anything_changes(
    server, run_cave, backups, ctx, tmp_path, state_tree
):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        for name, text in (
            ("manifest.json", json.dumps({"format": 1})),
            ("env", "CAVE_DATA=/tmp/x\n"),
            ("data/../../outside", "boom"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text.encode()))
    before = state_tree(server.data)

    result = run_cave("restore", str(evil))

    assert result.code == 1
    assert "leaves the target directory" in result.err
    assert server.stack.ops == []
    assert state_tree(server.data) == before


def test_restore_and_update_never_overlap(server, run_cave, backups, ctx):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    with Lock(ctx.paths.lock_file, "update"):
        result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "`cave update`" in result.err
    assert server.stack.ops == []


def test_no_backup_of_a_state_left_by_an_interrupted_update(
    server, run_cave, backups, killed, notifications
):
    server.release("v2026.11.15.1")

    def kill(release):
        if release == "v2026.11.15.1":
            raise killed

    server.stack.before["up"] = kill
    with pytest.raises(killed):
        run_cave("update")

    result = run_cave("backup")

    assert result.code == 1
    assert not backups.exists() or archives(backups) == []
    assert "`sudo ./cave update --recover` finishes it" in result.err
