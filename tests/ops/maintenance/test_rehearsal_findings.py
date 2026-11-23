"""Defects the server rehearsal (tests/server/, work package 17) found on a real Debian machine,
each with the test that fails without its fix."""

from __future__ import annotations

import errno
import io
import os

import pytest

from cave_ops import alert, records
from cave_ops.commands import _server

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


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


@pytest.mark.parametrize("record", ["watch", "stopped"])
def test_a_record_root_writes_in_cave_belongs_to_the_service_user(ctx, monkeypatch, record):
    """A restore's alert, delivered while the stack was down, marked the watch's memory as root
    (``mark_alerted``), and a restore with ``--no-start`` writes ``stopped.json`` as root: the
    record then stayed root's in the service user's ``.cave``, and the owners in the state changed
    with each such run (hardware-acceptance 6.2). Root now gives it to the directory's owner."""
    cave = ctx.paths.lock_file.parent
    cave.mkdir(parents=True, exist_ok=True)
    owners: dict[str, int] = {}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "lchown", lambda p, u, g: owners.__setitem__(str(p), u))

    if record == "watch":
        alert.mark_alerted(ctx, "cave restore: restore stopped half-way")
        path = ctx.paths.watch_record
    else:
        records.note_stopped(ctx.paths, "a restore finished with --no-start")
        path = ctx.paths.stopped_record

    assert path.is_file()
    assert owners.get(str(path)) == cave.stat().st_uid


def test_an_undo_puts_back_the_env_as_the_checkouts_owner(
    server, run_cave, backups, ctx, monkeypatch
):
    """The consequence named with the finding above: a restore that has replaced .env and is
    then undone puts the copy back as .env. Run as root, that .env must be the checkout owner's,
    or every `sudo -u cave ./cave` fails afterwards."""
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    env = server.root / ".env"
    env.write_text(env.read_text() + "# the owner's own note\n")
    before = env.read_text()
    ctx.runner = _GitAsTheOwner(ctx.runner)
    owners: dict[str, int] = {}
    monkeypatch.setattr(_server, "is_root", lambda: True)
    monkeypatch.setattr(_server.os, "lchown", lambda p, u, g: owners.__setitem__(str(p), u))
    monkeypatch.setattr(_server.os, "chown", lambda p, u, g: owners.__setitem__(str(p), u))
    server.stack.fail["up"] = 1  # the restore replaces .env, then cannot start the stack

    assert run_cave("restore", str(archive)).code == 1
    assert env.read_text() != before
    server.stack.fail.clear()
    owners.clear()

    result = run_cave("restore", "--undo")

    assert result.code == 0, result.err
    assert env.read_text() == before
    assert owners.get(str(env)) == server.root.stat().st_uid


# --------------------------------------------- output whose reader went away (review of WP17)
class _ReaderGoes(io.RawIOBase):
    """A pipe whose reader takes ``lines`` lines and then exits (`| head`, a pager closed early):
    every later write fails with EPIPE, as Python sees it with SIGPIPE ignored."""

    def __init__(self, lines: int) -> None:
        self.lines = lines
        self.got = b""

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        if self.got.count(b"\n") >= self.lines:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        self.got += bytes(data)
        return len(data)


def _run_with_a_reader_that_goes(ctx, stream: str, lines: int, *argv: str) -> int:
    from cave_ops import cli

    raw = _ReaderGoes(lines)
    ctx.out, ctx.err = io.StringIO(), io.StringIO()
    setattr(ctx, stream, io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8"))
    try:
        return cli.main(list(argv), ctx=ctx)
    finally:
        raw.lines = 10**9  # so that the test's own clean-up of the stream does not fail


# Every line of each run's standard output (an update says at most 12, a rollback and a restore
# fewer), and the first lines of its warnings.
CUTS = [("out", lines) for lines in range(13)] + [("err", lines) for lines in range(6)]


@pytest.mark.parametrize(("stream", "lines"), CUTS)
def test_an_update_whose_reader_goes_away_still_installs(server, ctx, stream, lines):
    """With its output flushed line by line, a vanished reader made every later line raise
    BrokenPipeError: the update stopped half-way, left the stack stopped and recorded a healthy
    release as failed. Output is best effort now: the update finishes as if nobody had read."""
    server.release(NEW)

    code = _run_with_a_reader_that_goes(ctx, stream, lines, "update")

    assert code == 0
    assert server.stack.running == NEW
    record = server.record()
    assert record["switch"] is None
    assert record["failed"] == {}


@pytest.mark.parametrize(("stream", "lines"), CUTS)
def test_a_rollback_whose_reader_goes_away_still_finishes(server, ctx, stream, lines):
    """The way back above all must not be stopped by its own output."""
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    code = _run_with_a_reader_that_goes(ctx, stream, lines, "update")

    assert code == 2
    assert server.stack.running == OLD
    record = server.record()
    assert record["switch"] is None
    assert list(record["failed"]) == [NEW]


@pytest.mark.parametrize(("stream", "lines"), CUTS)
def test_a_restore_whose_reader_goes_away_still_finishes(
    server, run_cave, backups, ctx, stream, lines
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    code = _run_with_a_reader_that_goes(ctx, stream, lines, "restore", str(archive))

    assert code == 0
    assert server.stack.running == OLD
    assert not (server.data / ".cave/ops/restore.json").exists()


def test_a_vanished_reader_is_replaced_by_dev_null(tmp_path):
    """A real pipe whose reader exited: the first failed line points the descriptor at /dev/null,
    so the lines after it, the programs a command starts with this output, and Python's flush at
    exit no longer fail."""
    from cave_ops.context import write_line

    read_end, write_end = os.pipe()
    stream = os.fdopen(write_end, "w")
    os.close(read_end)  # the reader is gone
    try:
        write_line(stream, "first")  # EPIPE: Python ignores SIGPIPE
        assert os.path.samestat(os.fstat(stream.fileno()), os.stat(os.devnull))
        write_line(stream, "second")
        stream.flush()
    finally:
        stream.close()
