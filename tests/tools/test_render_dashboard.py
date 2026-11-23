"""The dashboard generator: golden files, determinism, the allow-list and failing cleanly.

Golden files live in ``tests/tools/golden/<home>/``: ``heating.yaml`` is the input and
``dashboard.yaml`` the expected output. After changing the generator on purpose, rewrite them
and review the diff in Git::

    UPDATE_GOLDEN=1 uv run pytest tests/tools/test_render_dashboard.py

These tests do not use Home Assistant. The templates of the maintenance view are rendered in
tests/tools/templates.
"""

import copy
import difflib
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools import render_dashboard
from tools.render_dashboard import (
    DashboardError,
    build_dashboard,
    check_dashboard,
    load_home,
    main,
    parse_home,
    render,
    render_file,
)

GOLDEN = Path(__file__).parent / "golden"
HOMES = ["no_rooms", "one_room", "all_optional", "demo_home"]
REPO = Path(__file__).parents[2]

# Written out here on purpose, apart from the generator's own allow-list: a card type added to
# the generator by mistake must fail this test, not silently extend both lists.
BUILT_IN_CARDS = {"entities", "heading", "history-graph", "markdown", "thermostat", "tile"}
BUILT_IN_FEATURES = {
    "climate-hvac-modes",
    "climate-preset-modes",
    "numeric-input",
    "select-options",
}
BUILT_IN_BADGES = {"entity"}
# Name items of the frontend (`name: {type: ...}`), filled in from the registries.
BUILT_IN_NAME_ITEMS = {"entity"}
OWN_NAME = {"type": "entity"}


def _golden_diff(expected: str, actual: str, name: str) -> str:
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"golden/{name}/dashboard.yaml",
        tofile="generated",
    )
    return "".join(diff)


@pytest.mark.parametrize("home", HOMES)
def test_output_matches_the_golden_file(home: str) -> None:
    """The generated dashboard is the reviewed one, byte for byte."""
    actual = render_file(GOLDEN / home / "heating.yaml")
    golden = GOLDEN / home / "dashboard.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(actual, encoding="utf-8")
    expected = golden.read_text(encoding="utf-8")

    assert actual == expected, "\n" + _golden_diff(expected, actual, home)


@pytest.mark.parametrize("home", HOMES)
def test_same_input_gives_identical_bytes(home: str) -> None:
    """Nothing in the output depends on the run: no timestamps, no hash order."""
    first = render_file(GOLDEN / home / "heating.yaml")
    second = render_file(GOLDEN / home / "heating.yaml")

    assert first == second


def test_rooms_keep_the_order_of_the_file(tmp_path: Path) -> None:
    """Reversing the rooms in the input reverses the room sections in the output."""
    data = yaml.safe_load((GOLDEN / "demo_home" / "heating.yaml").read_text())

    def headings(rooms: list[dict[str, Any]]) -> list[str]:
        dashboard = build_dashboard(parse_home({**data, "rooms": rooms}))
        sections = dashboard["views"][0]["sections"][1:]
        return [section["cards"][0]["heading"] for section in sections]

    assert headings(data["rooms"]) == ["Wohnzimmer", "Schlafzimmer", "Bad"]
    assert headings(list(reversed(data["rooms"]))) == ["Bad", "Schlafzimmer", "Wohnzimmer"]


@pytest.mark.parametrize("home", HOMES)
def test_output_parses_back_to_the_dashboard_that_was_built(home: str) -> None:
    """Writing as text loses nothing: multi-line templates survive as literal blocks."""
    dashboard = build_dashboard(load_home(GOLDEN / home / "heating.yaml"))

    assert yaml.safe_load(render(dashboard)) == dashboard


def test_no_rooms_gives_a_dashboard_that_says_so() -> None:
    """A home without rooms still has both views; the room view explains itself."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "no_rooms" / "heating.yaml"))

    room_view, maintenance = dashboard["views"]
    assert [view["path"] for view in dashboard["views"]] == ["rooms", "maintenance"]
    (home_section,) = room_view["sections"]
    assert "No rooms are configured" in home_section["cards"][-1]["content"]
    assert len(maintenance["sections"]) == 3


def test_a_room_with_nothing_optional_has_only_what_it_always_has() -> None:
    """Without sensors, windows or a schedule, the room shows its own entities only."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "one_room" / "heating.yaml"))

    heading, thermostat, rows, history = dashboard["views"][0]["sections"][1]["cards"]
    assert heading["heading"] == "Office"
    assert thermostat["entity"] == "climate.cave_office"
    assert rows["entities"] == [
        {"entity": "sensor.cave_office_reason", "name": OWN_NAME},
        {"entity": "binary_sensor.cave_office_window_open", "name": OWN_NAME},
        {"entity": "number.cave_office_comfort_temperature", "name": OWN_NAME},
        {"entity": "number.cave_office_eco_temperature", "name": OWN_NAME},
        {"entity": "button.cave_office_resume_schedule", "name": OWN_NAME},
    ]
    assert history["entities"] == ["climate.cave_office"]


def test_every_optional_key_reaches_the_dashboard() -> None:
    """Presence, sensors, window contacts and schedule each show up where a person looks."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "all_optional" / "heating.yaml"))

    sections = dashboard["views"][0]["sections"]
    presence = sections[0]["cards"][-1]
    assert presence["title"] == "Who is home"
    assert presence["entities"] == ["device_tracker.max_phone", "person.anna", "zone.home"]
    living = sections[1]["cards"]
    assert living[2]["entities"] == [
        {"entity": "sensor.cave_living_room_reason", "name": OWN_NAME},
        {"entity": "binary_sensor.cave_living_room_window_open", "name": OWN_NAME},
        {"entity": "number.cave_living_room_comfort_temperature", "name": OWN_NAME},
        {"entity": "number.cave_living_room_eco_temperature", "name": OWN_NAME},
        {"entity": "button.cave_living_room_resume_schedule", "name": OWN_NAME},
        "schedule.living_room_heating",
        {"entity": "binary_sensor.wohnzimmer_fenster_contact", "name": "Window contact 1"},
        {"entity": "binary_sensor.wohnzimmer_balkontuer_contact", "name": "Window contact 2"},
    ]
    assert living[3]["entities"] == [
        "climate.cave_living_room",
        "sensor.wohnzimmer_temperature",
        "sensor.wohnzimmer_humidity",
    ]
    bathroom = sections[2]["cards"]
    assert {"entity": "binary_sensor.bad_fenster_contact", "name": "Window contact"} in bathroom[2][
        "entities"
    ]


def _rows(dashboard: dict[str, Any]) -> Iterator[dict[str, Any] | str]:
    for section in dashboard["views"][0]["sections"]:
        for card in section["cards"]:
            if card["type"] == "entities":
                yield from card["entities"]


@pytest.mark.parametrize("home", HOMES)
def test_no_window_contact_is_left_to_the_name_of_its_device_class(home: str) -> None:
    """Every contact of `windows` has a name of its own, not "Wohnzimmer Fenster Door".

    Home Assistant names a Zigbee contact after its device and its device class, so the row of a
    window would read as a door.
    """
    source = GOLDEN / home / "heating.yaml"
    windows = {w for room in load_home(source).rooms for w in room.windows}
    dashboard = yaml.safe_load(render_file(source))

    named = {row["entity"]: row["name"] for row in _rows(dashboard) if isinstance(row, dict)}

    assert windows <= set(named)
    assert all(named[window].startswith("Window contact") for window in windows)


@pytest.mark.parametrize(
    ("windows", "names"),
    [
        (["binary_sensor.a"], ["Window contact"]),
        (
            ["binary_sensor.fenster", "binary_sensor.balkontuer", "input_boolean.x"],
            ["Window contact 1", "Window contact 2", "Window contact 3"],
        ),
    ],
    ids=["one", "several"],
)
def test_window_contacts_are_numbered_in_the_order_of_the_file_when_there_are_several(
    windows: list[str], names: list[str]
) -> None:
    rows = render_dashboard.window_rows(windows)

    assert rows == [
        {"entity": window, "name": name} for window, name in zip(windows, names, strict=True)
    ]


def test_the_rooms_and_the_homes_own_entities_are_named_without_their_device() -> None:
    """Under the heading "Wohnzimmer" a row says "Comfort temperature", not the room again.

    Home Assistant's name, "Wohnzimmer Comfort temperature", is cut to "Wohnzimmer Comf..." on a
    phone. Every `cave_` entity in a row or tile is named by its own name, and nothing else is.
    """
    dashboard = yaml.safe_load(render_file(GOLDEN / "all_optional" / "heating.yaml"))
    sections = dashboard["views"][0]["sections"]
    tiles = [card for card in sections[0]["cards"] if card["type"] == "tile"]
    rows = [row for row in _rows(dashboard) if isinstance(row, dict)]

    for item in [*tiles, *rows]:
        is_cave = item["entity"].split(".")[1].startswith("cave_")
        assert (item.get("name") == OWN_NAME) == is_cave, item
    assert len(tiles) == 3
    assert "schedule.living_room_heating" in list(_rows(dashboard))


def test_the_room_view_has_the_controls_of_the_home_header() -> None:
    """Home mode, away temperature and occupancy are on the first section."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "one_room" / "heating.yaml"))

    cards = dashboard["views"][0]["sections"][0]["cards"]
    assert [card.get("entity") for card in cards[1:]] == [
        "select.cave_home_mode",
        "number.cave_away_temperature",
        "binary_sensor.cave_home_occupied",
    ]


def test_the_problem_card_lists_exactly_the_configured_rooms() -> None:
    """The maintenance view names each room's problem sensor, and no other room's."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "demo_home" / "heating.yaml"))

    problems = dashboard["views"][1]["sections"][2]["cards"][1]
    assert problems["entity_id"] == [
        "binary_sensor.cave_living_room_problem",
        "binary_sensor.cave_bedroom_problem",
        "binary_sensor.cave_bathroom_problem",
    ]
    assert "thermostats" in problems["content"]
    assert "messages" in problems["content"]


def test_the_generator_names_no_device(tmp_path: Path) -> None:
    """Battery and availability come from templates, so no thermostat or sensor id is in them."""
    dashboard = yaml.safe_load(render_file(GOLDEN / "demo_home" / "heating.yaml"))

    batteries, unavailable = dashboard["views"][1]["sections"][:2]
    for section in (batteries, unavailable):
        content = section["cards"][1]["content"]
        assert "wohnzimmer" not in content
        assert "climate." not in content


# ---------------------------------------------------------------------------------------------
# Built-in cards only
# ---------------------------------------------------------------------------------------------


def _walk_types(dashboard: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Every ``(kind, type)`` the dashboard uses: cards, features, heading badges, name items."""
    for view in dashboard["views"]:
        for section in view["sections"]:
            for card in section["cards"]:
                yield "card", card["type"]
                for feature in card.get("features", []):
                    yield "feature", feature["type"]
                for badge in card.get("badges", []):
                    yield "badge", badge["type"]
                rows = [row for row in card.get("entities", []) if isinstance(row, dict)]
                for item in [card, *card.get("badges", []), *rows]:
                    if isinstance(item.get("name"), dict):
                        yield "name item", item["name"]["type"]


def _all_used_types() -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    for home in HOMES:
        used.update(_walk_types(yaml.safe_load(render_file(GOLDEN / home / "heating.yaml"))))
    return used


def test_only_built_in_cards_features_and_badges_are_used() -> None:
    """Everything the generator emits for the golden homes is on the written-out list."""
    used = _all_used_types()

    assert {t for kind, t in used if kind == "card"} <= BUILT_IN_CARDS
    assert {t for kind, t in used if kind == "feature"} <= BUILT_IN_FEATURES
    assert {t for kind, t in used if kind == "badge"} <= BUILT_IN_BADGES
    assert {t for kind, t in used if kind == "name item"} <= BUILT_IN_NAME_ITEMS
    assert not [t for _, t in used if t.startswith("custom:")]


def test_the_generators_allow_list_is_the_written_out_list() -> None:
    """Widening the allow-list is a change this test makes someone look at."""
    assert render_dashboard.ALLOWED_CARD_TYPES == BUILT_IN_CARDS
    assert set(render_dashboard.ALLOWED_FEATURE_OPTIONS) == BUILT_IN_FEATURES
    assert set(render_dashboard.ALLOWED_BADGE_OPTIONS) == BUILT_IN_BADGES
    assert set(render_dashboard.ALLOWED_NAME_ITEM_OPTIONS) == BUILT_IN_NAME_ITEMS


def test_the_allow_list_is_not_wider_than_what_is_used() -> None:
    """Every allowed card type, feature, badge and name item is exercised by a golden file."""
    used = _all_used_types()

    assert {t for kind, t in used if kind == "card"} == BUILT_IN_CARDS
    assert {t for kind, t in used if kind == "feature"} == BUILT_IN_FEATURES
    assert {t for kind, t in used if kind == "badge"} == BUILT_IN_BADGES
    assert {t for kind, t in used if kind == "name item"} == BUILT_IN_NAME_ITEMS


@pytest.mark.parametrize("home", HOMES)
def test_the_golden_dashboards_pass_the_check(home: str) -> None:
    """The check reports nothing for what the generator produces."""
    assert check_dashboard(yaml.safe_load(render_file(GOLDEN / home / "heating.yaml"))) == []


def _dashboard() -> dict[str, Any]:
    result: dict[str, Any] = yaml.safe_load(render_file(GOLDEN / "all_optional" / "heating.yaml"))
    return result


def test_the_check_rejects_a_custom_card() -> None:
    """A card type outside the allow-list is reported with where it is."""
    dashboard = _dashboard()
    dashboard["views"][0]["sections"][1]["cards"][1]["type"] = "custom:better-thermostat-ui-card"

    (problem,) = check_dashboard(dashboard)

    assert "views[0].sections[1].cards[1]" in problem
    assert "custom:better-thermostat-ui-card" in problem


def test_the_check_rejects_a_card_that_exists_but_is_not_on_the_list() -> None:
    """A built-in card the generator does not use is refused as well."""
    dashboard = _dashboard()
    dashboard["views"][0]["sections"][0]["cards"].append({"type": "gauge", "entity": "sensor.x"})

    (problem,) = check_dashboard(dashboard)

    assert "`gauge`" in problem


def test_the_check_rejects_an_option_the_card_does_not_have() -> None:
    """A misspelt option would show an error card in the browser, so it fails here."""
    dashboard = _dashboard()
    dashboard["views"][0]["sections"][1]["cards"][2]["show_header_toogle"] = False

    (problem,) = check_dashboard(dashboard)

    assert "show_header_toogle" in problem


def test_the_check_rejects_a_feature_badge_or_row_outside_the_lists() -> None:
    """Features, heading badges and entity rows are checked like cards."""
    dashboard = _dashboard()
    cards = dashboard["views"][0]["sections"][1]["cards"]
    cards[1]["features"].append({"type": "custom:feature"})
    cards[0]["badges"].append({"type": "button", "text": "x"})
    cards[2]["entities"].append({"type": "button", "entity": "button.x"})

    problems = check_dashboard(dashboard)

    assert len(problems) == 3
    assert any("feature type" in p for p in problems)
    assert any("badge type" in p for p in problems)
    assert any("row type" in p for p in problems)


def test_the_check_rejects_a_name_item_outside_the_list() -> None:
    """A name item the generator does not use, or a misspelt one, is refused wherever it is."""
    dashboard = _dashboard()
    cards = dashboard["views"][0]["sections"][1]["cards"]
    cards[0]["badges"][0]["name"] = {"type": "device"}
    cards[2]["entities"][0]["name"] = [{"type": "entity"}, {"type": "aera"}]
    dashboard["views"][0]["sections"][0]["cards"][1]["name"] = {"type": "entity", "x": 1}

    problems = check_dashboard(dashboard)

    assert problems == [
        "views[0].sections[0].cards[1].name: name item option `x` is not allowed",
        "views[0].sections[1].cards[0].badges[0].name: name item `device` is not allowed",
        "views[0].sections[1].cards[2].entities[0].name[1]: name item `aera` is not allowed",
    ]


def test_the_check_rejects_a_view_type_outside_sections() -> None:
    """Only the sections view is used."""
    dashboard = _dashboard()
    dashboard["views"][0]["type"] = "custom:grid-layout"

    (problem,) = check_dashboard(dashboard)

    assert "view type" in problem


def test_the_generator_refuses_to_write_a_card_outside_the_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the generator ever produced a disallowed card, it fails instead of writing it."""
    original = render_dashboard._room_section

    def with_custom_card(room: render_dashboard.Room) -> dict[str, Any]:
        section = original(room)
        section["cards"].append({"type": "custom:mushroom-chips-card"})
        return section

    monkeypatch.setattr(render_dashboard, "_room_section", with_custom_card)

    with pytest.raises(DashboardError, match=r"not allowed.*custom:mushroom-chips-card"):
        render_file(GOLDEN / "one_room" / "heating.yaml")


# ---------------------------------------------------------------------------------------------
# Reading the file and failing cleanly
# ---------------------------------------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "heating.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_keys_and_a_single_presence_entity_are_accepted(tmp_path: Path) -> None:
    """Keys the dashboard does not use, from this or a later release, do not matter."""
    path = _write(
        tmp_path,
        "version: 1\nfuture_key: {a: 1}\npresence: person.max\n"
        "rooms:\n  - id: a\n    name: A\n    something_new: true\n    trvs: []\n",
    )

    home = load_home(path)

    assert home.presence == ("person.max",)
    assert [room.id for room in home.rooms] == ["a"]


def test_a_missing_rooms_key_means_no_rooms(tmp_path: Path) -> None:
    """The starter file has no rooms; `rooms:` empty or absent is the same."""
    assert load_home(_write(tmp_path, "version: 1\n")).rooms == ()
    assert load_home(_write(tmp_path, "version: 1\nrooms:\n")).rooms == ()


def test_umlauts_are_written_as_they_are(tmp_path: Path) -> None:
    """Room names are shown to people, so the file is UTF-8 text, not escapes."""
    path = _write(tmp_path, "rooms:\n  - id: kitchen\n    name: Küche\n")

    text = render_file(path)

    assert "heading: Küche" in text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("rooms: [", "not valid YAML at line"),
        ("- a\n- b\n", "must be a mapping"),
        ("", "must be a mapping"),
        ("version: 2\nrooms: []\n", "version 2 is not supported"),
        ("rooms: living_room\n", "`rooms` must be a list"),
        ("rooms:\n  - just text\n", r"rooms\[0\]: expected a mapping"),
        ("rooms:\n  - name: A\n", r"rooms\[0\]: id None must be"),
        ("rooms:\n  - id: Living Room\n    name: A\n", "id 'Living Room' must be"),
        ("rooms:\n  - id: a\n", "room a: name must be"),
        ("rooms:\n  - {id: a, name: A}\n  - {id: a, name: B}\n", "room id a is used twice"),
        (
            "rooms:\n  - {id: a, name: A, humidity_sensor: not an entity}\n",
            "room a, humidity_sensor: 'not an entity' is not an entity ID",
        ),
        ("rooms:\n  - {id: a, name: A, windows: [1]}\n", "room a, windows: 1 is not an entity ID"),
        ("rooms:\n  - {id: a, name: A, windows: {x: 1}}\n", "expected an entity ID or a list"),
        ("presence: [Person.Max]\n", "presence: 'Person.Max' is not an entity ID"),
    ],
)
def test_a_file_it_cannot_use_is_refused_with_one_clear_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, message: str
) -> None:
    """No traceback, a non-zero status, and a message that says what to fix."""
    path = _write(tmp_path, text)
    output = tmp_path / "out" / "heating.yaml"

    status = main([str(path), str(output)])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err.startswith("render_dashboard: ")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert str(path) in captured.err
    assert re.search(message, captured.err)
    assert not output.exists()


def test_a_missing_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The message names the path."""
    status = main([str(tmp_path / "nope.yaml"), str(tmp_path / "out.yaml")])

    assert status == 1
    assert "nope.yaml: no such file" in capsys.readouterr().err


def test_a_failed_run_leaves_the_existing_dashboard_alone(tmp_path: Path) -> None:
    """Home Assistant keeps serving the last good dashboard when the new one cannot be made."""
    output = tmp_path / "heating.dashboard.yaml"
    output.write_text("previous\n")
    source = _write(tmp_path, "rooms: [")

    assert main([str(source), str(output)]) == 1

    assert output.read_text() == "previous\n"


def test_the_dashboard_is_written_to_the_given_file(tmp_path: Path) -> None:
    """The file appears with the directories above it, readable by the container user."""
    output = tmp_path / "dashboards" / "heating.yaml"

    status = main([str(GOLDEN / "demo_home" / "heating.yaml"), str(output)])

    assert status == 0
    assert output.read_text() == (GOLDEN / "demo_home" / "dashboard.yaml").read_text()
    assert output.stat().st_mode & 0o777 == 0o644
    assert list(output.parent.iterdir()) == [output]


def test_the_dashboard_can_go_to_standard_output(capsys: pytest.CaptureFixture[str]) -> None:
    """`-` as the output prints it, for a look without writing anything."""
    assert main([str(GOLDEN / "no_rooms" / "heating.yaml"), "-"]) == 0

    assert capsys.readouterr().out == (GOLDEN / "no_rooms" / "dashboard.yaml").read_text()


def test_it_runs_as_a_script_without_the_repository_on_the_path(tmp_path: Path) -> None:
    """In the container it is started as `python /cave/tools/render_dashboard.py`."""
    output = tmp_path / "heating.yaml"
    command = [
        sys.executable,
        "-I",
        str(REPO / "tools" / "render_dashboard.py"),
        str(GOLDEN / "one_room" / "heating.yaml"),
        str(output),
    ]

    # The command is this interpreter running a file of this repository: nothing untrusted.
    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert output.read_text() == (GOLDEN / "one_room" / "dashboard.yaml").read_text()


def test_the_generator_does_not_import_the_integration() -> None:
    """It must stay runnable on its own, so it never depends on custom_components."""
    source = (REPO / "tools" / "render_dashboard.py").read_text()
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }

    assert not imported & {"custom_components", "homeassistant"}


def test_the_inputs_are_not_modified() -> None:
    """Building twice from one parsed home gives the same dashboard and leaves it untouched."""
    home = load_home(GOLDEN / "all_optional" / "heating.yaml")
    snapshot = copy.deepcopy(home)

    assert build_dashboard(home) == build_dashboard(home)
    assert home == snapshot
