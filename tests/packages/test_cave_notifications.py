"""homeassistant/packages/cave_notifications.yaml, loaded into a real Home Assistant.

The package is merged into the configuration the way Home Assistant merges a package, so a
mistake in it (a key Home Assistant does not know, a template that does not render) fails here.
Stand-in entities carry the entity IDs of concept section 5.2; changing their states and moving
the clock forward shows which notifications appear and which do not.
"""

from collections.abc import Callable
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from tests.packages.helpers import (
    PACKAGE,
    Shown,
    advance,
    failed_copy,
    idle,
    load_package,
    room,
    set_helper,
    set_humidity,
)

BATTERY = {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Hall contact"}


@pytest.fixture
async def shown(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> Shown:
    """Load the package and everything it needs; return what the UI would show."""
    return await load_package(hass, freezer)


@pytest.fixture
def phone(hass: HomeAssistant) -> list[ServiceCall]:
    """A notify service called ``notify.test_phone``, recording its calls."""
    return async_mock_service(hass, "notify", "test_phone")


async def _call_script(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call("script", "cave_notify", data, blocking=True)
    await idle(hass)


# ---------------------------------------------------------------------------------------------
# script.cave_notify and input_text.cave_notify_service
# ---------------------------------------------------------------------------------------------


async def test_defaults_are_in_one_block_at_the_top_of_the_file() -> None:
    """Every default threshold and duration is a variable of the first block of the package."""
    package = load_yaml_dict(str(PACKAGE))

    assert next(iter(package)) == "template"
    variables = package["template"][0]["variables"]
    defaults = {key: value for key, value in variables.items() if key.endswith("_default")}
    assert defaults == {
        "battery_below_default": 20,
        "battery_clear_default": 30,
        "unavailable_minutes_default": 60,
        "window_open_minutes_default": 30,
        "humidity_above_default": 70,
        "humidity_clear_default": 65,
        "problem_minutes_default": 10,
    }
    assert list(variables)[:7] == list(defaults)


async def test_the_helper_and_the_script_exist_and_the_helper_starts_unset(
    hass: HomeAssistant, shown: Shown
) -> None:
    """The names the concept and `cave` rely on are there."""
    helper = hass.states.get("input_text.cave_notify_service")
    assert helper is not None
    assert helper.state in ("", "unknown")
    assert hass.services.has_service("script", "cave_notify")
    assert hass.states.get("script.cave_notify") is not None


async def test_without_a_service_only_the_persistent_notification_is_created(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall]
) -> None:
    """With the helper empty nothing is sent anywhere else."""
    await _call_script(hass, title="Backup", message="Backup finished")

    assert shown.titles == ["Backup"]
    assert shown.messages == ["Backup finished"]
    assert phone == []


@pytest.mark.parametrize("value", ["notify.test_phone", "test_phone", "  notify.test_phone  "])
async def test_with_a_service_the_message_goes_there_as_well(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], value: str
) -> None:
    """The service named in the helper receives title and message; the notification stays."""
    await set_helper(hass, "cave_notify_service", value)

    await _call_script(hass, title="Update", message="Updated to 1.2.0")

    assert shown.messages == ["Updated to 1.2.0"]
    assert [call.data for call in phone] == [{"title": "Update", "message": "Updated to 1.2.0"}]


MISSING = "Action notify.does_not_exist not found"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("notify.does_not_exist", MISSING),
        ("does_not_exist", MISSING),
        ("not a service!", "Template rendered invalid service: notify.not a service!"),
    ],
)
async def test_a_wrong_service_name_does_not_break_the_notification(
    hass: HomeAssistant,
    shown: Shown,
    phone: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    expected_errors: list[str],
    value: str,
    reason: str,
) -> None:
    """The persistent notification is there, the call returns normally, nothing is sent."""
    expected_errors.extend(failed_copy(reason))
    await set_helper(hass, "cave_notify_service", value)

    await _call_script(hass, title="Disk", message="Disk is 90 % full")

    assert shown.messages == ["Disk is 90 % full"]
    assert phone == []
    # The failure is not swallowed silently: it is in the log, for whoever looks for it.
    assert any(
        record.levelname == "ERROR" and "send a notification copy" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "value",
    ["script.cave_notify", "homeassistant.restart", "light.turn_on", "..", "a.b.c", "notify."],
)
@pytest.mark.timeout(30)  # a script that calls itself would otherwise never finish
async def test_only_a_notify_action_is_ever_called(
    hass: HomeAssistant,
    shown: Shown,
    phone: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    """Any other kind of action in the helper is logged and ignored, never run.

    `script.cave_notify` there would call itself for ever; `homeassistant.restart` would restart
    Home Assistant with every notification.
    """
    other = async_mock_service(hass, "light", "turn_on")
    restarts = async_mock_service(hass, "homeassistant", "restart")
    await set_helper(hass, "cave_notify_service", value)

    await _call_script(hass, title="Disk", message="Disk is 90 % full")
    await idle(hass)

    assert shown.messages == ["Disk is 90 % full"]
    assert phone == []
    assert other == []
    assert restarts == []
    assert any("is not a notify action" in record.getMessage() for record in caplog.records)


async def test_a_service_that_fails_does_not_break_the_notification(
    hass: HomeAssistant, shown: Shown, expected_errors: list[str]
) -> None:
    """A phone service that raises (no connection, for example) does not fail the script."""
    expected_errors.extend(failed_copy("push service is down"))

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("push service is down")

    hass.services.async_register("notify", "broken_phone", broken)
    await set_helper(hass, "cave_notify_service", "notify.broken_phone")

    await _call_script(hass, title="Disk", message="Disk is 90 % full")

    assert shown.messages == ["Disk is 90 % full"]


async def test_the_script_keeps_working_after_a_failed_service_call(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], expected_errors: list[str]
) -> None:
    """A wrong name once and the right name next: the second message arrives."""
    expected_errors.extend(failed_copy(MISSING))
    await set_helper(hass, "cave_notify_service", "notify.does_not_exist")
    await _call_script(hass, title="First", message="one")
    await set_helper(hass, "cave_notify_service", "notify.test_phone")

    await _call_script(hass, title="Second", message="two")

    assert shown.titles == ["First", "Second"]
    assert [call.data["message"] for call in phone] == ["two"]


async def test_a_notification_id_replaces_the_earlier_notification(
    hass: HomeAssistant, shown: Shown
) -> None:
    """Calls with the same id update one notification; calls without an id add one each."""
    await _call_script(hass, title="Battery", message="at 19 %", notification_id="hall")
    await _call_script(hass, title="Battery", message="at 18 %", notification_id="hall")
    assert shown.messages == ["at 18 %"]

    await _call_script(hass, title="Plain", message="a")
    await _call_script(hass, title="Plain", message="b")

    assert shown.messages == ["a", "at 18 %", "b"]


# ---------------------------------------------------------------------------------------------
# Low battery
# ---------------------------------------------------------------------------------------------


async def test_a_battery_that_falls_to_the_limit_is_reported_once(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Above the limit nothing; at it, one notification; staying there adds none."""
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    hass.states.async_set("sensor.hall_contact_battery", "55", BATTERY)
    await advance(hass, freezer, 1)
    assert shown.current == {}

    hass.states.async_set("sensor.hall_contact_battery", "21", BATTERY)
    await advance(hass, freezer, 1)
    assert shown.current == {}

    hass.states.async_set("sensor.hall_contact_battery", "20", BATTERY)
    await advance(hass, freezer, 1)
    assert shown.titles == ["Low battery"]
    assert shown.messages == ["Hall contact is at 20 %"]

    hass.states.async_set("sensor.hall_contact_battery", "19", BATTERY)
    await advance(hass, freezer, 5)
    assert shown.titles == ["Low battery"]
    assert len(phone) == 1
    assert phone[0].data == {"title": "Low battery", "message": "Hall contact is at 20 %"}


async def test_a_replaced_battery_dismisses_the_notification(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """When the level is back above the limit the notification goes away."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    await advance(hass, freezer, 1)
    assert shown.titles == ["Low battery"]

    hass.states.async_set("sensor.hall_contact_battery", "100", BATTERY)
    await advance(hass, freezer, 1)

    assert shown.current == {}


async def test_a_low_battery_flag_is_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Devices that only report low or ok (a binary battery sensor) are covered too."""
    flag = {"device_class": "battery", "friendly_name": "Gate contact"}
    hass.states.async_set("binary_sensor.gate_contact_battery", "off", flag)
    await advance(hass, freezer, 1)
    assert shown.current == {}

    hass.states.async_set("binary_sensor.gate_contact_battery", "on", flag)
    await advance(hass, freezer, 1)

    assert shown.messages == ["Gate contact reports a low battery"]


async def test_other_low_numbers_are_not_batteries(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """A cold room, an empty tank and an unavailable battery are no low-battery news."""
    hass.states.async_set("sensor.cellar_temperature", "5", {"device_class": "temperature"})
    hass.states.async_set("sensor.tank_level", "3", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.hall_contact_battery", "unavailable", BATTERY)
    hass.states.async_set("sensor.other_battery", "unknown", BATTERY)
    await advance(hass, freezer, 1)

    assert shown.current == {}


async def test_two_low_batteries_are_two_notifications(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Each sensor is reported on its own, so each can be dismissed on its own."""
    hass.states.async_set("sensor.a_battery", "10", {**BATTERY, "friendly_name": "A"})
    await advance(hass, freezer, 1)
    hass.states.async_set("sensor.b_battery", "8", {**BATTERY, "friendly_name": "B"})
    await advance(hass, freezer, 1)
    assert shown.messages == ["A is at 10 %", "B is at 8 %"]

    hass.states.async_set("sensor.a_battery", "90", {**BATTERY, "friendly_name": "A"})
    await advance(hass, freezer, 1)

    assert shown.messages == ["B is at 8 %"]


async def test_a_notification_the_person_dismissed_does_not_come_back(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Dismissing it in the UI is final until the situation changes."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    await advance(hass, freezer, 1)
    (notification_id,) = shown.current
    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notification_id}, blocking=True
    )
    assert shown.current == {}

    await advance(hass, freezer, 10)
    hass.states.async_set("sensor.hall_contact_battery", "11", BATTERY)
    await advance(hass, freezer, 2)

    assert shown.current == {}


def _restore_battery_sensor(hass: HomeAssistant, entries: dict[str, str]) -> None:
    """What Home Assistant stored for the battery sensor when it was last stopped."""
    state = State("sensor.cave_attention_battery", str(len(entries)), {"entries": entries})
    extra = {"native_value": str(len(entries)), "native_unit_of_measurement": None}
    mock_restore_cache_with_extra_data(hass, [(state, extra)])


async def test_after_a_restart_what_was_already_reported_is_not_announced_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The sensors restore what they listed, so a restart does not push everything again."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    reported = {"sensor.hall_contact_battery": "Hall contact is at 12 %"}
    _restore_battery_sensor(hass, reported)
    shown = await load_package(hass, freezer)

    await advance(hass, freezer, 3)

    assert shown.current == {}


async def test_a_new_low_battery_after_a_restart_is_announced(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The same restored list does not hide a second device that ran low in the meantime."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    hass.states.async_set("sensor.gate_battery", "9", {**BATTERY, "friendly_name": "Gate"})
    reported = {"sensor.hall_contact_battery": "Hall contact is at 12 %"}
    _restore_battery_sensor(hass, reported)
    shown = await load_package(hass, freezer)

    await advance(hass, freezer, 3)

    assert shown.messages == ["Gate is at 9 %"]


# ---------------------------------------------------------------------------------------------
# A device unavailable for too long
# ---------------------------------------------------------------------------------------------


def _device(hass: HomeAssistant, name: str, states: dict[str, str]) -> None:
    """A device with entities in the given states, registered the way an integration does."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", name)}, name=name
    )
    for entity_id, state in states.items():
        domain, object_id = entity_id.split(".")
        er.async_get(hass).async_get_or_create(
            domain,
            "test",
            f"{name}_{object_id}",
            suggested_object_id=object_id,
            device_id=device.id,
        )
        hass.states.async_set(entity_id, state)


async def test_a_device_unavailable_for_an_hour_is_reported_and_then_dismissed(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Not before the time limit, once after it, and gone when the device comes back."""
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    _device(
        hass,
        "Bedroom window contact",
        {"binary_sensor.bedroom_contact": "off", "sensor.bedroom_contact_battery": "80"},
    )
    await advance(hass, freezer, 2)

    hass.states.async_set("binary_sensor.bedroom_contact", "unavailable")
    hass.states.async_set("sensor.bedroom_contact_battery", "unavailable")
    await advance(hass, freezer, 59)
    assert shown.current == {}

    await advance(hass, freezer, 2)
    assert shown.titles == ["Device unavailable"]
    assert shown.messages == [
        "Bedroom window contact has been unavailable for more than 60 minutes"
    ]
    await advance(hass, freezer, 30)
    assert len(phone) == 1

    hass.states.async_set("binary_sensor.bedroom_contact", "off")
    hass.states.async_set("sensor.bedroom_contact_battery", "80")
    await advance(hass, freezer, 1)
    assert shown.current == {}


async def test_a_device_with_one_entity_still_available_is_not_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Some entities of a device may be unavailable on their own; the device is not gone."""
    _device(hass, "Thermostat", {"climate.half": "unavailable", "sensor.half_temperature": "19.5"})

    await advance(hass, freezer, 90)

    assert shown.current == {}


async def test_an_entity_without_a_device_is_not_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Helpers and the like are not devices that went silent."""
    hass.states.async_set("sensor.some_helper", "unavailable")

    await advance(hass, freezer, 90)

    assert shown.current == {}


async def test_a_device_that_flaps_does_not_count_its_old_time(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """The hour is counted from the last time the device went unavailable."""
    _device(hass, "Flaky sensor", {"sensor.flaky": "20"})
    for _ in range(3):
        hass.states.async_set("sensor.flaky", "unavailable")
        await advance(hass, freezer, 40)
        hass.states.async_set("sensor.flaky", "20")
        await advance(hass, freezer, 1)

    assert shown.current == {}


# ---------------------------------------------------------------------------------------------
# A window left open
# ---------------------------------------------------------------------------------------------


def _room_window_sensor(hass: HomeAssistant, entity_id: str, area_name: str, state: str) -> None:
    """A stand-in for a room's window-pause sensor, placed in an area."""
    area = ar.async_get(hass).async_get_or_create(area_name)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(domain, "test", entity_id, suggested_object_id=object_id)
    er.async_get(hass).async_update_entity(entity_id, area_id=area.id)
    hass.states.async_set(entity_id, state)


async def test_a_window_pause_longer_than_half_an_hour_is_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Named by the room's area; dismissed when the window is closed."""
    _room_window_sensor(hass, "binary_sensor.cave_living_room_window_open", "Wohnzimmer", "off")
    await advance(hass, freezer, 1)
    hass.states.async_set("binary_sensor.cave_living_room_window_open", "on")

    await advance(hass, freezer, 29)
    assert shown.current == {}
    await advance(hass, freezer, 2)
    assert shown.titles == ["Window left open"]
    assert shown.messages == [
        "Wohnzimmer: the heating has been paused for more than 30 minutes because a window is open"
    ]

    hass.states.async_set("binary_sensor.cave_living_room_window_open", "off")
    await advance(hass, freezer, 1)
    assert shown.current == {}


async def test_a_short_window_pause_is_not_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Airing for ten minutes is what a window is for."""
    hass.states.async_set("binary_sensor.cave_bathroom_window_open", "on")
    await advance(hass, freezer, 10)
    hass.states.async_set("binary_sensor.cave_bathroom_window_open", "off")
    await advance(hass, freezer, 60)

    assert shown.current == {}


async def test_only_the_rooms_window_pause_sensors_count(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Window contacts and other binary sensors that stay on are not the room's pause."""
    hass.states.async_set("binary_sensor.wohnzimmer_fenster_contact", "on")
    hass.states.async_set("binary_sensor.shed_window_open", "on")
    hass.states.async_set("binary_sensor.cave_home_occupied", "on")

    await advance(hass, freezer, 120)

    assert shown.current == {}


# ---------------------------------------------------------------------------------------------
# High humidity
# ---------------------------------------------------------------------------------------------

HUMIDITY = {"device_class": "humidity", "unit_of_measurement": "%", "friendly_name": "Sensor"}


async def test_high_humidity_prompts_once_and_clears_with_a_margin(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Above 70 % it notifies; it stays until below 65 %, so a hovering reading says nothing new."""
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    room(hass, "Bad", "bathroom", humidity=69)
    await advance(hass, freezer, 2)
    assert shown.current == {}

    set_humidity(hass, "bathroom", 71)
    await advance(hass, freezer, 1)
    assert shown.titles == ["High humidity"]
    assert shown.messages == ["Bad: humidity is 71 %, time to air the room"]

    for value in (69, 72, 66, 70, 67):
        set_humidity(hass, "bathroom", value)
        await advance(hass, freezer, 1)
    assert shown.titles == ["High humidity"]
    assert len(phone) == 1

    set_humidity(hass, "bathroom", 64)
    await advance(hass, freezer, 1)
    assert shown.current == {}

    set_humidity(hass, "bathroom", 75)
    await advance(hass, freezer, 1)
    assert shown.titles == ["High humidity"]
    assert len(phone) == 2


async def test_humidity_needs_no_area_on_the_sensor_and_is_named_by_the_room(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """The room's reading comes from its thermostat, so a Zigbee sensor without an area counts."""
    room(hass, "Bad", "bathroom", humidity=80)
    hass.states.async_set("sensor.bad_humidity", "80", HUMIDITY)  # no area, not asked
    await advance(hass, freezer, 1)

    assert shown.messages == ["Bad: humidity is 80 %, time to air the room"]


async def test_a_room_without_a_reading_is_not_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """No humidity sensor, or an unavailable one: the attribute is absent, nothing is said."""
    room(hass, "Bad", "bathroom")
    room(hass, "Flur", "hall")
    await advance(hass, freezer, 5)

    assert shown.current == {}


async def test_other_percentages_are_not_humidity(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """Only sensors whose device class is humidity are looked at."""
    hass.states.async_set("sensor.tank_level", "95", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.hall_contact_battery", "95", BATTERY)
    await advance(hass, freezer, 1)

    assert shown.current == {}


# ---------------------------------------------------------------------------------------------
# A room's problem sensor
# ---------------------------------------------------------------------------------------------


async def test_a_problem_lasting_ten_minutes_is_reported_with_what_is_wrong(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """The sentence carries the messages of the sensor; it goes when the problem does."""
    _room_window_sensor(hass, "binary_sensor.cave_bedroom_problem", "Schlafzimmer", "off")
    await advance(hass, freezer, 1)
    hass.states.async_set(
        "binary_sensor.cave_bedroom_problem",
        "on",
        {"messages": ["Thermostat Heizung does not respond", "Window contact is unavailable"]},
    )

    await advance(hass, freezer, 9)
    assert shown.current == {}
    await advance(hass, freezer, 2)
    assert shown.titles == ["Heating problem"]
    assert shown.messages == [
        "Schlafzimmer: Thermostat Heizung does not respond; Window contact is unavailable"
    ]

    hass.states.async_set("binary_sensor.cave_bedroom_problem", "off", {"messages": []})
    await advance(hass, freezer, 1)
    assert shown.current == {}


async def test_a_problem_that_clears_quickly_is_never_reported(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """A thermostat that is only waiting for its confirmation is no reason to say anything."""
    hass.states.async_set("binary_sensor.cave_bedroom_problem", "on", {"messages": ["pending"]})
    await advance(hass, freezer, 6)
    hass.states.async_set("binary_sensor.cave_bedroom_problem", "off", {"messages": []})

    await advance(hass, freezer, 30)

    assert shown.current == {}


async def test_a_problem_without_messages_still_says_something(
    hass: HomeAssistant, shown: Shown, freezer: FrozenDateTimeFactory
) -> None:
    """The sentence is never empty."""
    hass.states.async_set("binary_sensor.cave_bathroom_problem", "on")

    await advance(hass, freezer, 12)

    assert shown.messages == ["cave bathroom problem: something needs attention"]


@pytest.mark.parametrize(
    "setup",
    [
        lambda hass: hass.states.async_set("binary_sensor.window_problem", "on"),
        lambda hass: hass.states.async_set("binary_sensor.cave_home_occupied", "on"),
    ],
    ids=["other_problem_sensor", "other_cave_entity"],
)
async def test_only_the_rooms_problem_sensors_count(
    hass: HomeAssistant,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    setup: Callable[[HomeAssistant], None],
) -> None:
    """Sensors that merely look similar are not the heating's."""
    setup(hass)

    await advance(hass, freezer, 60)

    assert shown.current == {}


async def test_several_things_at_once_are_separate_notifications(
    hass: HomeAssistant, shown: Shown, phone: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """A low battery and a problem together give two notifications and two pushes."""
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    hass.states.async_set("sensor.hall_contact_battery", "10", BATTERY)
    hass.states.async_set("binary_sensor.cave_bedroom_problem", "on", {"messages": ["x"]})
    await advance(hass, freezer, 12)

    assert shown.titles == ["Heating problem", "Low battery"]
    assert sorted(call.data["title"] for call in phone) == ["Heating problem", "Low battery"]
