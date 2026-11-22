"""Tests added for the review of pull request #10, beyond the reviewer's own (test_zz_review.py):
the breakages that survived the first suite, and further cases for each finding."""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import tarfile

import pytest

from cave_ops.commands import _archives, _server, _update_state

from .conftest import git

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"
NEWER = "v2026.11.16.1"


@pytest.fixture
def backups(ctx, tmp_path):
    directory = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(directory)
    return directory


def archives(directory):
    return [directory / name for name in _archives.archives(directory)]


# ----------------------------------------------------------- breakages the first suite missed
def test_a_release_that_takes_a_while_to_start_is_waited_for(server, run_cave, clock):
    server.release(NEW)
    server.stack.starting[NEW] = 30  # Home Assistant's start period: 30 polls of "starting"

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.stack.running == NEW
    assert clock.now >= 30 * 5  # it polled through the start period


def test_the_rollback_tries_stopping_twice(server, run_cave, notifications):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def hiccup(release):
        if release == NEW:
            server.stack.fail_times["down"] = 1  # the rollback's first `down` fails

    server.stack.after["up"] = hiccup

    result = run_cave("update")

    assert result.code == 2, result.err
    assert server.schema() == OLD
    assert "Rolled back" in notifications()[-1]


def test_the_snapshot_is_flushed_before_and_after_it_is_named_complete(
    server, run_cave, monkeypatch
):
    server.release(NEW)
    events = []
    real_rename = os.rename

    def rename(source, target):
        if str(source).endswith(".partial"):
            events.append("rename")
        real_rename(source, target)

    monkeypatch.setattr(_update_state.os, "rename", rename)
    monkeypatch.setattr(_server, "sync", lambda: events.append("sync"))

    assert run_cave("update").code == 0
    assert events[:3] == ["sync", "rename", "sync"]


# ------------------------------------------------------------------------------- M4 archives
def tar_bytes(members):
    """An archive of ``(name, kind, linkname, data)`` members, for the extractor."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, kind, link, data in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            info.size = len(data)
            info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            tar.addfile(info, io.BytesIO(data) if data else None)
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize(
    "members",
    [
        # absolute link, then a file through it
        [("l", tarfile.SYMTYPE, "/etc", b""), ("l/x", tarfile.REGTYPE, "", b"x")],
        # a link that leaves through `..` after another link: lexically inside, really outside
        [
            ("a", tarfile.SYMTYPE, ".", b""),
            ("b", tarfile.DIRTYPE, "", b""),
            ("b/c", tarfile.SYMTYPE, "../a/..", b""),
            ("b/c/x", tarfile.REGTYPE, "", b"x"),
        ],
        # the reviewer's chain
        [
            ("a", tarfile.SYMTYPE, ".", b""),
            ("a/a/a/a/l", tarfile.SYMTYPE, "../../../..", b""),
            ("l/x", tarfile.REGTYPE, "", b"x"),
        ],
        # a directory first, then the same path again as a link
        [
            ("d", tarfile.DIRTYPE, "", b""),
            ("d", tarfile.SYMTYPE, "..", b""),
            ("d/x", tarfile.REGTYPE, "", b"x"),
        ],
        [("h", tarfile.LNKTYPE, "/etc/passwd", b"")],
        [("dev", tarfile.CHRTYPE, "", b"")],
        [("../x", tarfile.REGTYPE, "", b"x")],
        [("/x", tarfile.REGTYPE, "", b"x")],
    ],
)
def test_no_archive_writes_outside_its_target(tmp_path, members):
    target = tmp_path / "deep" / "er" / "target"
    target.mkdir(parents=True)
    with tarfile.open(fileobj=tar_bytes(members)) as tar, pytest.raises(_server.UnsafeArchive):
        _server.extract(tar, tar.getmembers(), target)
    outside = [p for p in tmp_path.rglob("*") if p.is_file() and target not in p.parents]
    assert outside == []


def test_the_finished_tree_is_checked_even_without_the_link_rule(tmp_path, monkeypatch):
    """The final check does not depend on the rule for link targets: with the rule switched
    off, the reviewer's chain (each link harmless alone, created before the link it passes
    through exists) is still refused once the whole tree is there."""
    monkeypatch.setattr(_server, "safe_link_target", lambda target: True)
    target = tmp_path / "a" / "b" / "t"
    target.mkdir(parents=True)
    members = [
        ("x", tarfile.DIRTYPE, "", b""),
        ("homeassistant", tarfile.SYMTYPE, "x/s/x/s/../..", b""),
        ("x/s", tarfile.SYMTYPE, "..", b""),
    ]
    with (
        tarfile.open(fileobj=tar_bytes(members)) as tar,
        pytest.raises(_server.UnsafeArchive, match="resolves outside"),
    ):
        _server.extract(tar, tar.getmembers(), target)


def test_links_inside_the_target_are_restored(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    members = [
        ("dir", tarfile.DIRTYPE, "", b""),
        ("dir/file", tarfile.REGTYPE, "", b"content"),
        ("dir/link", tarfile.SYMTYPE, "file", b""),
    ]
    with tarfile.open(fileobj=tar_bytes(members)) as tar:
        _server.extract(tar, tar.getmembers(), target)

    assert (target / "dir" / "link").read_text() == "content"


def test_an_atomic_write_does_not_follow_a_planted_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    target = tmp_path / "checkout" / ".env"
    target.parent.mkdir()
    (target.parent / ".env.tmp").symlink_to(victim)

    _server.write_atomic(target, b"CAVE_DATA=/x\n", 0o600)

    assert victim.read_text() == "untouched\n"
    assert target.read_bytes() == b"CAVE_DATA=/x\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ------------------------------------------------------------------- S9 owners on a new server
def test_the_old_service_users_files_become_the_new_ones(
    server, run_cave, backups, ctx, tmp_path, monkeypatch
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    old_uid = manifest["owner"]["uid"]
    assert old_uid == server.root.stat().st_uid
    restored = tmp_path / "restored"
    restored.mkdir()
    owners = {}
    monkeypatch.setattr(_server, "is_root", lambda: True)
    monkeypatch.setattr(_server.os, "lchown", lambda p, u, g: owners.__setitem__(str(p), u))
    monkeypatch.setattr(_server.os, "chown", lambda p, u, g: owners.__setitem__(str(p), u))
    from cave_ops.commands import restore

    checked = restore._Archive(archive)
    checked.extract(restored, checked.owners(4321, 4321))

    token = str(restored / "homeassistant/.cave/token")
    assert owners[token] == 4321


# ------------------------------------------------------------ M2 restore and an interrupted update
def test_a_restore_supersedes_an_update_killed_after_its_checkout(
    server, run_cave, backups, killed, notifications, state_tree
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
    assert server.checked_out() == NEW

    result = run_cave("restore", str(archive))

    assert result.code == 0, result.err
    assert server.checked_out() == OLD
    assert server.stack.running == OLD
    record = server.record()
    assert record["switch"] is None
    assert record["failed"][NEW]["reason"] == "interrupted, then superseded by a restore"
    assert f"The interrupted update to {NEW} was abandoned" in notifications()[-1]
    after = state_tree(server.data)
    after.pop(f"homeassistant/started-{OLD}")
    wanted.pop(f"homeassistant/started-{OLD}")
    assert after == wanted
    assert run_cave("update", "--recover").out.startswith("No interrupted update")


# ---------------------------------------------------------------------- M3 Git left half-way
def test_a_rollback_removes_what_a_half_done_checkout_added(server, run_cave, killed):
    server.release(NEW, {"ops/new_module.py": "print('new')\n", "RELEASE.d/notes": "n\n"})

    def power_cut(argv):
        if "--force" not in argv:
            (server.root / ".git" / "HEAD.lock").write_text("")
            (server.root / "ops").mkdir(exist_ok=True)
            (server.root / "ops" / "new_module.py").write_text("print('new')\n")
            raise killed

    server.runner.git_before["checkout"] = power_cut
    with pytest.raises(killed):
        run_cave("update")
    server.runner.git_before.clear()

    result = run_cave("update", "--recover")

    assert result.code == 2, result.err
    assert not (server.root / "ops" / "new_module.py").exists()
    assert not (server.root / ".git" / "HEAD.lock").exists()
    status = git("status", "--porcelain", cwd=server.root)
    assert status == ""


# ----------------------------------------------------------------- S1 an unfinished way back
def test_an_unfinished_way_back_is_shown_and_blocks_backups(
    server, run_cave, backups, notifications
):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def hiccup(release):
        if release == NEW:
            server.stack.fail["down"] = 1

    server.stack.after["up"] = hiccup
    result = run_cave("update")
    assert result.code == 3
    assert "The way back is tried again" in notifications()[-1]
    server.stack.after.clear()
    server.stack.fail.clear()

    status = run_cave("status")
    assert status.code == 1
    assert "INTERRUPTED" in status.out
    assert run_cave("backup").code == 1


# -------------------------------------------------------------- S2 checks that could not run
@pytest.mark.parametrize("check", ["check_config", "validate"])
@pytest.mark.parametrize("status", [124, 125, 126, 127])
def test_a_check_that_could_not_run_is_not_a_rejection(
    server, run_cave, notifications, check, status
):
    server.release(NEW)
    server.stack.fail[check] = status

    result = run_cave("update")

    assert result.code == 1
    assert NEW not in server.record()["failed"]
    message = notifications()[-1]
    assert "could not run" in message
    assert "heating.yaml as the messages say" not in message
    assert "the next run tries again" in message
    assert "down" not in server.stack.names()


def test_a_release_without_the_validator_is_not_a_rejection(server, run_cave, notifications):
    server.release(NEW)
    server.stack.fail["validate"] = 1
    server.stack.validator_errors = (
        "/usr/local/bin/python3: No module named custom_components.cave_heating.validate\n"
    )

    run_cave("update")

    assert NEW not in server.record()["failed"]
    assert "the validator is not in this release" in notifications()[-1]


def test_a_rejected_heating_yaml_is_remembered_with_the_right_advice(
    server, run_cave, notifications
):
    server.release(NEW)
    server.stack.fail["validate"] = 1

    run_cave("update")

    assert NEW in server.record()["failed"]
    assert "Adapt cave/heating.yaml as the messages say" in notifications()[-1]


# --------------------------------------------------------------------- S3, S4 which releases
def test_a_release_on_main_is_installed_although_a_newer_tag_is_elsewhere(server, run_cave):
    server.release(NEW)
    git("checkout", "-q", "-b", "experiment", cwd=server.origin)
    server.release(NEWER)
    git("checkout", "-q", "main", cwd=server.origin)

    result = run_cave("update")

    assert result.code == 0, result.err
    assert server.checked_out() == NEW
    assert f"Ignoring {NEWER}: its commit is not on main." in result.out


def test_a_withdrawn_release_is_not_installed(server, run_cave):
    server.release(NEW)
    git("tag", "-d", NEW, cwd=server.origin)  # withdrawn on GitHub

    result = run_cave("update")

    assert result.code == 0
    assert "Up to date" in result.out
    assert server.checked_out() == OLD


# --------------------------------------------------------------- S5, S7 backups and restores
def test_the_second_location_keeps_the_new_archive_too(server, run_cave, backups, ctx, tmp_path):
    second = tmp_path / "nas"
    second.mkdir()
    for name in ("cave-backup-20991231T000000Z.tar.gz", "cave-backup-20991231T000001Z.tar.gz"):
        (second / name).write_bytes(b"")
    ctx.env["CAVE_BACKUP_COPY_TO"] = str(second)
    ctx.env["CAVE_BACKUP_KEEP"] = "2"

    assert run_cave("backup").code == 0

    [new] = archives(backups)
    assert (second / new.name).is_file()
    assert len(archives(second)) == 2


def test_a_restore_without_room_to_unpack_stops_nothing(
    server, run_cave, backups, monkeypatch, state_tree
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    before = state_tree(server.data)
    monkeypatch.setattr(_server, "free_bytes", lambda path: 1024)

    result = run_cave("restore", str(archive))

    assert result.code == 1
    assert "unpacking needs about" in result.err
    assert server.stack.ops == []
    assert state_tree(server.data) == before


# ---------------------------------------------------------------- optional: override, signals
def test_the_checks_include_the_local_override_and_a_misfit_stops_nothing(
    server, run_cave, notifications
):
    (server.root / "compose.override.yaml").write_text("services: {}\n")
    server.release(NEW)
    server.stack.fail["config"] = 1

    result = run_cave("update")

    assert result.code == 1
    assert server.stack.configured[0].endswith(f"{os.pathsep}{server.root}/compose.override.yaml")
    assert server.stack.ops == []
    assert "does not accept this server's .env and compose.override.yaml" in notifications()[-1]


def test_ctrl_c_during_the_way_back_is_ignored(server, run_cave):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    def interrupt(release):
        if release == NEW:  # the rollback's `down`: the checkout is the new release
            os.kill(os.getpid(), signal.SIGINT)

    server.stack.before["down"] = interrupt

    result = run_cave("update")

    assert result.code == 2, result.err
    assert server.stack.running == OLD
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
