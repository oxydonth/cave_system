"""The Zigbee2MQTT stand-in's messages, fed into Home Assistant's real MQTT integration.

Every model's discovery messages must produce the entities, with the entity IDs, that Zigbee2MQTT's
scheme gives (concept section 4.1), and one command per model issued through Home Assistant must
change the simulated device.
"""

import json
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from cave_sim.home import parse_home
from cave_sim.testing import DEMO_HOME, async_advance
from cave_sim.zigbee.models import MODELS

# One device per model, named so that the expected entity IDs are easy to read.
ONE_OF_EACH = parse_home(
    {
        "rooms": [
            {
                "id": "test_room",
                "devices": [
                    {"model": "TRV-ZBT", "name": "Test TRV ZBT"},
                    {"model": "TRVZB", "name": "Test TRVZB"},
                    {"model": "BTH-RA", "name": "Test Bosch"},
                    {"model": "SNZB-02P", "name": "Test Climate"},
                    {"model": "SNZB-04P", "name": "Test Contact"},
                ],
            }
        ]
    }
)

# The entity IDs every model must produce in Home Assistant, for the device names above.
EXPECTED_ENTITIES: dict[str, set[str]] = {
    "Test TRV ZBT": {
        "climate.test_trv_zbt",
        "number.test_trv_zbt_local_temperature_calibration",
        "sensor.test_trv_zbt_local_temperature",
        "sensor.test_trv_zbt_battery",
        "sensor.test_trv_zbt_fault_status",
        "select.test_trv_zbt_screen_direction",
        "switch.test_trv_zbt_child_lock",
        "switch.test_trv_zbt_open_window",
        "number.test_trv_zbt_frost_protection_temperature",
        "select.test_trv_zbt_temperature_sensor_select",
        "number.test_trv_zbt_external_temperature_input",
        "sensor.test_trv_zbt_idle_steps",
        "sensor.test_trv_zbt_closing_steps",
        "sensor.test_trv_zbt_valve_opening_limit_voltage",
        "sensor.test_trv_zbt_valve_closing_limit_voltage",
        "sensor.test_trv_zbt_valve_motor_running_voltage",
        "number.test_trv_zbt_heating_valve_position",
        "number.test_trv_zbt_idle_valve_position",
        "number.test_trv_zbt_temperature_accuracy",
        "switch.test_trv_zbt_smart_temperature_control",
        "select.test_trv_zbt_temporary_mode_mode",
        "number.test_trv_zbt_temporary_mode_duration",
        "number.test_trv_zbt_temporary_mode_target_temperature",
        "select.test_trv_zbt_low_battery_valve_state",
        "select.test_trv_zbt_schedule_active_group",
        "select.test_trv_zbt_schedule_group_to_edit",
        *(
            f"text.test_trv_zbt_weekly_schedule_{day}"
            for day in (
                "sunday",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
            )
        ),
        "switch.test_trv_zbt_remote_temperature_linkage",
        "number.test_trv_zbt_remote_setpoint_temperature",
        "binary_sensor.test_trv_zbt_open_window_detected",
        "sensor.test_trv_zbt_heat_percentage_hour",
        "button.test_trv_zbt_valve_travel_calibration",
        "sensor.test_trv_zbt_motor_travel_calibration_status",
        "select.test_trv_zbt_bluetooth_pairing",
        "sensor.test_trv_zbt_bluetooth_pairing_status",
        "sensor.test_trv_zbt_temperature_control_history",
        "sensor.test_trv_zbt_linkquality",
        "sensor.test_trv_zbt_last_seen",
        "update.test_trv_zbt",
    },
    "Test TRVZB": {
        "climate.test_trvzb",
        "number.test_trvzb_local_temperature_calibration",
        "sensor.test_trvzb_local_temperature",
        "sensor.test_trvzb_battery",
        "number.test_trvzb_timer_mode_target_temp",
        "number.test_trvzb_temporary_mode_duration",
        "select.test_trvzb_temporary_mode_select",
        "switch.test_trvzb_child_lock",
        "switch.test_trvzb_open_window",
        "number.test_trvzb_frost_protection_temperature",
        "select.test_trvzb_temperature_sensor_select",
        "number.test_trvzb_external_temperature_input",
        "sensor.test_trvzb_idle_steps",
        "sensor.test_trvzb_closing_steps",
        "sensor.test_trvzb_valve_opening_limit_voltage",
        "sensor.test_trvzb_valve_closing_limit_voltage",
        "sensor.test_trvzb_valve_motor_running_voltage",
        "number.test_trvzb_valve_opening_degree",
        "number.test_trvzb_valve_closing_degree",
        "number.test_trvzb_temperature_accuracy",
        *(
            f"text.test_trvzb_weekly_schedule_{day}"
            for day in (
                "sunday",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
            )
        ),
        "switch.test_trvzb_smart_temperature_control",
        "sensor.test_trvzb_linkquality",
        "sensor.test_trvzb_last_seen",
        "update.test_trvzb",
    },
    "Test Bosch": {
        "climate.test_bosch",
        "number.test_bosch_local_temperature_calibration",
        "number.test_bosch_pi_heating_demand",
        "sensor.test_bosch_local_temperature",
        "sensor.test_bosch_setpoint_change_source",
        "select.test_bosch_operating_mode",
        "switch.test_bosch_window_detection",
        "switch.test_bosch_boost_heating",
        "number.test_bosch_remote_temperature",
        "switch.test_bosch_child_lock",
        "number.test_bosch_display_brightness",
        "number.test_bosch_display_switch_on_duration",
        "select.test_bosch_display_orientation",
        "select.test_bosch_displayed_temperature",
        "sensor.test_bosch_valve_adapt_status",
        "binary_sensor.test_bosch_automatic_valve_adapt",
        "button.test_bosch_valve_adapt_process",
        "sensor.test_bosch_error_state",
        "sensor.test_bosch_battery",
        "binary_sensor.test_bosch_battery_low",
        *(
            f"text.test_bosch_weekly_schedule_{day}"
            for day in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
        ),
        "sensor.test_bosch_linkquality",
        "sensor.test_bosch_last_seen",
        "update.test_bosch",
    },
    "Test Climate": {
        "sensor.test_climate_battery",
        "sensor.test_climate_temperature",
        "sensor.test_climate_humidity",
        "number.test_climate_temperature_calibration",
        "number.test_climate_humidity_calibration",
        "sensor.test_climate_linkquality",
        "sensor.test_climate_last_seen",
        "update.test_climate",
    },
    "Test Contact": {
        "binary_sensor.test_contact_contact",
        "binary_sensor.test_contact_battery_low",
        "binary_sensor.test_contact_tamper",
        "sensor.test_contact_battery",
        "sensor.test_contact_voltage",
        "sensor.test_contact_linkquality",
        "sensor.test_contact_last_seen",
        "update.test_contact",
    },
}


def _published(sim: Any, topic: str) -> list[dict[str, Any]]:
    return [json.loads(m.payload) for m in sim.broker.messages(topic)]


async def test_every_model_yields_the_expected_entities(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    """Home Assistant accepts every discovery message and derives Zigbee2MQTT's entity IDs."""
    sim = await simulated_home(ONE_OF_EACH)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)

    assert {spec.model for _, spec in ONE_OF_EACH.devices()} == set(MODELS)
    for name, expected in EXPECTED_ENTITIES.items():
        ieee = sim.home.zigbee.devices[name].spec.ieee_address
        matches = devices.async_get_devices(identifiers={("mqtt", f"zigbee2mqtt_{ieee}")})
        assert len(matches) == 1, name
        device = matches[0]
        assert device.name == name
        entries = er.async_entries_for_device(registry, device.id, include_disabled_entities=True)
        actual = {entry.entity_id for entry in entries}
        assert actual == expected, name
    # Every discovery message the stand-in published became an entity: none was rejected.
    discovered = [m for m in sim.broker.messages("homeassistant/+/+/+/config") if m.payload]
    assert len(discovered) == len([e for e in registry.entities.values() if e.platform == "mqtt"])


async def test_entities_show_the_simulated_state(
    hass: HomeAssistant, simulated_home: Any, freezer: Any
) -> None:
    """After start-up the entities carry the devices' state, not unknown or unavailable."""
    sim = await simulated_home(ONE_OF_EACH, setup_fritzbox=False)
    sim.home.set_room_temperature("test_room", 20.5)
    # The SNZB-02P reports at most every 10 s (its minimum reporting interval).
    await async_advance(hass, freezer, 10, step=10)

    climate = hass.states.get("climate.test_trvzb")
    assert climate is not None
    assert climate.state == "heat"
    assert climate.attributes["temperature"] == 20.0
    assert climate.attributes["min_temp"] == 4
    assert climate.attributes["max_temp"] == 35
    assert climate.attributes["target_temp_step"] == 0.5
    assert hass.states.get("sensor.test_climate_temperature").state == "20.5"
    assert hass.states.get("binary_sensor.test_contact_contact").state == "off"
    assert hass.states.get("sensor.test_trvzb_battery").state == "100"
    assert (
        hass.states.get("select.test_trvzb_temperature_sensor_select").state == "local_temperature"
    )
    bosch = hass.states.get("climate.test_bosch")
    assert bosch.state == "heat"
    assert bosch.attributes["hvac_modes"] == ["off", "heat", "auto"]


@pytest.mark.parametrize(
    ("model", "service", "data", "check"),
    [
        (
            "TRV-ZBT",
            ("climate", "set_temperature"),
            {"entity_id": "climate.test_trv_zbt", "temperature": 22.5},
            lambda state: state["occupied_heating_setpoint"] == 22.5,
        ),
        (
            "TRVZB",
            ("climate", "set_hvac_mode"),
            {"entity_id": "climate.test_trvzb", "hvac_mode": "off"},
            lambda state: state["system_mode"] == "off",
        ),
        (
            "BTH-RA",
            ("climate", "set_hvac_mode"),
            {"entity_id": "climate.test_bosch", "hvac_mode": "off"},
            lambda state: state["operating_mode"] == "pause",
        ),
        (
            "SNZB-02P",
            ("number", "set_value"),
            {"entity_id": "number.test_climate_temperature_calibration", "value": 1.5},
            lambda state: state["temperature_calibration"] == 1.5,
        ),
    ],
)
async def test_a_command_through_home_assistant_changes_the_device(
    hass: HomeAssistant,
    simulated_home: Any,
    model: str,
    service: tuple[str, str],
    data: dict[str, Any],
    check: Any,
) -> None:
    """One command per model, issued as a Home Assistant service call, reaches the device."""
    sim = await simulated_home(ONE_OF_EACH, setup_fritzbox=False)
    device = next(d for d in sim.home.zigbee.devices.values() if d.model.model == model)
    assert not check(device.state)

    await hass.services.async_call(*service, data, blocking=True)
    await hass.async_block_till_done()

    assert check(device.state)
    entity_state = hass.states.get(data["entity_id"])
    assert entity_state is not None
    assert entity_state.state != STATE_UNAVAILABLE


async def test_the_contact_sensor_reaches_home_assistant(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    """The SNZB-04P has no settable property; its window contact must arrive in Home Assistant."""
    sim = await simulated_home(ONE_OF_EACH, setup_fritzbox=False)

    sim.home.set_window("test_contact", True)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.test_contact_contact").state == "on"

    sim.home.set_window("test_contact", False)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.test_contact_contact").state == "off"


async def test_demo_home_entity_ids(hass: HomeAssistant, simulated_home: Any) -> None:
    """The demo home produces the entity IDs listed in sim/README.md."""
    await simulated_home(DEMO_HOME)
    # Not yet written, so unknown until the controller feeds it; it must exist all the same.
    assert hass.states.get("number.schlafzimmer_heizung_remote_temperature") is not None
    for entity_id in (
        "climate.wohnzimmer_heizung_fenster",
        "number.wohnzimmer_heizung_fenster_external_temperature_input",
        "select.wohnzimmer_heizung_fenster_temperature_sensor_select",
        "switch.wohnzimmer_heizung_fenster_open_window",
        "climate.wohnzimmer_heizung_tuer",
        "sensor.wohnzimmer_temperature",
        "sensor.wohnzimmer_humidity",
        "binary_sensor.wohnzimmer_fenster_contact",
        "climate.schlafzimmer_heizung",
        "switch.schlafzimmer_heizung_window_detection",
        "climate.bad_heizung",
        "sensor.bad_humidity",
    ):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state not in ("unknown", STATE_UNAVAILABLE), entity_id
