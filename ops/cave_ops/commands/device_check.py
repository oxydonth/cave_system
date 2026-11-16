"""``cave device-check <thermostat>``: ask a real, paired thermostat what the device tests ask.

Concept sections 4.4 and 8, work package 7. Through Home Assistant's REST API with the script
token, against the running system:

1. Read the thermostat's entry from the applied ``heating.yaml`` (in a one-off Home Assistant
   container, with the integration's own loader): its room, its external temperature input and
   its ``settings``.
2. Hold it with ``cave_heating.hold``, for longer than this check can take, so the controller
   neither reacts to the check nor passes it on to the room. A thermostat the file does not
   name is not driven by the controller and is checked without a hold.
3. Check that it exists, is available and reports a temperature; that it accepts a target half a
   degree higher and reports it back, and how long that took; where the file names them, that
   the external input accepts a value and that the settings entities exist with the configured
   options.
4. Whatever happens, also on an error or Ctrl-C: restore the original target (and wait until the
   thermostat reports it, so that no late report of the check's own command arrives after the
   release) and release the hold. If the command is killed, the hold ends by itself.

If the hold disappears during the check (Home Assistant restarted), the result is "the check
could not complete", not a failure: the check holds the thermostat again to restore it.

Exit status: 0 every check passed, 1 a check failed, 2 the check could not complete.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
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
# Added to the hold for the requests themselves (each may take its 10 s) and the cleanup.
HOLD_MARGIN = 300.0
HOLD_MAX = 4 * 3600.0  # what cave_heating.hold accepts (concept 5.5)
# Retrying the cleanup while Home Assistant is restarting.
CLEANUP_PATIENCE = 300.0
STEP = 0.5
UNUSABLE = ("unavailable", "unknown")
MARKER = "CAVE-DEVICE-CHECK "

# Runs in the Home Assistant container with /config as the working directory, so that the
# integration's own loader reads the applied copy (with its version conversion).
CONFIG_SCRIPT = """\
import json, sys
from pathlib import Path
from custom_components.cave_heating.schema import ConfigError, load_file

entity, path = sys.argv[1], Path(sys.argv[2])
try:
    config = load_file(path)
except ConfigError as error:
    print("CAVE-DEVICE-CHECK " + json.dumps({"error": " ".join(error.errors)}))
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
print("CAVE-DEVICE-CHECK " + json.dumps({"thermostat": found}))
"""


class HoldLost(Exception):
    """The hold ended underneath the check: Home Assistant restarted, or someone released it."""


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
class Run:
    """The state of one check."""

    ctx: Context
    client: HAClient
    entity: str
    timeout: float
    interval: float
    config: Thermostat | None
    rows: list[Row] = field(default_factory=list)
    held: bool = False
    incomplete: str | None = None
    original: float | None = None  # the target before the check
    wanted: float | None = None  # the target the check sends
    changed: bool = False  # the check sent the thermostat a target that must be undone
    confirmed: bool = False  # the thermostat reported the check's target

    # --- results -----------------------------------------------------------------------------

    def record(self, check: str, ok: bool, seconds: float | None = None, note: str = "") -> bool:
        self.rows.append(Row(check, "pass" if ok else "FAIL", seconds, note))
        return ok

    def skip(self, check: str, note: str) -> None:
        self.rows.append(Row(check, "skipped", None, note))

    # --- Home Assistant ----------------------------------------------------------------------

    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self.client.get_state(entity_id)

    def problem_sensor(self) -> str:
        room = self.config.room if self.config is not None else ""
        return f"binary_sensor.cave_{room}_problem"

    def still_held(self) -> bool:
        """Whether the controller still shows the thermostat as held."""
        if not self.held:
            return True
        sensor = self.state(self.problem_sensor())
        statuses = (sensor or {}).get("attributes", {}).get("thermostats") or {}
        return bool(statuses.get(self.entity) == "held")

    def ensure_held(self) -> None:
        if not self.still_held():
            raise HoldLost

    def hold(self, seconds: float) -> None:
        self.client.call_service(
            "cave_heating", "hold", {"entity_id": self.entity, "duration": math.ceil(seconds)}
        )
        self.held = True

    def target(self) -> float | None:
        state = self.state(self.entity)
        if state is None or state.get("state") in UNUSABLE:
            return None
        return _number(state.get("attributes", {}).get("temperature"))

    def wait_for(self, read: Any, expected: float, limit: float) -> float | None:
        """Poll until ``read()`` reports ``expected``; the seconds it took, or None."""
        start = self.ctx.monotonic()
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
        self.client.call_service(
            "climate", "set_temperature", {"entity_id": self.entity, "temperature": value}
        )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def hold_seconds(timeout: float) -> float:
    """The hold asked for: longer than the worst case of everything done while holding.

    The target may take ``timeout``; restoring it up to two (a late report of the check's own
    target is undone, and the original waited for again); the external input ``INPUT_TIMEOUT``;
    ``HOLD_MARGIN`` covers the requests themselves and the release.
    """
    return 3 * timeout + INPUT_TIMEOUT + HOLD_MARGIN


def read_config(ctx: Context, entity: str) -> Thermostat | None:
    """The thermostat's entry in the applied heating.yaml; None if the file does not name it."""
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
    if "error" in answer:
        raise CaveError(f"the applied heating.yaml is not valid: {answer['error']}")
    found = answer.get("thermostat")
    if found is None:
        return None
    return Thermostat(
        room=found["room"],
        external_temperature=found.get("external_temperature"),
        settings=dict(found.get("settings") or {}),
        temperature_sensor=found.get("temperature_sensor"),
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    require_initialised(ctx)
    entity: str = args.entity
    if not entity.startswith("climate."):
        raise CaveError(f"{entity} is not a climate entity")
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
    config = read_config(ctx, entity)
    check = Run(ctx, client, entity, args.timeout, args.interval, config)
    try:
        _check(check)
    except HoldLost:
        check.incomplete = (
            "the hold ended during the check (did Home Assistant restart?); the results so far "
            "are shown, but the check could not complete"
        )
    except HAUnreachable as error:
        check.incomplete = f"Home Assistant could not be reached: {error}"
    finally:
        _clean_up(check)
    return _report(check)


def _check(check: Run) -> None:
    ctx, entity = check.ctx, check.entity
    if check.config is None:
        check.skip("held for the check", "not in heating.yaml: the controller does not drive it")
    else:
        seconds = hold_seconds(check.timeout)
        check.hold(seconds)
        held = check.still_held()
        check.record(f"held for the check ({seconds / 60:.0f} min at most)", held)
        if not held:
            raise HoldLost

    state = check.state(entity)
    if not check.record(f"{entity} exists", state is not None) or state is None:
        return
    available = state.get("state") not in UNUSABLE
    if not check.record("is available", available, note=f"state {state.get('state')}"):
        return
    attributes = state.get("attributes", {})
    current = _number(attributes.get("current_temperature"))
    check.record(
        "reports a temperature", current is not None, note=f"{current} °C" if current else ""
    )

    original = _number(attributes.get("temperature"))
    if state.get("state") == "off" or original is None:
        check.incomplete = (
            "the thermostat is off (its room is switched off, paused or in summer mode); run the "
            "check while its room heats"
        )
        return
    maximum = _number(attributes.get("max_temp"))
    wanted = original + STEP
    if maximum is not None and wanted > maximum:
        wanted = original - STEP
    check.original = original
    ctx.say(f"Setting {wanted:g} °C and waiting up to {check.timeout:g} s for the report ...")
    check.changed = True
    check.set_target(wanted)
    check.wanted = wanted
    took = check.wait_for(check.target, wanted, check.timeout)
    check.confirmed = took is not None
    check.record(
        f"accepts {wanted:g} °C and reports it back",
        took is not None,
        took,
        "" if took is not None else f"no report within {check.timeout:g} s",
    )

    if check.config is not None and check.config.external_temperature:
        _check_input(check, check.config.external_temperature, current)
    if check.config is not None:
        for setting, value in check.config.settings.items():
            _check_setting(check, setting, value)


def _check_input(check: Run, input_entity: str, current: float | None) -> None:
    state = check.state(input_entity)
    if state is None or state.get("state") in ("unavailable",):
        check.record(f"external input {input_entity} accepts a value", False, note="unavailable")
        return
    attributes = state.get("attributes", {})
    sensor = check.config.temperature_sensor if check.config else None
    sensor_state = check.state(sensor) if sensor else None
    # The room's temperature, which the controller writes anyway; else what the input has.
    value = (
        _number((sensor_state or {}).get("state")) or _number(state.get("state")) or current or 20.0
    )
    step = _number(attributes.get("step")) or 0.1
    value = round(round(value / step) * step, 2)
    domain = input_entity.split(".", 1)[0]
    started = check.ctx.monotonic()
    check.client.call_service(domain, "set_value", {"entity_id": input_entity, "value": value})

    def read() -> float | None:
        return _number((check.state(input_entity) or {}).get("state"))

    took = check.wait_for(read, value, INPUT_TIMEOUT)
    check.record(
        f"external input {input_entity} accepts {value:g}",
        took is not None,
        check.ctx.monotonic() - started if took is not None else None,
    )


def _check_setting(check: Run, setting: str, value: Any) -> None:
    state = check.state(setting)
    label = f"setting {setting} can be {value}"
    if state is None or state.get("state") in ("unavailable",):
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
    """Restore the original target and release the hold, retrying while Home Assistant restarts."""
    ctx = check.ctx
    original, changed = check.original, check.changed
    deadline = ctx.monotonic() + CLEANUP_PATIENCE
    while True:
        try:
            if changed and original is not None:
                if check.config is not None and not check.still_held():
                    # Hold again, so that restoring the target is not taken for a dial turn.
                    check.hold(check.timeout + HOLD_MARGIN)
                _restore(check, original)
                changed = False
            if check.held:
                check.client.call_service("cave_heating", "release", {"entity_id": check.entity})
                check.held = False
                check.record("hold released", True)
            return
        except (HoldLost, HAUnreachable, HAError) as error:
            # A hold lost again is taken up again at the top of the loop.
            if ctx.monotonic() >= deadline:
                note = str(error)
                if changed:
                    check.record("original target restored", False, note=note)
                if check.held:
                    check.record("hold released", False, note=f"{note}; it ends by itself")
                return
            ctx.sleep(5.0)


def _restore(check: Run, original: float) -> None:
    """Send the original target back and make sure no report of the check's own target follows.

    If the thermostat reported the check's target, the original is confirmed the same way. If it
    never did, that target may still arrive late (a FRITZ! thermostat can take minutes), and
    after the release it could be taken for a dial turn: so the thermostat is watched for the
    whole timeout, and the original is sent once more if the late report comes.
    """
    ctx = check.ctx
    ctx.say(f"Restoring {original:g} °C ...")
    label = f"original target {original:g} °C restored"
    check.set_target(original)
    if check.confirmed:
        took = check.wait_for(check.target, original, check.timeout)
        check.record(label, took is not None, took)
        return
    ctx.say(f"Watching {check.timeout:g} s for a late report of {check.wanted:g} °C ...")
    start = ctx.monotonic()
    resent = False
    value: float | None = None
    while True:
        elapsed = ctx.monotonic() - start
        back = value is not None and abs(value - original) < 0.01
        # The whole timeout, and after a late report until the original is back (at most a
        # second timeout: hold_seconds counts three).
        if (elapsed >= check.timeout and (back or not resent)) or elapsed >= 2 * check.timeout:
            break
        ctx.sleep(check.interval)
        try:
            value = check.target()
        except HAUnreachable:
            continue
        check.ensure_held()
        late = value is not None and check.wanted is not None and abs(value - check.wanted) < 0.01
        if late and not resent:
            check.set_target(original)
            resent = True
    value = check.target()
    note = "the late report came and was undone" if resent else "no late report came"
    ok = value is not None and abs(value - original) < 0.01
    check.record(label, ok, ctx.monotonic() - start, note)


def _report(check: Run) -> int:
    ctx = check.ctx
    width = max([len(row.check) for row in check.rows] + [5])
    ctx.say("")
    ctx.say(f"{'Check'.ljust(width)}  Result   Time     Note")
    for row in check.rows:
        took = f"{row.seconds:.1f} s" if row.seconds is not None else ""
        ctx.say(f"{row.check.ljust(width)}  {row.result.ljust(7)}  {took.ljust(7)}  {row.note}")
    ctx.say("")
    if check.incomplete:
        ctx.say(f"The check could not complete: {check.incomplete}.")
        return 2
    if any(row.result == "FAIL" for row in check.rows):
        ctx.say("The check failed.")
        return 1
    ctx.say("Every check passed.")
    return 0
