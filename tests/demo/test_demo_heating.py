"""``sim/demo/heating.yaml`` fits the simulated demo home.

Every entity the file names must be one the simulators produce in Home Assistant, with the
settings values the devices offer, or one that ``cave demo`` creates through the bootstrap (the
rooms' schedules and the presence switch). The file must be valid, cover both device families,
and its timings must leave the end-to-end scenarios room to work.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from cave_ops.commands.demo import HEATING, PRESENCE_SWITCH
from cave_sim.testing import DEMO_HOME
from custom_components.cave_heating.schema import load_file

REPO = Path(__file__).resolve().parents[2]


def test_the_demo_command_applies_this_file() -> None:
    assert REPO / HEATING == REPO / "sim" / "demo" / "heating.yaml"
    assert (REPO / HEATING).is_file()


def test_the_file_is_valid_and_covers_both_device_families() -> None:
    config = load_file(REPO / HEATING)

    thermostats = {trv.entity for room in config.rooms for trv in room.trvs}
    assert thermostats == {
        "climate.wohnzimmer_heizung_fenster",  # TRV-ZBT
        "climate.wohnzimmer_heizung_tuer",  # TRVZB
        "climate.schlafzimmer_heizung",  # BTH-RA
        "climate.bad_heizung",  # FRITZ!Smart Thermo 302
    }
    assert config.presence == (PRESENCE_SWITCH[0],)


def test_the_timings_suit_the_scenarios() -> None:
    timing = load_file(REPO / HEATING).timing

    # Seconds, not minutes: the end-to-end scenarios wait for them.
    assert timing.confirm_timeout <= timedelta(seconds=30)
    assert timing.manual_settle <= timedelta(seconds=10)
    # A slow FRITZ! thermostat is confirmed by the box's next poll (every 30 s) at the latest.
    assert timing.confirm_timeout_slow > timedelta(seconds=30) + timing.confirm_timeout


async def test_every_entity_it_names_is_produced_by_the_simulators(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    config = load_file(REPO / HEATING)
    await simulated_home(DEMO_HOME)

    created_by_bootstrap = {room.schedule for room in config.rooms if room.schedule}
    created_by_bootstrap.add(PRESENCE_SWITCH[0])
    named = {entity for room in config.rooms for entity, _ in room.entities()}
    named |= set(config.presence)

    missing = sorted(
        entity for entity in named - created_by_bootstrap if hass.states.get(entity) is None
    )
    assert missing == []
    assert all(
        entity.startswith(("schedule.", "input_boolean.")) for entity in created_by_bootstrap
    )


async def test_every_setting_is_a_value_the_device_offers(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    config = load_file(REPO / HEATING)
    await simulated_home(DEMO_HOME)

    checked = 0
    for room in config.rooms:
        for trv in room.trvs:
            for entity_id, value in trv.settings.items():
                state = hass.states.get(entity_id)
                assert state is not None, entity_id
                if entity_id.startswith("select."):
                    assert value in state.attributes["options"], (entity_id, value)
                else:
                    assert entity_id.startswith("switch.")
                    assert value in ("on", "off"), (entity_id, value)
                checked += 1
    assert checked == 5
