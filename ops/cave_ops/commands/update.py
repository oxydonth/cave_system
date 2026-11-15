"""``cave update``: install the newest release, and roll back if it is not healthy.

Concept section 9, in this order, holding the lock of section 4.5:

1. Fetch the tags and find a release newer than the installed one, skipping one that failed
   before unless ``--force``.
2. **Before anything is touched:** the running stack must be healthy; the checkout must have no
   local changes; there must be room for the snapshot. The new release is exported into
   ``CAVE_DATA/.cave/ops/tmp/release``, its images are pulled, and the Home Assistant
   configuration and the applied ``heating.yaml`` are validated by that release in its image.
   A failure here costs no downtime.
3. **The switch:** stop the stack, snapshot the state, check out the release, start it, wait for
   healthy.
4. **On failure:** stop, put back the snapshot and the previous release, start, wait for healthy.
   If even that is not healthy, the previous release is left running and the message says so.

Every step that changes something is announced in ``CAVE_DATA/.cave/ops/update.json`` first, so
that a run after a kill or a power cut (``cave update --recover``, which systemd runs at boot and
after the update service stops) finishes the job: an update interrupted before the snapshot was
complete starts the previous release again; one interrupted later is rolled back.

docs/operations.md explains every outcome to the owner. Exit status: 0 installed or nothing to
do; 1 not updated, the previous release runs as before; 2 the release failed and was rolled
back, the previous release runs healthy; 3 the previous release could not be brought back
healthy, or nothing runs: act now.
"""

from __future__ import annotations

import argparse
import dataclasses
import signal
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from ..container import run_in_homeassistant
from ..context import Context
from ..errors import CaveError
from ..ha import read_token
from ..health import Health, check_health, wait_healthy
from ..heating import validate
from ..lock import Lock, LockBusy
from ..notify import notify
from ..paths import CONTAINER_CONFIG
from ..runner import Result
from . import _releases, _server
from . import _update_state as state
from ._common import require_initialised

HELP = "Install the newest release: validate it first, then switch with a snapshot and roll back"

TITLE = "cave update"
UPDATE_PROJECT = "cave-update"
"""The Compose project of the one-off checks with the exported release; never the home's."""

OK, NOT_UPDATED, ROLLED_BACK, BROKEN = 0, 1, 2, 3

FETCH_TIMEOUT = 300.0
PULL_TIMEOUT = 3600.0
DOWN_TIMEOUT = 300.0
UP_TIMEOUT = 900.0
RECOVER_WAIT = 4 * 3600.0
"""How long ``--recover`` waits for the lock (a backup may hold it at boot)."""
DISK_MARGIN = 256 * 1024 * 1024
OPERATIONS = "docs/operations.md"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force", action="store_true", help="also install the newest release if it failed before"
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="started by the timer: do nothing when CAVE_AUTO_UPDATE=0 "
        "(an interrupted update is still finished)",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="only finish an interrupted update (systemd runs this at boot and after the update)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        metavar="SECONDS",
        help="how long a started release may take to become healthy (default 900)",
    )


def auto_updates_off(env: dict[str, str]) -> bool:
    return env.get("CAVE_AUTO_UPDATE", "1").strip().lower() in ("0", "false", "no", "off")


@contextmanager
def _terminate_as_exit() -> Iterator[None]:
    """Turn SIGTERM (``systemctl stop``, shutdown) into ``SystemExit``, so that a switch in
    progress is rolled back instead of leaving the stack stopped."""

    def handler(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    _server.require_state_access(ctx, "update")
    lock = ctx.lock("update")
    try:
        if args.recover:
            _wait_for_lock(ctx, lock)
        else:
            lock.acquire()
    except LockBusy as busy:
        message = f"Not updated: {busy}. Nothing was changed."
        ctx.warn(message)
        notify(ctx, TITLE, message)
        return NOT_UPDATED
    try:
        with _terminate_as_exit():
            return _Update(ctx, args).run()
    finally:
        lock.release()


def _wait_for_lock(ctx: Context, lock: Lock) -> None:
    """``--recover`` must not give up because a backup runs: the stack may be stopped."""
    deadline = ctx.monotonic() + RECOVER_WAIT
    while True:
        try:
            lock.acquire()
            return
        except LockBusy:
            if ctx.monotonic() >= deadline:
                raise
            ctx.sleep(10)


def _tail(result: Result, lines: int = 5) -> str:
    text = result.output.strip().splitlines()[-lines:]
    return " / ".join(line.strip() for line in text) or f"exit status {result.returncode}"


class _Update:
    def __init__(self, ctx: Context, args: argparse.Namespace) -> None:
        self.ctx = ctx
        self.args = args
        self.record = state.load(ctx)

    # ---------------------------------------------------------------------------- the run
    def run(self) -> int:
        ctx, args, record = self.ctx, self.args, self.record
        if record.problem:
            ctx.warn(record.problem)
        if record.switch is not None:
            return self.finish_interrupted()
        if args.recover:
            ctx.say("No interrupted update: nothing to recover.")
            return OK
        if args.auto and auto_updates_off(ctx.env):
            return self.no_op("Automatic updates are off (CAVE_AUTO_UPDATE=0); nothing was done.")
        self.clean_leftovers()

        fetched = _server.git(ctx, "fetch", "--tags", "origin", timeout=FETCH_TIMEOUT)
        if not fetched.ok:
            return self.not_updated(
                None,
                "fetch-failed",
                f"Could not fetch releases: {_tail(fetched)}. Nothing was changed; the next run "
                "tries again.",
            )
        try:
            newest = _releases.newest(self.git_lines("tag", "--list", _releases.TAG_PATTERN))
            installed = _releases.newest(
                self.git_lines("tag", "--list", _releases.TAG_PATTERN, "--merged", "HEAD")
            )
        except CaveError as error:
            return self.not_updated(None, "fetch-failed", f"Could not list the releases: {error}.")
        if newest is None or (installed is not None and newest <= installed):
            return self.no_op(
                f"Up to date: {installed or 'no release'} is installed, there is no newer release."
            )
        failed = record.failed.get(newest.tag)
        if failed and not args.force:
            return self.no_op(
                f"{newest} failed on {failed.get('time')} ({failed.get('reason')}) and is skipped "
                "until a newer release appears or `sudo ./cave update --force` is run."
            )
        return self.install(newest.tag)

    # ---------------------------------------------------------------- before anything is touched
    def install(self, tag: str) -> int:
        ctx = self.ctx
        try:
            to_commit = self.git_value("rev-parse", "--verify", f"{tag}^{{commit}}")
            from_commit = self.git_value("rev-parse", "--verify", "HEAD")
            from_label = self.git_value("describe", "--tags", "--always")
        except CaveError as error:
            return self.not_updated(tag, "refused", f"Not updating to {tag}: {error}")

        ctx.say(f"Checking the running stack before updating {from_label} to {tag} ...")
        health = check_health(ctx)
        if not health.healthy:
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: the running stack is {health.describe()}. An unhealthy "
                "stack is left alone, because a rollback could only return to it. Nothing was "
                f"changed. See {OPERATIONS}, 'The running stack is not healthy'.",
            )
        try:
            has_token = read_token(ctx.paths) is not None
        except CaveError as error:
            return self.not_updated(tag, "refused", f"Not updating to {tag}: {error}")
        if not has_token:
            message = (
                "there is no script token yet (`./cave bootstrap` creates it), so the health "
                "check cannot tell a Home Assistant in recovery mode from a working one, and a "
                "broken release could pass as healthy"
            )
            if self.args.auto:
                return self.not_updated(
                    tag,
                    "refused",
                    f"Not updating to {tag} automatically: {message}. Nothing was changed. Run "
                    "`sudo ./cave update` by hand to update anyway.",
                )
            ctx.warn(f"Warning: {message}.")

        dirty = _server.git(ctx, "status", "--porcelain", "--untracked-files=no")
        if not dirty.ok or dirty.stdout.strip():
            detail = dirty.stdout.strip() if dirty.ok else _tail(dirty)
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: the checkout {ctx.paths.root} has local changes "
                f"({detail}). Nothing was changed. See {OPERATIONS}, 'Local changes'.",
            )

        entries = _server.state_entries(ctx.paths.data)
        needed = int(sum(_server.tree_size(ctx.paths.data / e) for e in entries) * 1.1)
        needed += DISK_MARGIN
        free = _server.free_bytes(ctx.paths.data)
        if free < needed:
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: the snapshot needs about {needed // 2**20} MiB, but "
                f"only {free // 2**20} MiB are free on {ctx.paths.data}. Nothing was changed.",
            )

        work = _server.ops_dir(ctx) / "tmp"
        try:
            failure = self.check_release(tag, to_commit, work)
        finally:
            _server.remove(work)
        if failure is not None:
            return failure
        return self.switch(tag, to_commit, from_commit, from_label)

    def check_release(self, tag: str, commit: str, work: Path) -> int | None:
        """Export the release, pull its images and validate with it; ``None`` when all passed."""
        ctx = self.ctx
        ctx.say(f"Exporting {tag} ...")
        _server.remove(work)
        work.mkdir(parents=True, mode=0o700)
        _server.give_to_checkout_owner(ctx, work)
        archive_path = work / "release.tar"
        exported = _server.git(ctx, "archive", "--format=tar", "-o", str(archive_path), commit)
        release_dir = work / "release"
        try:
            if not exported.ok:
                raise CaveError(_tail(exported))
            release_dir.mkdir(mode=0o755)
            with tarfile.open(archive_path) as archive:
                _server.extract(archive, archive.getmembers(), release_dir)
            if not (release_dir / "compose.yaml").is_file():
                raise CaveError(f"{tag} has no compose.yaml")
        except (CaveError, OSError, tarfile.TarError) as error:
            return self.not_updated(
                tag, "check-failed", f"Not updating to {tag}: exporting it failed: {error}."
            )

        # Compose with the release's own files: COMPOSE_FILE names them, so neither the checkout's
        # compose.yaml nor its compose.override.yaml is read, and the bind mounts are resolved in
        # the export. The values of .env come through the environment, because Compose looks for
        # .env next to the Compose file. A project name of its own keeps the home's containers
        # out of reach.
        release_ctx = dataclasses.replace(
            ctx,
            compose_files=(),
            compose_env={
                **ctx.compose_env,
                **ctx.env,
                "COMPOSE_FILE": str(release_dir / "compose.yaml"),
                "COMPOSE_PROJECT_NAME": UPDATE_PROJECT,
            },
        )
        ctx.say(f"Pulling the images of {tag} ...")
        pulled = release_ctx.compose("pull", timeout=PULL_TIMEOUT)
        if not pulled.ok:
            return self.not_updated(
                tag,
                "pull-failed",
                f"Not updating to {tag}: pulling its images failed: {_tail(pulled)}. Nothing was "
                "changed; the next run tries again.",
            )

        ctx.say(f"Checking the Home Assistant configuration with {tag} ...")
        checked = run_in_homeassistant(
            release_ctx,
            ["-m", "homeassistant", "--script", "check_config", "--config", CONTAINER_CONFIG],
        )
        if not checked.ok:
            return self.not_updated(
                tag,
                "check-failed",
                f"Not updating to {tag}: its Home Assistant rejects the configuration: "
                f"{_tail(checked)}. Nothing was changed. {tag} is skipped until a newer release "
                "appears or `sudo ./cave update --force` is run.",
                remember=True,
            )

        ctx.say(f"Validating the applied heating.yaml with {tag} ...")
        try:
            valid, messages = validate(release_ctx, ctx.paths.heating_applied)
        except OSError as error:
            valid, messages = False, (f"cannot read {ctx.paths.heating_applied}: {error}",)
        if not valid:
            return self.not_updated(
                tag,
                "check-failed",
                f"Not updating to {tag}: it does not accept the applied heating.yaml: "
                f"{' / '.join(messages) or 'no message'}. Nothing was changed. Adapt "
                "cave/heating.yaml, run `./cave apply`, then `sudo ./cave update --force`.",
                remember=True,
            )
        return None

    # ------------------------------------------------------------------------------ the switch
    def switch(self, tag: str, to_commit: str, from_commit: str, from_label: str) -> int:
        ctx, record = self.ctx, self.record
        switch = state.Switch(state.BEFORE_SWITCH, from_commit, from_label, tag, to_commit)
        record.switch = switch
        state.save(ctx, record)
        try:
            reason = self.do_switch(switch)
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            # An unexpected error, Ctrl-C or SIGTERM: still never leave the stack stopped.
            reason = f"the update was interrupted ({type(error).__name__}: {error})"
        if reason is not None:
            if switch.phase == state.BEFORE_SWITCH:
                return self.restart_previous(reason)
            return self.roll_back(reason)

        record.failed.pop(tag, None)
        state.prune_snapshots(ctx)
        return self.conclude(
            tag,
            "installed",
            OK,
            f"Installed {tag} (was {from_label}); the stack is healthy. The snapshot of the state "
            f"from before the update is kept as {switch.snapshot}.",
        )

    def do_switch(self, switch: state.Switch) -> str | None:
        """Stop, snapshot, check out, start, wait. ``None`` on success, else what went wrong;
        ``switch.phase`` then says whether the snapshot is complete."""
        ctx, tag = self.ctx, switch.to_release
        ctx.say(f"Stopping the stack to switch from {switch.from_label} to {tag} ...")
        down = ctx.compose("down", timeout=DOWN_TIMEOUT)
        if not down.ok:
            return f"the stack could not be stopped: {_tail(down)}"
        ctx.say("Taking a snapshot of the state ...")
        try:
            name = state.take_snapshot(ctx, f"{switch.from_label}-to-{tag}")
        except OSError as error:
            return f"the snapshot of the state failed: {error}"
        switch.snapshot = name
        switch.phase = state.SWITCHING
        state.save(ctx, self.record)

        ctx.say(f"Checking out {tag} ...")
        checked_out = _server.git(ctx, "checkout", "--detach", switch.to_commit)
        if not checked_out.ok:
            return f"{tag} could not be checked out: {_tail(checked_out)}"
        ctx.say(f"Starting {tag} ...")
        started = ctx.compose("up", "--detach", "--force-recreate", timeout=UP_TIMEOUT)
        if not started.ok:
            return f"{tag} could not be started: {_tail(started)}"
        ctx.say("Waiting until healthy ...")
        health = wait_healthy(ctx, timeout=self.args.timeout)
        if not health.healthy:
            return f"{tag} did not become healthy within {self.args.timeout:.0f} s: " + "; ".join(
                health.problems
            )
        return None

    # ------------------------------------------------------------------------- the way back
    def restart_previous(self, reason: str) -> int:
        """Nothing changed yet (the snapshot is incomplete or not taken): start the previous
        release again."""
        ctx, switch = self.ctx, self._switch()
        ctx.warn(f"Update to {switch.to_release} stopped: {reason}. Starting {switch.from_label}.")
        state.remove_partial_snapshots(ctx)
        problems = self.check_out_previous()
        started, health = self.start()
        head = f"Update to {switch.to_release} stopped before the switch: {reason}."
        if started and health.healthy and not problems:
            return self.conclude(
                switch.to_release,
                "not-updated",
                NOT_UPDATED,
                f"{head} Nothing was changed: the previous release {switch.from_label} is running "
                "again and healthy. The next run tries again.",
            )
        return self.broken(head, started, health, problems)

    def roll_back(self, reason: str) -> int:
        """Stop, put back the snapshot and the previous release, start it and wait."""
        ctx, record, switch = self.ctx, self.record, self._switch()
        ctx.warn(f"Update to {switch.to_release} failed: {reason}. Rolling back.")
        switch.phase = state.ROLLING_BACK
        record.failed[switch.to_release] = {"time": state.now(), "reason": reason}
        state.save(ctx, record)

        problems: list[str] = []
        down = ctx.compose("down", timeout=DOWN_TIMEOUT)
        if not down.ok:
            down = ctx.compose("down", timeout=DOWN_TIMEOUT)
        if not down.ok:
            problems.append(
                f"the containers could not be stopped ({_tail(down)}), so the snapshot "
                f"{switch.snapshot} was NOT put back: something might still be writing the state"
            )
        elif switch.snapshot is None:
            problems.append("there is no snapshot to put back")
        else:
            ctx.say(f"Putting back the snapshot {switch.snapshot} ...")
            try:
                state.restore_snapshot(ctx, switch.snapshot)
            except (OSError, ValueError, KeyError) as error:
                problems.append(
                    f"the snapshot {switch.snapshot} could not be put back ({error}); the state "
                    f"is what {switch.to_release} left behind"
                )
        problems += self.check_out_previous()
        started, health = self.start()
        head = f"Update to {switch.to_release} failed: {reason}."
        if started and health.healthy and not problems:
            return self.conclude(
                switch.to_release,
                "rolled-back",
                ROLLED_BACK,
                f"{head} Rolled back: the previous release {switch.from_label} with the state "
                f"from before the update (snapshot {switch.snapshot}) is running and healthy. "
                f"{switch.to_release} is skipped until a newer release appears or "
                "`sudo ./cave update --force` is run.",
            )
        return self.broken(head, started, health, problems)

    def finish_interrupted(self) -> int:
        switch = self._switch()
        self.ctx.warn(
            f"An update from {switch.from_label} to {switch.to_release}, started "
            f"{switch.started}, was interrupted ({switch.phase}). Finishing it first."
        )
        if switch.phase == state.BEFORE_SWITCH:
            return self.restart_previous(
                "it was interrupted (killed or a power cut) before the snapshot was complete"
            )
        return self.roll_back("it was interrupted (killed or a power cut) during the switch")

    def check_out_previous(self) -> list[str]:
        # Always, and by force: a checkout that failed half-way leaves HEAD where it was but
        # files of the new release in the working tree.
        switch = self._switch()
        self.ctx.say(f"Checking out {switch.from_label} ...")
        done = _server.git(self.ctx, "checkout", "--force", "--detach", switch.from_commit)
        if done.ok:
            return []
        return [
            f"the previous release {switch.from_label} could not be checked out "
            f"({_tail(done)}); whatever is checked out was started"
        ]

    def start(self) -> tuple[bool, Health]:
        """Start the checkout (one retry) and wait until healthy."""
        ctx = self.ctx
        ctx.say("Starting the stack ...")
        result = ctx.compose("up", "--detach", "--force-recreate", timeout=UP_TIMEOUT)
        if not result.ok:
            ctx.sleep(10)
            result = ctx.compose("up", "--detach", "--force-recreate", timeout=UP_TIMEOUT)
        if not result.ok:
            return False, Health(False, (f"`docker compose up` failed: {_tail(result)}",))
        ctx.say("Waiting until healthy ...")
        return True, wait_healthy(ctx, timeout=self.args.timeout)

    def broken(self, head: str, started: bool, health: Health, problems: list[str]) -> int:
        """The loud ending: the previous release is not back healthy, or nothing runs."""
        switch = self._switch()
        listed = "; ".join(problems)
        also = f" Also: {listed}." if problems else ""
        if not started:
            message = (
                f"{head} NOTHING IS RUNNING: the previous release {switch.from_label} could not be "
                f"started ({'; '.join(health.problems)}).{also} Home Assistant does not control "
                "the heating; the thermostats keep their last setpoint. Act now: see "
                f"{OPERATIONS}, 'Nothing is running'."
            )
        elif not health.healthy:
            message = (
                f"{head} The previous release {switch.from_label} was started and left running, "
                f"but it is NOT healthy: {'; '.join(health.problems)}.{also} See {OPERATIONS}, "
                "'The rollback is not healthy'."
            )
        else:
            message = (
                f"{head} The previous release {switch.from_label} is running and healthy, but the "
                f"way back was not clean: {listed}. See {OPERATIONS}, 'The way back was not clean'."
            )
        return self.conclude(switch.to_release, "broken", BROKEN, message)

    # ---------------------------------------------------------------------------- reporting
    def _switch(self) -> state.Switch:
        if self.record.switch is None:
            raise CaveError("internal error: no update in flight")
        return self.record.switch

    def conclude(self, tag: str | None, outcome: str, status: int, message: str) -> int:
        record = self.record
        record.switch = None
        record.last_result = {
            "time": state.now(),
            "outcome": outcome,
            "release": tag,
            "message": message,
            "status": status,
        }
        record.last_check = {"time": state.now(), "message": message}
        state.save(self.ctx, record)
        (self.ctx.say if status == OK else self.ctx.warn)(message)
        notify(self.ctx, TITLE, message)
        return status

    def not_updated(
        self, tag: str | None, outcome: str, message: str, remember: bool = False
    ) -> int:
        if remember and tag is not None:
            self.record.failed[tag] = {"time": state.now(), "reason": message}
        return self.conclude(tag, outcome, NOT_UPDATED, message)

    def no_op(self, message: str) -> int:
        """Nothing to do: the journal and the record, but no notification (docs/operations.md)."""
        self.record.last_check = {"time": state.now(), "message": message}
        state.save(self.ctx, self.record)
        self.ctx.say(message)
        self.ctx.journal(f"{TITLE}: {message}")
        return OK

    # ---------------------------------------------------------------------------------- helpers
    def clean_leftovers(self) -> None:
        state.remove_partial_snapshots(self.ctx)
        for name in ("tmp", "restoring", "discard"):
            _server.remove(_server.ops_dir(self.ctx) / name)

    def git_lines(self, *args: str) -> list[str]:
        result = _server.git(self.ctx, *args)
        if not result.ok:
            raise CaveError(f"`git {' '.join(args)}` failed: {_tail(result)}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def git_value(self, *args: str) -> str:
        lines = self.git_lines(*args)
        if not lines:
            raise CaveError(f"`git {' '.join(args)}` printed nothing")
        return lines[0]
