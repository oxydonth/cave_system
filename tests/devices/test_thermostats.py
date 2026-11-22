"""Every thermostat model through Home Assistant's real integrations, with cave_heating on top.

Each test states the part of concept section 5 it checks and asserts that, including how many
commands the device received: these devices run on two AA cells. TRV-ZBT, TRVZB and BTH-RA reach
Home Assistant through its MQTT integration fed by the Zigbee2MQTT stand-in; the FRITZ!Smart
Thermo 302 through its ``fritzbox`` integration and the fake FRITZ!Box.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.cave_heating.const import DOMAIN
from tests.devices.capabilities import devices
from tests.devices.rig import (
    CONTACT_ID,
    FRITZ_AIN,
    HOUR,
    MIN,
    MODELS,
    OFF,
    ROOM_CLIMATE,
    SEC,
    SENSOR_ID,
    Rig,
    heat,
)

pytestmark = pytest.mark.usefixtures("hass")


@pytest.mark.parametrize("device", devices("discovery"))
async def test_discovery(rig: Rig, device: str) -> None:
    """Discovery gives the entities the heating logic uses, with the model's range and step.

    Concept 4.1: entity IDs follow Zigbee2MQTT's scheme (and the FRITZ! integration's device
    name); concept 5.5: the step is ``target_temp_step``, or for FRITZ!, which has none, its
    precision.
    """
    model = MODELS[device]
    await rig.start(model, sensor=False, contact=False, heating=False)
    for entity_id in model.entities:
        state = rig.state(entity_id)
        # The Bosch's remote temperature has no value until the controller writes one; concept
        # 5.5 says such an input is written all the same.
        unset = ("unavailable",) if entity_id == model.external else ("unknown", "unavailable")
        assert state.state not in unset, entity_id
    attributes = rig.state(model.climate).attributes
    assert attributes["min_temp"] == model.min_temp
    assert attributes["max_temp"] == model.max_temp
    assert tuple(attributes["hvac_modes"]) == model.hvac_modes
    if model.step is None:
        assert "target_temp_step" not in attributes
        entry = er.async_get(rig.hass).async_get(model.climate)
        assert entry is not None
        assert entry.platform == "fritzbox", "the platform that makes it slow by default"
    else:
        assert attributes["target_temp_step"] == model.step


@pytest.mark.parametrize(("device", "case"), devices("rounding_to_the_step"))
async def test_rounding_to_the_step(rig: Rig, device: str, case: str) -> None:
    """Concepts 5.2 and 5.5: the room's target is in 0.5 K steps, and a target is rounded half up
    to the thermostat's step before it is sent and compared.

    Every recommended model has a 0.5 K step (the FRITZ! one through its precision, as it has no
    ``target_temp_step``), so a room target arrives unchanged; that is what ``room-step`` checks
    (22.5 °C must arrive as 22.5, neither 22 nor 23). Rounding to a step other than the room's
    can therefore not occur on these models; WP4's tests cover it with fake thermostats, and the
    Bosch's 0.2 K external input is rounded in ``feed_reaches_external_input``.

    ``boost-off-grid``: a boost to 22.25 °C through ``cave_heating.boost``. Concept 5.2 puts the
    room's target on the 0.5 K grid, so the room shows 22.5 (half up), and the thermostat gets
    22.5 with one command.
    """
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.advance(1 * MIN)
    rig.mark()
    if case == "room-step":
        await rig.set_comfort(22.5)
    else:
        await rig.boost(temperature=22.25)
    await rig.advance(5 * SEC)
    assert rig.room()[0] == 22.5
    assert rig.commands(model) == heat(model, 22.5)
    await rig.refresh_fritzbox()
    assert rig.state(model.climate).attributes["temperature"] == 22.5
    await rig.advance(30 * MIN)
    assert rig.commands(model) == heat(model, 22.5), "it matches: nothing more is sent"
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("clamping_to_the_range"))
async def test_clamping_to_the_range(rig: Rig, device: str) -> None:
    """Concept 5.5: a target is clamped to the thermostat's range before it is sent and before it
    is compared. Only the FRITZ! range (8 to 28 °C) is narrower than the room's 5 to 30 °C, so
    only there can a room target lie outside it: a boost to 30 °C is sent as 28, an eco of 5 °C as
    8, and once the thermostat has the clamped value nothing more is sent."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.advance(1 * MIN)
    rig.mark()
    await rig.boost(temperature=30.0)
    await rig.advance(5 * SEC)
    assert rig.room()[0] == 30.0
    assert rig.commands(model) == heat(model, model.max_temp)

    rig.mark()
    await rig.call(DOMAIN, "resume_schedule", entity_id=ROOM_CLIMATE)
    await rig.call("climate", "set_preset_mode", entity_id=ROOM_CLIMATE, preset_mode="eco")
    await rig.set_eco(5.0)
    await rig.advance(5 * SEC)
    assert rig.room()[0] == 5.0
    assert rig.commands(model) == heat(model, model.min_temp)
    await rig.advance(30 * MIN)
    assert rig.commands(model) == heat(model, model.min_temp), (
        "the clamped target matches: nothing more is sent"
    )
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("heat_off_and_back_to_heat"))
async def test_heat_off_and_back_to_heat(rig: Rig, device: str) -> None:
    """Concept 5.5, commands: heat is one set_temperature with the target, with hvac_mode only
    when the thermostat is not heating already; off is set_hvac_mode off.

    The device starts at 20 °C in heat; the room wants comfort, 21 °C.
    """
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    assert rig.commands(model) == heat(model, 21.0)
    assert rig.controller_calls(model.climate) == [("set_temperature", {"temperature": 21.0})]
    assert rig.state(model.climate).attributes["temperature"] == 21.0

    rig.mark()
    await rig.room_off()
    await rig.advance(5 * SEC)
    assert rig.commands(model) == OFF
    assert rig.state(model.climate).state == "off"

    rig.mark()
    await rig.room_on()
    await rig.advance(5 * SEC)
    assert rig.commands(model) == heat(model, 21.0, with_mode=True)
    assert rig.controller_calls(model.climate)[-1] == (
        "set_temperature",
        {"temperature": 21.0, "hvac_mode": "heat"},
    )
    assert rig.state(model.climate).state == "heat"
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    await rig.advance(30 * MIN)
    assert rig.commands(model) == heat(model, 21.0, with_mode=True), "nothing more is sent"
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("command_confirmed_by_report"))
async def test_command_confirmed_by_report(rig: Rig, device: str) -> None:
    """Concept 5.5, confirmation: pending until the device reports heat and the wanted target,
    then ok; a confirmed command is not resent."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    rig.mark()
    await rig.set_comfort(22.0)
    await rig.advance(2 * SEC + 0.5)
    assert rig.commands(model) == heat(model, 22.0)
    await rig.advance(1 * SEC)
    assert rig.state(model.climate).attributes["temperature"] == 22.0
    assert rig.status(model) == "ok"
    assert not rig.problem()
    await rig.advance(1 * HOUR)
    assert rig.commands(model) == heat(model, 22.0)


def _go_deaf(rig: Rig, model: Any) -> None:
    """The device stops answering commands but stays available.

    Zigbee: it falls silent (dead battery, out of range) and Zigbee2MQTT keeps it available for
    25 hours. FRITZ!: the box accepts the setpoint but reports it only after three hours, as if
    the thermostat were out of DECT range while the box still lists it present.
    """
    if model.zigbee:
        rig.sim.home.set_silent(model.device_id, True)
    else:
        rig.sim.home.configure_fritzbox(report_delay=3 * HOUR)


@pytest.mark.parametrize("device", devices("unanswered_command_resent_then_flagged"))
async def test_unanswered_command_resent_then_flagged(rig: Rig, device: str) -> None:
    """Concept 5.5: unconfirmed after confirm_timeout (5 min; 20 min for slow FRITZ!) the command
    is resent, at most resend_attempts (3) times; then the thermostat is unresponsive, the
    problem sensor turns on and a repair notice is raised; it is retried every retry_interval
    (30 min). When the device answers again it is confirmed and the notice goes."""
    model = MODELS[device]
    timeout = 5 * MIN if model.zigbee else 20 * MIN
    await rig.start(model, contact=False, sensor=False)
    await rig.advance(1 * MIN)
    _go_deaf(rig, model)
    rig.mark()
    await rig.set_comfort(22.0)
    await rig.advance(3 * SEC)
    assert rig.commands(model) == heat(model, 22.0)
    assert rig.status(model) == "pending"
    for sends in (2, 3, 4):
        await rig.advance(timeout)
        assert rig.commands(model) == heat(model, 22.0) * sends
        assert rig.status(model) == "pending"
    await rig.advance(timeout)
    assert rig.commands(model) == heat(model, 22.0) * 4, "three resends, then no more"
    assert rig.status(model) == "unresponsive"
    assert rig.problem()
    issues = ir.async_get(rig.hass)
    assert issues.async_get_issue(DOMAIN, f"unresponsive_{model.climate}") is not None
    await rig.advance(29 * MIN)
    assert len(rig.commands(model)) == 4 * len(heat(model, 22.0))
    await rig.advance(1 * MIN + 3 * SEC)
    assert rig.commands(model) == heat(model, 22.0) * 5, "retried after retry_interval"

    if model.zigbee:
        rig.sim.home.set_silent(model.device_id, False)
        await rig.advance(30 * MIN + 5 * SEC)
        assert rig.commands(model) == heat(model, 22.0) * 6, "the next retry reaches it"
    else:
        await rig.advance(2 * HOUR)  # the first setpoint arrives three hours after it was sent
    assert rig.state(model.climate).attributes["temperature"] == 22.0
    assert rig.status(model) == "ok"
    assert not rig.problem()
    assert issues.async_get_issue(DOMAIN, f"unresponsive_{model.climate}") is None


@pytest.mark.parametrize("device", devices("dial_becomes_room_override"))
async def test_dial_becomes_room_override(rig: Rig, device: str) -> None:
    """Concept 5.3 and 5.5: a dial turned on the device (after the start's exemption window) is a
    hand change; manual_settle (45 s) after it, its target is the room's override. The turned
    device itself receives nothing: it already has the target."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    await rig.device(lambda: rig.sim.home.turn_dial(model.device_id, 23.0))
    assert rig.state(model.climate).attributes["temperature"] == 23.0
    await rig.advance(40 * SEC)
    assert rig.room()[2] == "comfort", "still collecting within manual_settle"
    await rig.advance(10 * SEC)
    assert rig.room() == (23.0, "none", "override")
    assert rig.state(ROOM_CLIMATE).attributes["override_until"] is not None
    await rig.advance(10 * MIN)
    assert rig.commands(model) == []
    assert rig.status(model) == "ok"


@pytest.mark.parametrize(("device", "mode"), devices("mode_changed_on_device_is_put_back"))
async def test_mode_changed_on_device_is_put_back(rig: Rig, device: str, mode: str) -> None:
    """Concept 5.5: only the target is adopted; a thermostat that changes its mode on its own is
    put back with one command, and the room's target is unchanged.

    Sonoff: system_mode off or auto. Bosch: operating_mode pause (off) or schedule (auto).
    FRITZ!: the dial turned all the way down (mode off) or up (ON, no target); concept 5.6 says
    the controller undoes it.
    """
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    if model.zigbee:
        await rig.device(lambda: rig.sim.home.set_device_mode(model.device_id, mode))
    else:
        await rig.device(lambda: rig.sim.home.turn_dial(model.device_id, mode))
    await rig.advance(5 * SEC)
    assert rig.commands(model) == heat(model, 21.0, with_mode=model.zigbee)
    state = rig.state(model.climate)
    assert (state.state, state.attributes["temperature"]) == ("heat", 21.0)
    await rig.advance(2 * MIN)
    assert rig.room() == (21.0, "comfort", "comfort"), "nothing adopted"
    assert rig.commands(model) == heat(model, 21.0, with_mode=model.zigbee)
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("mode_never_set_to_heat_alone"))
async def test_mode_never_set_to_heat_alone(rig: Rig, device: str) -> None:
    """Concept 5.5: the mode is never set to heat on its own, because a FRITZ! thermostat would
    then apply the FRITZ!Box's own temperatures. Through the room switched off, a window pause,
    summer mode and the mode changed on the device, every climate call that asks for heat
    carries the target, and the device ends at the room's target.

    The room's comfort is 22.5 °C, so that it differs from the FRITZ!Box's own comfort
    temperature (21 °C) and eco temperature (16 °C): a FRITZ! thermostat switched to heat alone
    would end at one of those.
    """
    model = MODELS[device]
    await rig.start(model, sensor=False)
    await rig.set_comfort(22.5)
    await rig.past_start_window()
    rig.mark()
    await rig.room_off()
    await rig.advance(5 * SEC)
    await rig.room_on()
    await rig.advance(5 * SEC)
    rig.sim.home.set_window(CONTACT_ID, True)
    await rig.advance(2 * MIN)
    rig.sim.home.set_window(CONTACT_ID, False)
    await rig.advance(3 * MIN)
    await rig.home_mode("off")
    await rig.advance(5 * SEC)
    await rig.home_mode("auto")
    await rig.advance(5 * SEC)
    if model.zigbee:
        off = "pause" if model.key == "BTH-RA" else "off"
        await rig.device(lambda: rig.sim.home.set_device_mode(model.device_id, off))
    else:
        await rig.device(lambda: rig.sim.home.turn_dial(model.device_id, "off"))
    await rig.advance(1 * MIN)
    calls = rig.controller_calls(model.climate)
    assert [data for service, data in calls if service == "set_hvac_mode"] == [
        {"hvac_mode": "off"}
    ] * 3, "set_hvac_mode only ever switches off"
    heat_calls = [data for _, data in calls if data.get("hvac_mode") == "heat"]
    assert len(heat_calls) == 4, "after room off, the window, home mode off and the device"
    assert all(data.get("temperature") == 22.5 for data in heat_calls)
    assert rig.state(model.climate).attributes["temperature"] == 22.5
    on_device = {("target", 22.5), ("mode", "heat"), ("mode", "off")}
    assert set(rig.commands(model)) <= on_device, "never the device's own temperatures"
    if not model.zigbee:
        box = rig.sim.home.fritzbox.thermostats[FRITZ_AIN]
        assert {box.komfort / 2, box.absenk / 2}.isdisjoint({22.5})


@pytest.mark.parametrize("device", devices("offline_and_back"))
async def test_offline_and_back(rig: Rig, device: str) -> None:
    """Concept 5.4 and 5.5: an unavailable thermostat is skipped (nothing is sent), shows as
    unavailable on the problem sensor, and its room is unavailable when it was the only one. When
    it returns, its first report is not a hand change, even with a dial turned meanwhile: it is
    brought in line with one command.

    Zigbee: unavailable at once (Zigbee2MQTT's availability). FRITZ!: the box reports it absent.
    """
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    await rig.device(lambda: rig.sim.home.set_online(model.device_id, False))
    assert rig.state(model.climate).state == "unavailable"
    assert rig.status(model) == "unavailable"
    assert rig.problem()
    assert rig.state(ROOM_CLIMATE).state == "unavailable"
    await rig.set_comfort(22.0)
    await rig.advance(30 * MIN)
    assert rig.commands(model) == [], "nothing is sent while it is unavailable"

    rig.sim.home.turn_dial(model.device_id, 24.0)
    await rig.device(lambda: rig.sim.home.set_online(model.device_id, True))
    await rig.advance(5 * SEC)
    assert rig.commands(model) == heat(model, 22.0)
    assert rig.state(model.climate).attributes["temperature"] == 22.0
    await rig.advance(2 * MIN)
    assert rig.room() == (22.0, "comfort", "comfort"), "the dial turned while away is not adopted"
    assert rig.status(model) == "ok"
    assert not rig.problem()
    assert rig.commands(model) == heat(model, 22.0)


@pytest.mark.parametrize("device", devices("dial_turned_while_silent"))
async def test_dial_turned_while_silent(rig: Rig, device: str) -> None:
    """A Zigbee device that falls silent stays available for 25 hours (Zigbee2MQTT). A dial
    turned meanwhile is reported when it answers again. It never was unavailable, so no
    exemption window applies: by concept 5.5 the report is a hand change and becomes the room's
    override, and the device receives nothing."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    rig.sim.home.set_silent(model.device_id, True)
    rig.sim.home.turn_dial(model.device_id, 23.5)
    await rig.advance(1 * HOUR)
    assert rig.state(model.climate).attributes["temperature"] == 21.0, "not heard yet"
    assert rig.state(model.climate).state == "heat", "still available"
    rig.sim.home.set_silent(model.device_id, False)
    await rig.advance(50 * SEC)
    assert rig.room() == (23.5, "none", "override")
    assert rig.commands(model) == []
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("battery_level"))
async def test_battery_level(rig: Rig, device: str) -> None:
    """Concept 6: the maintenance view lists every battery sensor Home Assistant knows. Each
    device has one, and a new level reaches Home Assistant."""
    if device in MODELS:
        model = MODELS[device]
        await rig.start(model, sensor=False, contact=False, heating=False)
        device_id, entity_id = model.device_id, model.battery
    else:
        climate = device == "SNZB-02P"
        await rig.start(sensor=climate, contact=not climate, heating=False)
        device_id = SENSOR_ID if climate else CONTACT_ID
        entity_id = f"sensor.{device_id}_battery"
    assert float(rig.state(entity_id).state) > 50
    assert rig.state(entity_id).attributes["device_class"] == "battery"
    await rig.device(lambda: rig.sim.home.set_battery(device_id, 15, low=True))
    assert float(rig.state(entity_id).state) == 15


@pytest.mark.parametrize("device", devices("hold_for_device_check"))
async def test_hold_for_device_check(rig: Rig, device: str) -> None:
    """Concept 5.5, hold: the sequence ``cave device-check`` runs. While held, the controller
    sends the thermostat nothing and adopts nothing from it: a target half a degree higher, set
    directly on the thermostat, is neither corrected nor adopted, and a room change is not sent.
    Release brings it in line, and nothing the check did is adopted."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    await rig.hold(model, "00:45:00")
    assert rig.status(model) == "held"
    assert not rig.problem(), "a hold is a status, not a problem"
    await rig.call("climate", "set_temperature", entity_id=model.climate, temperature=21.5)
    await rig.refresh_fritzbox()
    assert rig.state(model.climate).attributes["temperature"] == 21.5
    await rig.set_comfort(22.0)
    await rig.advance(10 * MIN)
    assert rig.room() == (22.0, "comfort", "comfort"), "nothing adopted from a held thermostat"
    assert rig.state(model.climate).attributes["temperature"] == 21.5
    await rig.call("climate", "set_temperature", entity_id=model.climate, temperature=21.0)
    await rig.refresh_fritzbox()
    own = heat(model, 21.5) + heat(model, 21.0)
    assert rig.commands(model) == own, "only the check's own commands"

    await rig.release(model)
    await rig.advance(5 * SEC)
    assert rig.commands(model) == [*own, *heat(model, 22.0)]
    await rig.advance(10 * MIN)
    assert rig.room() == (22.0, "comfort", "comfort")
    assert rig.status(model) == "ok"
    assert rig.commands(model) == [*own, *heat(model, 22.0)]


@pytest.mark.parametrize("device", devices("hold_ends_by_itself"))
async def test_hold_ends_by_itself(rig: Rig, device: str) -> None:
    """Concept 5.5, hold: it ends by itself after its duration, so a check that is killed cannot
    leave a thermostat outside the controller. A check killed with the thermostat at +0.5 K: when
    the ten-minute hold ends, the thermostat is brought back with one command, and its target is
    not adopted (no report within confirm_timeout after a hold ends is a hand change)."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.past_start_window()
    rig.mark()
    await rig.hold(model, "00:10:00")
    await rig.call("climate", "set_temperature", entity_id=model.climate, temperature=21.5)
    await rig.refresh_fritzbox()
    await rig.advance(9 * MIN + 50 * SEC)
    assert rig.status(model) == "held"
    assert rig.commands(model) == heat(model, 21.5), "only the killed check's command"
    await rig.advance(15 * SEC)
    assert rig.status(model) != "held"
    await rig.advance(1 * MIN)
    assert rig.commands(model) == heat(model, 21.5) + heat(model, 21.0)
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    assert rig.room() == (21.0, "comfort", "comfort"), "the check's target is not adopted"
    assert rig.status(model) == "ok"


@pytest.mark.parametrize("device", devices("late_report_after_release_not_adopted"))
async def test_late_report_after_release_not_adopted(rig: Rig, device: str) -> None:
    """Concept 5.5: for confirm_timeout after a hold is released, no report from the thermostat
    is a hand change. A FRITZ!Box that reports a setpoint three minutes after it was sent: the
    check's +0.5 K is sent, the hold released before the box reports it; when the report comes it
    is corrected with one command, and the room takes no override."""
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False, fritzbox={"report_delay": 180})
    await rig.advance(4 * MIN)
    await rig.past_start_window()
    assert rig.status(model) == "ok"
    rig.mark()
    await rig.hold(model)
    await rig.call("climate", "set_temperature", entity_id=model.climate, temperature=21.5)
    await rig.release(model)
    await rig.advance(3 * MIN + 35 * SEC)
    assert rig.state(model.climate).attributes["temperature"] == 21.5, "the late report"
    await rig.advance(1 * MIN)
    assert rig.room() == (21.0, "comfort", "comfort"), "not taken for a dial turn"
    assert rig.commands(model) == heat(model, 21.5) + heat(model, 21.0)
    await rig.advance(5 * MIN)
    assert rig.state(model.climate).attributes["temperature"] == 21.0
    assert rig.room() == (21.0, "comfort", "comfort")
    assert rig.status(model) == "ok"
