"""The maintenance view's templates, rendered by Home Assistant's own template engine.

The generator does not know any device name; the cards ask Home Assistant. These tests build
stand-in entities that Home Assistant "knows", render the generated cards' templates against
them, and check what a person would read.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tools.render_dashboard import render_file

GOLDEN = Path(__file__).parents[1] / "golden"


def _maintenance_cards(home: str) -> dict[str, dict[str, Any]]:
    """The markdown cards of the maintenance view, by the heading above them."""
    dashboard = yaml.safe_load(render_file(GOLDEN / home / "heating.yaml"))
    (view,) = (view for view in dashboard["views"] if view["path"] == "maintenance")
    cards: dict[str, dict[str, Any]] = {}
    for section in view["sections"]:
        heading, markdown = section["cards"]
        assert markdown["type"] == "markdown"
        cards[heading["heading"]] = markdown
    return cards


def _render(hass: HomeAssistant, card: dict[str, Any]) -> list[str]:
    """The non-empty lines the card shows."""
    # The markdown card renders its content with `strict: true` (frontend, hui-markdown-card):
    # an undefined attribute raises there, the subscription fails, and the card shows the
    # template's own source. So the tests render the same way.
    text = Template(card["content"], hass).async_render(parse_result=False, strict=True)
    return [line for line in str(text).splitlines() if line.strip()]


async def test_battery_card_lists_every_battery_sensor_lowest_first(hass: HomeAssistant) -> None:
    """Every battery sensor Home Assistant knows appears, whatever it is called."""
    battery = {"device_class": "battery", "unit_of_measurement": "%"}
    hass.states.async_set("sensor.zz_odd_name_battery", "87", battery)
    hass.states.async_set("sensor.aa_odd_name_battery", "12", {**battery, "friendly_name": "Hall"})
    hass.states.async_set("sensor.gone_battery", "unavailable", battery)
    hass.states.async_set(
        "binary_sensor.contact_battery_low",
        "on",
        {"device_class": "battery", "friendly_name": "Door"},
    )
    hass.states.async_set(
        "binary_sensor.other_battery_low",
        "off",
        {"device_class": "battery", "friendly_name": "Gate"},
    )
    hass.states.async_set("sensor.room_temperature", "21.5", {"device_class": "temperature"})
    hass.states.async_set("sensor.plain_percentage", "55", {"unit_of_measurement": "%"})

    lines = _render(hass, _maintenance_cards("demo_home")["Batteries"])

    assert lines == [
        "- Door: **low**",
        "- Hall: **12 %**",
        "- zz odd name battery: **87 %**",
        "- gone battery: **unavailable**",
        "- Gate: **ok**",
    ]


async def test_battery_card_says_so_when_there_are_none(hass: HomeAssistant) -> None:
    """With no battery sensor the card is not empty."""
    hass.states.async_set("sensor.room_temperature", "21.5", {"device_class": "temperature"})

    lines = _render(hass, _maintenance_cards("no_rooms")["Batteries"])

    assert lines == ["No battery sensors known to Home Assistant."]


async def test_battery_card_follows_a_changing_level(hass: HomeAssistant) -> None:
    """The template reads the current states, so a replaced battery shows its new level."""
    battery = {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Hall"}
    hass.states.async_set("sensor.hall_battery", "12", battery)
    card = _maintenance_cards("one_room")["Batteries"]
    assert _render(hass, card) == ["- Hall: **12 %**"]

    hass.states.async_set("sensor.hall_battery", "100", battery)

    assert _render(hass, card) == ["- Hall: **100 %**"]


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, name: str, entities: dict[str, str]
) -> None:
    """A device with entities in the given states, created as an integration would."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", name)}, name=name
    )
    for entity_id, state in entities.items():
        domain, object_id = entity_id.split(".")
        er.async_get(hass).async_get_or_create(
            domain,
            "test",
            f"{name}_{object_id}",
            suggested_object_id=object_id,
            device_id=device.id,
        )
        hass.states.async_set(entity_id, state)


async def test_unavailable_card_names_devices_that_are_entirely_unavailable(
    hass: HomeAssistant,
) -> None:
    """A device is listed once, only when all of its entities are unavailable."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    _device(
        hass,
        entry,
        "Dead window contact",
        {"binary_sensor.dead_contact": "unavailable", "sensor.dead_battery": "unavailable"},
    )
    _device(
        hass,
        entry,
        "Half alive thermostat",
        {"climate.half": "unavailable", "sensor.half_temperature": "19.5"},
    )
    _device(hass, entry, "Fine sensor", {"sensor.fine": "20"})
    hass.states.async_set("sensor.no_device_helper", "unavailable")

    lines = _render(hass, _maintenance_cards("one_room")["Unavailable devices"])

    assert lines == ["- Dead window contact"]


async def test_unavailable_card_when_everything_is_available(hass: HomeAssistant) -> None:
    """The card reads as a statement, not as an empty box."""
    hass.states.async_set("sensor.fine", "20")

    lines = _render(hass, _maintenance_cards("one_room")["Unavailable devices"])

    assert lines == ["All devices are available."]


async def test_problem_card_shows_each_rooms_messages_and_thermostats(hass: HomeAssistant) -> None:
    """A room in trouble shows what is wrong and which thermostat; the others show ok."""
    hass.states.async_set(
        "binary_sensor.cave_living_room_problem",
        "on",
        {
            "messages": ["Thermostat Fenster does not respond", "Window contact is unavailable"],
            "thermostats": {
                "climate.wohnzimmer_heizung_fenster": "unresponsive",
                "climate.wohnzimmer_heizung_tuer": "ok",
            },
        },
    )
    hass.states.async_set(
        "binary_sensor.cave_bedroom_problem", "off", {"messages": [], "thermostats": {"x": "ok"}}
    )
    hass.states.async_set("binary_sensor.cave_bathroom_problem", "unavailable")

    lines = _render(hass, _maintenance_cards("demo_home")["Rooms needing attention"])

    assert lines == [
        "**Wohnzimmer**: needs attention",
        "- Thermostat Fenster does not respond",
        "- Window contact is unavailable",
        "- climate.wohnzimmer_heizung_fenster: unresponsive",
        "- climate.wohnzimmer_heizung_tuer: ok",
        "**Bad**: unavailable",
    ]


async def test_problem_card_before_the_integration_has_created_its_entities(
    hass: HomeAssistant,
) -> None:
    """A room whose sensor does not exist yet is reported, not skipped and not an error."""
    lines = _render(hass, _maintenance_cards("one_room")["Rooms needing attention"])

    assert lines == ["**Office**: unknown"]


async def test_problem_card_lists_only_the_rooms_that_are_not_ok(hass: HomeAssistant) -> None:
    """With many rooms the person reads the one that matters, and a line when none does."""
    card = _maintenance_cards("demo_home")["Rooms needing attention"]
    for room in ("living_room", "bedroom", "bathroom"):
        hass.states.async_set(f"binary_sensor.cave_{room}_problem", "off")

    assert _render(hass, card) == ["All rooms are ok."]

    hass.states.async_set("binary_sensor.cave_bedroom_problem", "on", {"messages": ["x"]})
    assert _render(hass, card) == ["**Schlafzimmer**: needs attention", "- x"]


async def test_problem_card_shows_a_room_name_as_text_not_as_markdown_or_template(
    hass: HomeAssistant,
) -> None:
    """Quotes, a pipe, braces and Markdown characters in a name are text; none is evaluated."""
    hass.states.async_set("binary_sensor.cave_kids_problem", "on", {"messages": []})

    lines = _render(hass, _maintenance_cards("all_optional")["Rooms needing attention"])

    assert '**Kid\'s "room" \\| {{ 1 }}**: needs attention' in lines


def test_markdown_text_escapes_what_markdown_would_format() -> None:
    """The escaping is a function of its own so that the card stays text."""
    from tools.render_dashboard import markdown_text

    assert markdown_text("a_b *c* <b>d</b> [e](f) `g` # h") == (
        "a\\_b \\*c\\* \\<b\\>d\\</b\\> \\[e\\](f) \\`g\\` \\# h"
    )
    assert markdown_text("  two\nlines  ") == "two lines"


@pytest.mark.parametrize("home", ["no_rooms", "one_room", "all_optional", "demo_home"])
async def test_every_maintenance_template_renders_for_every_golden_home(
    hass: HomeAssistant, home: str
) -> None:
    """No generated template is a syntax error in Home Assistant's engine."""
    for card in _maintenance_cards(home).values():
        assert _render(hass, card)
