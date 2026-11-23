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
import os
import re
import signal
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from .. import alert
from ..container import run_in_homeassistant
from ..context import Context
from ..errors import CaveError
from ..ha import read_token
from ..health import Health, check_health, wait_healthy
from ..heating import validation_result
from ..lock import Lock, LockBusy
from ..notify import notify
from ..paths import CONTAINER_CONFIG
from ..records import clear_stopped, stopped
from ..runner import Result
from . import _images, _releases, _server
from . import _update_state as state
from ._common import require_initialised

HELP = "Install the newest release: validate it first, then switch with a snapshot and roll back"

TITLE = "cave update"
UPDATE_PROJECT = "cave-update"
"""The Compose project of the one-off checks with the exported release; never the home's."""

OK, NOT_UPDATED, ROLLED_BACK, BROKEN = 0, 1, 2, 3

WARNINGS = {
    "rolled-back": (
        "update rolled back",
        "A new release failed and was rolled back: the previous release runs again, with the "
        "state from before the update.",
    ),
    "broken": (
        "update failed, act now",
        "A new release failed, and the way back did not end with the previous release running "
        "and healthy, or it was not clean.",
    ),
    "start-finished": (
        "restored stack not healthy",
        "A restore had been cut off before it started the stack. The stack was started now, and "
        "it is not healthy.",
    ),
    "start-owed": (
        "restored stack not started",
        "A restore had been cut off before it started the stack, and it could not be started now.",
    ),
}
"""What the alert says (``alert.warn_owner``) for each outcome that ends with exit status 2 or 3:
fixed words, never the message itself."""

FETCH_TIMEOUT = 300.0
PULL_TIMEOUT = 3600.0
DOWN_TIMEOUT = 300.0
UP_TIMEOUT = 900.0
RECOVER_WAIT = 4 * 3600.0
"""How long ``--recover`` waits for the lock (a backup may hold it at boot)."""
DISK_MARGIN = 256 * 1024 * 1024
OPERATIONS = "docs/operations.md"
MAIN = "refs/remotes/origin/main"
DOCKER_TIMEOUT = 120.0
COULD_NOT_RUN = {
    124: "it timed out",
    125: "Docker could not run the container",
    126: "the command could not be executed",
    127: "the command was not found",
}


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


# The validator's own words for a file it could not open (cave_heating/schema.py, read_file):
# "The file <path> does not exist." and "The file <path> cannot be read: [Errno ...]". A file
# that opens but is not UTF-8 is also "cannot be read", but without "[Errno": that is a problem
# of the file itself, a rejection.
_FILE_NOT_THERE = re.compile(
    r"^the file \S+ (does not exist\.?$|cannot be read: \[Errno )", re.MULTILINE | re.IGNORECASE
)


def could_not_run(result: Result, what: str) -> str | None:
    """Why a check in the new image did not run at all, or ``None`` when it ran and rejected.

    The validator and Home Assistant's ``check_config`` exit with 1 for a file they reject and
    write no traceback. Anything else (124 a timeout, 125-127 Docker or the command, 2 the
    validator called wrongly, a traceback, a missing module) says nothing about the file.
    """
    output = result.output
    if "No module named" in output:
        return f"the {what} is not in this release"
    if result.returncode == 1 and _FILE_NOT_THERE.search(output):
        # The validator exits 1 also when it cannot find or read the file it was given: then it
        # judged nothing, and the owner must not be told to edit the file.
        return f"the {what} could not read the file it was given"
    if result.returncode == 1 and "Traceback" not in output:
        return None
    return COULD_NOT_RUN.get(result.returncode, f"exit status {result.returncode}")


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


@contextmanager
def _shielded() -> Iterator[None]:
    """Ignore Ctrl-C and SIGTERM while the way back runs: interrupting it would leave the stack
    stopped. systemd's TimeoutStopSec still ends it by SIGKILL, and ExecStopPost then repeats it."""
    try:
        previous = {
            number: signal.signal(number, signal.SIG_IGN)
            for number in (signal.SIGINT, signal.SIGTERM)
        }
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


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
            status = _Update(ctx, args).run()
        if not args.recover and not (args.auto and auto_updates_off(ctx.env)):
            _remove_old_images(ctx)
        return status
    finally:
        lock.release()


def _remove_old_images(ctx: Context) -> None:
    """Still holding the lock: remove the images no release needs any more (``_images``). What
    happens here never changes the exit status."""
    try:
        pruned = _images.prune(ctx)
    except Exception as error:  # whatever goes wrong here, the update's outcome stands
        ctx.warn(f"Old images were not removed: {type(error).__name__}: {error}.")
        return
    if pruned.skipped:
        ctx.say(f"Old images were not removed: {pruned.skipped}.")
        return
    if pruned.removed:
        message = (
            f"{TITLE}: removed {len(pruned.removed)} image(s) that no release here needs any "
            f"more: {', '.join(pruned.removed)}."
        )
        ctx.say(message)
        ctx.journal(message)
    if pruned.refused:
        ctx.say(f"Docker kept these images (a container uses them): {', '.join(pruned.refused)}.")


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
        self.notes: list[str] = []
        self.cleanup: Callable[[], None] | None = None

    # ---------------------------------------------------------------------------- the run
    def run(self) -> int:
        ctx, args, record = self.ctx, self.args, self.record
        if record.problem:
            ctx.warn(record.problem)
        if state.complete_undo(ctx):
            # A `restore --undo` was cut off after it had put the state from before back; the
            # update it had superseded (if any) is in flight again and is finished below.
            self.record = record = state.load(ctx)
        owed = _server.restore_start_owed(ctx)
        if owed is not None:
            return self.finish_owed_start(owed)
        if stopped(ctx.paths) is not None and check_health(ctx).healthy:
            clear_stopped(ctx.paths, ctx.wall())  # started since it was stopped on purpose
        restoring = _server.restore_in_progress(ctx)
        if restoring is not None:
            # A refusal changes nothing: an update in flight stays recorded (keep_switch).
            return self.conclude(
                None,
                "refused",
                NOT_UPDATED,
                f"Not updating: a restore of {restoring} was interrupted, and the state may be "
                "half restored. " + _server.RESTORE_WAY_OUT + " Nothing was changed.",
                keep_switch=True,
            )
        if record.switch is not None:
            return self.finish_interrupted()
        if args.recover:
            ctx.say("No interrupted update: nothing to recover.")
            return OK
        if args.auto and auto_updates_off(ctx.env):
            return self.no_op("Automatic updates are off (CAVE_AUTO_UPDATE=0); nothing was done.")
        self.clean_leftovers()

        # --force and --prune-tags: the server's tags follow GitHub's, so a tag that was moved
        # or deleted there (a withdrawn release) neither blocks the fetch nor stays installable.
        fetched = _server.git(
            ctx,
            "fetch",
            "--prune",
            "--prune-tags",
            "--force",
            "--tags",
            "origin",
            timeout=FETCH_TIMEOUT,
        )
        if not fetched.ok:
            return self.not_updated(
                None,
                "fetch-failed",
                f"Could not fetch releases: {_tail(fetched)}. Nothing was changed; the next run "
                "tries again.",
            )
        try:
            installed = _releases.newest(
                self.git_lines("tag", "--list", _releases.TAG_PATTERN, "--merged", "HEAD")
            )
            newer = self.newer_on_main(installed)
        except CaveError as error:
            return self.not_updated(None, "fetch-failed", f"Could not list the releases: {error}.")
        if not newer:
            return self.no_op(
                f"Up to date: {installed or 'no release'} is installed, there is no newer release."
            )
        newest = newer[0]
        # The newest release that has not failed before: one that failed is skipped, but a release
        # between it and the installed one is still installed. --force takes the newest.
        chosen = (
            newest
            if args.force
            else next((version for version in newer if version.tag not in record.failed), None)
        )
        skipped = [version for version in newer if chosen is None or version > chosen]
        said = "; ".join(
            f"{version} failed on {record.failed[version.tag].get('time')} "
            f"({record.failed[version.tag].get('reason')})"
            for version in skipped
        )
        if chosen is None:
            return self.no_op(
                f"{said} and {'is' if len(skipped) == 1 else 'are'} skipped until a newer release "
                "appears or `sudo ./cave update --force` is run."
            )
        if skipped:
            ctx.say(f"{said}: skipped. {chosen} is the newest release that has not failed.")
            self.notes.append(
                f"{', '.join(str(version) for version in skipped)} failed before and "
                f"{'was' if len(skipped) == 1 else 'were'} skipped; {chosen} is the newest release "
                "that has not failed (`sudo ./cave update --force` tries the newest)"
            )
        return self.install(chosen.tag)

    def newer_on_main(self, installed: _releases.Version | None) -> list[_releases.Version]:
        """The release tags newer than ``installed`` whose commit is on ``origin/main``, newest
        first.

        Only the pipeline creates release tags, and only for commits on ``main``; a tag that
        matches the pattern on any other commit (pushed by hand, on a branch) is ignored.
        """
        candidates = _releases.releases(self.git_lines("tag", "--list", _releases.TAG_PATTERN))
        found: list[_releases.Version] = []
        for version in reversed(candidates):
            if installed is not None and version <= installed:
                break
            on_main = _server.git(
                self.ctx, "merge-base", "--is-ancestor", f"{version.tag}^{{commit}}", MAIN
            )
            if on_main.returncode == 0:
                found.append(version)
            elif on_main.returncode == 1:
                self.ctx.say(f"Ignoring {version}: its commit is not on main.")
            else:
                raise CaveError(f"cannot tell whether {version} is on main: {_tail(on_main)}")
        return found

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

        elsewhere = _server.entries_elsewhere(ctx)
        if elsewhere:
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: {', '.join(elsewhere)} in {ctx.paths.data} is on another "
                "file system than the rest of the state, so the snapshot could not be put back by "
                f"renaming. Nothing was changed. See {OPERATIONS}, 'One file system'.",
            )

        dirty = _server.git(ctx, "status", "--porcelain", "--untracked-files=no")
        if not dirty.ok or dirty.stdout.strip():
            detail = dirty.stdout.strip() if dirty.ok else _tail(dirty)
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: the checkout {ctx.paths.root} has local changes "
                f"({detail}). Nothing was changed. See {OPERATIONS}, 'Local changes'.",
            )

        work = _server.ops_dir(ctx) / "tmp"
        try:
            failure = self.check_release(tag, to_commit, work)
        finally:
            _server.remove(work)
        if failure is not None:
            return failure
        # Last, right before the stack is stopped: `cave device-check` takes no lock, and stopping
        # Home Assistant under a running check would spoil it.
        held = self.held_thermostats()
        if held:
            return self.not_updated(
                tag,
                "postponed",
                f"Not updating to {tag} now: `cave device-check` holds {', '.join(held)}, and "
                "the switch would stop Home Assistant under it. Nothing was changed; the next run "
                "tries again.",
            )
        return self.switch(tag, to_commit, from_commit, from_label)

    def held_thermostats(self) -> list[str]:
        """The thermostats that ``cave_heating`` shows as held: only ``cave device-check`` holds
        one (concept section 5.5). Empty when Home Assistant cannot be asked (it was healthy a
        moment ago, with the same token)."""
        try:
            client = self.ctx.ha()
            listing = client.states() if client is not None else []
        except CaveError as error:
            self.ctx.warn(f"Could not ask Home Assistant whether a thermostat is held: {error}.")
            return []
        held: list[str] = []
        for item in listing:
            entity = str(item.get("entity_id", ""))
            if not (entity.startswith("binary_sensor.cave_") and entity.endswith("_problem")):
                continue
            statuses = (item.get("attributes") or {}).get("thermostats") or {}
            if isinstance(statuses, dict):
                held += sorted(str(name) for name, status in statuses.items() if status == "held")
        return held

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

        # Compose with the release's own files: COMPOSE_FILE names them (and the local
        # compose.override.yaml, which the real start will use too), so the checkout's
        # compose.yaml is not read and the bind mounts are resolved in the export. The values of
        # .env come through the environment, because Compose looks for .env next to the Compose
        # file. A project name of its own keeps the home's containers out of reach.
        files = [str(release_dir / "compose.yaml")]
        override = ctx.paths.root / "compose.override.yaml"
        if override.is_file():
            files.append(str(override))
        release_ctx = dataclasses.replace(
            ctx,
            compose_files=(),
            compose_env={
                **ctx.compose_env,
                **ctx.env,
                "COMPOSE_FILE": os.pathsep.join(files),
                "COMPOSE_PROJECT_NAME": UPDATE_PROJECT,
            },
        )
        configured = release_ctx.compose("config", "--quiet", timeout=DOCKER_TIMEOUT)
        if not configured.ok:
            return self.not_updated(
                tag,
                "check-failed",
                f"Not updating to {tag}: its Compose file does not accept this server's .env and "
                f"compose.override.yaml: {_tail(configured)}. Nothing was changed. Run "
                "`sudo -u cave ./cave init` (it adds variables a release needs), check "
                "compose.override.yaml, then `sudo ./cave update`.",
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

        # Measured after the pull, which usually fills the same disk. The snapshot needs a copy
        # of the state; putting it back is done by renaming and needs nothing more.
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

        ctx.say(f"Checking the Home Assistant configuration with {tag} ...")
        checked = run_in_homeassistant(
            release_ctx,
            ["-m", "homeassistant", "--script", "check_config", "--config", CONTAINER_CONFIG],
        )
        if not checked.ok:
            return self.check_failed(
                tag, checked, "its Home Assistant rejects the configuration", "check_config"
            )

        ctx.say(f"Validating the applied heating.yaml with {tag} ...")
        if not ctx.paths.heating_applied.is_file():
            return self.not_updated(
                tag,
                "refused",
                f"Not updating to {tag}: {ctx.paths.heating_applied} does not exist. Run "
                "`sudo -u cave ./cave apply`. Nothing was changed.",
            )
        validated, messages = validation_result(release_ctx, ctx.paths.heating_applied)
        if not validated.ok:
            return self.check_failed(
                tag,
                validated,
                "it does not accept the applied heating.yaml",
                "validator",
                messages,
            )
        return None

    def check_failed(
        self,
        tag: str,
        result: Result,
        rejected: str,
        what: str,
        messages: tuple[str, ...] | None = None,
    ) -> int:
        """A check in the new image failed: a rejection (exit 1 from a check that ran) is
        remembered; a check that could not run at all (Docker, a timeout, a missing module) is
        not, and the next night tries again."""
        output = result.output
        reason = could_not_run(result, what)
        if reason is not None:
            return self.not_updated(
                tag,
                "check-could-not-run",
                f"Not updating to {tag}: the {what} could not run ({reason}: {_tail(result)}). "
                "This says nothing about your configuration. Nothing was changed; the next run "
                "tries again.",
            )
        lines = list(messages) if messages else [line.strip() for line in output.splitlines()]
        lines = [line.strip() for line in lines if line.strip()]
        detail = " / ".join(lines[-10:]) or "no message"
        if what == "validator":
            advice = (
                "Adapt cave/heating.yaml as the messages say, run `sudo -u cave ./cave apply`, "
                "then `sudo ./cave update --force`."
            )
        else:
            advice = f"{tag} is skipped until a newer release appears or `--force` is given."
        return self.not_updated(
            tag,
            "check-failed",
            f"Not updating to {tag}: {rejected}: {detail}. Nothing was changed. {advice}",
            remember=True,
        )

    # ------------------------------------------------------------------------------ the switch
    def switch(self, tag: str, to_commit: str, from_commit: str, from_label: str) -> int:
        ctx, record = self.ctx, self.record
        switch = state.Switch(state.BEFORE_SWITCH, from_commit, from_label, tag, to_commit)
        record.switch = switch
        record.last_switch = {"release": from_label, "commit": from_commit}
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
        record.previous = {"release": from_label, "commit": from_commit}
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
        self.remove_stale_git_locks()
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
        with _shielded():
            return self._restart_previous(reason)

    def _restart_previous(self, reason: str) -> int:
        ctx, switch = self.ctx, self._switch()
        ctx.warn(f"Update to {switch.to_release} stopped: {reason}. Starting {switch.from_label}.")
        try:
            state.remove_partial_snapshots(ctx)
        except OSError as error:
            ctx.warn(f"The partial snapshot could not be removed: {error}")
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
        return self.broken(head, started, health, problems, retry=not started)

    def roll_back(self, reason: str) -> int:
        """Stop, put back the snapshot and the previous release, start it and wait."""
        with _shielded():
            return self._roll_back(reason)

    def _roll_back(self, reason: str) -> int:
        ctx, record, switch = self.ctx, self.record, self._switch()
        ctx.warn(f"Update to {switch.to_release} failed: {reason}. Rolling back.")
        switch.phase = state.ROLLING_BACK
        if switch.to_release not in record.failed:
            record.failed[switch.to_release] = {"time": state.now(), "reason": reason}
        self.save_quietly()

        problems: list[str] = []
        down = ctx.compose("down", timeout=DOWN_TIMEOUT)
        if not down.ok:
            ctx.sleep(10)  # Docker may still be starting (after a power cut)
            down = ctx.compose("down", timeout=DOWN_TIMEOUT)
        if not down.ok:
            problems.append(
                f"the containers could not be stopped ({_tail(down)}), so the snapshot "
                f"{switch.snapshot} was NOT put back: something might still be writing the state"
            )
        elif switch.snapshot is None:
            problems.append("there is no snapshot to put back")
        elif state.snapshot_put_back(ctx, switch.snapshot):
            # An earlier run put it back already (the disk is the record of that).
            switch.restored = True
        else:
            ctx.say(f"Putting back the snapshot {switch.snapshot} ...")
            try:
                state.restore_snapshot(ctx, switch.snapshot)
            except (OSError, ValueError, KeyError) as error:
                problems.append(
                    f"the snapshot {switch.snapshot} could not be put back ({error}); the state "
                    f"is partly what {switch.to_release} left behind"
                )
            else:
                switch.restored = True
                self.save_quietly()
        if switch.restored and switch.snapshot is not None:
            # Removed only once the outcome is recorded (see conclude).
            self.cleanup = lambda name=switch.snapshot: state.discard_after_restore(ctx, name)
        problems += self.check_out_previous()
        started, health = self.start()
        # Keep the update in flight, so that the next run (boot, ExecStopPost, the next night,
        # `--recover`) tries the way back again, when the snapshot is still to be put back or
        # nothing runs.
        retry = not started or (switch.snapshot is not None and not switch.restored)
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
        return self.broken(head, started, health, problems, retry=retry)

    def finish_interrupted(self) -> int:
        switch = self._switch()
        self.ctx.warn(
            f"An update from {switch.from_label} to {switch.to_release}, started "
            f"{switch.started}, was interrupted ({switch.phase}). Finishing it first."
        )
        if (
            switch.phase != state.BEFORE_SWITCH
            and switch.snapshot is not None
            and state.snapshot_put_back(self.ctx, switch.snapshot)
        ):
            done = self.way_back_already_done()
            if done is not None:
                return done
        last = self.record.last_result or {}
        if last.get("outcome") == "broken" and last.get("release") == switch.to_release:
            # Not an interruption: the way back of an earlier run could not finish.
            return (
                self.restart_previous("an earlier attempt could not start the previous release")
                if switch.phase == state.BEFORE_SWITCH
                else self.roll_back("an earlier attempt could not finish the way back")
            )
        if switch.phase == state.BEFORE_SWITCH:
            return self.restart_previous(
                "it was interrupted (killed or a power cut) before the snapshot was complete"
            )
        return self.roll_back("it was interrupted (killed or a power cut) during the switch")

    def way_back_already_done(self) -> int | None:
        """The snapshot is back, the previous release checked out and healthy: an earlier way back
        finished but could not record it (a full disk). Record it now, without stopping anything.
        ``None`` when that is not the case and the way back has to run."""
        ctx, switch = self.ctx, self._switch()
        head = _server.git(ctx, "rev-parse", "--verify", "HEAD")
        if not head.ok or head.stdout.strip() != switch.from_commit:
            return None
        if not check_health(ctx).healthy:
            return None
        switch.restored = True
        if switch.snapshot is not None:
            self.cleanup = lambda name=switch.snapshot: state.discard_after_restore(ctx, name)
        return self.conclude(
            switch.to_release,
            "rolled-back",
            ROLLED_BACK,
            f"Update to {switch.to_release} failed earlier and was rolled back: the previous "
            f"release {switch.from_label} with the state from before the update runs healthy. "
            "That could not be recorded then (a full disk?); it is now. Nothing was stopped.",
        )

    def finish_owed_start(self, what: str) -> int:
        """A restore or ``--undo`` put the state in place but was cut off before it started the
        stack: start it now, and only then remove the marker."""
        ctx = self.ctx
        ctx.warn(f"{what[0].upper()}{what[1:]} was cut off before it started the stack.")
        with _shielded():
            started, health = self.start()
        if started:
            (_server.ops_dir(ctx) / _server.RESTORE_MARKER).unlink(missing_ok=True)
        head = (
            f"{what[0].upper()}{what[1:]} had put the state in place but was cut off before it "
            "started the stack."
        )
        if started and health.healthy:
            return self.conclude(
                None, "start-finished", OK, f"{head} The stack was started and is healthy."
            )
        if started:
            return self.conclude(
                None,
                "start-finished",
                BROKEN,
                f"{head} The stack was started but is {health.describe()}. See {OPERATIONS}, "
                "'The rollback is not healthy'.",
            )
        return self.conclude(
            None,
            "start-owed",
            BROKEN,
            f"{head} NOTHING IS RUNNING: the stack could not be started "
            f"({'; '.join(health.problems)}). The next run tries again; see {OPERATIONS}, "
            "'Nothing is running'.",
        )

    def check_out_previous(self) -> list[str]:
        """Check out exactly the previous release, whatever an interrupted checkout left.

        Files are removed only after a forward checkout may have run (the switch had begun),
        and only those whose content is the new release's own.
        """
        switch = self._switch()
        self.ctx.say(f"Checking out {switch.from_label} ...")
        error, kept, removed = _server.check_out_exactly(
            self.ctx,
            switch.from_commit,
            switch.to_commit,
            remove_added=switch.phase != state.BEFORE_SWITCH,
        )
        if kept:
            self.notes.append(
                "left in place, because they are not the new release's files: " + ", ".join(kept)
            )
        if removed:
            self.notes.append(
                f"removed, because they are {switch.to_release}'s own files, byte for byte (Git "
                f"gives them back with {switch.to_release}): " + ", ".join(removed)
            )
        if error is None:
            return []
        return [
            f"the previous release {switch.from_label} could not be checked out ({error}); "
            "whatever is checked out was started"
        ]

    def remove_stale_git_locks(self) -> None:
        _server.remove_stale_git_locks(self.ctx)

    def start(self) -> tuple[bool, Health]:
        """Start the checkout (one retry) and wait until healthy."""
        ctx = self.ctx
        ctx.say("Starting the stack ...")
        clear_stopped(ctx.paths, ctx.wall())
        result = ctx.compose("up", "--detach", "--force-recreate", timeout=UP_TIMEOUT)
        if not result.ok:
            ctx.sleep(10)
            result = ctx.compose("up", "--detach", "--force-recreate", timeout=UP_TIMEOUT)
        if not result.ok:
            return False, Health(False, (f"`docker compose up` failed: {_tail(result)}",))
        ctx.say("Waiting until healthy ...")
        return True, wait_healthy(ctx, timeout=self.args.timeout)

    def broken(
        self,
        head: str,
        started: bool,
        health: Health,
        problems: list[str],
        retry: bool = False,
    ) -> int:
        """The loud ending: the previous release is not back healthy, or nothing runs.

        ``retry`` keeps the update in flight, so that the way back is tried again."""
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
        if retry:
            message += (
                " The way back is tried again at the next start of the server, after the next "
                "update run, or now with `sudo ./cave update --recover`; until then no backup is "
                "made."
            )
        return self.conclude(switch.to_release, "broken", BROKEN, message, keep_switch=retry)

    # ---------------------------------------------------------------------------- reporting
    def save_quietly(self) -> bool:
        """Write the record; on the way back a full disk must not stop the stack from starting."""
        try:
            state.save(self.ctx, self.record)
        except OSError as error:
            self.ctx.warn(f"The update record could not be written: {error}")
            return False
        return True

    def _switch(self) -> state.Switch:
        if self.record.switch is None:
            raise CaveError("internal error: no update in flight")
        return self.record.switch

    def conclude(
        self, tag: str | None, outcome: str, status: int, message: str, keep_switch: bool = False
    ) -> int:
        record = self.record
        if not keep_switch:
            record.switch = None
        if self.notes:
            message += " Note: " + "; ".join(self.notes) + "."
        try:
            state.prune_snapshots(self.ctx)  # after every outcome, the newest two are kept
        except OSError as error:
            self.ctx.warn(f"Old snapshots could not be removed: {error}")
        record.last_result = {
            "time": state.now(),
            "outcome": outcome,
            "release": tag,
            "message": message,
            "status": status,
        }
        record.last_check = {"time": state.now(), "message": message}
        if not self.save_quietly():
            message += (
                " (The update record could not be written; `cave status` may be out of date.)"
            )
        elif not keep_switch and self.cleanup is not None:
            # Only now that the outcome is recorded may the used-up snapshot go.
            try:
                self.cleanup()
            except OSError as error:
                self.ctx.warn(f"The used-up snapshot could not be removed: {error}")
        (self.ctx.say if status == OK else self.ctx.warn)(message)
        notify(self.ctx, TITLE, message)
        if status in (ROLLED_BACK, BROKEN):
            headline, happened = WARNINGS.get(outcome, WARNINGS["broken"])
            alert.warn_owner(
                self.ctx, "update", headline, happened, about_the_stack=status == BROKEN
            )
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
        for name in ("tmp", "discard"):
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
