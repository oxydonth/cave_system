"""Defects the server rehearsal (tests/server/, work package 17) found on a real Debian machine,
each with the test that fails without its fix."""

from __future__ import annotations

import pytest

from cave_ops.commands import _server


@pytest.fixture
def backups(ctx, tmp_path):
    directory = tmp_path / "backups"
    ctx.env["CAVE_BACKUP_DIR"] = str(directory)
    return directory


def archives(directory):
    return sorted(directory.glob("cave-backup-*.tar.gz"))


class _GitAsTheOwner:
    """As root, Git runs through ``runuser -u <owner> --``; here everything is the owner's."""

    def __init__(self, runner):
        self.runner = runner

    def run(self, args, **kwargs):
        if list(args[:1]) == ["runuser"]:
            args = args[4:]
        return self.runner.run(args, **kwargs)


def test_the_env_from_before_a_restore_belongs_to_the_checkouts_owner(
    server, run_cave, backups, ctx, monkeypatch
):
    """Run as root, the restore wrote ``.env.before-restore-*`` as root's file into the service
    user's checkout, which hardware-acceptance 6.4 does not allow and which the service user could
    not read. It now gets the checkout's owner, as ``.env`` itself does."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    owners: dict[str, int] = {}
    ctx.runner = _GitAsTheOwner(ctx.runner)
    monkeypatch.setattr(_server, "is_root", lambda: True)
    monkeypatch.setattr(_server.os, "lchown", lambda p, u, g: owners.__setitem__(str(p), u))
    monkeypatch.setattr(_server.os, "chown", lambda p, u, g: owners.__setitem__(str(p), u))

    result = run_cave("restore", str(archive))

    assert result.code == 0, result.err
    [kept] = server.root.glob(".env.before-restore-*")
    assert owners.get(str(kept)) == server.root.stat().st_uid
    assert owners.get(str(server.root / ".env")) == server.root.stat().st_uid
