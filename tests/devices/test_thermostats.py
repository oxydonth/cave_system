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
    HOUR,
    MIN,
    MODELS,
    OFF,
    ROOM_CLIMATE,
    SEC,
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


@pytest.mark.parametrize("device", devices("rounding_and_clamping"))
async def test_rounding_and_clamping(rig: Rig, device: str) -> None:
    """Concept 5.5: the target is rounded half up to the step and clamped to the range.

    A boost to 22.25 °C must reach every model as 22.5 (half up, not to even); a boost to 30 °C
    reaches each model at its maximum at most; an eco of 5 °C at its minimum at least. A clamped
    target is compared as such: once the device has it, nothing more is sent.
    """
    model = MODELS[device]
    await rig.start(model, contact=False, sensor=False)
    await rig.advance(1 * MIN)
    rig.mark()
    await rig.boost(temperature=22.25)
    await rig.advance(5 * SEC)
    assert rig.commands(model) == heat(model, 22.5)

    rig.mark()
    await rig.boost(temperature=30.0)
    await rig.advance(5 * SEC)
    assert rig.commands(model) == heat(model, min(30.0, model.max_temp))

    rig.mark()
    await rig.call(DOMAIN, "resume_schedule", entity_id=ROOM_CLIMATE)
    await rig.call("climate", "set_preset_mode", entity_id=ROOM_CLIMATE, preset_mode="eco")
    await rig.set_eco(5.0)
    await rig.advance(5 * SEC)
    assert rig.commands(model)[-1:] == heat(model, max(5.0, model.min_temp))[-1:]
    await rig.advance(30 * MIN)
    assert rig.commands(model) == heat(model, max(5.0, model.min_temp)), (
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
