"""``cave device-check <thermostat>``: ask a real, paired thermostat what the device tests ask.

Concept sections 4.4 and 8, work package 7. Through Home Assistant's REST API with the script
token, against the running system:

1. Read the thermostat's entry from the applied ``heating.yaml`` (in a one-off Home Assistant
   container, with the integration's own loader): its room, its external temperature input, its
   ``settings``, the file's ``sensor_stale``, and the entity's platform from Home Assistant's
   entity registry. A room thermostat ``climate.cave_<room>``, or anything else of
   ``cave_heating`` itself, is refused: setting a temperature on it would start an override.
   A thermostat the file does not name is refused too, unless ``--not-in-heating-yaml`` says the
   check is meant for one paired but not yet applied; it is then checked without a hold.
2. Hold it with ``cave_heating.hold``, for longer than this check can take, so the controller
   neither reacts to the check nor passes it on to the room.
3. Check that it exists, is available and reports a temperature; that it accepts a target half a
   degree higher and reports it back, and how long that took; where the file names them, that
   the external input accepts the room sensor's fresh reading (and nothing else) and that the
   settings entities exist with the configured options.
4. Whatever happens, also on an error from Home Assistant, Ctrl-C, SIGTERM or SIGHUP: restore the
   original target (and wait until the thermostat reports it, so that no late report of the
   check's own command arrives after the release), and, in a step of its own that runs even when
   the restore fails, release the hold. If the command is killed outright, the hold ends by
   itself, and the user is always told when.

The result table is always printed. Exit status: 0 every check passed, 1 a check failed, 2 the
check could not complete (an error from Home Assistant, the hold lost to a restart, the
thermostat off), 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..container import run_in_homeassistant
from ..context import Context
from ..errors import CaveError
from ..ha import HAClient, HAError, HAUnreachable
from ._common import require_initialised

HELP = "Check a real, paired thermostat through Home Assistant, holding it meanwhile"

# How long a FRITZ! thermostat may take to confirm (confirm_timeout_slow, concept 5.5).
DEFAULT_TIMEOUT = 1200.0
DEFAULT_INTERVAL = 2.0
# Waiting for an external input, which Zigbee2MQTT reports at once.
INPUT_TIMEOUT = 60.0
# Added to every hold for the requests themselves (each may take its 10 s) and the release.
HOLD_MARGIN = 300.0
HOLD_MAX = 4 * 3600.0  # what cave_heating.hold accepts (concept 5.5)
# How long the clean-up keeps trying while Home Assistant restarts (a Raspberry Pi can take
# several minutes); the hold covers it.
CLEANUP_PATIENCE = 900.0
# How long a single reading during the check is tried again when it fails (an error status, a
# timeout): a passing hiccup is not a reason to give up the check.
READ_PATIENCE = 60.0
RETRY_PAUSE = 5.0
STEP = 0.5
UNUSABLE = ("unavailable", "unknown")
MARKER = "CAVE-DEVICE-CHECK "
OWN_PREFIX = "climate.cave_"
INTEGRATION = "cave_heating"
TIMING_NOTE = (
    "until Home Assistant showed it (FRITZ!: what the box reports, polled every 30 s; "
    "see docs/devices.md)"
)

# Runs in the Home Assistant container with /config as the working directory, so that the
# integration's own loader reads the applied copy (with its version conversion), and Home
# Assistant's entity registry tells which integration the entity belongs to.
CONFIG_SCRIPT = """\
import json, sys
from pathlib import Path
from custom_components.cave_heating.schema import ConfigError, load_file

entity, path = sys.argv[1], Path(sys.argv[2])
platform = None
try:
    registry = json.loads(Path(".storage/core.entity_registry").read_text(encoding="utf-8"))
    for item in registry["data"]["entities"]:
        if item.get("entity_id") == entity:
            platform = item.get("platform")
except (OSError, ValueError, KeyError, TypeError):
    pass
try:
    config = load_file(path)
except ConfigError as error:
    answer = {"error": " ".join(error.errors), "platform": platform}
    print("CAVE-DEVICE-CHECK " + json.dumps(answer))
    sys.exit(0)
found = None
for room in config.rooms:
    for trv in room.trvs:
        if trv.entity == entity:
            found = {
                "room": room.id,
                "external_temperature": trv.external_temperature,
                "settings": dict(trv.settings),
                "temperature_sensor": room.temperature_sensor,
            }
answer = {
    "thermostat": found,
    "platform": platform,
    "rooms": [room.id for room in config.rooms],
    "sensor_stale": config.timing.sensor_stale.total_seconds(),
}
print("CAVE-DEVICE-CHECK " + json.dumps(answer))
"""


def _utcnow() -> datetime:
    """The wall clock, for the freshness of the room sensor and the hold's end (replaceable)."""
    return datetime.now(UTC)


class HoldLost(Exception):
    """The hold ended underneath the check: Home Assistant restarted, or someone released it."""


class ReadFailed(HAUnreachable):
    """Reading a state failed with an error status: retried like an unreachable Home Assistant,
    because only a refused service call is a refusal."""


@dataclass
class Row:
    """One line of the result table."""

    check: str
    result: str  # pass, FAIL, skipped
    seconds: float | None = None
    note: str = ""


@dataclass
class Thermostat:
    """What the applied heating.yaml says about the thermostat."""

    room: str
    external_temperature: str | None
    settings: dict[str, Any]
    temperature_sensor: str | None


@dataclass
class Lookup:
    """What the container found: the thermostat's entry, and what tells it apart from others."""

    thermostat: Thermostat | None
    platform: str | None
    rooms: list[str]
    sensor_stale: float


@dataclass
class Run:
    """The state of one check."""

    ctx: Context
    client: HAClient
    entity: str
    timeout: float
    interval: float
    config: Thermostat | None
    sensor_stale: float = 3 * 3600.0
    rows: list[Row] = field(default_factory=list)
    # A hold was asked for and may be in effect (also when the call timed out), until released.
    hold_requested: bool = False
    hold_end: float | None = None  # ctx.monotonic() when the last hold asked for ends by itself
    incomplete: str | None = None
    original: float | None = None  # the target before the check
    wanted: float | None = None  # the target the check sends
    changed: bool = False  # the check may have changed the target: it must be put back
    confirmed: bool = False  # the thermostat reported the check's target
    hold_lost: bool = False  # the hold disappeared once: Home Assistant may be restarting
    restore_sent: bool = False  # the original target was sent back
    restore_recorded: bool = False  # the table has a row for the original target

    # --- output ------------------------------------------------------------------------------

    def say(self, message: str = "") -> None:
        """Print, if the terminal is still there: ``Context.say`` never lets a failed write (a
        hung-up terminal, a closed stream) stop the clean-up."""
        self.ctx.say(message)

    # --- results -----------------------------------------------------------------------------

    def record(self, check: str, ok: bool, seconds: float | None = None, note: str = "") -> bool:
        self.rows.append(Row(check, "pass" if ok else "FAIL", seconds, note))
        return ok

    def skip(self, check: str, note: str) -> None:
        self.rows.append(Row(check, "skipped", None, note))

    def hold_ends(self) -> str:
        """When the hold asked for ends by itself, in words."""
        if self.hold_end is None:
            return "now"
        remaining = max(self.hold_end - self.ctx.monotonic(), 0.0)
        at = (_utcnow() + timedelta(seconds=remaining)).astimezone().strftime("%H:%M")
        return f"at about {at} (in {math.ceil(remaining / 60)} min)"

    # --- Home Assistant ----------------------------------------------------------------------

    def state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            return self.client.get_state(entity_id)
        except HAError as error:
            raise ReadFailed(f"reading {entity_id} failed: {error}") from error

    def read(self, entity_id: str) -> dict[str, Any] | None:
        """``state``, tried again for ``READ_PATIENCE`` when a reading fails."""
        deadline = self.ctx.monotonic() + READ_PATIENCE
        while True:
            try:
                return self.state(entity_id)
            except HAUnreachable:  # also ReadFailed
                if self.ctx.monotonic() >= deadline:
                    raise
                self.ctx.sleep(RETRY_PAUSE)

    def problem_sensor(self) -> str:
        room = self.config.room if self.config is not None else ""
        return f"binary_sensor.cave_{room}_problem"

    def still_held(self) -> bool:
        """Whether the controller still shows the thermostat as held."""
        if not self.hold_requested:
            return True
        sensor = self.read(self.problem_sensor())
        statuses = (sensor or {}).get("attributes", {}).get("thermostats") or {}
        held = statuses.get(self.entity) == "held"
        if not held:
            # However it was noticed: Home Assistant may be restarting, and until cave_heating
            # has loaded its services answer "Service not found", which is not a refusal.
            self.hold_lost = True
        return held

    def ensure_held(self) -> None:
        if not self.still_held():
            self.hold_lost = True
            raise HoldLost

    def hold(self, seconds: float) -> None:
        # Counted as held before the call: a call that times out may still have taken effect.
        self.hold_requested = True
        self.hold_end = self.ctx.monotonic() + seconds
        try:
            self.client.call_service(
                INTEGRATION, "hold", {"entity_id": self.entity, "duration": math.ceil(seconds)}
            )
        except HAError:
            self.hold_requested = False  # refused: no hold was taken
            raise

    def target(self) -> float | None:
        state = self.state(self.entity)
        if state is None or state.get("state") in UNUSABLE:
            return None
        return _number(state.get("attributes", {}).get("temperature"))

    def wait_for(
        self, read: Callable[[], float | None], expected: float, limit: float, start: float
    ) -> float | None:
        """Poll until ``read()`` reports ``expected``; seconds since ``start``, or None."""
        while True:
            try:
                value = read()
            except HAUnreachable:
                value = None  # Home Assistant restarting: keep trying, the hold check decides
            else:
                self.ensure_held()
            elapsed = self.ctx.monotonic() - start
            if value is not None and abs(value - expected) < 0.01:
                return elapsed
            if elapsed >= limit:
                return None
            self.ctx.sleep(self.interval)

    def set_target(self, value: float) -> None:
        """Send a target. A refused call changed nothing; an unanswered one may have."""
        try:
            self.client.call_service(
                "climate", "set_temperature", {"entity_id": self.entity, "temperature": value}
            )
        except HAUnreachable:
            self.changed = True
            raise
        self.changed = True


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close(first: float | None, second: float | None) -> bool:
    return first is not None and second is not None and abs(first - second) < 0.01


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("entity", help="the thermostat's climate entity, e.g. climate.bad_heizung")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait for the thermostat to report a target (default {DEFAULT_TIMEOUT:g},"
        " enough for a FRITZ! thermostat)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"seconds between two looks at the thermostat (default {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument(
        "--not-in-heating-yaml",
        action="store_true",
        help="check a thermostat the applied heating.yaml does not name yet (paired, not yet "
        "applied); it is checked without a hold, because the controller does not drive it",
    )


def hold_seconds(timeout: float) -> float:
    """The hold asked for: longer than the worst case of everything done while holding.

    The target may take ``timeout``; restoring it up to two (a late report of the check's own
    target is undone, and the original waited for again); the external input ``INPUT_TIMEOUT``;
    ``HOLD_MARGIN`` covers the requests themselves and the release.
    """
    return 3 * timeout + INPUT_TIMEOUT + HOLD_MARGIN


def rehold_seconds(timeout: float) -> float:
    """A hold taken again during the clean-up covers the whole restore, two timeouts at most."""
    return 2 * timeout + HOLD_MARGIN


def read_config(ctx: Context, entity: str) -> Lookup:
    """The thermostat's entry in the applied heating.yaml, its platform, and the room IDs."""
    applied = ctx.paths.container_path(ctx.paths.heating_applied)
    if applied is None:
        raise CaveError(f"{ctx.paths.heating_applied} is not inside the state directory")
    result = run_in_homeassistant(
        ctx, ["-c", CONFIG_SCRIPT, entity, applied], workdir="/config", timeout=600.0
    )
    lines = [line for line in result.output.splitlines() if line.startswith(MARKER)]
    if not result.ok or not lines:
        raise CaveError(
            "cannot read the applied heating.yaml in the Home Assistant container: "
            + (result.output.strip()[-500:] or f"exit status {result.returncode}")
        )
    answer = json.loads(lines[-1].removeprefix(MARKER))
    if answer.get("platform") == INTEGRATION:
        raise CaveError(_own_entity(entity))
    if "error" in answer:
        raise CaveError(f"the applied heating.yaml is not valid: {answer['error']}")
    found = answer.get("thermostat")
    thermostat = None
    if found is not None:
        thermostat = Thermostat(
            room=found["room"],
            external_temperature=found.get("external_temperature"),
            settings=dict(found.get("settings") or {}),
            temperature_sensor=found.get("temperature_sensor"),
        )
    return Lookup(
        thermostat,
        answer.get("platform"),
        list(answer.get("rooms") or []),
        float(answer.get("sensor_stale") or 3 * 3600.0),
    )


def _own_entity(entity: str) -> str:
    return (
        f"{entity} belongs to cave_heating itself (a room thermostat climate.cave_<room>): "
        "setting a temperature on it starts an override for the whole room. Name a radiator "
        "thermostat of the room instead, as listed under its trvs in heating.yaml. (Every "
        "climate.cave_* name is refused, because it is the room thermostats' pattern; a "
        "radiator thermostat named like that has to be renamed in Home Assistant first.)"
    )


@contextlib.contextmanager
def _signals_interrupt() -> Iterator[None]:
    """SIGTERM and SIGHUP (a dropped SSH session) interrupt the check like Ctrl-C, so the
    clean-up runs; the previous handlers are back afterwards."""

    def interrupt(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(signal.Signals(signum).name)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, interrupt)
        except ValueError:  # not the main thread: leave the signals alone
            continue
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    entity: str = args.entity
    if not entity.startswith("climate."):
        raise CaveError(f"{entity} is not a climate entity")
    if entity.startswith(OWN_PREFIX):
        raise CaveError(_own_entity(entity))
    if args.timeout <= 0 or args.interval <= 0:
        raise CaveError("--timeout and --interval must be more than zero")
    if hold_seconds(args.timeout) > HOLD_MAX:
        raise CaveError(
            f"--timeout {args.timeout:g} is too long: the hold it needs would exceed four hours"
        )
    client = ctx.ha()
    if client is None:
        raise CaveError("there is no script token yet; run `./cave bootstrap` first")

    ctx.say(f"Reading {entity} from the applied heating.yaml ...")
    lookup = read_config(ctx, entity)
    if entity in {f"{OWN_PREFIX}{room}" for room in lookup.rooms}:
        raise CaveError(_own_entity(entity))
    if lookup.thermostat is None and not args.not_in_heating_yaml:
        raise CaveError(
            f"{entity} is not in the applied heating.yaml, so cave_heating does not drive it and "
            "nothing would be checked against the room. If it is paired but not yet applied, run "
            "again with --not-in-heating-yaml to check it on its own, without a hold"
        )
    check = Run(
        ctx,
        client,
        entity,
        args.timeout,
        args.interval,
        lookup.thermostat,
        sensor_stale=lookup.sensor_stale,
    )
    interrupted = False
    with _signals_interrupt():
        try:
            try:
                _check(check)
            except HoldLost:
                check.incomplete = (
                    "the hold ended during the check (did Home Assistant restart?); the results "
                    "so far are shown"
                )
            except HAUnreachable as error:
                check.hold_lost = True  # it may be restarting; the clean-up waits for it
                check.incomplete = f"Home Assistant did not answer: {error}"
            except HAError as error:
                check.incomplete = f"Home Assistant refused a request: {error}"
            except KeyboardInterrupt as stop:
                interrupted = True
                check.incomplete = f"interrupted ({str(stop) or 'Ctrl-C'})"
                check.say(_interrupt_notice(check))
        finally:
            # Also when something above failed in a way not foreseen: the thermostat is put back
            # and the hold released in any case. Output is best effort from here on.
            try:
                _clean_up(check)
            except KeyboardInterrupt as stop:
                interrupted = True
                _interrupted_during_clean_up(check, str(stop) or "Ctrl-C")
    code = _report(check)
    return 130 if interrupted else code


def _interrupt_notice(check: Run) -> str:
    """What happens now, and what a second interrupt would do, in words."""
    if not check.changed or check.original is None:
        nothing = "Interrupted: the thermostat's target was not changed."
        return nothing + (" Releasing the hold." if check.hold_requested else "")
    back = f"Interrupted: putting the thermostat back to {check.original:g} °C."
    if check.hold_requested:
        return (
            f"{back} Interrupting again skips that; the hold is still released, and the "
            "controller then brings the thermostat to its room's target."
        )
    left = f"{check.wanted:g} °C" if check.wanted is not None else "the check's target"
    return f"{back} Interrupting again leaves it at {left}."


def _interrupted_during_clean_up(check: Run, how: str) -> None:
    """An interrupt while the thermostat was being put back: say what state it was left in."""
    check.skip("clean-up", f"interrupted during the clean-up ({how})")
    if check.changed and check.original is not None and not check.restore_recorded:
        sent = "sent" if check.restore_sent else "not sent"
        then = (
            "the controller brings it to its room's target after the release"
            if check.hold_requested or check.config is not None
            else f"it may be left at {check.wanted:g} °C"
            if check.wanted is not None
            else "it may be left at the check's target"
        )
        check.record(
            f"original target {check.original:g} °C restored",
            False,
            note=f"not confirmed: {sent}; {then}",
        )
        check.restore_recorded = True
    check.incomplete = check.incomplete or f"interrupted during the clean-up ({how})"


def _check(check: Run) -> None:
    ctx, entity = check.ctx, check.entity
    if check.config is None:
        check.skip(
            "held for the check",
            "not in heating.yaml (--not-in-heating-yaml): the controller does not drive it",
        )
    else:
        seconds = hold_seconds(check.timeout)
        check.hold(seconds)
        held = check.still_held()
        check.record(f"held for the check ({seconds / 60:.0f} min at most)", held)
        if not held:
            raise HoldLost

    state = check.read(entity)
    if not check.record(f"{entity} exists", state is not None) or state is None:
        return
    available = state.get("state") not in UNUSABLE
    if not check.record("is available", available, note=f"state {state.get('state')}"):
        return
    attributes = state.get("attributes", {})
    current = _number(attributes.get("current_temperature"))
    check.record(
        "reports a temperature",
        current is not None,
        note="" if current is None else f"{current:g} °C",
    )

    original = _number(attributes.get("temperature"))
    if state.get("state") == "off":
        check.incomplete = (
            "the thermostat is off (its room is switched off or paused, or the home is in "
            "summer mode); run the check while its room heats"
        )
        return
    if original is None:
        check.incomplete = (
            "the thermostat shows no target (a FRITZ! thermostat in holiday mode or summer "
            "mode, or with its dial turned to ON); run the check when it shows a target"
        )
        return
    maximum = _number(attributes.get("max_temp"))
    wanted = original + STEP
    if maximum is not None and wanted > maximum:
        wanted = original - STEP
    check.original, check.wanted = original, wanted
    check.say(f"Setting {wanted:g} °C and waiting up to {check.timeout:g} s for the report ...")
    start = ctx.monotonic()
    check.set_target(wanted)
    took = check.wait_for(check.target, wanted, check.timeout, start)
    check.confirmed = took is not None
    check.record(
        f"accepts {wanted:g} °C and reports it back",
        took is not None,
        took,
        TIMING_NOTE if took is not None else f"no report within {check.timeout:g} s",
    )

    if check.config is not None and check.config.external_temperature:
        _check_input(check, check.config.external_temperature)
    if check.config is not None:
        for setting, value in check.config.settings.items():
            _check_setting(check, setting, value)


def _fresh_sensor_reading(check: Run) -> tuple[float | None, str]:
    """The room sensor's reading if the controller's feed would write it now, else why not."""
    sensor = check.config.temperature_sensor if check.config is not None else None
    if sensor is None:
        return None, "the room has no temperature sensor, so the controller writes nothing"
    state = check.read(sensor)
    value = _number(state.get("state")) if state is not None else None
    if state is None or state.get("state") in UNUSABLE or value is None:
        return None, f"room sensor {sensor} unavailable: the controller's feed is stopped"
    reported = state.get("last_reported") or state.get("last_updated")
    try:
        when = datetime.fromisoformat(str(reported))
    except ValueError:
        return None, f"room sensor {sensor} does not say when it reported"
    if when.tzinfo is None:
        return None, f"room sensor {sensor} does not say when it reported"
    age = _utcnow() - when
    if age >= timedelta(seconds=check.sensor_stale):
        hours = age.total_seconds() / 3600
        return None, (
            f"room sensor {sensor} has not reported a new temperature for {hours:.1f} h, so the "
            "check writes nothing (the controller judges freshness on all of the sensor's "
            "readings and may still be feeding it)"
        )
    return value, ""


def _check_input(check: Run, input_entity: str) -> None:
    label = f"external input {input_entity} accepts the room's temperature"
    state = check.read(input_entity)
    if state is None or state.get("state") == "unavailable":
        check.record(label, False, note="missing" if state is None else "unavailable")
        return
    value, reason = _fresh_sensor_reading(check)
    if value is None:
        # Writing anything else would leave the thermostat regulating on a made-up value (a
        # Sonoff for two hours, a Bosch for 30 minutes) that the stopped feed never corrects.
        check.skip(label, reason)
        return
    step = _number(state.get("attributes", {}).get("step"))
    step = step if step is not None and step > 0 else 0.1
    value = round(round(value / step) * step, 2)
    domain = input_entity.split(".", 1)[0]
    start = check.ctx.monotonic()
    check.client.call_service(domain, "set_value", {"entity_id": input_entity, "value": value})

    def read() -> float | None:
        return _number((check.state(input_entity) or {}).get("state"))

    took = check.wait_for(read, value, INPUT_TIMEOUT, start)
    check.record(
        f"external input {input_entity} accepts {value:g}",
        took is not None,
        took,
        f"from {value:g} °C",
    )


def _check_setting(check: Run, setting: str, value: Any) -> None:
    state = check.read(setting)
    label = f"setting {setting} can be {value}"
    if state is None or state.get("state") == "unavailable":
        check.record(label, False, note="missing" if state is None else "unavailable")
        return
    domain = setting.split(".", 1)[0]
    attributes = state.get("attributes", {})
    if domain == "select":
        options = attributes.get("options") or []
        check.record(label, str(value) in options, note=f"options {', '.join(options)}")
    elif domain == "switch":
        check.record(label, str(value) in ("on", "off"))
    else:
        number = _number(value)
        low, high = _number(attributes.get("min")), _number(attributes.get("max"))
        ok = (
            number is not None
            and (low is None or number >= low)
            and (high is None or number <= high)
        )
        check.record(label, ok, note=f"range {low} to {high}")


def _clean_up(check: Run) -> None:
    """Restore the original target; release the hold in any case, also if the restore failed."""
    try:
        if check.changed and check.original is not None:
            _restore_patiently(check, check.original)
    finally:
        _release(check)


def _restore_patiently(check: Run, original: float) -> None:
    """Restore, taking the hold again if it was lost, and waiting out a restart."""
    ctx = check.ctx
    label = f"original target {original:g} °C restored"
    deadline = ctx.monotonic() + CLEANUP_PATIENCE
    while True:
        try:
            # A hold refused during a restart left none at all: take it, too.
            if check.config is not None and (not check.hold_requested or not check.still_held()):
                # Hold again, for the whole restore, so that it is not taken for a dial turn.
                check.hold(rehold_seconds(check.timeout))
            _restore(check, original)
            return
        except HAError as error:
            if not check.hold_lost:
                check.record(label, False, note=f"refused: {error}")
                check.restore_recorded = True
                check.incomplete = check.incomplete or f"Home Assistant refused to restore: {error}"
                return
            # The hold was lost: Home Assistant is restarting, and until cave_heating has loaded
            # its services answer "Service not found". Wait for it, like for an unreachable one.
            if ctx.monotonic() >= deadline and not check.hold_requested:
                # The hold itself was refused to the end: Home Assistant answers, cave_heating
                # has not loaded, so the controller does not run, and once it starts it adopts
                # no report for a while. Put the thermostat back without a hold rather than
                # leave it at the check's target.
                _restore_unheld(check, original, error)
                return
            if ctx.monotonic() >= deadline:
                check.record(
                    label,
                    False,
                    note=f"{error}; the controller brings it to the room's target once it runs",
                )
                check.restore_recorded = True
                return
            ctx.sleep(RETRY_PAUSE)
        except (HoldLost, HAUnreachable) as error:
            check.hold_lost = True
            if ctx.monotonic() >= deadline:
                reason = "the hold kept ending" if isinstance(error, HoldLost) else str(error)
                check.record(
                    label,
                    False,
                    note=f"{reason}; the controller brings it to the room's target once it runs",
                )
                check.restore_recorded = True
                return
            ctx.sleep(RETRY_PAUSE)


def _restore_unheld(check: Run, original: float, why: HAError) -> None:
    """The last resort of ``_restore_patiently``: send the original target without a hold, and
    wait for the report as long as for the check's own."""
    ctx = check.ctx
    label = f"original target {original:g} °C restored"
    reason = f"cave_heating had not loaded within {CLEANUP_PATIENCE / 60:.0f} min ({why})"
    check.say(f"Restoring {original:g} °C without a hold: {reason} ...")
    start = ctx.monotonic()
    try:
        check.set_target(original)
        check.restore_sent = True
        value: float | None = None
        while True:
            try:
                value = check.target()
            except HAUnreachable:
                value = None
            if _close(value, original) or ctx.monotonic() - start >= check.timeout:
                break
            ctx.sleep(check.interval)
    except (HAError, HAUnreachable) as error:
        check.record(label, False, note=f"sent without a hold, which failed too: {error}")
        check.restore_recorded = True
        return
    took = ctx.monotonic() - start
    note = f"without a hold: {reason}"
    if not _close(value, original):
        note += f"; not reported within {check.timeout:g} s"
    check.record(label, _close(value, original), took, note)
    check.restore_recorded = True


def _restore(check: Run, original: float) -> None:
    """Send the original target back and make sure no report of the check's own target follows.

    If the thermostat reported the check's target, the original is confirmed the same way. If it
    never did, that target may still arrive late (a FRITZ! thermostat can take minutes), and
    after the release it could be taken for a dial turn: so the thermostat is watched for the
    whole timeout, and the original is sent once more if the late report comes.
    """
    ctx = check.ctx
    check.say(f"Restoring {original:g} °C ...")
    label = f"original target {original:g} °C restored"
    start = ctx.monotonic()
    check.set_target(original)
    check.restore_sent = True
    if check.confirmed:
        took = check.wait_for(check.target, original, check.timeout, start)
        check.record(label, took is not None, took)
        check.restore_recorded = True
        return
    check.say(f"Watching {check.timeout:g} s for a late report of {check.wanted:g} °C ...")
    resent = False
    value: float | None = None
    while True:
        elapsed = ctx.monotonic() - start
        back = _close(value, original)
        # The whole timeout, and after a late report until the original is back (at most a
        # second timeout: hold_seconds counts three, rehold_seconds two).
        if (elapsed >= check.timeout and (back or not resent)) or elapsed >= 2 * check.timeout:
            break
        ctx.sleep(check.interval)
        try:
            value = check.target()
        except HAUnreachable:  # also a failed read: keep watching
            continue
        check.ensure_held()
        if _close(value, check.wanted) and not resent:
            check.set_target(original)
            resent = True
    with contextlib.suppress(HAUnreachable):  # if the last read fails, the one before stands
        value = check.target()
    note = "the late report came and was undone" if resent else "no late report came"
    check.record(label, _close(value, original), ctx.monotonic() - start, note)
    check.restore_recorded = True


def _release(check: Run) -> None:
    """Release the hold, retrying while Home Assistant restarts; say when it ends otherwise."""
    if not check.hold_requested:
        return
    ctx = check.ctx
    deadline = ctx.monotonic() + CLEANUP_PATIENCE
    while True:
        try:
            check.client.call_service(INTEGRATION, "release", {"entity_id": check.entity})
        except HAUnreachable as error:
            if check.hold_end is not None and ctx.monotonic() >= check.hold_end:
                check.hold_requested = False
                check.record("hold released", True, note="it had ended by itself")
                return
            if ctx.monotonic() >= deadline:
                check.record("hold released", False, note=f"{error}; it ends by itself")
                return
            ctx.sleep(RETRY_PAUSE)
        except HAError as error:
            if check.hold_lost and ctx.monotonic() < deadline:
                ctx.sleep(RETRY_PAUSE)  # cave_heating not loaded yet after a restart
                continue
            check.record("hold released", False, note=f"refused: {error}; it ends by itself")
            return
        else:
            check.hold_requested = False
            check.record("hold released", True)
            return


def _report(check: Run) -> int:
    say = check.say
    width = max([len(row.check) for row in check.rows] + [5])
    say("")
    say(f"{'Check'.ljust(width)}  Result   Time     Note")
    for row in check.rows:
        took = f"{row.seconds:.1f} s" if row.seconds is not None else ""
        say(f"{row.check.ljust(width)}  {row.result.ljust(7)}  {took.ljust(7)}  {row.note}")
    say("")
    if check.hold_requested:
        say(
            f"The hold on {check.entity} could not be released; it ends by itself "
            f"{check.hold_ends()}, and the controller then brings the thermostat back to its "
            "room's target."
        )
    if check.incomplete:
        say(f"The check could not complete: {check.incomplete}.")
        return 2
    if any(row.result == "FAIL" for row in check.rows):
        say("The check failed.")
        return 1
    say("Every check passed.")
    return 0
