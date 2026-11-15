"""The helpers behind update, backup and restore: Git as the owner, copies, archive checks."""

from __future__ import annotations

import os
import pwd
import stat
import tarfile

import pytest

from cave_ops.commands import _server
from cave_ops.lock import Lock, held_by
from cave_ops.runner import Result


def test_as_root_git_runs_as_the_owner_of_the_checkout(ctx, monkeypatch, fake_runner):
    monkeypatch.setattr(_server, "current_uid", lambda: 0)
    owner = pwd.getpwuid(ctx.paths.root.stat().st_uid).pw_name
    calls = []

    class Recorder:
        def run(self, args, **kwargs):
            calls.append((tuple(args), kwargs["cwd"]))
            return Result(tuple(args), 0, "", "")

    ctx.runner = Recorder()

    _server.git(ctx, "fetch", "--tags", "origin")

    assert calls == [
        (("runuser", "-u", owner, "--", "git", "fetch", "--tags", "origin"), ctx.paths.root)
    ]


def test_as_the_owner_git_runs_directly(ctx):
    result = _server.git(ctx, "rev-parse", "--is-inside-work-tree")

    assert result.args[0] == "git"
    assert result.stdout.strip() == "true"


def test_copies_keep_modes_times_and_links(tmp_path):
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    secret = source / "sub" / "secret"
    secret.write_text("x")
    secret.chmod(0o600)
    os.utime(secret, (1_000_000, 1_000_000))
    (source / "link").symlink_to("sub/secret")
    (source / "sub").chmod(0o750)

    _server.copy_entry(source, tmp_path / "copy")

    copied = tmp_path / "copy"
    assert (copied / "sub" / "secret").read_text() == "x"
    assert stat.S_IMODE((copied / "sub" / "secret").stat().st_mode) == 0o600
    assert (copied / "sub" / "secret").stat().st_mtime == 1_000_000
    assert stat.S_IMODE((copied / "sub").stat().st_mode) == 0o750
    assert os.readlink(copied / "link") == "sub/secret"


def member(name, kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        (member("/etc/passwd"), "leaves the target"),
        (member("data/../../etc/passwd"), "leaves the target"),
        (member("data/dev", tarfile.CHRTYPE), "only directories, files and symbolic links"),
        (member("data/hard", tarfile.LNKTYPE, "data/x"), "only directories"),
        (member("data/a/link", tarfile.SYMTYPE, "/etc/shadow"), "leaves the target"),
        (member("data/a/link", tarfile.SYMTYPE, "../../../etc"), "leaves the target"),
        (member("elsewhere/file"), "not below data/"),
    ],
)
def test_archive_members_that_could_escape_are_refused(entry, why):
    with pytest.raises(_server.UnsafeArchive, match=why):
        _server.check_member(entry, "data")


def test_archive_members_inside_are_accepted():
    assert str(_server.check_member(member("data/a/b"), "data")) == "a/b"
    link = member("data/a/link", tarfile.SYMTYPE, "../b")
    assert str(_server.check_member(link, "data")) == "a/link"


def test_held_by_names_the_holder_without_taking_the_lock(tmp_path):
    path = tmp_path / "lock"
    assert held_by(path) is None
    with Lock(path, "backup"):
        assert held_by(path).startswith("`cave backup` (pid ")
    assert held_by(path) is None
    with Lock(path, "update"):  # the probe did not keep the lock
        pass
