"""The lock of concept section 4.5 for root and the service user, in either order, and the
owner commands (`apply`, `bootstrap`) refusing while an update or a restore is unfinished."""

from __future__ import annotations

import json
import os

import pytest

from cave_ops import lock as lock_module
from cave_ops.errors import CaveError
from cave_ops.lock import Lock, LockBusy

VALID = (
    "version: 1\nrooms:\n  - id: office\n    name: Office\n    trvs:\n      - entity: climate.a\n"
)


@pytest.fixture
def home(ctx, run_cave):
    """An initialised state directory (as in test_apply.py, which cannot be imported in the
    Python 3.11 run of these tests)."""
    assert run_cave("init").code == 0
    return ctx.paths


@pytest.fixture
def initialised(run_cave, ctx):
    assert run_cave("init").code == 0
    return ctx.env


def edit(home, text=VALID):
    home.heating.write_text(text)


@pytest.fixture
def as_root(monkeypatch):
    """Pretend to be root, with CAVE_DATA and .cave owned by uid/gid 4321, and record every
    change of owner and mode instead of making it."""
    changes = []
    monkeypatch.setattr(lock_module, "_euid", lambda: 0)
    monkeypatch.setattr(lock_module, "_owner_of", lambda path: (4321, 4321))
    monkeypatch.setattr(lock_module.os, "chown", lambda p, u, g: changes.append(("chown", u, g)))
    monkeypatch.setattr(lock_module.os, "fchown", lambda fd, u, g: changes.append(("fchown", u, g)))
    return changes


def test_root_first_gives_the_lock_and_its_directory_to_the_owner_of_the_state(tmp_path, as_root):
    path = tmp_path / "data" / ".cave" / "lock"
    (tmp_path / "data").mkdir()

    with Lock(path, "backup"):
        pass

    # The test's files belong to the test's user, not 4321: both were handed over.
    assert ("chown", 4321, 4321) in as_root  # .cave
    assert ("fchown", 4321, 4321) in as_root  # the lock file
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_the_service_user_first_then_root_changes_no_owner(tmp_path, monkeypatch):
    path = tmp_path / "data" / ".cave" / "lock"
    (tmp_path / "data").mkdir()
    with Lock(path, "apply"):  # the service user creates it
        pass
    me = (os.getuid(), os.getgid())
    changes = []
    monkeypatch.setattr(lock_module, "_euid", lambda: 0)
    monkeypatch.setattr(lock_module, "_owner_of", lambda p: me)
    monkeypatch.setattr(lock_module.os, "fchown", lambda fd, u, g: changes.append((u, g)))
    monkeypatch.setattr(lock_module.os, "chown", lambda p, u, g: changes.append((u, g)))

    with Lock(path, "update"):  # then root
        pass

    assert changes == []


def test_a_lock_file_the_user_cannot_write_is_still_an_exclusive_lock(tmp_path):
    path = tmp_path / ".cave" / "lock"
    path.parent.mkdir()
    path.write_text("`cave backup` (pid 1, since 2026-11-22 02:30:00)\n")
    path.chmod(0o444)  # root's 0644 file, seen by the service user

    with Lock(path, "apply"), pytest.raises(LockBusy):
        Lock(path, "update").acquire()
    assert path.read_text().startswith("`cave backup`")  # not writable: not rewritten


def test_a_directory_the_user_cannot_write_and_no_lock_file_says_what_to_do(tmp_path):
    directory = tmp_path / ".cave"
    directory.mkdir()
    directory.chmod(0o555)  # root's .cave, from an earlier version, and no lock file yet
    try:
        with pytest.raises(CaveError, match="the next root command that takes the lock"):
            Lock(directory / "lock", "apply").acquire()
    finally:
        directory.chmod(0o755)


# ---------------------------------------------------- apply and bootstrap refuse while unfinished
def unfinished_update(ctx):
    record = ctx.paths.data / ".cave" / "ops" / "update.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    switch = {
        "phase": "switching",
        "from_commit": "a" * 40,
        "from_label": "v2026.11.14.1",
        "to_release": "v2026.11.15.1",
        "to_commit": "b" * 40,
        "snapshot": "0001-x",
    }
    record.write_text(json.dumps({"switch": switch, "failed": {}}))


def unfinished_restore(ctx):
    marker = ctx.paths.data / ".cave" / "ops" / "restore.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"archive": "/var/backups/cave/x.tar.gz"}))


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (unfinished_update, "an update to v2026.11.15.1 was interrupted"),
        (unfinished_restore, "a restore of /var/backups/cave/x.tar.gz stopped half-way"),
    ],
)
def test_apply_refuses_while_an_update_or_restore_is_unfinished(
    run_cave,
    home,
    ctx,
    fake_runner,
    make,
    says,
):
    edit(home)
    applied = home.heating_applied.read_bytes()
    make(ctx)

    result = run_cave("apply")

    assert result.code == 1
    assert says in result.err
    assert "Nothing was changed." in result.err
    assert home.heating_applied.read_bytes() == applied
    assert fake_runner.calls == []


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (unfinished_update, "an update to v2026.11.15.1 was interrupted"),
        (unfinished_restore, "stopped half-way"),
    ],
)
def test_bootstrap_refuses_while_an_update_or_restore_is_unfinished(
    initialised,
    run_cave,
    ctx,
    fake_runner,
    make,
    says,
):
    make(ctx)

    result = run_cave("bootstrap")

    assert result.code == 1
    assert says in result.err
    assert fake_runner.calls == []


def test_bootstrap_holds_the_shared_lock(initialised, run_cave, ctx, fake_runner):
    with Lock(ctx.paths.lock_file, "update"):
        result = run_cave("bootstrap")

    assert result.code == 1
    assert "`cave update`" in result.err
    assert fake_runner.calls == []
