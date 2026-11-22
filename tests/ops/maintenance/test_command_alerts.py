"""The alerts of ``update``, ``backup`` and ``restore`` through ``CAVE_ALERT_URL`` (WP11).

Every outcome whose message is a warning also goes out as an alert, in addition to the
notification of concept section 4.5; one that is not, does not. The alert carries fixed words
only, never the message (which can name rooms). A receiver that refuses or hangs changes neither
the exit status nor, beyond the alert's timeout, how long the command takes.
"""

from __future__ import annotations

import errno
import os
import threading
import time

import pytest

from cave_ops import alert
from cave_ops.commands import restore

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


@pytest.fixture
def receiver(ctx, alert_endpoint):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url
    return alert_endpoint


def headlines(receiver):
    return [title.split(": ", 1)[1] for title in receiver.titles()]


# ------------------------------------------------------------------------------------ update
def test_a_rollback_is_alerted_with_fixed_words_only(server, run_cave, receiver, notifications):
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    assert run_cave("update").code == 2

    assert headlines(receiver) == ["update rolled back"]
    body = receiver.received[0].body
    assert "Command: cave update" in body
    assert "Stack: running and healthy" in body
    assert "did not become healthy" in notifications()[-1]
    assert "did not become healthy" not in body, "the message itself went out"


def test_nothing_running_after_an_update_is_alerted(server, run_cave, receiver):
    server.release(NEW)
    server.stack.fail_up.update({NEW, OLD})

    assert run_cave("update").code == 3

    assert headlines(receiver) == ["update failed, act now"]
    assert "Stack: stopped: nothing runs" in receiver.received[0].body


@pytest.mark.parametrize(
    "setup",
    ["installed", "nothing new", "pull failed", "heating.yaml rejected", "lock busy"],
)
def test_an_update_that_ends_without_a_warning_is_not_alerted(
    server, run_cave, receiver, ctx, setup
):
    from cave_ops.lock import Lock

    if setup != "nothing new":
        server.release(NEW)
    if setup == "pull failed":
        server.stack.fail["pull"] = 1
    if setup == "heating.yaml rejected":
        server.stack.fail["validate"] = 1
    if setup == "lock busy":
        with Lock(ctx.paths.lock_file, "backup"):
            run_cave("update")
    else:
        run_cave("update")

    assert receiver.received == []


@pytest.mark.parametrize("mode", ["hang", "refuse", "error"])
def test_a_receiver_that_fails_changes_neither_the_exit_status_nor_the_outcome(
    server, run_cave, receiver, ctx, monkeypatch, mode
):
    monkeypatch.setattr(alert, "TIMEOUT", 0.5)
    if mode == "refuse":
        ctx.env["CAVE_ALERT_URL"] = receiver.refusing_url
    else:
        receiver.mode = mode
    server.release(NEW)
    server.stack.unhealthy.add(NEW)

    started = time.monotonic()
    result = run_cave("update")
    took = time.monotonic() - started

    assert result.code == 2
    assert server.stack.running == OLD
    assert "The alert to CAVE_ALERT_URL could not be sent" in result.err
    assert took < 10, "the update waited for the receiver"
    receiver.release.set()
    for thread in threading.enumerate():
        if thread.name == "cave-alert":
            thread.join(10)


# ------------------------------------------------------------------------------------ backup
def test_a_failed_backup_is_alerted(server, run_cave, receiver, backups, monkeypatch):  # noqa: F811
    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    from cave_ops.commands import backup

    monkeypatch.setattr(backup, "write_archive", disk_full)

    assert run_cave("backup").code == 1

    assert headlines(receiver) == ["backup failed"]
    assert "Stack: running and healthy" in receiver.received[0].body


def test_a_backup_whose_copy_failed_is_alerted(server, run_cave, receiver, backups, ctx, tmp_path):  # noqa: F811
    ctx.env["CAVE_BACKUP_COPY_TO"] = str(tmp_path / "not-mounted")

    assert run_cave("backup").code == 1

    assert headlines(receiver) == ["backup copy failed"]


def test_a_backup_that_worked_is_not_alerted(server, run_cave, receiver, backups):  # noqa: F811
    assert run_cave("backup").code == 0

    assert receiver.received == []


# ----------------------------------------------------------------------------------- restore
def test_a_restore_that_cannot_unpack_is_alerted(
    server,
    run_cave,
    receiver,
    backups,  # noqa: F811
    monkeypatch,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(restore._server, "extract", disk_full)

    assert run_cave("restore", str(archive)).code == 1

    assert headlines(receiver) == ["restore failed"]


def test_a_restore_that_stops_half_way_is_alerted(
    server,
    run_cave,
    receiver,
    backups,  # noqa: F811
    monkeypatch,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    renames = []
    real_rename = os.rename

    def disk_error(source, target):
        renames.append(source)
        if len(renames) == 6:
            raise OSError(errno.EIO, "Input/output error")
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", disk_error)

    assert run_cave("restore", str(archive)).code == 1

    assert headlines(receiver) == ["restore stopped half-way"]
    assert "Stack: stopped: nothing runs" in receiver.received[0].body


def test_a_restored_stack_that_does_not_start_is_alerted(
    server,
    run_cave,
    receiver,
    backups,  # noqa: F811
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    server.stack.fail_times["up"] = 1

    assert run_cave("restore", str(archive)).code == 1

    assert headlines(receiver) == ["restored stack not started"]


def test_a_restore_that_worked_is_not_alerted(
    server,
    run_cave,
    receiver,
    backups,  # noqa: F811
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)

    assert run_cave("restore", str(archive)).code == 0
    assert run_cave("restore", str(archive), "--no-start").code == 0

    assert receiver.received == []


def test_a_restore_says_that_the_alert_url_changed_without_printing_it(
    server,
    run_cave,
    receiver,
    backups,  # noqa: F811
    ctx,
    journal,
):
    assert run_cave("backup").code == 0  # its .env has no CAVE_ALERT_URL
    [archive] = archives(backups)
    url = f"{receiver.url}?auth=tk_notarealtokennotarealtoken"
    env = server.root / ".env"
    env.write_text(env.read_text() + f"CAVE_ALERT_URL='{url}'\n")
    ctx.env["CAVE_ALERT_URL"] = url
    server.stack.fail_times["up"] = 1

    result = run_cave("restore", str(archive))

    assert "CAVE_ALERT_URL is now the archive's (empty: no alerts); check it" in result.out + (
        result.err
    )
    assert headlines(receiver) == ["restored stack not started"], "this restore's own alert"
    printed = result.out + result.err + "\n".join(journal)
    assert "tk_notarealtoken" not in printed
