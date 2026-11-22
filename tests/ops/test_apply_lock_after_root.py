"""Reviewer's fourth-round test for PR #10 (goes in tests/ops/). Asserts the correct behaviour."""

from __future__ import annotations

from .test_apply import VALID, containers, edit, home  # noqa: F401  (home is a fixture)


# On a server the first command to take the lock is a root one: the nightly backup at 02:30, or
# `sudo ./cave restore` on a new server. It creates CAVE_DATA/.cave/lock owned by root, mode 0644.
# From then on `sudo -u cave ./cave apply` cannot open the lock for writing. Here the lock file is
# made unwritable for the test's own user, which is what root's 0644 file is for the service user.
def test_apply_works_after_a_root_command_created_the_lock(
    run_cave,
    home,  # noqa: F811
    fake_runner,
    fake_ha,
    write_token,
    ctx,
):
    edit(home)
    write_token()
    containers(fake_runner, home)
    lock = ctx.paths.lock_file
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("`cave backup` (pid 1, since 2026-11-22 02:30:00)\n")
    lock.chmod(0o444)  # root's 0644 file, seen by the service user

    result = run_cave("apply")

    assert result.code == 0, result.err
    assert home.heating_applied.read_text() == VALID
