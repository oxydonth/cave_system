"""Tests added for the second review of pull request #10, beyond the reviewer's own
(test_zz_review2.py): archives at the level of `cave restore`, backups that stay restorable, the
breakages the suite did not notice, and the optional items."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cave_ops.commands import _archives, _server, restore, update

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def backups(ctx, tmp_path):
    directory = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(directory)
    return directory


def archives(directory):
    return [directory / name for name in _archives.archives(directory)]


def make_archive(path, data_dir, members):
    """A cave backup archive with the given ``(name, kind, link)`` members below ``data/``."""
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
        for name, kind, link in members:
            add(f"data/{name}", kind, link, b"x" if kind == tarfile.REGTYPE else b"")


# ------------------------------------------------------------------------- N1, at restore level
@pytest.mark.parametrize(
    "members",
    [
        # absolute
        [("homeassistant", tarfile.SYMTYPE, "/etc")],
        # `..` that stays lexically inside, and one that leaves
        [("x", tarfile.DIRTYPE, ""), ("homeassistant", tarfile.SYMTYPE, "./x/..")],
        [("homeassistant", tarfile.SYMTYPE, "../../..")],
        # inside `.cave`, the one directory restored entry by entry
        [(".cave", tarfile.DIRTYPE, ""), (".cave/mqtt-credentials", tarfile.SYMTYPE, "../../x")],
        # the reviewer's chain, and the same chain in the other order
        [
            ("x", tarfile.DIRTYPE, ""),
            ("homeassistant", tarfile.SYMTYPE, "x/s/x/s/../.."),
            ("x/s", tarfile.SYMTYPE, ".."),
        ],
        [
            ("x", tarfile.DIRTYPE, ""),
            ("x/s", tarfile.SYMTYPE, ".."),
            ("homeassistant", tarfile.SYMTYPE, "x/s/x/s/../.."),
        ],
        # a file written through a link the archive creates
        [("z", tarfile.SYMTYPE, "zigbee2mqtt"), ("z/evil", tarfile.REGTYPE, "")],
    ],
)
def test_restore_refuses_link_archives_before_touching_anything(
    server, run_cave, tmp_path, state_tree, members
):
    archive = tmp_path / "in" / "cave-backup-20261115T000000Z.tar.gz"
    make_archive(archive, server.data, members)
    before = state_tree(server.data)

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "refusing archive member" in result.err
    assert server.stack.ops == []  # nothing stopped
    assert state_tree(server.data) == before
    assert not list(tmp_path.rglob("evil"))


def test_links_inside_the_state_are_restored(server, run_cave, tmp_path):
    archive = tmp_path / "in" / "cave-backup-20261115T000000Z.tar.gz"
    make_archive(
        archive,
        server.data,
        [
            ("zigbee2mqtt", tarfile.DIRTYPE, ""),
            ("zigbee2mqtt/log", tarfile.DIRTYPE, ""),
            ("zigbee2mqtt/log/2026-11-16", tarfile.DIRTYPE, ""),
            ("zigbee2mqtt/log/2026-11-16/log.log", tarfile.REGTYPE, ""),
            ("zigbee2mqtt/log/current", tarfile.SYMTYPE, "./2026-11-16"),
        ],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    assert (server.data / "zigbee2mqtt/log/current/log.log").read_text() == "x"


# --------------------------------------------------------- backups that stay restorable (N1)
def test_a_backup_leaves_out_links_restore_would_refuse_and_can_be_restored(
    server, run_cave, backups, ctx, tmp_path, notifications
):
    z2m = server.data / "zigbee2mqtt"
    (z2m / "log" / "2026-11-16").mkdir(parents=True)
    (z2m / "log" / "current").symlink_to("./2026-11-16")
    (z2m / "absolute").symlink_to("/etc/hosts")
    (z2m / "climbing").symlink_to("../mosquitto")

    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert "data/zigbee2mqtt/log/current" in names
    assert "data/zigbee2mqtt/absolute" not in names
    assert "data/zigbee2mqtt/climbing" not in names
    assert sorted(manifest["links_left_out"]) == [
        "zigbee2mqtt/absolute -> /etc/hosts",
        "zigbee2mqtt/climbing -> ../mosquitto",
    ]
    assert (
        "2 symbolic link(s) pointing outside their directory were left out" in (notifications()[-1])
    )

    result = run_cave("restore", str(archive))

    assert result.code == 0, result.err
    assert os.readlink(server.data / "zigbee2mqtt/log/current") == "./2026-11-16"


# --------------------------------------------------------------- N5: breakages not noticed
def test_the_previous_release_is_waited_for_after_a_rollback(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def slow_restart(release):
        if release == NEW:  # once the new release ran, the old one takes a while to start
            server.stack.starting[OLD] = 30

    server.stack.after["up"] = slow_restart

    result = run_cave("update")

    assert result.code == 2, result.err
    assert server.stack.running == OLD


def test_the_reset_fallback_restores_the_previous_release(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def failing_forced_checkout(argv):
        if "--force" in argv:
            server.runner.git_fail.setdefault("checkout", [(128, "fatal: unable to write\n")])

    server.runner.git_before["checkout"] = failing_forced_checkout

    result = run_cave("update")

    assert result.code == 2, result.err
    assert server.checked_out() == OLD
    assert any(call.args[1:3] == ("reset", "--hard") for call in server.runner.fake.calls)


def test_state_entries_only_the_new_release_made_are_set_aside(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def new_service(release):
        if release == NEW:
            (server.data / "newservice").mkdir()
            (server.data / "newservice" / "data").write_text("new\n")

    server.stack.after["up"] = new_service

    assert run_cave("update").code == 2
    assert not (server.data / "newservice").exists()


def test_the_second_stop_of_the_rollback_waits_first(server, run_cave, clock):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    stops = []

    def note_stop(release):
        stops.append(clock.now)

    def hiccup(release):
        if release == NEW:
            server.stack.fail_times["down"] = 1
            server.stack.before["down"] = note_stop

    server.stack.after["up"] = hiccup

    assert run_cave("update").code == 2
    assert len(stops) == 2
    assert stops[1] - stops[0] >= 10


def test_a_finished_but_unrecorded_way_back_says_so_truthfully(
    server, run_cave, monkeypatch, notifications
):
    from cave_ops.commands import _update_state

    server.release(NEW)
    server.stack.unhealthy.add(NEW)
    real_save = _update_state.save
    full = []

    def full_disk(ctx, record):
        if full or (record.switch is not None and record.switch.restored):
            full.append(1)
            raise OSError(28, "No space left on device")
        real_save(ctx, record)

    monkeypatch.setattr(_update_state, "save", full_disk)
    run_cave("update")
    monkeypatch.undo()
    # Not removed while unrecorded: the record still names it.
    assert (server.data / ".cave/ops/snapshots").exists()

    result = run_cave("update", "--recover")

    assert result.code == 2
    assert "That could not be recorded then" in notifications()[-1]
    assert "left behind" not in notifications()[-1]
    assert list((server.data / ".cave/ops/snapshots").iterdir()) == []


# ---------------------------------------------------------- N2: what the clean-up leaves alone
def test_a_rollback_names_what_it_left_in_place(server, run_cave, notifications):
    server.release(NEW, files={"notes.txt": "from the release\n"})
    (server.root / "notes.txt").write_text("the owner's notes\n")

    assert run_cave("update").code == 2

    assert (server.root / "notes.txt").read_text() == "the owner's notes\n"
    assert (
        "left in place, because they are not the new release's files: notes.txt"
        in (notifications()[-1])
    )


# --------------------------------------------------------- optional: another file system
def test_a_state_entry_on_another_file_system_is_refused_before_anything(
    server, run_cave, monkeypatch, notifications, backups
):
    real = _server.device_of

    def device(path):
        return 999 if path.name == "homeassistant" else real(path)

    monkeypatch.setattr(_server, "device_of", device)
    server.release(NEW)

    result = run_cave("update")

    assert result.code == 1
    assert "homeassistant in" in notifications()[-1]
    assert "on another file system" in notifications()[-1]
    assert "down" not in server.stack.names()

    monkeypatch.undo()
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    monkeypatch.setattr(_server, "device_of", device)
    refused = run_cave("restore", str(archive))
    assert refused.code == 1
    assert "on another file system" in refused.err


# ----------------------------------------------- optional: an interrupted restore, its window
def test_an_interrupted_restore_stops_update_and_backup_until_it_is_run_again(
    server, run_cave, backups, killed, monkeypatch, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    wanted = state_tree(server.data)
    (server.data / "homeassistant/automations.yaml").write_text("- id: later\n")
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 2:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive), "--no-start")
    monkeypatch.undo()

    status = run_cave("status")
    assert any(
        line.startswith("INTERRUPTED:") and "a restore of" in line
        for line in status.out.splitlines()
    )
    assert status.code == 1
    assert run_cave("backup").code == 1
    server.stack.ops.clear()
    refused = run_cave("update", "--recover")
    assert refused.code == 1
    assert "Run the same `sudo ./cave restore` again" in refused.err
    assert server.stack.ops == []

    assert run_cave("restore", str(archive), "--no-start").code == 0
    assert state_tree(server.data) == wanted
    assert "INTERRUPTED" not in run_cave("status").out


def test_a_restore_killed_while_moving_is_never_undone_by_the_update_recovery(
    server, run_cave, backups, killed, monkeypatch, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    wanted = state_tree(server.data)
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.after["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == 2:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive), "--no-start")
    monkeypatch.undo()
    # The update is forgotten already: no recovery can put its snapshot back.
    assert server.record()["switch"] is None
    assert run_cave("update", "--recover").code == 1

    assert run_cave("restore", str(archive)).code == 0
    after = state_tree(server.data)
    after.pop(f"homeassistant/started-{OLD}", None)
    wanted.pop(f"homeassistant/started-{OLD}", None)
    assert after == wanted
    assert server.checked_out() == OLD


# ---------------------------------------------------------------------------- the real validator
@pytest.mark.skipif(
    importlib.util.find_spec("homeassistant") is None,
    reason="the validator's package imports Home Assistant, which this environment lacks",
)
def test_the_real_validators_answers_are_classified_as_update_expects(tmp_path):
    """Runs `python -m custom_components.cave_heating.validate` as a subprocess (nothing of Home
    Assistant is imported here) and feeds its results to update's classifier."""
    invalid = tmp_path / "heating.yaml"
    invalid.write_text("version: 1\nrooms:\n  - name: Bad room\n    thermostats: 7\n")

    def run(*args):
        done = subprocess.run(
            [sys.executable, "-m", "custom_components.cave_heating.validate", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        return _server.Result(tuple(args), done.returncode, done.stdout, done.stderr)

    valid = run(str(REPO / "homeassistant/heating.example.yaml"))
    assert valid.returncode == 0, valid.output
    rejected = run(str(invalid))
    assert rejected.returncode == 1
    assert (
        rejected.stderr.splitlines()[0].endswith("problem):")
        or "is not valid" in (rejected.stderr.splitlines()[0])
    )
    assert "Traceback" not in rejected.output
    assert update.could_not_run(rejected, "validator") is None  # a rejection
    wrongly = run()
    assert wrongly.returncode == 2
    assert update.could_not_run(wrongly, "validator") == "exit status 2"  # not a rejection
