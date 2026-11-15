"""The heating.yaml format of concept section 5.1: validation, defaults, versions, messages."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.cave_heating import schema
from custom_components.cave_heating.schema import (
    CURRENT_VERSION,
    ConfigError,
    HeatingConfig,
    Timing,
    load_file,
    parse,
)

VERSIONS = Path(__file__).with_name("fixtures") / "versions"


def _errors(data: Any) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        parse(data)
    return caught.value.errors


def _room(**fields: Any) -> dict[str, Any]:
    return {"id": "living_room", "name": "Wohnzimmer", "trvs": [{"entity": "climate.a"}], **fields}


def test_concept_example_is_valid_with_every_value_read() -> None:
    """The example of concept section 5.1, field by field."""
    config = load_file(VERSIONS / "v1.yaml")
    assert config.version == 1
    assert config.presence == ("device_tracker.max_phone",)
    assert config.away_delay == timedelta(minutes=30)
    assert config.away_temperature == 16.0
    living, bathroom = config.rooms
    assert living.id == "living_room"
    assert living.name == "Wohnzimmer"
    assert [trv.entity for trv in living.trvs] == [
        "climate.wohnzimmer_heizung_fenster",
        "climate.wohnzimmer_heizung_tuer",
    ]
    window, door = living.trvs
    assert window.external_temperature == (
        "number.wohnzimmer_heizung_fenster_external_temperature_input"
    )
    assert window.settings == {
        "select.wohnzimmer_heizung_fenster_temperature_sensor_select": "remote_temperature",
        "switch.wohnzimmer_heizung_fenster_open_window": "off",
    }
    assert door.settings == {}
    assert window.slow is None
    assert living.temperature_sensor == "sensor.wohnzimmer_temperature"
    assert living.humidity_sensor == "sensor.wohnzimmer_humidity"
    assert living.windows == ("binary_sensor.wohnzimmer_fenster_contact",)
    assert living.schedule == "schedule.living_room_heating"
    assert (living.comfort_temperature, living.eco_temperature) == (21.0, 17.5)
    assert living.boost_temperature == 25.0
    assert living.boost_duration == timedelta(minutes=30)
    assert living.override_max == timedelta(hours=6)
    assert living.window_open_delay == timedelta(minutes=1)
    assert living.window_close_delay == timedelta(minutes=2)
    assert bathroom.window_open_delay == timedelta(minutes=5), "the room's own value wins"
    assert bathroom.windows == ()
    assert bathroom.schedule is None
    assert config.timing == Timing()


def test_every_version_that_ever_existed_has_a_stored_example_that_loads() -> None:
    """The loader converts older versions: one stored example per version, each loading."""
    stored = sorted(VERSIONS.glob("v*.yaml"))
    assert [path.stem for path in stored] == [f"v{n}" for n in range(1, CURRENT_VERSION + 1)]
    for path in stored:
        assert load_file(path).version == CURRENT_VERSION


def test_minimal_file_and_empty_room_list_are_valid() -> None:
    """Per room only id, name and one thermostat are required; no rooms is valid."""
    assert parse({"version": 1}) == HeatingConfig()
    assert parse({"version": 1, "rooms": []}).rooms == ()
    room = parse({"version": 1, "rooms": [_room()]}).rooms[0]
    assert room.temperature_sensor is None
    assert room.windows == ()
    assert room.comfort_temperature == 21.0


def test_defaults_apply_to_every_room_unless_the_room_sets_its_own() -> None:
    """The defaults block."""
    config = parse(
        {
            "version": 1,
            "defaults": {"comfort_temperature": 20.0, "override_max": "02:00:00"},
            "rooms": [
                _room(),
                _room(id="bath", trvs=[{"entity": "climate.b"}], comfort_temperature=23),
            ],
        }
    )
    living, bath = config.rooms
    assert living.comfort_temperature == 20.0
    assert bath.comfort_temperature == 23.0
    assert living.override_max == bath.override_max == timedelta(hours=2)


def test_presence_takes_one_entity_or_a_list() -> None:
    """Section 5.1: one entity or a list."""
    assert parse({"version": 1, "presence": "zone.home"}).presence == ("zone.home",)
    assert parse({"version": 1, "presence": ["person.a", "person.b"]}).presence == (
        "person.a",
        "person.b",
    )


def test_timing_block_overrides_each_value() -> None:
    """Every key of the timing block, in seconds as the demo home uses them."""
    timing = parse(
        {
            "version": 1,
            "timing": {
                "debounce": 0,
                "confirm_timeout": 5,
                "confirm_timeout_slow": 20,
                "resend_attempts": 1,
                "retry_interval": "00:00:30",
                "manual_settle": 3,
                "command_budget_per_hour": 100,
                "budget_exhausted_hours": 2,
                "feed_threshold": 0.5,
                "feed_min_interval": 10,
                "feed_max_interval": 25,
                "sensor_stale": 180,
                "window_unavailable_grace": 30,
                "presence_unavailable_grace": 60,
            },
        }
    ).timing
    assert timing == Timing(
        debounce=timedelta(0),
        confirm_timeout=timedelta(seconds=5),
        confirm_timeout_slow=timedelta(seconds=20),
        resend_attempts=1,
        retry_interval=timedelta(seconds=30),
        manual_settle=timedelta(seconds=3),
        command_budget_per_hour=100,
        budget_exhausted_hours=2,
        feed_threshold=0.5,
        feed_min_interval=timedelta(seconds=10),
        feed_max_interval=timedelta(seconds=25),
        sensor_stale=timedelta(seconds=180),
        window_unavailable_grace=timedelta(seconds=30),
        presence_unavailable_grace=timedelta(seconds=60),
    )


def test_settings_values_are_normalised_per_domain() -> None:
    """YAML's unquoted on/off become the switch states; numbers stay numbers."""
    trv = (
        parse(
            {
                "version": 1,
                "rooms": [
                    _room(
                        trvs=[
                            {
                                "entity": "climate.a",
                                "settings": {
                                    "switch.a_window": False,
                                    "switch.a_child_lock": "on",
                                    "select.a_sensor": "external",
                                    "select.a_mode": True,
                                    "number.a_offset": 1,
                                },
                                "slow": True,
                            }
                        ]
                    )
                ],
            }
        )
        .rooms[0]
        .trvs[0]
    )
    assert dict(trv.settings) == {
        "switch.a_window": "off",
        "switch.a_child_lock": "on",
        "select.a_sensor": "external",
        "select.a_mode": "on",
        "number.a_offset": 1.0,
    }
    assert trv.slow is True


def test_unquoted_clock_times_are_not_read_as_base_60_numbers() -> None:
    """YAML 1.1 would read 12:00 as 720; here it stays a duration of twelve hours."""
    config = schema.parse(schema._StrictLoader("version: 1\naway_delay: 12:00\n").get_data())
    assert config.away_delay == timedelta(hours=12)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, 'The file is empty; it needs at least "version: 1".'),
        ([1, 2], "The file must be a mapping of keys to values, but is [1, 2]."),
        ({"rooms": []}, 'Field "version": must be a whole number, but is None.'),
        (
            {"version": 2},
            'Field "version": version 2 is not supported; this release reads versions 1 to 1.',
        ),
        ({"version": True}, 'Field "version": must be a whole number, but is True.'),
    ],
)
def test_file_level_problems(data: Any, message: str) -> None:
    """Problems with the file as a whole."""
    assert _errors(data) == [message]


@pytest.mark.parametrize(
    ("room", "message"),
    [
        (
            _room(trvs=[{"entity": "clima.a"}]),
            'Room "living_room" (rooms[0]), field "trvs[0].entity": must be an entity of the '
            'domain climate, but is "clima.a".',
        ),
        (
            _room(trvs=[{"entity": "climate.A"}]),
            'Room "living_room" (rooms[0]), field "trvs[0].entity": must be an entity ID such as '
            'climate.living_room, but is "climate.A".',
        ),
        (
            _room(window_open_delay="5 min"),
            'Room "living_room" (rooms[0]), field "window_open_delay": must be a duration such as '
            '"00:05:00" (hours:minutes:seconds) or a number of seconds, but is "5 min".',
        ),
        (
            _room(comfort_temperature=21.3),
            'Room "living_room" (rooms[0]), field "comfort_temperature": must be a multiple of '
            "0.5 °C, but is 21.3.",
        ),
        (
            _room(eco_temperature=31),
            'Room "living_room" (rooms[0]), field "eco_temperature": must be between 5 and 30 °C, '
            "but is 31.",
        ),
        (
            _room(boost_temperature="warm"),
            'Room "living_room" (rooms[0]), field "boost_temperature": must be a temperature in '
            '°C, but is "warm".',
        ),
        (
            _room(boost_duration=0),
            'Room "living_room" (rooms[0]), field "boost_duration": must be longer than zero.',
        ),
        (
            _room(temprature_sensor="sensor.t"),
            'Room "living_room" (rooms[0]), field "temprature_sensor": is not a known field; the '
            "known fields are: boost_duration, boost_temperature, comfort_temperature, "
            "eco_temperature, humidity_sensor, id, name, override_max, schedule, "
            "temperature_sensor, trvs, window_close_delay, window_open_delay, windows.",
        ),
        (
            _room(trvs=[]),
            'Room "living_room" (rooms[0]), field "trvs": must list at least one thermostat.',
        ),
        (
            _room(trvs=[{"external_temperature": "number.x"}]),
            'Room "living_room" (rooms[0]), field "trvs[0].entity": is required.',
        ),
        (
            _room(trvs=["climate.a"]),
            'Room "living_room" (rooms[0]), field "trvs[0]": must be a mapping of keys to values, '
            'but is "climate.a".',
        ),
        (
            _room(trvs=[{"entity": "climate.a", "external_temperature": "sensor.t"}]),
            'Room "living_room" (rooms[0]), field "trvs[0].external_temperature": must be an '
            'entity of the domain number or input_number, but is "sensor.t".',
        ),
        (
            _room(trvs=[{"entity": "climate.a", "slow": "yes please"}]),
            'Room "living_room" (rooms[0]), field "trvs[0].slow": must be true or false, but is '
            '"yes please".',
        ),
        (
            _room(trvs=[{"entity": "climate.a", "settings": {"switch.w": "maybe"}}]),
            'Room "living_room" (rooms[0]), field "trvs[0].settings.switch.w": must be "on" or '
            '"off" for a switch, but is "maybe".',
        ),
        (
            _room(trvs=[{"entity": "climate.a", "settings": {"number.o": "one"}}]),
            'Room "living_room" (rooms[0]), field "trvs[0].settings.number.o": must be a number '
            'for a number entity, but is "one".',
        ),
        (
            _room(trvs=[{"entity": "climate.a", "settings": {"light.l": "on"}}]),
            'Room "living_room" (rooms[0]), field "trvs[0].settings.light.l": must be an entity '
            'of the domain select or switch or number, but is "light.l".',
        ),
        (
            _room(windows=["binary_sensor.w", "binary_sensor.w"]),
            'Room "living_room" (rooms[0]), field "windows[1]": "binary_sensor.w" is listed twice.',
        ),
        (
            _room(schedule="sensor.s"),
            'Room "living_room" (rooms[0]), field "schedule": must be an entity of the domain '
            'schedule or input_boolean or binary_sensor, but is "sensor.s".',
        ),
        (
            _room(id="Living Room"),
            'Room "Living Room" (rooms[0]), field "id": must be lower-case ASCII letters, digits '
            "and single underscores, starting and ending with a letter or digit, but is "
            '"Living Room".',
        ),
        (
            {"name": "Wohnzimmer", "trvs": [{"entity": "climate.a"}]},
            'Room 1 (rooms[0]), field "id": is required.',
        ),
        (
            _room(name="  "),
            'Room "living_room" (rooms[0]), field "name": must be a non-empty text, but is "  ".',
        ),
        (
            "living_room",
            'Room 1 (rooms[0]): must be a mapping of keys to values, but is "living_room".',
        ),
    ],
)
def test_room_problems_name_the_room_and_the_field(room: Any, message: str) -> None:
    """Every message names the room and the field, as one complete sentence."""
    assert _errors({"version": 1, "rooms": [room]}) == [message]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 1, "rooms": {}}, 'Field "rooms": must be a list of rooms, but is {}.'),
        (
            {"version": 1, "presence": [3]},
            'Field "presence[0]": must be an entity ID such as climate.living_room, but is 3.',
        ),
        (
            {"version": 1, "away_temperature": 4.5},
            'Field "away_temperature": must be between 5 and 30 °C, but is 4.5.',
        ),
        (
            {"version": 1, "defaults": {"comfort": 21}},
            'Field "defaults.comfort": is not a known field; the known fields are: '
            "boost_duration, boost_temperature, comfort_temperature, eco_temperature, "
            "override_max, window_close_delay, window_open_delay.",
        ),
        (
            {"version": 1, "timing": {"resend_attempts": -1}},
            'Field "timing.resend_attempts": must be a whole number of at least 0, but is -1.',
        ),
        (
            {"version": 1, "timing": {"command_budget_per_hour": 0}},
            'Field "timing.command_budget_per_hour": must be a whole number of at least 1, but '
            "is 0.",
        ),
        (
            {"version": 1, "timing": {"feed_threshold": 0}},
            'Field "timing.feed_threshold": must be a temperature difference above 0 K, but is 0.',
        ),
        (
            {"version": 1, "timing": {"confirm_timeout": 0}},
            'Field "timing.confirm_timeout": must be longer than zero.',
        ),
        (
            {"version": 1, "timing": {"feed_min_interval": "00:30:00"}},
            'Field "timing.feed_min_interval": must not be longer than timing.feed_max_interval.',
        ),
        (
            {"version": 1, "timing": []},
            'Field "timing": must be a mapping of keys to values, but is [].',
        ),
        (
            {"version": 1, "romos": []},
            'Field "romos": is not a known field; the known fields '
            "are: away_delay, away_temperature, defaults, presence, rooms, timing, version.",
        ),
    ],
)
def test_top_level_problems_name_the_field(data: Any, message: str) -> None:
    """Top-level fields."""
    assert _errors(data) == [message]


def test_duplicates_across_rooms_are_found() -> None:
    """Room ids are unique; a thermostat and an external input belong to one room only."""
    errors = _errors(
        {
            "version": 1,
            "rooms": [
                _room(trvs=[{"entity": "climate.a", "external_temperature": "number.x"}]),
                _room(
                    trvs=[
                        {"entity": "climate.a"},
                        {"entity": "climate.b", "external_temperature": "number.x"},
                    ]
                ),
            ],
        }
    )
    assert errors == [
        'Room "living_room" (rooms[1]), field "id": is already used by rooms[0].',
        'Room "living_room" (rooms[1]), field "trvs[0].entity": "climate.a" is already a '
        'thermostat of room "living_room".',
        'Room "living_room" (rooms[1]), field "trvs[1].external_temperature": "number.x" is '
        "already the input of climate.a.",
    ]


def test_every_problem_is_reported_not_just_the_first() -> None:
    """Validation is complete: all problems in one go."""
    errors = _errors(
        {
            "version": 1,
            "away_delay": "soon",
            "rooms": [_room(window_close_delay=-1), _room(id="bath", trvs=[])],
        }
    )
    assert len(errors) == 3


def test_file_problems(tmp_path: Path) -> None:
    """A missing file, unreadable text, broken YAML and duplicate keys."""
    with pytest.raises(ConfigError) as missing:
        load_file(tmp_path / "nope.yaml")
    assert missing.value.errors == [f"The file {tmp_path / 'nope.yaml'} does not exist."]

    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError) as unreadable:
        load_file(binary)
    assert unreadable.value.errors[0].startswith(f"The file {binary} cannot be read")

    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nrooms:\n  - id: a\n   name: b\n")
    with pytest.raises(ConfigError) as syntax:
        load_file(broken)
    [message] = syntax.value.errors
    assert message.startswith("The file is not valid YAML: ")
    assert "(line 4, column 4)" in message
    assert "\n" not in message

    twice = tmp_path / "twice.yaml"
    twice.write_text("version: 1\nversion: 1\n")
    with pytest.raises(ConfigError) as duplicate:
        load_file(twice)
    assert "found the key 'version' twice" in duplicate.value.errors[0]
    assert "(line 2, column 1)" in duplicate.value.errors[0]


def test_new_timing_keys_have_their_defaults_and_limits() -> None:
    """budget_exhausted_hours (P6) and presence_unavailable_grace (P7)."""
    assert Timing().budget_exhausted_hours == 3
    assert Timing().presence_unavailable_grace == timedelta(minutes=30)
    assert _errors({"version": 1, "timing": {"budget_exhausted_hours": 0}}) == [
        'Field "timing.budget_exhausted_hours": must be a whole number of at least 1, but is 0.'
    ]
    assert parse(
        {"version": 1, "timing": {"presence_unavailable_grace": 0}}
    ).timing.presence_unavailable_grace == timedelta(0)


def test_a_room_thermostat_cannot_be_a_radiator_thermostat() -> None:
    """O5: climate.cave_<room> is what cave_heating creates."""
    assert _errors(
        {
            "version": 1,
            "rooms": [
                _room(trvs=[{"entity": "climate.cave_bath"}]),
                _room(id="bath", trvs=[{"entity": "climate.b"}]),
            ],
        }
    ) == [
        'Room "living_room" (rooms[0]), field "trvs[0].entity": "climate.cave_bath" is the room '
        'thermostat of room "bath" that cave_heating creates, not a radiator thermostat.'
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "version: 1\naway_delay: 99999999999999999999\n",
            'Field "away_delay": is too long to be a duration: 99999999999999999999.',
        ),
        (
            "version: 1\naway_temperature: " + "9" * 400 + "\n",
            'Field "away_temperature": must be a temperature in °C, but is ' + "9" * 400 + ".",
        ),
        (
            "version: 1\ntiming:\n  feed_threshold: " + "9" * 400 + "\n",
            'Field "timing.feed_threshold": must be a temperature difference above 0 K, but is '
            + "9" * 400
            + ".",
        ),
        ("version: 1\nrooms: " + "[" * 3000 + "\n", "The file is nested too deeply to be read."),
        (
            "version: 1\naway_delay: " + "9" * 5000 + "\n",
            "The file contains a value that cannot be read: ",
        ),
    ],
    ids=["huge_duration", "huge_temperature", "huge_threshold", "deep_nesting", "huge_integer"],
)
def test_values_the_loader_cannot_digest_are_validation_errors(
    tmp_path: Path, text: str, message: str
) -> None:
    """S6: never an exception, always a message."""
    path = tmp_path / "heating.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError) as caught:
        load_file(path)
    assert caught.value.errors[0].startswith(message)
