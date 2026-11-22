"""The reviewer's tests of the fifth review of pull request #10: a restore series that
superseded an interrupted update, the space message during a series, kills in `--undo` and in
a repeat, and a marker of an earlier version."""

from __future__ import annotations

import os

import pytest

from cave_ops.commands import restore

from .test_backup import archives, backups  # noqa: F401  (backups is a fixture)

OLD = "v2026.11.14.1"
NEW = "v2026.11.15.1"


def _kill_at_rename(monkeypatch, killed, n):
    renames = []
    real_rename = os.rename

    def power_cut(source, target):
        renames.append(source)
        if len(renames) == n:
            raise killed
        real_rename(source, target)

    monkeypatch.setattr(restore.os, "rename", power_cut)


def _clean(tree):
    return {k: v for k, v in tree.items() if not k.startswith("homeassistant/started-")}


# 1. An update is interrupted after the new release migrated the state; the owner restores an
#    archive, which supersedes the update (checks out the previous release, forgets the update);
#    the restore stops half-way; the owner runs `restore --undo`. The state "from before" that
#    undo puts back is the new release's migrated state -- under the previous release's
#    checkout, with the update forgotten, so no recovery will ever put its snapshot back.
def test_undo_never_leaves_the_previous_release_on_the_new_releases_state(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    server.release(NEW)

    def die(release):
        if release == NEW:
            raise killed

    server.stack.after["up"] = die
    with pytest.raises(killed):
        run_cave("update")
    server.stack.after.clear()
    assert server.schema() == NEW

    _kill_at_rename(monkeypatch, killed, 6)  # four entries aside, one moved in, then the cut
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()

    run_cave("restore", "--undo")
    run_cave("update", "--recover")  # what the next boot does

    assert not (server.checked_out() == OLD and server.schema() == NEW), (
        "the previous release runs on the state the new release migrated, and nothing is left "
        f"to roll it back: record switch = {server.record().get('switch')}"
    )


# 2. In the middle of a failed restore, the space message tells the owner to delete "older
#    .cave/ops/before-restore-* directories". The one before-restore-* directory of this series
#    holds the only copy of the state from before (what --undo needs); the message must not
#    lead the owner to delete it.
def test_the_space_message_does_not_offer_the_state_from_before_for_deletion(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    _kill_at_rename(monkeypatch, killed, 2)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    [aside] = (server.data / ".cave" / "ops").glob("before-restore-*")

    monkeypatch.setattr(restore._server, "free_bytes", lambda path: 0)
    result = run_cave("restore", str(archive))

    assert result.code != 0
    offers = "before-restore-*" in result.err
    assert not offers or aside.name in result.err, result.err


# 3. A kill at every rename of `--undo`; running `--undo` again brings back the state from before.
@pytest.mark.parametrize("cut", [1, 2, 3, 4])
def test_undo_killed_anywhere_is_finished_by_running_it_again(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    state_tree,
    cut,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    (server.data / "homeassistant/automations.yaml").write_text("- id: after the backup\n")
    before = _clean(state_tree(server.data))
    env_before = (server.root / ".env").read_text()
    _kill_at_rename(monkeypatch, killed, 6)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()

    _kill_at_rename(monkeypatch, killed, cut)
    with pytest.raises(killed):
        run_cave("restore", "--undo", "--no-start")
    monkeypatch.undo()
    result = run_cave("restore", "--undo")

    assert result.code == 0, result.err
    assert _clean(state_tree(server.data)) == before
    assert (server.root / ".env").read_text() == env_before


# 4. A repeat killed at each rename, then --undo: the state from before.
@pytest.mark.parametrize("cut", [1, 2, 3, 4])
def test_a_repeat_killed_anywhere_can_still_be_undone(
    server,
    run_cave,
    backups,  # noqa: F811
    monkeypatch,
    killed,
    state_tree,
    cut,
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    (server.data / "homeassistant/automations.yaml").write_text("- id: after the backup\n")
    before = _clean(state_tree(server.data))
    _kill_at_rename(monkeypatch, killed, 6)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()
    _kill_at_rename(monkeypatch, killed, cut)
    with pytest.raises(killed):
        run_cave("restore", str(archive))
    monkeypatch.undo()

    result = run_cave("restore", "--undo")

    assert result.code == 0, result.err
    assert _clean(state_tree(server.data)) == before


# 5. (Optional) A marker in the earlier format ({"archive": ...}), or an unreadable one: a repeat
#    should refuse before stopping anything, as --undo does, not crash after `down`.
def test_a_repeat_with_an_old_format_marker_refuses_before_stopping(
    server,
    run_cave,
    backups,  # noqa: F811
):
    assert run_cave("backup").code == 0
    [archive] = archives(backups)
    marker = server.data / ".cave" / "ops" / "restore.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"archive": "/somewhere/old.tar.gz"}\n')

    try:
        code = run_cave("restore", str(archive)).code
    except TypeError as error:
        code = f"crashed: {error}"
    assert server.stack.names() == [], f"exit {code}; stack operations: {server.stack.names()}"
