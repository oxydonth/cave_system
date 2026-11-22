"""Model-specific behaviour: the Sonoff and Bosch inputs and settings, the FRITZ!Box's quirks.

Several of these are behaviours concept section 12 lists as unconfirmed on real hardware. The
simulators can play each possibility, and each test says which one it covers:

* whether a Sonoff goes back to the external temperature by itself when the feed resumes
  (``returns_on_feed``): both;
* whether the FRITZ!Box reports a new setpoint at once or only after the valve has it: both, with
  the delays of the 302 (about 3 minutes) and the older 301 (up to 15);
* what the FRITZ!Box answers to a command for a locked thermostat that Home Assistant did not yet
  know was locked: an HTTP error, or acceptance without effect;
* what the FRITZ!Box reports during its own window detection: the target unchanged, or OFF.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.devices.capabilities import devices
from tests.devices.rig import (
    FRITZ_302,
    FRITZ_AIN,
    HOUR,
    MIN,
    MODELS,
    ROOM,
    SEC,
    SENSOR_ID,
    TEMPERATURE,
    Rig,
    ThermostatModel,
    heat,
)

pytestmark = pytest.mark.usefixtures("hass")


def half_up(value: float, step: float) -> float:
    """``value`` rounded half up to ``step``."""
    exact = Decimal(str(value)) / Decimal(str(step))
    return float(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP) * Decimal(str(step)))


def _input(rig: Rig, model: ThermostatModel) -> tuple[str, float]:
    """The external input's Zigbee2MQTT property, and its step as Home Assistant shows it."""
    assert model.external is not None
    prop = model.external.removeprefix(f"number.{model.device_id}_")
    return prop, float(rig.state(model.external).attributes["step"])


def _sensor(rig: Rig) -> float:
    return float(rig.state(TEMPERATURE).state)


def _regulates_on(rig: Rig, model: ThermostatModel) -> float:
    return float(rig.sim.home.device_state(model.device_id)["regulation_temperature"])


# --- the temperature feed -------------------------------------------------------------------


@pytest.mark.parametrize("device", devices("feed_reaches_external_input"))
async def test_feed_reaches_external_input(rig: Rig, device: str) -> None:
    """Concept 5.5, room temperature to the thermostat: the room sensor's reading is written to
    the external input at once when the feed starts, rounded to the input's step; a change of
    feed_threshold (0.2 K) is written once feed_min_interval (10 min) has passed since the last
    write, and not before. The device then regulates on the room's temperature."""
    model = MODELS[device]
    await rig.start(model, contact=False)
    prop, step = _input(rig, model)
    first = half_up(_sensor(rig), step)
    assert rig.writes(model.name, prop) == [first]
    assert float(rig.state(model.external).state) == first
    assert _regulates_on(rig, model) == pytest.approx(first)

    rig.mark()
    rig.sim.home.set_room_temperature(ROOM, 20.5)
    await rig.advance(15 * SEC)  # the SNZB-02P reports at most every 10 s
    assert _sensor(rig) == 20.5
    await rig.advance(9 * MIN)
    assert rig.writes(model.name, prop) == [], "not before feed_min_interval"
    await rig.advance(1 * MIN)
    assert rig.writes(model.name, prop) == [half_up(20.5, step)]
    assert _regulates_on(rig, model) == pytest.approx(half_up(20.5, step))


@pytest.mark.parametrize("device", devices("feed_at_least_every_30_minutes"))
async def test_feed_at_least_every_30_minutes(rig: Rig, device: str) -> None:
    """Concept 5.5: the reading is written when it has changed by feed_threshold (0.2 K) and
    feed_min_interval (10 min) has passed, and after feed_max_interval (25 min) in any case,
    because a Bosch falls back to its own sensor after 30 minutes without a value, and a Sonoff
    after two hours.

    In a steady room (19 °C inside and out, comfort 19 °C, so no valve opens and nothing
    changes) the reading never changes, so over three hours the input is written exactly every
    25 minutes: at the start and then 7 times, no more, and the device never falls back."""
    model = MODELS[device]

    async def steady(rig: Rig) -> None:
        rig.sim.home.set_outdoor_temperature(19.0)

    await rig.start(model, contact=False, room={"comfort_temperature": 19.0}, before_heating=steady)
    prop, _ = _input(rig, model)
    assert len(rig.writes(model.name, prop)) == 1, "at once when the feed starts"
    writes_at: list[int] = []
    seen = 1
    for minute in range(1, 181):
        await rig.advance(1 * MIN)
        count = len(rig.writes(model.name, prop))
        assert count - seen <= 1, "never two writes within a minute"
        if count > seen:
            writes_at.append(minute)
        seen = count
        state = rig.sim.home.device_state(model.device_id)
        received = state.get("remote_received") or state.get("external_received")
        assert received is not None
        age = rig.now().timestamp() - received
        assert age < 30 * MIN, f"the device has had no value for {age / 60:.0f} minutes"
        if model.key != "BTH-RA":
            assert state["state"]["temperature_sensor_select"] == "remote_temperature"
    assert float(rig.state(TEMPERATURE).state) == 19.0, "the room stayed steady"
    assert writes_at == [25, 50, 75, 100, 125, 150, 175]
    assert set(rig.writes(model.name, prop)) == {19.0}


@pytest.mark.parametrize("device", devices("feed_stops_while_the_room_is_off"))
async def test_feed_stops_while_the_room_is_off(rig: Rig, device: str) -> None:
    """Concept 5.5: the feed stops while the room is switched off (the thermostat falls back to
    its own sensor), and when it starts again it writes at once, because the last write was more
    than feed_min_interval ago. Exactly one write then, and no device setting is written again,
    since the settings still match five minutes after the feed resumed."""
    model = MODELS[device]
    await rig.start(model, contact=False)
    prop, _ = _input(rig, model)
    await rig.advance(1 * MIN)
    rig.mark()
    await rig.room_off()
    await rig.advance(1 * HOUR)
    assert rig.writes(model.name, prop) == [], "nothing while the room is off"
    await rig.room_on()
    await rig.advance(3 * SEC)
    assert rig.writes(model.name, prop) == [half_up(_sensor(rig), _input(rig, model)[1])]
    await rig.advance(6 * MIN)
    assert len(rig.writes(model.name, prop)) == 1
    for setting in model.settings:
        name = setting.split(".", 1)[1].removeprefix(f"{model.device_id}_")
        assert rig.writes(model.name, name) == [], setting


# --- Sonoff sensor selection ----------------------------------------------------------------


async def _feed_stops_and_sonoff_falls_back(rig: Rig, model: ThermostatModel) -> None:
    """The room sensor goes unavailable: the feed stops, and two hours after its last value the
    Sonoff switches its sensor selection to remote_source_offline by itself."""
    select = f"select.{model.device_id}_temperature_sensor_select"
    await rig.device(lambda: rig.sim.home.set_online(SENSOR_ID, False))
    assert rig.state(TEMPERATURE).state == "unavailable"
    await rig.advance(2 * HOUR + 5 * MIN)
    assert rig.state(select).state == "remote_source_offline"


@pytest.mark.parametrize("device", devices("sensor_selection_set_once_and_left_alone"))
async def test_sensor_selection_set_once_and_left_alone(rig: Rig, device: str) -> None:
    """Concept 5.5, device settings: the sensor selection is written once when the configuration
    is loaded. Settings are not watched in between, so when the Sonoff switches to its own
    sensor by itself after the feed stops, it is not fought. This Sonoff goes back by itself when
    values arrive again (``returns_on_feed``, observed on a TRVZB, zigbee-herdsman-converters PR
    8873): confirm_timeout after the feed resumes the setting matches, and nothing is written."""
    model = MODELS[device]
    select = f"select.{model.device_id}_temperature_sensor_select"
    await rig.start(model, contact=False)
    assert rig.writes(model.name, "temperature_sensor_select") == ["remote_temperature"]
    assert rig.state(select).state == "remote_temperature"

    rig.mark()
    prop, _ = _input(rig, model)
    await _feed_stops_and_sonoff_falls_back(rig, model)
    await rig.advance(1 * HOUR)
    assert rig.writes(model.name, "temperature_sensor_select") == [], "left alone"
    assert rig.writes(model.name, prop) == [], "the feed stopped with the sensor"

    await rig.device(lambda: rig.sim.home.set_online(SENSOR_ID, True))
    await rig.advance(3 * SEC)
    assert len(rig.writes(model.name, prop)) == 1, "the feed writes at once when it resumes"
    assert rig.state(select).state == "remote_temperature", "the device went back by itself"
    await rig.advance(10 * MIN)
    assert rig.writes(model.name, "temperature_sensor_select") == []


@pytest.mark.parametrize("device", devices("sensor_selection_written_again_after_feed_resumes"))
async def test_sensor_selection_written_again_after_feed_resumes(rig: Rig, device: str) -> None:
    """Concept 5.5 and 12: settings are written again confirm_timeout (5 min) after the feed has
    started again, which covers a Sonoff that does not go back to the external temperature by
    itself (``returns_on_feed`` off). Exactly one write, not before the five minutes."""
    model = MODELS[device]
    select = f"select.{model.device_id}_temperature_sensor_select"
    await rig.start(model, contact=False)
    rig.sim.home.set_returns_on_feed(model.device_id, False)
    rig.mark()
    await _feed_stops_and_sonoff_falls_back(rig, model)
    await rig.device(lambda: rig.sim.home.set_online(SENSOR_ID, True))
    await rig.advance(4 * MIN + 50 * SEC)
    assert rig.state(select).state == "remote_source_offline"
    assert rig.writes(model.name, "temperature_sensor_select") == []
    await rig.advance(15 * SEC)
    assert rig.writes(model.name, "temperature_sensor_select") == ["remote_temperature"]
    assert rig.state(select).state == "remote_temperature"
    await rig.advance(1 * MIN)
    assert _regulates_on(rig, model) == pytest.approx(
        float(rig.state(model.external).state)  # type: ignore[arg-type]
    ), "regulating on the room's temperature again"
    await rig.advance(1 * HOUR)
    assert rig.writes(model.name, "temperature_sensor_select") == ["remote_temperature"]


# --- on-device window detection -------------------------------------------------------------


@pytest.mark.parametrize("device", devices("window_detection_switched_off"))
async def test_window_detection_switched_off(rig: Rig, device: str) -> None:
    """Concepts 5.5 and 5.6: the window detection switch is written off once when the
    configuration is loaded. A device that switches it on by itself is left alone (settings are
    not watched), and it is written off once more when the device returns from being
    unavailable, one of the three moments.

    The Sonoffs ship with it on; the Bosch ships with it off, so it is switched on through Home
    Assistant first.
    """
    model = MODELS[device]
    switch = next(entity for entity in model.settings if entity.startswith("switch."))
    prop = switch.removeprefix(f"switch.{model.device_id}_")

    async def switch_it_on(rig: Rig) -> None:
        if rig.state(switch).state == "off":
            await rig.call("switch", "turn_on", entity_id=switch)
        assert rig.state(switch).state == "on"

    await rig.start(model, contact=False, sensor=False, before_heating=switch_it_on)
    assert rig.state(switch).state == "off"
    assert rig.registers(model.device_id)[prop] == "OFF"
    rig.mark()

    device_object = rig.sim.home.zigbee.devices[model.name]
    device_object.report({prop: "ON"})  # the device switches it on by itself
    await rig.advance(1 * HOUR)
    assert rig.state(switch).state == "on"
    assert rig.writes(model.name, prop) == [], "not watched, not fought"

    await rig.device(lambda: rig.sim.home.set_online(model.device_id, False))
    await rig.advance(1 * MIN)
    await rig.device(lambda: rig.sim.home.set_online(model.device_id, True))
    await rig.advance(3 * SEC)
    assert rig.writes(model.name, prop) == ["OFF"]
    assert rig.state(switch).state == "off"


# --- FRITZ! ----------------------------------------------------------------------------------

_DELAY_SECONDS = {"at-once": 0.0, "after-3-min": 180.0, "after-15-min": 900.0}


@pytest.mark.parametrize(("device", "behaviour"), devices("fritz_reporting_behaviours"))
async def test_fritz_reporting_behaviours(rig: Rig, device: str, behaviour: str) -> None:
    """Concept 3.1 and 12: a new setpoint reaches a FRITZ! valve after about 3 minutes (up to 15
    on the 301), and whether the box reports it before then is unconfirmed. A FRITZ! thermostat
    is slow (confirm_timeout_slow, 20 min): whether the report comes at once or only after the
    delay, the command is sent once, waits as pending (not a problem), and is confirmed. The
    second command is sent after the start's five-minute exemption window, so that its report is
    not a hand change because of the queue of sent targets, not because of the window."""
    delay = _DELAY_SECONDS[behaviour]
    await rig.start(FRITZ_302, contact=False, sensor=False, fritzbox={"report_delay": delay})
    assert rig.commands(FRITZ_302) == heat(FRITZ_302, 21.0)
    if delay:
        assert rig.status(FRITZ_302) == "pending"
        assert not rig.problem()
        await rig.advance(delay - 10 * SEC)
        assert rig.state(FRITZ_302.climate).attributes["temperature"] == 20.0
    await rig.advance(40 * SEC)
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 21.0
    assert rig.status(FRITZ_302) == "ok"
    await rig.past_start_window()

    rig.mark()
    await rig.set_comfort(22.0)
    await rig.advance(3 * SEC)
    if delay:
        assert rig.status(FRITZ_302) == "pending"
    await rig.advance(delay + 40 * SEC)
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 22.0
    await rig.advance(1 * HOUR)
    assert rig.commands(FRITZ_302) == heat(FRITZ_302, 22.0)
    assert rig.room() == (22.0, "comfort", "comfort")
    assert rig.status(FRITZ_302) == "ok"


@pytest.mark.parametrize("device", devices("late_report_while_a_later_command_waits"))
async def test_late_report_while_a_later_command_waits(rig: Rig, device: str) -> None:
    """Concept 5.5: a report that removes entries from the queue of sent targets is not a hand
    change, so a late report of an earlier command never counts. With a three-minute delay, 22 °C
    is sent, then 23 °C two minutes later; the box reports 22 while 23 waits. No override, no
    extra command, and the thermostat ends at 23."""
    await rig.start(FRITZ_302, contact=False, sensor=False, fritzbox={"report_delay": 180})
    await rig.advance(4 * MIN)
    await rig.past_start_window()
    rig.mark()
    await rig.set_comfort(22.0)
    await rig.advance(2 * MIN)
    await rig.set_comfort(23.0)
    await rig.advance(1 * MIN + 50 * SEC)
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 22.0, "the late report"
    assert rig.status(FRITZ_302) == "pending"
    await rig.advance(1 * MIN)
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 22.0
    assert rig.room() == (23.0, "comfort", "comfort"), "not taken for a dial turn"
    await rig.advance(1 * MIN)
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 23.0
    assert rig.status(FRITZ_302) == "ok"
    await rig.advance(30 * MIN)
    assert rig.commands(FRITZ_302) == heat(FRITZ_302, 22.0) + heat(FRITZ_302, 23.0)
    assert rig.room() == (23.0, "comfort", "comfort")


async def _until_blocked(rig: Rig, case: str) -> tuple[int, int]:
    """Make the FRITZ! thermostat blocked the way ``case`` says, and check that it is.

    Home Assistant refuses by the lock it last polled. In the up to 30 s before the next poll
    the command reaches the box, whose answer is unconfirmed: an HTTP error (refused), or
    acceptance without effect (ignored), which shows as an unconfirmed command until the poll
    makes Home Assistant refuse the resend. Returns how many commands reached the box and how
    many climate calls the controller had made before.
    """
    await rig.start(FRITZ_302, contact=False, sensor=False)
    home = rig.sim.home
    await rig.advance(1 * MIN)
    rig.mark()
    calls_before = len(rig.controller_calls(FRITZ_302.climate))
    if case in ("summer-mode", "holiday-mode"):
        # Home Assistant shows summer mode as off, and in holiday mode no target at all (the
        # entity then supports presets only): either differs from what is wanted, so the
        # controller tries to put it back, and Home Assistant refuses.
        await rig.device(lambda: home.configure_fritzbox(**{case.split("-")[0]: True}))
        await rig.advance(3 * SEC)
        attributes = rig.state(FRITZ_302.climate).attributes
        assert attributes["preset_mode"] == case.split("-")[0]
        assert attributes.get("temperature") is None
    elif case == "key-lock":
        await rig.device(lambda: home.set_fritz_lock(FRITZ_302.device_id, lock=True))
        await rig.advance(10 * MIN)
        assert rig.status(FRITZ_302) == "ok", "it matches: nothing to send"
        await rig.set_comfort(22.0)
        await rig.advance(3 * SEC)
    else:
        answer = "refuse" if case.endswith("refused") else "ignore"
        home.configure_fritzbox(locked_command=answer)
        home.set_fritz_lock(FRITZ_302.device_id, lock=True)  # not yet polled
        await rig.set_comfort(22.0)
        await rig.advance(3 * SEC)
        assert rig.commands(FRITZ_302) == heat(FRITZ_302, 22.0), "it reached the box"
        assert rig.fritz_answers()[-1] == (400 if answer == "refuse" else 200)
        if answer == "ignore":
            assert rig.status(FRITZ_302) == "pending"
            await rig.advance(20 * MIN)
    assert rig.status(FRITZ_302) == "blocked"
    assert rig.problem()
    assert any(FRITZ_302.climate in message for message in rig.messages())
    sent = len(rig.commands(FRITZ_302))
    assert sent == (1 if case.startswith("lock-not-yet-polled") else 0)
    box = rig.sim.home.fritzbox.thermostats[FRITZ_AIN]
    assert box.device_tsoll == 42, "the box kept 21 °C"
    return sent, calls_before


@pytest.mark.parametrize(("device", "case"), devices("refused_command_makes_blocked"))
async def test_refused_command_makes_blocked(rig: Rig, device: str, case: str) -> None:
    """Concept 5.5, refused commands: if Home Assistant rejects a command, as for a FRITZ!
    thermostat in summer mode, holiday mode or key lock, the thermostat is blocked and the
    problem sensor says why. A change of its mode or target is the trigger to try again; once the
    box accepts commands again, it is brought in line."""
    sent, _ = await _until_blocked(rig, case)
    home = rig.sim.home
    # Released. Summer and holiday mode: the mode and target the box reports change back,
    # which is the trigger to retry at once, and 21 °C matches, so nothing needs sending. The
    # key lock: nothing the thermostat reports changes, so the retry waits for retry_interval.
    if case in ("summer-mode", "holiday-mode"):
        await rig.device(lambda: home.configure_fritzbox(**{case.split("-")[0]: False}))
        await rig.advance(5 * SEC)
        wanted, expected = 21.0, []
    else:
        await rig.device(lambda: home.set_fritz_lock(FRITZ_302.device_id, lock=False))
        await rig.advance(5 * SEC)
        assert rig.status(FRITZ_302) == "blocked"
        await rig.advance(30 * MIN)
        wanted, expected = 22.0, heat(FRITZ_302, 22.0)
    assert rig.status(FRITZ_302) == "ok"
    assert not rig.problem()
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == wanted
    assert rig.commands(FRITZ_302)[sent:] == expected


@pytest.mark.parametrize(("device", "case"), devices("blocked_not_retried_before_retry_interval"))
async def test_blocked_not_retried_before_retry_interval(rig: Rig, device: str, case: str) -> None:
    """Concept 5.5: a blocked thermostat is not resent into; while nothing it reports changes
    (its measured temperature does not count), it is tried again only after retry_interval
    (30 min), and Home Assistant refuses that try again before it reaches the box."""
    sent, calls_before = await _until_blocked(rig, case)
    tries = len(rig.controller_calls(FRITZ_302.climate)) - calls_before
    await rig.advance(29 * MIN)
    assert len(rig.controller_calls(FRITZ_302.climate)) - calls_before == tries, "no resend"
    assert rig.status(FRITZ_302) == "blocked"
    await rig.advance(1 * MIN + 5 * SEC)
    assert len(rig.controller_calls(FRITZ_302.climate)) - calls_before == tries + 1, (
        "tried again after retry_interval, and refused again"
    )
    assert rig.status(FRITZ_302) == "blocked"
    assert len(rig.commands(FRITZ_302)) == sent, "Home Assistant refused it before the box"


@pytest.mark.parametrize(("device", "report"), devices("fritz_window_detection"))
async def test_fritz_window_detection(rig: Rig, device: str, report: str) -> None:
    """Concept 5.6: a FRITZ! thermostat's own window detection may stay on. What the box reports
    meanwhile is unconfirmed (concept 12).

    Target unchanged: nothing differs, so nothing is sent, and the room's window pause (which
    comes from contacts only) does not start. Target OFF: the thermostat reports the mode off by
    itself, which concept 5.5 puts back: one command, which the box accepts but cannot show until
    its window mode ends after 15 minutes, shorter than confirm_timeout_slow, so it is not resent.
    In both cases nothing is adopted and the problem sensor stays off.
    """
    target = "off" if report == "target-off" else "unchanged"
    await rig.start(FRITZ_302, contact=False, sensor=False, fritzbox={"window_target": target})
    await rig.past_start_window()
    rig.mark()
    await rig.device(lambda: rig.sim.home.detect_open_window(FRITZ_302.device_id, 15))
    assert rig.state("binary_sensor.bad_heizung_open_window_detected").state == "on"
    state = rig.state(FRITZ_302.climate)
    await rig.advance(5 * SEC)
    if target == "unchanged":
        assert (state.state, state.attributes["temperature"]) == ("heat", 21.0)
        expected: list[tuple[str, float]] = []
    else:
        assert state.state == "off"
        expected = heat(FRITZ_302, 21.0)
        assert rig.status(FRITZ_302) == "pending"
    assert rig.commands(FRITZ_302) == expected
    assert rig.room() == (21.0, "comfort", "comfort")
    assert rig.state(f"binary_sensor.cave_{ROOM}_window_open").state == "off"
    await rig.advance(16 * MIN)
    assert rig.state("binary_sensor.bad_heizung_open_window_detected").state == "off"
    assert rig.state(FRITZ_302.climate).state == "heat"
    assert rig.state(FRITZ_302.climate).attributes["temperature"] == 21.0
    await rig.advance(30 * MIN)
    assert rig.commands(FRITZ_302) == expected
    assert rig.status(FRITZ_302) == "ok"
    assert not rig.problem()
