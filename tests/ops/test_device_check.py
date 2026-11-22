"""``cave device-check`` against a fake Home Assistant (WP7).

The thermostat here is a small model of what Home Assistant's REST API shows for one: it takes
``climate.set_temperature`` and reports the target after a delay of simulated time (or never),
and ``cave_heating.hold`` / ``release`` change what the room's problem sensor shows for it. The
fake clock of ``ctx`` advances when the command sleeps, so nothing waits for real. ``Faults``
sits between the command and the fake Home Assistant and makes chosen requests fail.

In every case the original target must be back and the hold released at the end, or the output
must say that it could not be and when the hold ends by itself.
"""

from __future__ import annotations

import errno
import io
import json
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cave_ops.commands import device_check

ENTITY = "climate.schlafzimmer_heizung"
PROBLEM = "binary_sensor.cave_bedroom_problem"
INPUT = "number.schlafzimmer_heizung_remote_temperature"
SENSOR = "sensor.schlafzimmer_temperature"
WINDOW = "switch.schlafzimmer_heizung_window_detection"
SELECT = "select.wohnzimmer_heizung_fenster_temperature_sensor_select"
CONFIG_RUN = ["docker", "compose", "run"]
HOLD_SECONDS = 3 * 1200 + 60 + 300
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def configured(
    *,
    external: str | None = INPUT,
    settings: dict[str, Any] | None = None,
    found: bool = True,
    platform: str | None = "mqtt",
) -> dict[str, Any]:
    """What the container script prints for the thermostat and the applied heating.yaml."""
    thermostat = {
        "room": "bedroom",
        "external_temperature": external,
        "settings": {WINDOW: "off"} if settings is None else settings,
        "temperature_sensor": SENSOR,
    }
    return {
        "thermostat": thermostat if found else None,
        "platform": platform,
        "rooms": ["bedroom", "living_room"],
        "sensor_stale": 10800.0,
    }


class FakeThermostat:
    """A thermostat, its room's problem sensor and its inputs, as Home Assistant shows them."""

    def __init__(self, fake_ha: Any, clock: Any) -> None:
        self.clock = clock
        self.target = 21.0
        self.mode = "heat"
        self.available = True
        self.delay = 6.0  # seconds until a new target is reported
        self.confirms = True
        self.held = False
        self.lose_hold_on_set = False  # Home Assistant restarts during the check
        self.pending: list[tuple[float, float]] = []  # (when, value), reported in this order
        self.hold_calls: list[tuple[float, Any]] = []
        self.release_calls: list[float] = []
        self.input_value = 19.0
        self.states: dict[str, dict[str, Any]] = {
            SENSOR: {
                "state": "19.4",
                "attributes": {},
                "last_reported": (NOW - timedelta(minutes=20)).isoformat(),
            },
            WINDOW: {"state": "on", "attributes": {}},
            SELECT: {
                "state": "local_temperature",
                "attributes": {"options": ["local_temperature", "remote_temperature"]},
            },
        }
        fake_ha.states = _Live(self)
        fake_ha.on_service("climate", "set_temperature", self._set_temperature)
        fake_ha.on_service("cave_heating", "hold", self._hold)
        fake_ha.on_service("cave_heating", "release", self._release)
        fake_ha.on_service("number", "set_value", self._set_value)

    def _set_temperature(self, data: dict[str, Any]) -> None:
        if self.lose_hold_on_set:
            self.lose_hold_on_set = False
            self.held = False
        if self.confirms and self.available:
            self.pending.append((self.clock.now + self.delay, float(data["temperature"])))

    def _hold(self, data: dict[str, Any]) -> None:
        self.hold_calls.append((self.clock.now, data.get("duration")))
        self.held = True

    def _release(self, data: dict[str, Any]) -> None:
        self.release_calls.append(self.clock.now)
        self.held = False

    def _set_value(self, data: dict[str, Any]) -> None:
        self.input_value = float(data["value"])

    def state(self, entity_id: str) -> dict[str, Any] | None:
        while self.pending and self.clock.now >= self.pending[0][0]:
            self.target = self.pending.pop(0)[1]
        if entity_id == ENTITY:
            return {
                "entity_id": ENTITY,
                "state": self.mode if self.available else "unavailable",
                "attributes": {
                    "temperature": self.target if self.mode != "off" else None,
                    "current_temperature": 20.4,
                    "min_temp": 5.0,
                    "max_temp": 30.0,
                },
            }
        if entity_id == PROBLEM:
            status = "held" if self.held else "ok"
            return {"state": "off", "attributes": {"thermostats": {ENTITY: status}}}
        if entity_id == INPUT:
            return {"state": str(self.input_value), "attributes": {"step": 0.2, "min": 0}}
        return self.states.get(entity_id)


class _Live(dict):  # type: ignore[type-arg]
    """``fake_ha.states``, computed from the thermostat at the time of each request."""

    def __init__(self, thermostat: FakeThermostat) -> None:
        super().__init__()
        self.thermostat = thermostat

    def get(self, key: str, default: Any = None) -> Any:
        return self.thermostat.state(key) or default


class Faults:
    """Between the command and the fake Home Assistant: makes chosen requests fail.

    ``fail[(domain, service)]`` is a list of answers for the successive calls of that service:
    ``None`` passes the call through, ``500`` answers with that status without reaching Home
    Assistant, ``"timeout"`` lets the call through and then raises a timeout, as when Home
    Assistant applies it but answers too late. ``fail_reads[entity_id]`` does the same for
    ``GET /api/states/<entity_id>``, once ``reads_armed`` is set. ``down`` refuses every
    connection.
    """

    def __init__(self, fake_ha: Any) -> None:
        self.fake_ha = fake_ha
        self.fail: dict[tuple[str, str], list[Any]] = {}
        self.fail_reads: dict[str, list[Any]] = {}
        self.reads_armed = True
        self.down = False

    def __call__(self, method, url, headers, body, timeout):
        if self.down:
            raise ConnectionRefusedError("connection refused")
        if "/api/states/" in url and self.reads_armed:
            answers = self.fail_reads.get(url.rsplit("/", 1)[-1])
            if answers:
                answer = answers.pop(0)
                if answer is not None:
                    return answer, b'{"message": "a read failed in the fake"}'
        if "/api/services/" in url:
            domain, service = url.rsplit("/", 2)[-2:]
            answers = self.fail.get((domain, service))
            if answers:
                answer = answers.pop(0)
                if answer == "timeout":
                    self.fake_ha(method, url, headers, body, timeout)
                    raise TimeoutError("timed out")
                if answer is not None:
                    return answer, b'{"message": "refused by the fake"}'
        return self.fake_ha(method, url, headers, body, timeout)


@pytest.fixture
def thermostat(fake_ha, clock, ctx, write_token, fake_runner, monkeypatch):
    """A checkout that is initialised, a token, and the thermostat configured in the file."""
    ctx.paths.env_file.write_text("", encoding="utf-8")
    write_token()
    answer(fake_runner, configured())
    monkeypatch.setattr(device_check, "_utcnow", lambda: NOW + timedelta(seconds=clock.now))
    return FakeThermostat(fake_ha, clock)


@pytest.fixture
def faults(ctx, fake_ha):
    wrapper = Faults(fake_ha)
    ctx.transport = wrapper
    return wrapper


def answer(fake_runner, payload: Any) -> None:
    fake_runner.on(CONFIG_RUN, out="noise\n" + device_check.MARKER + json.dumps(payload) + "\n")


def calls(fake_ha) -> list[tuple[str, str, dict[str, Any]]]:
    return [call for call in fake_ha.service_calls if call[0] != "persistent_notification"]


def services(fake_ha) -> list[str]:
    return [call[1] for call in calls(fake_ha)]


def rows(out: str) -> dict[str, str]:
    """The result table: check -> result."""
    table = {}
    for line in out.splitlines():
        for result in ("pass", "FAIL", "skipped"):
            marker = f"  {result} "
            if marker in line + " ":
                table[line.split(marker)[0].strip()] = result
    return table


# --- the normal cases ----------------------------------------------------------------------


def test_a_device_that_passes(run_cave, fake_ha, fake_runner, thermostat):
    result = run_cave("device-check", ENTITY)

    assert result.code == 0, result.out + result.err
    assert "Every check passed." in result.out
    assert calls(fake_ha) == [
        ("cave_heating", "hold", {"entity_id": ENTITY, "duration": HOLD_SECONDS}),
        ("climate", "set_temperature", {"entity_id": ENTITY, "temperature": 21.5}),
        ("number", "set_value", {"entity_id": INPUT, "value": 19.4}),
        ("climate", "set_temperature", {"entity_id": ENTITY, "temperature": 21.0}),
        ("cave_heating", "release", {"entity_id": ENTITY}),
    ]
    table = rows(result.out)
    assert table["accepts 21.5 °C and reports it back"] == "pass"
    assert table[f"external input {INPUT} accepts 19.4"] == "pass"
    assert table[f"setting {WINDOW} can be off"] == "pass"
    assert table["original target 21 °C restored"] == "pass"
    assert table["hold released"] == "pass"
    timing = next(line for line in result.out.splitlines() if "reports it back" in line)
    assert "6.0 s" in timing, "how long the thermostat took to report the target"
    assert "until Home Assistant showed it (FRITZ!: what the box reports" in timing
    assert thermostat.target == 21.0
    assert not thermostat.held
    # The configuration is read with the integration's own loader in a one-off container.
    (run,) = fake_runner.matching(*CONFIG_RUN)
    assert "--workdir" in run.args
    assert "/config" in run.args
    assert run.args[-2:] == (ENTITY, "/config/cave/.applied/heating.yaml")


def test_the_time_counts_from_before_the_command(run_cave, fake_ha, clock, thermostat, faults):
    """A call that itself takes long (the FRITZ! integration polls the box within it) counts."""
    original = faults.fake_ha

    def slow(method, url, headers, body, timeout):
        if url.endswith("/climate/set_temperature"):
            clock.now += 4.0
        return original(method, url, headers, body, timeout)

    faults.fake_ha = slow
    thermostat.delay = 2.0

    result = run_cave("device-check", ENTITY)

    timing = next(line for line in result.out.splitlines() if "reports it back" in line)
    assert "6.0 s" in timing, "4 s in the call and 2 s until the report"


def test_a_device_that_never_confirms(run_cave, fake_ha, clock, thermostat):
    thermostat.confirms = False

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    assert result.code == 1
    assert "The check failed." in result.out
    assert rows(result.out)["accepts 21.5 °C and reports it back"] == "FAIL"
    assert "no report within 300 s" in result.out
    assert ("climate", "set_temperature", {"entity_id": ENTITY, "temperature": 21.0}) in calls(
        fake_ha
    ), "the original target is sent back all the same"
    assert thermostat.target == 21.0
    assert thermostat.release_calls
    assert not thermostat.held
    # The hold asked for outlasts the whole run, timeouts included.
    (held_at, duration), released_at = thermostat.hold_calls[0], thermostat.release_calls[-1]
    assert duration == 3 * 300 + 60 + 300
    assert released_at - held_at < duration


def test_a_late_report_of_the_check_s_target_is_undone_before_the_release(
    run_cave, fake_ha, thermostat
):
    """A thermostat that reports only after the timeout: its report of the check's target comes
    while the check watches after restoring, and the original is sent once more, so that no
    report of the check's own command can arrive after the release and be taken for a dial."""
    thermostat.delay = 400.0

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    assert result.code == 1, "it did not report within the timeout"
    targets = [call[2]["temperature"] for call in calls(fake_ha) if call[1] == "set_temperature"]
    assert targets == [21.5, 21.0, 21.0]
    assert "the late report came and was undone" in result.out
    assert rows(result.out)["original target 21 °C restored"] == "pass"
    assert thermostat.release_calls
    assert all(value == 21.0 for _, value in thermostat.pending), (
        "no report of the check's own target can follow the release"
    )
    assert thermostat.target == 21.0


def test_a_device_that_is_unavailable(run_cave, fake_ha, thermostat):
    thermostat.available = False

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert rows(result.out)["is available"] == "FAIL"
    assert services(fake_ha) == ["hold", "release"], "nothing was sent to the thermostat"
    assert thermostat.target == 21.0
    assert not thermostat.held


@pytest.mark.parametrize(
    ("mode", "target", "words"),
    [
        ("off", 21.0, "run the check while its room heats"),
        ("heat", None, "holiday mode"),
    ],
)
def test_a_thermostat_that_is_off_or_shows_no_target_is_left_alone(
    run_cave, fake_ha, thermostat, mode, target, words
):
    thermostat.mode = mode
    thermostat.target = target

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert words in result.out
    assert services(fake_ha) == ["hold", "release"]


# --- what is refused ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity", "payload"),
    [
        ("climate.cave_bedroom", configured()),
        ("climate.cave_attic", configured(found=False)),  # by its name alone
        ("climate.renamed_room", configured(found=False, platform="cave_heating")),
    ],
)
def test_a_room_thermostat_of_cave_heating_is_refused(
    run_cave, fake_ha, fake_runner, thermostat, entity, payload
):
    """Setting a temperature on climate.cave_<room> would start a six-hour override."""
    answer(fake_runner, payload)

    result = run_cave("device-check", entity)

    assert result.code == 1
    assert "belongs to cave_heating itself" in result.err
    assert calls(fake_ha) == []


def test_a_thermostat_not_in_the_file_is_refused_without_the_option(
    run_cave, fake_ha, fake_runner, thermostat
):
    answer(fake_runner, configured(found=False))

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert "--not-in-heating-yaml" in result.err
    assert calls(fake_ha) == []


def test_with_the_option_a_thermostat_not_in_the_file_is_checked_without_a_hold(
    run_cave, fake_ha, fake_runner, thermostat
):
    answer(fake_runner, configured(found=False))

    result = run_cave("device-check", ENTITY, "--not-in-heating-yaml")

    assert result.code == 0, result.out
    assert rows(result.out)["held for the check"] == "skipped"
    assert "the controller does not drive it" in result.out
    assert services(fake_ha) == ["set_temperature", "set_temperature"]
    assert thermostat.target == 21.0


def test_a_setting_without_the_configured_option_fails(run_cave, fake_ha, fake_runner, thermostat):
    answer(fake_runner, configured(external=None, settings={SELECT: "external"}))

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert rows(result.out)[f"setting {SELECT} can be external"] == "FAIL"
    assert "options local_temperature, remote_temperature" in result.out
    assert thermostat.target == 21.0
    assert not thermostat.held


@pytest.mark.parametrize("sensor", ["unavailable", "stale"])
def test_the_external_input_is_written_only_with_a_fresh_room_sensor_reading(
    run_cave, fake_ha, thermostat, sensor
):
    """Anything but the room's current temperature would leave the thermostat regulating on a
    made-up value that the controller's stopped feed never corrects."""
    if sensor == "unavailable":
        thermostat.states[SENSOR] = {"state": "unavailable", "attributes": {}}
    else:
        thermostat.states[SENSOR]["last_reported"] = (NOW - timedelta(hours=3)).isoformat()

    result = run_cave("device-check", ENTITY)

    assert result.code == 0, result.out
    assert rows(result.out)[f"external input {INPUT} accepts the room's temperature"] == "skipped"
    if sensor == "unavailable":
        assert "unavailable: the controller's feed is stopped" in result.out
    else:
        assert "has not reported a new temperature for 3.0 h, so the check writes nothing" in (
            result.out
        )
    assert "set_value" not in services(fake_ha)
    assert thermostat.input_value == 19.0


def test_without_a_token_nothing_is_touched(run_cave, fake_ha, ctx, fake_runner):
    ctx.paths.env_file.write_text("", encoding="utf-8")

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert "run `./cave bootstrap` first" in result.err
    assert fake_ha.requests == []
    assert fake_runner.calls == []


def test_the_applied_file_is_invalid(run_cave, fake_ha, fake_runner, thermostat):
    answer(fake_runner, {"error": 'Room "bedroom" (rooms[0]), field "trvs": is missing.'})

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert "the applied heating.yaml is not valid" in result.err
    assert calls(fake_ha) == []


def test_a_timeout_that_needs_a_hold_over_four_hours_is_refused(run_cave, fake_ha, thermostat):
    result = run_cave("device-check", ENTITY, "--timeout", "7200")

    assert result.code == 1
    assert "would exceed four hours" in result.err
    assert calls(fake_ha) == []


# --- errors from Home Assistant ------------------------------------------------------------


def test_a_refused_command_cannot_complete_and_the_hold_is_released(
    run_cave, fake_ha, thermostat, faults
):
    """A FRITZ! thermostat with its key lock on: Home Assistant answers 500. Nothing changed, so
    nothing is restored; the hold is released and the table is printed."""
    faults.fail[("climate", "set_temperature")] = [500]

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "The check could not complete: Home Assistant refused a request" in result.out
    assert rows(result.out)["hold released"] == "pass"
    assert services(fake_ha) == ["hold", "release"]
    assert thermostat.release_calls
    assert thermostat.target == 21.0


def test_a_refused_restore_is_reported_and_the_hold_still_released(
    run_cave, fake_ha, thermostat, faults
):
    faults.fail[("climate", "set_temperature")] = [None, 500]

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert rows(result.out)["original target 21 °C restored"] == "FAIL"
    assert "Home Assistant refused to restore" in result.out
    assert rows(result.out)["hold released"] == "pass"
    assert thermostat.release_calls
    assert not thermostat.held


def test_a_refused_release_says_when_the_hold_ends(run_cave, fake_ha, thermostat, faults):
    faults.fail[("cave_heating", "release")] = [500]

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert rows(result.out)["hold released"] == "FAIL"
    assert "could not be released; it ends by itself at about" in result.out
    assert thermostat.target == 21.0


def test_a_hold_call_that_timed_out_is_still_released(run_cave, fake_ha, thermostat, faults):
    """Home Assistant applied the hold but answered too late: it must be released all the same."""
    faults.fail[("cave_heating", "hold")] = ["timeout"]

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "Home Assistant did not answer" in result.out
    assert thermostat.hold_calls
    assert thermostat.release_calls
    assert not thermostat.held


def test_a_restart_during_the_check_cannot_complete_and_cleans_up(run_cave, fake_ha, thermostat):
    """The hold is not kept across a restart: the check says it could not complete (not that the
    device failed), holds the thermostat again for the whole restore, restores and releases."""
    thermostat.lose_hold_on_set = True

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "The check could not complete" in result.out
    assert "did Home Assistant restart?" in result.out
    assert "The check failed." not in result.out
    assert services(fake_ha) == ["hold", "set_temperature", "hold", "set_temperature", "release"]
    assert thermostat.hold_calls[1][1] == 2 * 1200 + 300, "covers the restore it protects"
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_home_assistant_down_until_the_hold_ends_is_reported_as_ended(
    run_cave, fake_ha, clock, thermostat, faults
):
    """Home Assistant goes away after the check's target was confirmed and stays away: the
    restore gives up, and the hold, which cannot be released, is reported as ended once its time
    is over, not as left behind."""
    original = faults.fake_ha

    def down_after_the_input(method, url, headers, body, timeout):
        if url.endswith("/number/set_value"):
            faults.down = True
        return original(method, url, headers, body, timeout)

    faults.fake_ha = down_after_the_input
    thermostat.held = True

    result = run_cave("device-check", ENTITY, "--timeout", "60")

    assert result.code == 2
    assert "Home Assistant did not answer" in result.out
    assert rows(result.out)["original target 21 °C restored"] == "FAIL"
    assert "the controller brings it to the room's target once it runs" in result.out
    assert "it had ended by itself" in result.out
    assert "could not be released" not in result.out


# --- interruptions -------------------------------------------------------------------------


def _interrupt_on_first_sleep(ctx, clock, action):
    sleep = clock.sleep
    fired = []

    def sleep_once(seconds: float) -> None:
        if not fired:
            fired.append(seconds)
            action()
        sleep(seconds)

    ctx.sleep = sleep_once


def _keyboard_interrupt() -> None:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    "how",
    [
        "ctrl-c",
        pytest.param(
            "SIGTERM",
            marks=pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM"),
        ),
        pytest.param(
            "SIGHUP",
            marks=pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP"),
        ),
    ],
)
def test_interrupted_it_restores_releases_and_prints_the_table(
    run_cave, ctx, clock, fake_ha, thermostat, how
):
    """Ctrl-C, or SIGTERM / SIGHUP (a dropped SSH session) while waiting for the report: the
    clean-up runs, the table is printed, and the user is told how to abandon it."""
    if how == "ctrl-c":
        _interrupt_on_first_sleep(ctx, clock, _keyboard_interrupt)
    else:
        number = getattr(signal, how)
        _interrupt_on_first_sleep(ctx, clock, lambda: os.kill(os.getpid(), number))
    before = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP")}

    result = run_cave("device-check", ENTITY)

    assert result.code == 130
    assert (
        "Interrupted: putting the thermostat back to 21 °C. Interrupting again skips that; the "
        "hold is still released, and the controller then brings the thermostat to its room's "
        "target." in result.out
    )
    assert (
        f"The check could not complete: interrupted ({how if how != 'ctrl-c' else 'Ctrl-C'})."
        in (result.out)
    )
    assert "interrupted ()" not in result.out
    assert rows(result.out)["hold released"] == "pass"
    assert "original target 21 °C restored" in rows(result.out)
    assert thermostat.target == 21.0
    assert thermostat.release_calls
    assert not thermostat.held
    after = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP")}
    assert after == before, "the previous signal handlers are back"


def test_a_second_interrupt_abandons_the_restore_but_still_releases(
    run_cave, ctx, clock, fake_ha, thermostat
):
    sleep = clock.sleep
    count = []

    def interrupt_twice(seconds: float) -> None:
        count.append(seconds)
        if len(count) in (1, 2):
            raise KeyboardInterrupt
        sleep(seconds)

    ctx.sleep = interrupt_twice

    result = run_cave("device-check", ENTITY)

    assert result.code == 130
    assert rows(result.out)["clean-up"] == "skipped"
    assert "interrupted during the clean-up (Ctrl-C)" in result.out
    assert rows(result.out)["original target 21 °C restored"] == "FAIL"
    assert "not confirmed: sent; the controller brings it to its room's target" in result.out
    assert rows(result.out)["hold released"] == "pass"
    assert "Every check passed." not in result.out
    assert thermostat.release_calls


# --- review round 2 ------------------------------------------------------------------------


class HangingUp(io.StringIO):
    """A terminal that goes away: once ``hung_up`` is set, every write fails as on a real
    hung-up terminal after a dropped SSH session (EIO)."""

    hung_up = False

    def write(self, text: str) -> int:
        if self.hung_up:
            raise OSError(errno.EIO, "Input/output error")
        return super().write(text)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP")
def test_a_dropped_ssh_session_still_restores_and_releases(
    run_cave, ctx, clock, fake_ha, thermostat, monkeypatch
):
    """The SSH session drops while the check waits for the report: the terminal hangs up and
    SIGHUP arrives. Writing to the terminal now fails, which must not stop the clean-up: the
    original target is sent back and the hold released, silently."""
    terminal = HangingUp()

    def hang_up() -> None:
        terminal.hung_up = True
        os.kill(os.getpid(), signal.SIGHUP)

    _interrupt_on_first_sleep(ctx, clock, hang_up)
    original_say = ctx.say

    def say(message: str = "") -> None:
        ctx.out = terminal
        original_say(message)

    monkeypatch.setattr(ctx, "say", say)

    result = run_cave("device-check", ENTITY)

    assert result.code == 130
    assert ("climate", "set_temperature", {"entity_id": ENTITY, "temperature": 21.0}) in calls(
        fake_ha
    ), "the original target is sent back"
    assert thermostat.release_calls, "the hold is released"
    assert not thermostat.held
    assert thermostat.target == 21.0
    assert "Interrupted" not in terminal.getvalue(), "nothing could be written after the hang-up"


def test_the_interrupt_notice_without_a_hold(run_cave, ctx, clock, fake_runner, thermostat):
    """Without a hold (--not-in-heating-yaml) a second interrupt would leave the thermostat at
    the check's target, and the notice says so instead of promising a hold that does not exist."""
    answer(fake_runner, configured(found=False))
    _interrupt_on_first_sleep(ctx, clock, _keyboard_interrupt)

    result = run_cave("device-check", ENTITY, "--not-in-heating-yaml")

    assert result.code == 130
    assert (
        "Interrupted: putting the thermostat back to 21 °C. Interrupting again leaves it at "
        "21.5 °C." in result.out
    )
    assert "hold" not in result.out.split("Interrupted:", 1)[1].split("\n", 1)[0]
    assert thermostat.target == 21.0


def test_a_single_interrupt_during_the_late_report_watch(run_cave, ctx, clock, fake_ha, thermostat):
    """The thermostat did not report the check's target in time, so after restoring the check
    watches for a late report; one Ctrl-C there. The result says the clean-up was interrupted,
    whether the original target was sent, and that the check could not complete; the hold is
    released all the same."""
    thermostat.confirms = False
    sleep = clock.sleep

    def interrupt_in_the_watch(seconds: float) -> None:
        if clock.now > 300:
            ctx.sleep = sleep
            raise KeyboardInterrupt
        sleep(seconds)

    ctx.sleep = interrupt_in_the_watch

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    assert result.code == 130
    assert "Watching 300 s for a late report" in result.out
    assert rows(result.out)["clean-up"] == "skipped"
    assert "interrupted during the clean-up (Ctrl-C)" in result.out
    assert rows(result.out)["original target 21 °C restored"] == "FAIL"
    assert "not confirmed: sent; the controller brings it to its room's target" in result.out
    assert "The check could not complete: interrupted during the clean-up (Ctrl-C)." in result.out
    assert "Every check passed." not in result.out
    assert thermostat.release_calls
    assert not thermostat.held


def test_a_restart_before_cave_heating_has_loaded_is_waited_for(
    run_cave, fake_ha, thermostat, faults
):
    """Home Assistant restarts during the check and answers before cave_heating has loaded:
    taking the hold again gets 400 "Service not found" for a while. That is the restart, not a
    refusal: the check keeps trying within its patience, then restores and releases."""
    thermostat.lose_hold_on_set = True
    faults.fail[("cave_heating", "hold")] = [None, 400, 400, 400]

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "did Home Assistant restart?" in result.out
    assert rows(result.out)["original target 21 °C restored"] == "pass"
    assert rows(result.out)["hold released"] == "pass"
    assert "refused" not in result.out
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_a_failed_read_during_the_restore_is_not_a_refusal(
    run_cave, fake_ha, clock, thermostat, faults
):
    """One 500 on reading the thermostat while watching for a late report: the watch goes on
    and the restore is judged on the next reads."""
    thermostat.confirms = False
    faults.fail_reads[ENTITY] = []
    original = faults.fake_ha

    def arm_after_the_timeout(method, url, headers, body, timeout):
        if clock.now > 300 and not faults.fail_reads[ENTITY] and not getattr(faults, "_done", 0):
            faults.fail_reads[ENTITY] = [500]
            faults._done = 1
        return original(method, url, headers, body, timeout)

    faults.fake_ha = arm_after_the_timeout

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    assert getattr(faults, "_done", 0) == 1, "the failing read happened"
    assert rows(result.out)["original target 21 °C restored"] == "pass"
    assert "refused" not in result.out
    assert "no late report came" in result.out
    assert thermostat.release_calls


# --- issue #24 (work package 11) -----------------------------------------------------------


def test_a_restart_noticed_outside_a_hold_check_is_waited_for(
    run_cave, fake_ha, thermostat, faults
):
    """Issue #24, item 1: Home Assistant restarts during the input write, which fails as
    unreachable. The clean-up finds the hold gone, and the re-hold gets "Service not found" while
    cave_heating loads: that is the restart, not a refusal."""
    original = faults.fake_ha
    restarted = []

    def restart_at_the_input(method, url, headers, body, timeout):
        if url.endswith("/number/set_value") and not restarted:
            restarted.append(1)
            thermostat.held = False
            faults.fail[("cave_heating", "hold")] = [400, 400, 400]
            raise ConnectionRefusedError("connection refused")
        return original(method, url, headers, body, timeout)

    faults.fake_ha = restart_at_the_input

    result = run_cave("device-check", ENTITY)

    assert restarted
    assert result.code == 2
    assert "refused: Home Assistant answered 400" not in result.out
    assert rows(result.out)["original target 21 °C restored"] == "pass"
    assert rows(result.out)["hold released"] == "pass"
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_when_cave_heating_never_loads_the_target_is_put_back_without_a_hold(
    run_cave, fake_ha, clock, thermostat, faults
):
    """Issue #24, item 2: after a restart cave_heating does not load within the patience. The
    controller is not running, so the original target is sent without a hold at the end."""
    thermostat.lose_hold_on_set = True
    faults.fail[("cave_heating", "hold")] = [None] + [400] * 10_000

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    restores = [
        call for call in calls(fake_ha) if call[1] == "set_temperature" and call[2]["temperature"]
    ]
    assert restores[-1][2]["temperature"] == 21.0, "the original target was never sent"
    assert rows(result.out)["original target 21 °C restored"] == "pass"
    assert "without a hold: cave_heating had not loaded within 15 min" in result.out
    assert thermostat.target == 21.0
    assert clock.now >= device_check.CLEANUP_PATIENCE


def test_a_release_refused_while_cave_heating_reloads_is_tried_again(
    run_cave, fake_ha, thermostat, faults
):
    """Issue #24, item 3: after a lost hold the release is tried again while cave_heating's
    services are not found yet."""
    thermostat.lose_hold_on_set = True
    faults.fail[("cave_heating", "release")] = [400, 400]

    result = run_cave("device-check", ENTITY)

    assert rows(result.out)["hold released"] == "pass"
    assert len(thermostat.release_calls) == 1
    assert not thermostat.held
    assert "ends by itself" not in result.out


@pytest.mark.parametrize("entity", [PROBLEM, SENSOR, WINDOW, ENTITY])
def test_one_failed_reading_during_the_check_is_tried_again(
    run_cave, fake_ha, thermostat, faults, entity
):
    """Issue #24, item 4: one failed reading of the problem sensor, the room sensor, a setting or
    the thermostat during the check is not the end of the check."""
    faults.fail_reads[entity] = [500]

    result = run_cave("device-check", ENTITY)

    assert faults.fail_reads[entity] == [], "the failing reading happened"
    assert result.code == 0, result.out
    assert "Every check passed." in result.out


def test_a_reading_that_keeps_failing_still_ends_the_check(run_cave, fake_ha, thermostat, faults):
    faults.fail_reads[WINDOW] = [500] * 1000

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "The check could not complete" in result.out
    assert rows(result.out)["hold released"] == "pass"
