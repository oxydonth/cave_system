"""The notifications package and the generated dashboard against the real ``cave_heating``.

The other tests use stand-in entities with the IDs of concept section 5.2. Here the entities are
the integration's own: a small ``heating.yaml`` is applied, ``cave_heating`` sets up its room, and
the package watches the real ``binary_sensor.cave_living_room_window_open`` and ``_problem``.
The thermostat and the contacts are the fake devices of ``tests/cave_heating/fakes.py``.
"""

import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component

from tests.cave_heating.fakes import FakeHome, FakeThermostat, config, room
from tools.render_dashboard import render_file

from .helpers import Shown, load_package

MIN = timedelta(minutes=1)
WINDOW = "binary_sensor.cave_living_room_window_open"
PROBLEM = "binary_sensor.cave_living_room_problem"
HEATING = config(
    room(
        "living_room",
        "climate.trv",
        name="Wohnzimmer",
        windows=["binary_sensor.window"],
        humidity_sensor="sensor.living_humidity",
    )
)


async def start(
    home: FakeHome, freezer: FrozenDateTimeFactory | None = None
) -> tuple[FakeThermostat, Shown]:
    """One room with one thermostat and a window contact, then the notifications package."""
    trv = await home.thermostat("climate.trv", target=21.0)
    home.set("binary_sensor.window", "off")
    home.set("sensor.living_humidity", "50", device_class="humidity", unit_of_measurement="%")
    await home.start(HEATING)
    trv.report(current=20.0)
    shown = await load_package(home.hass)
    if freezer is not None:
        await home.advance(freezer, MIN)  # the sensors' first evaluation
    return trv, shown


def _entity_ids(node: Any) -> Iterator[str]:
    """Every string in a dashboard that looks like one of the integration's entity IDs."""
    if isinstance(node, str):
        if re.fullmatch(r"[a-z_]+\.cave_[a-z0-9_]+", node):
            yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _entity_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from _entity_ids(value)


async def test_an_open_window_is_reported_after_half_an_hour_with_the_rooms_area_name(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The real window pause, from a real contact, named by the real room device's area."""
    _, shown = await start(home, freezer)
    await home.advance(freezer, 2 * MIN)

    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 2 * MIN)
    assert home.state(WINDOW).state == "on", "the integration paused the room"
    await home.advance(freezer, 27 * MIN)
    assert shown.current == {}, "not yet half an hour of pause"

    await home.advance(freezer, 4 * MIN)
    assert shown.titles == ["Window left open"]
    assert shown.messages == [
        "Wohnzimmer: the heating has been paused for more than 30 minutes because a window is open"
    ]

    home.set("binary_sensor.window", "off")
    await home.advance(freezer, 5 * MIN)
    assert home.state(WINDOW).state == "off"
    assert shown.current == {}


async def test_a_short_airing_is_not_reported(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """Ten minutes with the window open is a pause the integration makes and nobody reports."""
    _, shown = await start(home, freezer)
    home.set("binary_sensor.window", "on")
    await home.advance(freezer, 10 * MIN)
    assert home.state(WINDOW).state == "on"
    home.set("binary_sensor.window", "off")

    await home.advance(freezer, 40 * MIN)

    assert shown.current == {}


async def test_a_real_problem_is_reported_with_the_integrations_own_messages(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat that goes away turns the real problem sensor on; ten minutes on, it is said."""
    trv, shown = await start(home, freezer)
    await home.advance(freezer, 2 * MIN)
    assert home.state(PROBLEM).state == "off"

    trv.set_available(False)
    await home.advance(freezer, 8 * MIN)
    problem = home.state(PROBLEM)
    assert problem.state == "on"
    assert shown.current == {}, "not yet ten minutes"

    await home.advance(freezer, 4 * MIN)
    messages = home.state(PROBLEM).attributes["messages"]
    assert messages, "the integration says what is wrong"
    assert shown.titles == ["Heating problem"]
    assert shown.messages == [f"Wohnzimmer: {'; '.join(messages)}"]

    trv.set_available(True)
    await home.advance(freezer, 10 * MIN)
    assert home.state(PROBLEM).state == "off"
    assert shown.current == {}


async def test_pending_and_held_thermostats_do_not_notify(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """A held thermostat is a status, not a problem; nothing is reported however long it lasts."""
    _, shown = await start(home, freezer)
    await hass.services.async_call(
        "cave_heating",
        "hold",
        {"entity_id": "climate.trv", "duration": "01:00:00"},
        blocking=True,
    )
    await home.advance(freezer, 30 * MIN)

    assert home.state(PROBLEM).state == "off"
    assert home.state(PROBLEM).attributes["thermostats"] == {"climate.trv": "held"}
    assert shown.current == {}


async def test_every_cave_entity_in_the_generated_dashboard_exists_in_the_integration(
    hass: HomeAssistant, home: FakeHome, tmp_path: Path
) -> None:
    """The entity IDs the generator writes are the ones the integration creates."""
    await start(home)
    source = tmp_path / "heating.yaml"
    source.write_text(yaml.safe_dump(HEATING, sort_keys=False))

    dashboard = yaml.safe_load(render_file(source))

    wanted = set(_entity_ids(dashboard))
    assert len(wanted) == 10
    missing = sorted(entity for entity in wanted if hass.states.get(entity) is None)
    assert missing == []


async def test_the_dashboards_entities_have_the_domains_its_cards_need(
    hass: HomeAssistant, home: FakeHome, tmp_path: Path
) -> None:
    """Features need the right entity kinds: presets on a climate, options on a select."""
    await start(home)
    source = tmp_path / "heating.yaml"
    source.write_text(yaml.safe_dump(HEATING, sort_keys=False))
    dashboard = yaml.safe_load(render_file(source))
    cards = [card for s in dashboard["views"][0]["sections"] for card in s["cards"]]

    thermostat = next(card for card in cards if card["type"] == "thermostat")
    state = home.state(thermostat["entity"])
    assert state.attributes["preset_modes"] == ["comfort", "eco", "away", "boost", "none"]
    assert set(state.attributes["hvac_modes"]) == {"heat", "off"}
    mode = next(card for card in cards if card.get("entity") == "select.cave_home_mode")
    assert mode["features"][0]["type"] == "select-options"
    assert home.state("select.cave_home_mode").attributes["options"] == ["auto", "away", "off"]
    away = home.state("number.cave_away_temperature").attributes
    assert (away["min"], away["max"], away["step"]) == (5.0, 30.0, 0.5)


async def test_the_maintenance_view_shows_a_real_rooms_problem(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> None:
    """The problem card reads the real `messages` and `thermostats` attributes."""
    trv, _ = await start(home, freezer)
    source = tmp_path / "heating.yaml"
    source.write_text(yaml.safe_dump(HEATING, sort_keys=False))
    dashboard = yaml.safe_load(render_file(source))
    card = dashboard["views"][1]["sections"][2]["cards"][1]

    def shown_lines() -> list[str]:
        text = Template(card["content"], hass).async_render(parse_result=False)
        return [line for line in str(text).splitlines() if line.strip()]

    await home.advance(freezer, 2 * MIN)
    assert shown_lines() == ["All rooms are ok."]

    trv.set_available(False)
    await home.advance(freezer, 2 * MIN)
    attributes = home.state(PROBLEM).attributes

    lines = shown_lines()
    assert lines[0] == "**Wohnzimmer**: needs attention"
    for message in attributes["messages"]:
        assert f"- {message}" in lines
    for thermostat, status in attributes["thermostats"].items():
        assert f"- {thermostat}: {status}" in lines
    assert attributes["thermostats"] == {"climate.trv": "unavailable"}


async def test_the_room_is_a_device_in_the_area_of_its_name(
    hass: HomeAssistant, home: FakeHome
) -> None:
    """The notifications name rooms by their area, so the integration must place the room there."""
    await start(home)

    name = Template(
        "{{ area_name('binary_sensor.cave_living_room_problem') }}", hass
    ).async_render()

    assert name == "Wohnzimmer"


async def test_what_the_real_reload_answers_over_rest_reaches_the_person_through_apply(
    hass: HomeAssistant, home: FakeHome, hass_client: Any
) -> None:
    """An invalid applied copy: Home Assistant's real 400 answer, read by `cave_ops`' client."""
    from cave_ops.ha import HAClient, HAError

    assert await async_setup_component(hass, "api", {})
    await start(home)
    await hass.async_add_executor_job(
        home.write_applied, "version: 1\nrooms:\n  - id: Living Room\n    name: x\n"
    )
    client = await hass_client()

    response = await client.post("/api/services/cave_heating/reload", json={})
    status, body = response.status, await response.read()

    def transport(method: str, url: str, headers: Any, payload: Any, timeout: float) -> Any:
        return status, body

    with pytest.raises(HAError) as raised:
        HAClient("token", transport=transport).call_service("cave_heating", "reload")
    assert status == 400
    assert raised.value.status == 400
    text = str(raised.value)
    assert not text.startswith("Home Assistant answered 400: {")
    assert "Living Room" in text
    assert "previous configuration stays in effect" in text
    assert "must list at least one thermostat" in text
    # The running configuration stays in effect.
    assert home.state("climate.cave_living_room").state != "unavailable"


async def test_a_rooms_humidity_comes_from_its_thermostat_and_needs_no_area(
    hass: HomeAssistant, home: FakeHome, freezer: FrozenDateTimeFactory
) -> None:
    """The room's configured humidity sensor is the thermostat's `current_humidity`.

    The sensor here has no area at all, as a Zigbee2MQTT device has none until the owner
    assigns one; an unavailable sensor makes the attribute disappear.
    """
    _, shown = await start(home, freezer)
    assert hass.states.get("sensor.living_humidity") is not None
    assert er.async_get(hass).async_get("sensor.living_humidity") is None  # not in any area
    assert home.state("climate.cave_living_room").attributes["current_humidity"] == 50

    home.set("sensor.living_humidity", "75", device_class="humidity", unit_of_measurement="%")
    await home.advance(freezer, 2 * MIN)
    assert home.state("climate.cave_living_room").attributes["current_humidity"] == 75
    assert shown.titles == ["High humidity"]
    assert shown.messages == ["Wohnzimmer: humidity is 75 %, time to air the room"]

    home.set("sensor.living_humidity", "unavailable")
    await home.advance(freezer, 2 * MIN)
    assert "current_humidity" not in home.state("climate.cave_living_room").attributes
    assert shown.titles == ["High humidity"], "kept while there is no reading"

    home.set("sensor.living_humidity", "55", device_class="humidity", unit_of_measurement="%")
    await home.advance(freezer, 2 * MIN)
    assert shown.current == {}
