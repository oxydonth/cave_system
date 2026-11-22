"""Tests added for the third review of pull request #10, beyond the reviewer's own
(test_zz_review3.py): what the archive may and may not decide in `cave restore`, the guards of a
way back found done, and the validator's answers."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cave_ops.commands import _server, restore, update
from cave_ops.envfile import load_env
from cave_ops.runner import Result

from .test_backup import new_host

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
REPO = Path(__file__).resolve().parents[3]


def make_archive(path, env_text, members, manifest_extra=None):
    """A cave backup archive; members are ``(name, kind, link, data, mode)`` below ``data/``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:

        def add(name, kind=tarfile.REGTYPE, link="", data=b"", mode=None):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            info.mode = mode if mode is not None else (0o755 if kind == tarfile.DIRTYPE else 0o644)
            info.uid, info.gid = os.getuid(), os.getgid()
            tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)

        manifest = {"format": 1, "release_tag": None, "release": "x", "databases": []}
        manifest.update(manifest_extra or {})
        add("manifest.json", data=json.dumps(manifest).encode())
        add("env", data=env_text.encode())
        add("data", tarfile.DIRTYPE)
        for member in members:
            name, *rest = member
            add(f"data/{name}", *rest)
    return path


# ------------------------------------------------------------------ R1: where the restore goes
def test_on_a_new_server_the_default_directory_is_used_and_said(server, run_cave, ctx, tmp_path):
    root, _ = new_host(server, ctx, tmp_path)
    elsewhere = tmp_path / "named-by-the-archive"
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz",
        f"CAVE_DATA={elsewhere}\nTZ=Europe/Berlin\n",
        [("homeassistant", tarfile.DIRTYPE), ("homeassistant/x", tarfile.REGTYPE, "", b"x\n")],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    target = (root / "data").resolve()
    assert f"Restoring into {target} (no .env yet: the default state directory)." in result.out
    assert f"The archive was taken from {elsewhere}; the restored .env will name {target}." in (
        result.out
    )
    assert (target / "homeassistant/x").read_text() == "x\n"
    assert not elsewhere.exists()
    # The restored .env agrees with the directory actually used.
    assert load_env(root / ".env")["CAVE_DATA"] == str(target)


def test_the_existing_env_decides_and_says_so(server, run_cave, tmp_path):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz",
        f"CAVE_DATA={tmp_path / 'other'}\n",
        [("homeassistant", tarfile.DIRTYPE)],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    assert f"Restoring into {server.data} (CAVE_DATA in {server.root / '.env'})." in result.out
    assert not (tmp_path / "other").exists()


def test_a_directory_that_is_not_a_state_directory_needs_force(server, run_cave, tmp_path):
    foreign = tmp_path / "home-of-someone"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine\n")
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz", "CAVE_DATA=/x\n", [("homeassistant", tarfile.DIRTYPE)]
    )

    refused = run_cave("restore", str(archive), "--data", str(foreign), "--no-start")
    assert refused.code == 1
    assert "is neither empty nor a cave state directory" in refused.err
    assert server.stack.ops == []
    assert sorted(os.listdir(foreign)) == ["notes.txt"]

    forced = run_cave("restore", str(archive), "--data", str(foreign), "--no-start", "--force")
    assert forced.code == 0, forced.err
    assert (foreign / "homeassistant").is_dir()


def test_the_env_holds_only_known_variables(server, run_cave, tmp_path):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz",
        "CAVE_DATA=/somewhere\nCOMPOSE_FILE=/tmp/evil.yaml\nCOMPOSE_PROJECT_NAME=x\n"
        "CAVE_BACKUP_DIR=/srv/backups\nTZ=Europe/Vienna\n",
        [("homeassistant", tarfile.DIRTYPE)],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 0, result.err
    env = load_env(server.root / ".env")
    assert "COMPOSE_FILE" not in env
    assert "COMPOSE_PROJECT_NAME" not in env
    assert env["TZ"] == "Europe/Vienna"
    assert env["CAVE_DATA"] == str(server.data)
    assert "that cave does not know were left out: COMPOSE_FILE, COMPOSE_PROJECT_NAME" in (
        result.out
    )
    assert "From the archive's .env, check: CAVE_BACKUP_DIR=/srv/backups" in result.out


def test_modes_keep_only_permission_bits(server, run_cave, tmp_path):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz",
        "CAVE_DATA=/x\n",
        [
            ("homeassistant", tarfile.DIRTYPE, "", b"", 0o2775),
            ("homeassistant/run", tarfile.REGTYPE, "", b"#!/bin/sh\n", 0o4755),
        ],
    )

    assert run_cave("restore", str(archive), "--no-start").code == 0
    assert stat.S_IMODE((server.data / "homeassistant/run").stat().st_mode) == 0o755
    assert stat.S_IMODE((server.data / "homeassistant").stat().st_mode) == 0o775


def test_the_manifest_cannot_give_roots_files_away(tmp_path):
    archive = make_archive(
        tmp_path / "a.tar.gz",
        "CAVE_DATA=/x\n",
        [("f", tarfile.REGTYPE, "", b"x")],
        {"owner": {"uid": 0, "gid": 0}},
    )
    checked = restore._Archive(archive)
    owner_of = checked.owners(4321, 4321)
    member = tarfile.TarInfo("f")
    member.uid, member.gid = 0, 0

    assert owner_of(member) == (0, 0)


# ----------------------------------------------------- R2: the commands' own records and lock
@pytest.mark.parametrize(
    ("members", "why"),
    [
        ([(".cave", tarfile.SYMTYPE, "homeassistant")], ".cave must be a directory"),
        ([(".cave", tarfile.REGTYPE, "", b"x")], ".cave must be a directory"),
        (
            [(".cave", tarfile.DIRTYPE), (".cave/ops", tarfile.DIRTYPE)],
            "the commands' own records and lock",
        ),
        (
            [(".cave", tarfile.DIRTYPE), (".cave/ops/update.json", tarfile.REGTYPE, "", b"{}")],
            "the commands' own records and lock",
        ),
        (
            [(".cave", tarfile.DIRTYPE), (".cave/lock", tarfile.REGTYPE, "", b"")],
            "the commands' own records and lock",
        ),
        ([("homeassistant", tarfile.SYMTYPE, ".cave/ops")], "points into .cave/ops"),
        (
            [("x", tarfile.SYMTYPE, ".cave"), ("homeassistant", tarfile.SYMTYPE, "x/ops")],
            "points into .cave/ops",
        ),
        ([("l", tarfile.SYMTYPE, ".cave/lock")], "points into .cave/lock"),
    ],
)
def test_the_commands_own_records_cannot_come_from_an_archive(
    server, run_cave, tmp_path, state_tree, members, why
):
    archive = make_archive(tmp_path / "in" / "a.tar.gz", "CAVE_DATA=/x\n", members)
    before = state_tree(server.data)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert why in result.err
    assert state_tree(server.data) == before
    assert not (server.data / ".cave/ops/restore.json").exists()


def test_a_restore_stopped_half_way_names_the_way_out(server, run_cave, tmp_path, monkeypatch):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz", "CAVE_DATA=/x\n", [("homeassistant", tarfile.DIRTYPE)]
    )
    real_rename = os.rename
    calls = []

    def failing(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError(5, "Input/output error")
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", failing)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert "restore another archive" in result.err
    assert "'A restore that cannot finish'" in result.err


def test_a_repeated_restore_without_space_says_what_state_it_is_in(
    server, run_cave, tmp_path, monkeypatch
):
    archive = make_archive(
        tmp_path / "in" / "a.tar.gz", "CAVE_DATA=/x\n", [("homeassistant", tarfile.DIRTYPE)]
    )
    marker = server.data / ".cave/ops/restore.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    series = {
        "archive": str(archive),
        "aside": "before-restore-20261122T000000Z",
        "original_entries": ["homeassistant"],
        "env_backup": None,
    }
    marker.write_text(json.dumps(series))
    monkeypatch.setattr(_server, "free_bytes", lambda path: 1024)

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code == 1
    assert "half restored from an earlier attempt" in result.err
    assert "or restore another archive" in result.err
    assert "Nothing was changed" not in result.err
    # The one directory that must be kept is named; no other before-restore-* is offered.
    keep = server.data / ".cave/ops/before-restore-20261122T000000Z"
    assert f"Do not delete {keep}" in result.err
    assert "before-restore-*" not in result.err


# ------------------------------------------------------- guards of a way back found done (N3)
def test_a_way_back_is_not_taken_as_done_while_the_new_release_is_checked_out(
    server, run_cave, killed
):
    """The snapshot is back, but the rollback was killed before it checked out the previous
    release; then someone ran `cave up` by hand, so the new release runs healthy on the old state.
    The recovery must check out the previous release and restart, not record "done"."""
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def die(argv):
        if "--force" in argv:
            raise killed

    server.runner.git_before["checkout"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.runner.git_before.clear()
    assert server.schema() == OLD  # the snapshot is back
    assert server.checked_out() == NEW
    server.stack.unhealthy.discard(NEW)
    server.stack.start(NEW)  # `cave up` by hand

    result = run_cave("update", "--recover")

    assert result.code == 2, result.err
    assert server.checked_out() == OLD
    assert server.stack.running == OLD


# ------------------------------------------------------------------- the validator's answers
@pytest.mark.parametrize(
    ("returncode", "output", "rejection"),
    [
        (1, "heating.yaml is not valid (1 problem):\nRoom 'A': the field 'x' is wrong.\n", True),
        (1, "Traceback (most recent call last):\n  File ...\nKeyError: 'rooms'\n", False),
        (
            1,
            "/config/cave/x.yaml is not valid (1 problem):\nthe file /config/cave/x.yaml "
            "does not exist\n",
            False,
        ),
        (
            1,
            "x is not valid (1 problem):\nthe file x cannot be read: [Errno 13] Permission "
            "denied\n",
            False,
        ),
        (
            1,
            "x is not valid (1 problem):\nthe file contains a value that cannot be read: big\n",
            True,
        ),
        # the real validator's wording, capitalised and with a full stop
        (1, "x is not valid (1 problem):\nThe file /config/x.yaml does not exist.\n", False),
        # a file that opens but is not UTF-8: a problem of the file itself
        (
            1,
            "x is not valid (1 problem):\nThe file x cannot be read: 'utf-8' codec can't "
            "decode byte 0xff in position 0: invalid start byte.\n",
            True,
        ),
        (2, "Usage: python -m custom_components.cave_heating.validate <heating.yaml>\n", False),
    ],
)
def test_only_a_judgement_of_the_file_is_a_rejection(returncode, output, rejection):
    result = Result(("validate",), returncode, "", output)

    assert (update.could_not_run(result, "validator") is None) is rejection


@pytest.mark.skipif(
    importlib.util.find_spec("homeassistant") is None,
    reason="the validator's package imports Home Assistant, which this environment lacks",
)
def test_the_real_validator_on_a_missing_file_is_not_a_rejection(tmp_path):
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "custom_components.cave_heating.validate",
            str(tmp_path / "missing.yaml"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    result = Result(("validate",), done.returncode, done.stdout, done.stderr)

    assert result.returncode == 1
    assert update.could_not_run(result, "validator") == (
        "the validator could not read the file it was given"
    )
