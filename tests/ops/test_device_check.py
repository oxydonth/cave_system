"""``cave device-check`` against a fake Home Assistant (WP7).

The thermostat here is a small model of what Home Assistant's REST API shows for one: it takes
``climate.set_temperature`` and reports the target after a delay of simulated time (or never),
and ``cave_heating.hold`` / ``release`` change what the room's problem sensor shows for it. The
fake clock of ``ctx`` advances when the command sleeps, so nothing waits for real.

In every case the original target must be back and the hold released at the end.
"""

from __future__ import annotations

import json
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


def configured(
    *, external: str | None = INPUT, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What the container script prints for a thermostat in the applied heating.yaml."""
    return {
        "thermostat": {
            "room": "bedroom",
            "external_temperature": external,
            "settings": {WINDOW: "off"} if settings is None else settings,
            "temperature_sensor": SENSOR,
        }
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
            SENSOR: {"state": "19.4", "attributes": {}},
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


@pytest.fixture
def thermostat(fake_ha, clock, ctx, write_token, fake_runner):
    """A checkout that is initialised, a token, and the thermostat configured in the file."""
    ctx.paths.env_file.write_text("", encoding="utf-8")
    write_token()
    answer(fake_runner, configured())
    return FakeThermostat(fake_ha, clock)


def answer(fake_runner, payload: Any) -> None:
    fake_runner.on(CONFIG_RUN, out="noise\n" + device_check.MARKER + json.dumps(payload) + "\n")


def calls(fake_ha) -> list[tuple[str, str, dict[str, Any]]]:
    return [call for call in fake_ha.service_calls if call[0] != "persistent_notification"]


def rows(out: str) -> dict[str, str]:
    """The result table: check -> result."""
    table = {}
    for line in out.splitlines():
        for result in ("pass", "FAIL", "skipped"):
            marker = f"  {result} "
            if marker in line + " ":
                table[line.split(marker)[0].strip()] = result
    return table


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
    assert table[f"setting {WINDOW} can be off"] == "pass"
    assert table["original target 21 °C restored"] == "pass"
    assert table["hold released"] == "pass"
    timing = next(line for line in result.out.splitlines() if "reports it back" in line)
    assert "6.0 s" in timing, "how long the thermostat took to report the target"
    assert thermostat.target == 21.0
    assert not thermostat.held
    # The configuration is read with the integration's own loader in a one-off container.
    (run,) = fake_runner.matching(*CONFIG_RUN)
    assert "--workdir" in run.args
    assert "/config" in run.args
    assert run.args[-2:] == (ENTITY, "/config/cave/.applied/heating.yaml")


def test_a_device_that_never_confirms(run_cave, fake_ha, clock, thermostat):
    thermostat.confirms = False

    result = run_cave("device-check", ENTITY, "--timeout", "300")

    assert result.code == 1
    assert "The check failed." in result.out
    table = rows(result.out)
    assert table["accepts 21.5 °C and reports it back"] == "FAIL"
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
    assert not [call for call in calls(fake_ha) if call[0] == "climate"], "nothing was sent"
    assert [call[1] for call in calls(fake_ha)] == ["hold", "release"]
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_a_restart_during_the_check_cannot_complete_and_cleans_up(run_cave, fake_ha, thermostat):
    """The hold is not kept across a restart: the check says it could not complete (not that the
    device failed), holds the thermostat again, restores the target and releases."""
    thermostat.lose_hold_on_set = True

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "The check could not complete" in result.out
    assert "did Home Assistant restart?" in result.out
    assert "The check failed." not in result.out
    assert [(call[0], call[1]) for call in calls(fake_ha)] == [
        ("cave_heating", "hold"),
        ("climate", "set_temperature"),
        ("cave_heating", "hold"),
        ("climate", "set_temperature"),
        ("cave_heating", "release"),
    ]
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_a_thermostat_that_is_off_cannot_be_checked_and_is_left_alone(
    run_cave, fake_ha, thermostat
):
    thermostat.mode = "off"

    result = run_cave("device-check", ENTITY)

    assert result.code == 2
    assert "run the check while its room heats" in result.out
    assert [call[1] for call in calls(fake_ha)] == ["hold", "release"]


def test_a_thermostat_not_in_the_file_is_checked_without_a_hold(
    run_cave, fake_ha, fake_runner, thermostat
):
    answer(fake_runner, {"thermostat": None})

    result = run_cave("device-check", ENTITY)

    assert result.code == 0, result.out
    assert rows(result.out)["held for the check"] == "skipped"
    assert [call[1] for call in calls(fake_ha)] == ["set_temperature", "set_temperature"]
    assert thermostat.target == 21.0


def test_a_setting_without_the_configured_option_fails(run_cave, fake_ha, fake_runner, thermostat):
    answer(fake_runner, configured(external=None, settings={SELECT: "external"}))

    result = run_cave("device-check", ENTITY)

    assert result.code == 1
    assert rows(result.out)[f"setting {SELECT} can be external"] == "FAIL"
    assert "options local_temperature, remote_temperature" in result.out
    assert thermostat.target == 21.0
    assert not thermostat.held


def test_interrupted_it_still_restores_and_releases(run_cave, ctx, clock, thermostat):
    """Ctrl-C while waiting for the report: the cleanup runs before the command exits."""
    sleep = clock.sleep
    interrupted = []

    def interrupt_once(seconds: float) -> None:
        if not interrupted:
            interrupted.append(seconds)
            raise KeyboardInterrupt
        sleep(seconds)

    ctx.sleep = interrupt_once
    result = run_cave("device-check", ENTITY)

    assert result.code == 130
    assert thermostat.target == 21.0
    assert thermostat.release_calls
    assert not thermostat.held


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
