"""Reviewer's third-round tests for PR #10. Each asserts the correct behaviour; a failure is a
defect. Not for merging."""

from __future__ import annotations

import io
import itertools
import json
import os
import tarfile

import pytest

from .test_backup import new_host


def _archive(path, env_text, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:

        def add(name, kind=tarfile.REGTYPE, link="", data=b""):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            info.size = len(data)
            info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            info.uid, info.gid = os.getuid(), os.getgid()
            tar.addfile(info, io.BytesIO(data) if data and kind == tarfile.REGTYPE else None)

        manifest = {"format": 1, "release_tag": None, "release": "x", "databases": []}
        add("manifest.json", data=json.dumps(manifest).encode())
        add("env", data=env_text.encode())
        add("data", tarfile.DIRTYPE)
        for member in members:
            add(*member)


def _escaped(root):
    """Every link below ``root`` that resolves outside it."""
    real_root = os.path.realpath(root)
    found = []
    for current, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = os.path.join(current, name)
            if os.path.islink(path):
                real = os.path.realpath(path)
                if not (real == real_root or real.startswith(real_root + os.sep)):
                    found.append((path, real))
    return found


# 1. Links that each obey the rule (relative, no "..") chained in every order, plus a hard link:
#    nothing may end up pointing outside, and nothing may be written outside.
CHAIN = [
    ("data/x", tarfile.DIRTYPE),
    ("data/x/s", tarfile.SYMTYPE, "."),
    ("data/homeassistant", tarfile.SYMTYPE, "x/s/x/s/x"),
    ("data/l", tarfile.SYMTYPE, "homeassistant/s"),
]


@pytest.mark.parametrize("order", list(itertools.permutations(range(1, 4))))
def test_rule_abiding_link_chains_stay_inside(server, run_cave, tmp_path, order):
    members = [CHAIN[0], *(CHAIN[i] for i in order)]
    archive = tmp_path / "in" / "cave-backup-20261116T000000Z.tar.gz"
    _archive(archive, f"CAVE_DATA={server.data}\n", members)

    run_cave("restore", str(archive), "--no-start")

    assert _escaped(server.data) == []


def test_a_hard_link_is_refused_before_anything(server, run_cave, tmp_path):
    archive = tmp_path / "in" / "cave-backup-20261116T000000Z.tar.gz"
    _archive(archive, f"CAVE_DATA={server.data}\n", [("data/h", tarfile.LNKTYPE, "env")])

    result = run_cave("restore", str(archive), "--no-start")

    assert result.code != 0
    assert server.stack.names() == []


# 2. On a new server (no .env yet) the archive's own .env decides where the state goes. An
#    archive naming any directory makes root move that directory's contents aside and put the
#    archive's entries in it: CAVE_DATA=/root with a data/.ssh/authorized_keys, or CAVE_DATA=/.
def test_the_archive_cannot_choose_where_root_restores_on_a_new_server(
    server, run_cave, ctx, tmp_path
):
    _root, _data = new_host(server, ctx, tmp_path)
    victim = tmp_path / "victim"  # stands for /root or /etc
    (victim / ".ssh").mkdir(parents=True)
    (victim / ".ssh" / "authorized_keys").write_text("the owner's key\n")
    archive = tmp_path / "in" / "cave-backup-20261116T000000Z.tar.gz"
    _archive(
        archive,
        f"CAVE_DATA={victim}\n",
        [
            ("data/.ssh", tarfile.DIRTYPE),
            ("data/.ssh/authorized_keys", tarfile.REGTYPE, "", b"ssh-ed25519 AAAA attacker\n"),
        ],
    )

    result = run_cave("restore", str(archive), "--no-start")

    assert (victim / ".ssh" / "authorized_keys").read_text() == "the owner's key\n", (
        f"restore exited {result.code} and replaced {victim}/.ssh: {result.out}"
    )


# 3. An archive whose data/.cave is a link passes every check, then fails half-way while moving
#    the state in, every time: the marker stays, update and backup refuse, the stack is stopped,
#    and "run the same restore again" can never succeed.
def test_an_archive_that_passes_the_checks_does_not_fail_half_way(server, run_cave, tmp_path):
    archive = tmp_path / "in" / "cave-backup-20261116T000000Z.tar.gz"
    _archive(
        archive,
        f"CAVE_DATA={server.data}\n",
        [
            ("data/homeassistant", tarfile.DIRTYPE),
            ("data/homeassistant/x", tarfile.REGTYPE, "", b"x\n"),
            ("data/.cave", tarfile.SYMTYPE, "homeassistant"),
        ],
    )

    first = run_cave("restore", str(archive), "--no-start")
    second = run_cave("restore", str(archive), "--no-start")

    marker = server.data / ".cave" / "ops" / "restore.json"
    assert not (first.code != 0 and second.code != 0 and marker.exists()), f"stuck: {second.err}"


# 4. Killed during the way back after the snapshot is back and the previous release is checked
#    out, but before it was started: the snapshot is "back" on disk and HEAD is the previous
#    release, yet nothing runs. The recovery must start the stack, not record "runs healthy,
#    nothing was stopped". (Passes today; no test fails when the health check in
#    way_back_already_done is removed.)
def test_killed_before_the_previous_release_was_started_again(server, run_cave, killed):
    old, new = "v2026.11.14.1", "v2026.11.15.1"
    server.release(new)
    server.stack.unhealthy.add(new)

    def die(release):
        if release == old:
            raise killed

    server.stack.before["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.before.clear()
    assert server.stack.running is None
    assert server.checked_out() == old

    result = run_cave("update", "--recover")

    assert server.stack.running == old, result.out + result.err
    assert result.code == 2
