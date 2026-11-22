"""The notifications package across a restart, with many entries at once, and with its helpers.

A restart is simulated the way Home Assistant does it: persistent notifications are gone (a new
test instance has none), the template sensors restore what they had listed (the restore cache),
entities come back with a fresh ``last_changed`` and sometimes as ``unknown`` or ``unavailable``,
and the start event is fired.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import (
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import entity_sources
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

from tests.packages.helpers import (
    PACKAGE,
    START,
    Shown,
    advance,
    in_area,
    load_package,
    room,
    set_helper,
    set_humidity,
)

BATTERY = {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Hall contact"}
HUMIDITY = {"device_class": "humidity", "unit_of_measurement": "%", "friendly_name": "Sensor"}
WINDOW = "binary_sensor.cave_living_room_window_open"
PROBLEM = "binary_sensor.cave_living_room_problem"
WINDOW_TEXT = (
    "Wohnzimmer: the heating has been paused for more than 30 minutes because a window is open"
)


@pytest.fixture
def phone(hass: HomeAssistant) -> list[ServiceCall]:
    """A notify service called ``notify.test_phone``, recording its calls."""
    return async_mock_service(hass, "notify", "test_phone")


async def restart(
    hass: HomeAssistant,
    restored: dict[str, dict[str, str]],
    attributes: dict[str, dict[str, Any]] | None = None,
) -> Shown:
    """Start Home Assistant with the sensors' last entries restored and nothing in the UI.

    ``restored`` maps a kind (``battery``, ``windows``, ...) to the entries it had listed, and
    ``attributes`` a kind to further attributes its sensor had (``missing_since``).
    States the test sets before this call are what the entities have at start-up.
    """
    stored = []
    for kind, entries in restored.items():
        more = (attributes or {}).get(kind, {})
        state = State(
            f"sensor.cave_attention_{kind}", str(len(entries)), {"entries": entries, **more}
        )
        extra = {"native_value": str(len(entries)), "native_unit_of_measurement": None}
        stored.append((state, extra))
    mock_restore_cache_with_extra_data(hass, stored)
    shown = await load_package(hass)
    await use_phone(hass)  # the owner's setting is there before the first evaluation
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    return shown


async def use_phone(hass: HomeAssistant) -> None:
    await set_helper(hass, "cave_notify_service", "notify.test_phone")


def add_device(hass: HomeAssistant, name: str, states: dict[str, str]) -> None:
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


# ---------------------------------------------------------------------------------------------
# Restarts
# ---------------------------------------------------------------------------------------------


async def test_after_a_restart_a_battery_that_is_still_low_is_shown_again_and_not_pushed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """A battery at 12 % for three weeks is in the UI after the nightly restart, silently."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown = await restart(hass, {"battery": {"sensor.hall_contact_battery": "old sentence"}})
    assert shown.titles == ["Low battery"], "shown again at start"

    await advance(hass, freezer, 5)

    assert shown.titles == ["Low battery"]
    assert shown.messages == ["Hall contact is at 12 %"]
    assert phone == [], "the phone was told before the restart"


async def test_after_a_restart_a_battery_that_was_replaced_meanwhile_goes_away(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """What is shown again at start is not kept for ever: the first evaluation corrects it."""
    hass.states.async_set("sensor.hall_contact_battery", "100", BATTERY)
    shown = await restart(hass, {"battery": {"sensor.hall_contact_battery": "old sentence"}})

    await advance(hass, freezer, 3)

    assert shown.current == {}
    assert phone == []


async def test_after_a_restart_a_battery_that_is_unknown_until_zigbee2mqtt_reports_is_not_new(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Zigbee2MQTT republishes a minute or two after start; until then the state is unknown."""
    hass.states.async_set("sensor.hall_contact_battery", "unknown", BATTERY)
    shown = await restart(
        hass, {"battery": {"sensor.hall_contact_battery": "Hall contact is at 12 %"}}
    )
    await advance(hass, freezer, 2)
    assert shown.messages == ["Hall contact is at 12 %"], "kept while the reading is missing"

    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    await advance(hass, freezer, 3)

    assert shown.messages == ["Hall contact is at 12 %"]
    assert phone == []


async def test_after_a_restart_a_window_pause_is_not_announced_again_however_young_it_looks(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The window pause is restored as on; its last_changed is the start, not the window."""
    room(hass, "Wohnzimmer", "living_room")
    in_area(hass, WINDOW, "Wohnzimmer")
    hass.states.async_set(WINDOW, "on")
    shown = await restart(hass, {"windows": {WINDOW: WINDOW_TEXT}})
    assert shown.messages == [WINDOW_TEXT]

    await advance(hass, freezer, 45)

    assert shown.messages == [WINDOW_TEXT], "still there after the restart's own thirty minutes"
    assert phone == [], "and never pushed again"


async def test_after_a_restart_a_window_that_was_closed_meanwhile_goes_away(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    room(hass, "Wohnzimmer", "living_room")
    hass.states.async_set(WINDOW, "off")
    shown = await restart(hass, {"windows": {WINDOW: WINDOW_TEXT}})

    await advance(hass, freezer, 3)

    assert shown.current == {}
    assert phone == []


async def test_after_a_restart_a_problem_that_goes_on_is_not_announced_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """An ongoing heating problem is not pushed again every night."""
    room(hass, "Wohnzimmer", "living_room")
    in_area(hass, PROBLEM, "Wohnzimmer")
    hass.states.async_set(PROBLEM, "on", {"messages": ["Thermostat does not respond"]})
    text = "Wohnzimmer: Thermostat does not respond"
    shown = await restart(hass, {"problems": {PROBLEM: text}})

    await advance(hass, freezer, 30)

    assert shown.messages == [text]
    assert phone == []


async def test_after_a_restart_a_problem_sensor_that_is_not_there_yet_keeps_its_entry(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The integration's entities can come after the first evaluation: unknown, then on."""
    room(hass, "Wohnzimmer", "living_room")
    in_area(hass, PROBLEM, "Wohnzimmer")
    hass.states.async_set(PROBLEM, "unknown")
    text = "Wohnzimmer: Thermostat does not respond"
    shown = await restart(hass, {"problems": {PROBLEM: text}})
    await advance(hass, freezer, 3)
    assert shown.messages == [text]

    hass.states.async_set(PROBLEM, "on", {"messages": ["Thermostat does not respond"]})
    await advance(hass, freezer, 15)

    assert shown.messages == [text]
    assert phone == []


async def test_after_a_restart_a_device_that_is_still_dead_is_not_announced_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Its entities are unavailable again with a young last_changed; that is not a new outage."""
    add_device(hass, "Dead contact", {"binary_sensor.dead": "unavailable"})
    entity = er.async_get(hass).async_get("binary_sensor.dead")
    assert entity is not None
    assert entity.device_id is not None
    text = "Dead contact has been unavailable for more than 60 minutes"
    shown = await restart(hass, {"unavailable": {entity.device_id: text}})

    await advance(hass, freezer, 75)

    assert shown.messages == [text]
    assert phone == []


async def test_after_a_restart_a_device_that_is_only_slow_to_connect_is_not_reported(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Every Zigbee entity is unavailable at start-up; that is not 60 minutes of silence."""
    add_device(hass, "Hall contact", {"binary_sensor.hall": "unavailable"})
    shown = await restart(hass, {})

    await advance(hass, freezer, 30)
    hass.states.async_set("binary_sensor.hall", "off")
    await advance(hass, freezer, 60)

    assert shown.current == {}
    assert phone == []


async def test_after_a_restart_high_humidity_that_goes_on_is_not_announced_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    room(hass, "Bad", "bathroom")  # the humidity sensor has not reported yet: no reading
    text = "Bad: humidity is 80 %, time to air the room"
    shown = await restart(hass, {"humidity": {"climate.cave_bathroom": text}})
    await advance(hass, freezer, 2)

    set_humidity(hass, "bathroom", 72)
    await advance(hass, freezer, 3)

    assert shown.messages == [text], "the one notification, as it was reported"
    assert phone == []


HUMIDITY_TEXT = "Bad: humidity is 80 %, time to air the room"


async def test_high_humidity_ends_when_the_room_has_had_no_reading_for_an_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The humidity sensor was taken out of the room, or its battery died: the entry ends.

    Before, an entry stayed for as long as the reading was missing, which could be for ever.
    """
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    room(hass, "Bad", "bathroom", humidity=80)
    await advance(hass, freezer, 2)
    assert shown.messages == [HUMIDITY_TEXT]

    set_humidity(hass, "bathroom", None)
    await advance(hass, freezer, 59)
    assert shown.messages == [HUMIDITY_TEXT], "kept while the reading is missing for less"
    await advance(hass, freezer, 2)

    assert shown.current == {}
    assert hass.states.get("sensor.cave_attention_humidity").attributes["missing_since"] == {}
    assert len(phone) == 1, "the end is not pushed"


async def test_a_reading_that_comes_back_within_the_hour_starts_the_hour_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A gap in the readings is forgotten once a reading arrives; the next gap counts anew."""
    shown = await load_package(hass, freezer)
    room(hass, "Bad", "bathroom", humidity=80)
    await advance(hass, freezer, 2)
    set_humidity(hass, "bathroom", None)
    await advance(hass, freezer, 40)

    set_humidity(hass, "bathroom", 75)
    await advance(hass, freezer, 1)
    set_humidity(hass, "bathroom", None)
    await advance(hass, freezer, 40)

    assert shown.messages == [HUMIDITY_TEXT]
    await advance(hass, freezer, 21)
    assert shown.current == {}


async def test_after_a_restart_a_room_without_a_reading_for_an_hour_is_not_shown_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The room has had no humidity sensor since before the restart: it does not come back."""
    room(hass, "Bad", "bathroom")
    since = (START - timedelta(hours=2)).isoformat()
    shown = await restart(
        hass,
        {"humidity": {"climate.cave_bathroom": HUMIDITY_TEXT}},
        {"humidity": {"missing_since": {"climate.cave_bathroom": since}}},
    )
    assert shown.current == {}, "shown again at start"

    await advance(hass, freezer, 5)

    assert shown.current == {}
    assert phone == []


async def test_after_a_restart_a_reading_missing_for_less_than_an_hour_keeps_its_entry(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The hour counts from when the reading went missing, also across the restart."""
    room(hass, "Bad", "bathroom")
    since = (START - timedelta(minutes=50)).isoformat()
    shown = await restart(
        hass,
        {"humidity": {"climate.cave_bathroom": HUMIDITY_TEXT}},
        {"humidity": {"missing_since": {"climate.cave_bathroom": since}}},
    )
    assert shown.messages == [HUMIDITY_TEXT]

    await advance(hass, freezer, 8)
    assert shown.messages == [HUMIDITY_TEXT]
    await advance(hass, freezer, 3)

    assert shown.current == {}
    assert phone == []


async def test_after_a_restart_an_entry_whose_reading_never_returns_ends_after_an_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Reported before the restart with a reading, and none since: an hour from the start."""
    room(hass, "Bad", "bathroom")
    shown = await restart(hass, {"humidity": {"climate.cave_bathroom": HUMIDITY_TEXT}})

    await advance(hass, freezer, 59)
    assert shown.messages == [HUMIDITY_TEXT]
    await advance(hass, freezer, 2)

    assert shown.current == {}
    assert phone == []


async def test_a_restart_with_nothing_reported_shows_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    shown = await restart(hass, {})
    await advance(hass, freezer, 5)

    assert shown.current == {}
    assert phone == []


async def test_a_new_condition_after_a_restart_is_still_announced(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Keeping what was reported must not hide what is new."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    hass.states.async_set("sensor.gate_battery", "9", {**BATTERY, "friendly_name": "Gate"})
    shown = await restart(
        hass, {"battery": {"sensor.hall_contact_battery": "Hall contact is at 12 %"}}
    )

    await advance(hass, freezer, 3)

    assert shown.messages == ["Gate is at 9 %", "Hall contact is at 12 %"]
    assert [call.data["message"] for call in phone] == ["Gate is at 9 %"]


# ---------------------------------------------------------------------------------------------
# A real start (issue #31)
# ---------------------------------------------------------------------------------------------

# The package's automations, restored as Home Assistant restores them: on, with the time they
# last ran before the restart. Nothing that is restored may stand in for "this start has been
# seen": in a real home it is all there from the first moment.
AUTOMATIONS = (
    "automation.cave_send_a_notification_copy",
    "automation.cave_show_reported_notifications_again_after_a_restart",
    "automation.cave_notify_about_what_needs_attention",
)
LAST_RAN_BEFORE_THE_RESTART = START - timedelta(hours=7)

# Where in its minute the start lands. At second 0.6, with the tracker's offset of 100 ms, the
# minute's slot falls inside the start phase: the time pattern that is attached there fires at
# once. At second 30 the start is over long before the next slot.
START_SECONDS = pytest.mark.parametrize(
    "start_second", [30.0, 0.6], ids=["start-at-second-30", "start-at-second-0.6"]
)


def _other_integration_at_start(_event: Event) -> None:
    """Any integration that does work at start: not a callback, so it runs in the executor."""


async def real_start(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    start_second: float,
    restored: dict[str, dict[str, str]] | None,
) -> tuple[Shown, list[CoreState]]:
    """Start Home Assistant for real, from not running, at ``start_second`` of a minute.

    Unlike ``restart``, which sets the package up on a running Home Assistant and fires the start
    events by hand, this goes through ``async_start``: the template integration attaches the
    sensors' triggers at START, and the automations are armed only after every START listener
    has finished. ``restored`` maps a kind to the entries its sensor had listed, or is ``None``
    for a first install, where no sensor has anything to restore; the automations are always
    restored with a ``last_triggered``. States the test sets before this call are what the
    entities have at start-up.

    Returns what the UI shows and the core state at each minute's check (the event
    ``cave_attention_check``) that came during the start: one, while Home Assistant is still
    starting, when the start lands at the beginning of a minute, and none at second 30. The
    clock is back at second 30 of the minute after ``START`` when it returns, for ``advance``.
    """
    hass.set_state(CoreState.not_running)
    stored: list[tuple[State, dict[str, Any]]] = []
    for kind, entries in (restored or {}).items():
        state = State(f"sensor.cave_attention_{kind}", str(len(entries)), {"entries": entries})
        stored.append(
            (state, {"native_value": str(len(entries)), "native_unit_of_measurement": None})
        )
    for automation in AUTOMATIONS:
        last = LAST_RAN_BEFORE_THE_RESTART.isoformat()
        stored.append((State(automation, "on", {"last_triggered": last}), {}))
    mock_restore_cache_with_extra_data(hass, stored)
    shown = await load_package(hass)
    await use_phone(hass)
    for automation in AUTOMATIONS:
        last_triggered = hass.states.get(automation).attributes["last_triggered"]
        assert last_triggered == LAST_RAN_BEFORE_THE_RESTART, f"{automation} not restored"
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _other_integration_at_start)
    checks: list[CoreState] = []
    hass.bus.async_listen("cave_attention_check", callback(lambda _: checks.append(hass.state)))
    freezer.move_to(START.replace(second=0) + timedelta(seconds=start_second))
    with patch("homeassistant.helpers.event.randint", return_value=100000):
        await hass.async_start()
        await hass.async_block_till_done()
    assert hass.state is CoreState.running
    freezer.move_to(START + timedelta(minutes=1))  # back to second 30, see check_clock
    return shown, checks


@START_SECONDS
async def test_a_new_condition_across_a_real_start_is_announced(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    start_second: float,
) -> None:
    """A battery that ran low while Home Assistant was down is pushed once, wherever it starts.

    Before the fix of issue #31, a start at second 0.6 evaluated the sensors before the
    automation that pushes was listening: the gate's battery was shown, never pushed.
    """
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    hass.states.async_set("sensor.gate_battery", "9", {**BATTERY, "friendly_name": "Gate"})
    shown, checks = await real_start(
        hass,
        freezer,
        start_second,
        {"battery": {"sensor.hall_contact_battery": "Hall contact is at 12 %"}},
    )
    # At second 0.6 the minute's slot still falls inside the start phase; its check now comes
    # from the automation that pushes, so it is seen.
    assert checks == ([CoreState.starting] if start_second < 1 else [])

    await advance(hass, freezer, 3)

    assert shown.messages == ["Gate is at 9 %", "Hall contact is at 12 %"]
    assert [call.data["message"] for call in phone] == ["Gate is at 9 %"]


@START_SECONDS
async def test_a_condition_that_ended_during_a_real_start_is_gone_and_not_pushed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    start_second: float,
) -> None:
    """The battery was replaced while Home Assistant was down: nothing in the UI, nothing sent.

    The restore automation must not bring the old notification back after the first evaluation
    has dropped it, in whichever order the two run.
    """
    hass.states.async_set("sensor.hall_contact_battery", "100", BATTERY)
    hass.states.async_set("binary_sensor.gate_battery", "off", {"device_class": "battery"})
    shown, _ = await real_start(
        hass,
        freezer,
        start_second,
        {
            "battery": {
                "sensor.hall_contact_battery": "Hall contact is at 12 %",
                "binary_sensor.gate_battery": "Gate reports a low battery",
            }
        },
    )
    assert shown.current == {}, "shown again although it had ended"

    await advance(hass, freezer, 5)

    assert shown.current == {}
    assert phone == []


@START_SECONDS
async def test_many_new_conditions_across_a_real_start_are_one_summary(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    start_second: float,
) -> None:
    """More than the group limit new since before the restart: one push, one notification each."""
    for number in range(5):
        hass.states.async_set(
            f"sensor.b{number}_battery", "10", {**BATTERY, "friendly_name": f"B{number}"}
        )
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown, _ = await real_start(
        hass,
        freezer,
        start_second,
        {"battery": {"sensor.hall_contact_battery": "Hall contact is at 12 %"}},
    )

    await advance(hass, freezer, 3)

    assert len(shown.current) == 6
    assert len(phone) == 1
    assert phone[0].data["message"].startswith("5 new: B")


@START_SECONDS
async def test_a_dismissed_notification_comes_back_once_after_a_real_start_and_stays_dismissed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    start_second: float,
) -> None:
    """Dismissals are not remembered across a restart: it is shown again, silently, once.

    Dismissed again after the start, it stays away while the condition goes on.
    """
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown, _ = await real_start(
        hass,
        freezer,
        start_second,
        {"battery": {"sensor.hall_contact_battery": "Hall contact is at 12 %"}},
    )
    assert shown.messages == ["Hall contact is at 12 %"]

    (notification_id,) = shown.current
    await hass.services.async_call(
        "persistent_notification", "dismiss", {"notification_id": notification_id}, blocking=True
    )
    await advance(hass, freezer, 10)

    assert shown.current == {}
    assert phone == []


@START_SECONDS
async def test_the_first_real_start_shows_what_exists_and_pushes_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    start_second: float,
) -> None:
    """A first install: nothing to compare with, so what already holds is shown, not pushed."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown, _ = await real_start(hass, freezer, start_second, None)

    await advance(hass, freezer, 3)
    assert shown.messages == ["Hall contact is at 12 %"]
    assert phone == []

    hass.states.async_set("sensor.gate_battery", "9", {**BATTERY, "friendly_name": "Gate"})
    await advance(hass, freezer, 2)
    assert [call.data["message"] for call in phone] == ["Gate is at 9 %"], "later ones are news"


# ---------------------------------------------------------------------------------------------
# Reloading the template entities, and the first evaluation
# ---------------------------------------------------------------------------------------------


async def test_reloading_the_template_entities_does_not_push_what_is_reported_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Developer tools, YAML, "Template entities": the sensors are removed and added again.

    That is not a change in the home. What they list is shown, nothing is dismissed and the
    phone hears nothing a second time.
    """
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    await advance(hass, freezer, 2)
    assert len(phone) == 1
    assert shown.titles == ["Low battery"]

    package = load_yaml_dict(str(PACKAGE))
    reloaded = {"homeassistant": {"packages": {"cave_notifications": package}}}
    with patch("homeassistant.config.load_yaml_config_file", return_value=reloaded):
        await hass.services.async_call("template", "reload", blocking=True)
        await hass.async_block_till_done()
    await advance(hass, freezer, 5)

    assert len(phone) == 1, "no second push"
    assert shown.removed == [], "nothing was dismissed"
    assert shown.titles == ["Low battery"]
    assert hass.states.get("sensor.cave_attention_battery") is not None


async def test_what_exists_when_the_sensors_are_first_added_is_shown_and_not_pushed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """A first install, or a first evaluation without restored entries, is the start-up case."""
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    shown = await load_package(hass)
    await use_phone(hass)

    await advance(hass, freezer, 3)

    assert shown.titles == ["Low battery"]
    assert phone == []
    assert shown.removed == []

    hass.states.async_set("sensor.gate_battery", "9", {**BATTERY, "friendly_name": "Gate"})
    await advance(hass, freezer, 2)
    assert [call.data["message"] for call in phone] == ["Gate is at 9 %"], "later ones are news"


async def test_the_automation_copes_with_a_sensor_without_a_group_limit(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """A state that has `entries` but no `group_limit` (an older state) does not fail the run."""
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    hass.states.async_set("sensor.cave_attention_humidity", "0", {"entries": {}})
    hass.states.async_set(
        "sensor.cave_attention_humidity",
        "1",
        {"entries": {"climate.cave_x": "X: humidity is 80 %, time to air the room"}},
    )
    await hass.async_block_till_done()

    assert shown.titles == ["High humidity"]
    assert len(phone) == 1


# ---------------------------------------------------------------------------------------------
# The phone's own battery, and batteries that hover
# ---------------------------------------------------------------------------------------------


async def test_the_battery_of_a_phone_with_the_app_is_not_a_home_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """The Companion app's battery sensor has the device class battery; it is nobody's radiator."""
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    # `integration_entities('mobile_app')` asks which integration added an entity; Home Assistant
    # records that for every entity a platform adds, and the test records it by hand.
    entity_sources(hass)["sensor.pixel_8_battery_level"] = {
        "domain": "mobile_app",
        "custom_component": False,
    }
    hass.states.async_set(
        "sensor.pixel_8_battery_level", "12", {**BATTERY, "friendly_name": "Pixel 8 battery level"}
    )
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)

    await advance(hass, freezer, 3)

    assert shown.messages == ["Hall contact is at 12 %"]
    assert [call.data["message"] for call in phone] == ["Hall contact is at 12 %"]


async def test_a_battery_hovering_around_the_limit_notifies_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """21, 20, 21, 20, ... every half hour is one notification, not one per dip."""
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    for value in ("21", "20", "21", "20", "21", "20", "24", "29"):
        hass.states.async_set("sensor.hall_contact_battery", value, BATTERY)
        await advance(hass, freezer, 30)

    assert shown.messages == ["Hall contact is at 20 %"], "the sentence of the one notification"
    assert len(phone) == 1

    hass.states.async_set("sensor.hall_contact_battery", "30", BATTERY)
    await advance(hass, freezer, 2)
    assert shown.current == {}, "cleared at 30 %"

    hass.states.async_set("sensor.hall_contact_battery", "19", BATTERY)
    await advance(hass, freezer, 2)
    assert len(phone) == 2, "a new dip below the limit is a new notification"


# ---------------------------------------------------------------------------------------------
# Many at once
# ---------------------------------------------------------------------------------------------


async def test_a_coordinator_outage_is_one_push_and_one_notification_per_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """Thirty devices that vanish together: thirty entries in the UI, one summary on the phone."""
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    for number in range(30):
        add_device(hass, f"Sensor {number:02d}", {f"sensor.zigbee_{number:02d}": "20"})
    await advance(hass, freezer, 2)
    for number in range(30):
        hass.states.async_set(f"sensor.zigbee_{number:02d}", "unavailable")

    await advance(hass, freezer, 65)

    assert len(shown.current) == 30
    assert shown.titles == ["Device unavailable"] * 30
    assert len(phone) == 1
    assert phone[0].data["title"] == "Device unavailable"
    assert phone[0].data["message"].startswith("30 new: Sensor ")
    assert len(phone[0].data["message"]) < 600

    for number in range(30):
        hass.states.async_set(f"sensor.zigbee_{number:02d}", "20")
    await advance(hass, freezer, 2)
    assert shown.current == {}
    assert len(phone) == 1, "recovery is not pushed"


@pytest.mark.parametrize(("count", "pushes"), [(1, 1), (3, 3), (4, 1), (12, 1)])
async def test_up_to_three_new_entries_are_pushed_one_by_one_more_as_one_summary(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    phone: list[ServiceCall],
    count: int,
    pushes: int,
) -> None:
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    for number in range(count):
        hass.states.async_set(f"sensor.b{number}_battery", "10", BATTERY)

    await advance(hass, freezer, 2)

    assert len(shown.current) == count
    assert len(phone) == pushes


# ---------------------------------------------------------------------------------------------
# What counts as a room's humidity
# ---------------------------------------------------------------------------------------------


async def test_only_the_humidity_of_a_room_prompts_to_air(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, phone: list[ServiceCall]
) -> None:
    """A cellar, a garden and a sensor that belongs to no room have no room thermostat."""
    shown = await load_package(hass, freezer)
    await use_phone(hass)
    room(hass, "Bad", "bathroom", humidity=92)
    room(hass, "Flur", "hall")  # a room without a humidity sensor
    in_area(hass, "sensor.cellar_humidity", "Keller")
    for entity in ("bath", "cellar", "garden", "nowhere"):
        attributes = {**HUMIDITY, "friendly_name": entity}
        hass.states.async_set(f"sensor.{entity}_humidity", "92", attributes)

    await advance(hass, freezer, 3)

    assert shown.messages == ["Bad: humidity is 92 %, time to air the room"]
    assert len(phone) == 1


# ---------------------------------------------------------------------------------------------
# The helpers the owner sets
# ---------------------------------------------------------------------------------------------


async def test_no_helper_of_the_package_has_an_initial_value() -> None:
    """What the owner enters must survive restarts and updates, so nothing resets it."""
    helpers = load_yaml_dict(str(PACKAGE))["input_text"]

    assert sorted(helpers) == [
        "cave_notify_battery_below",
        "cave_notify_battery_clear_above",
        "cave_notify_humidity_above",
        "cave_notify_humidity_clear_below",
        "cave_notify_problem_minutes",
        "cave_notify_service",
        "cave_notify_unavailable_minutes",
        "cave_notify_window_open_minutes",
    ]
    assert all("initial" not in config for config in helpers.values())


@pytest.mark.parametrize("value", ["", None])
async def test_an_empty_or_unset_helper_means_the_default(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, value: str | None
) -> None:
    """Twenty percent is the limit when the helper says nothing: empty, or never set."""
    shown = await load_package(hass, freezer)
    if value is not None:
        await set_helper(hass, "cave_notify_battery_below", value)
    hass.states.async_set("sensor.a_battery", "25", {**BATTERY, "friendly_name": "A"})
    hass.states.async_set("sensor.b_battery", "19", {**BATTERY, "friendly_name": "B"})

    await advance(hass, freezer, 2)

    assert shown.messages == ["B is at 19 %"]


async def test_text_that_is_not_a_number_is_refused_by_the_helper_and_the_default_stands(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    shown = await load_package(hass, freezer)

    with pytest.raises(ValueError, match="does not match expected pattern"):
        await set_helper(hass, "cave_notify_battery_below", "abc")
    hass.states.async_set("sensor.a_battery", "25", {**BATTERY, "friendly_name": "A"})
    await advance(hass, freezer, 2)

    assert shown.current == {}


async def test_the_battery_limit_the_owner_enters_is_the_one_used(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    shown = await load_package(hass, freezer)
    await set_helper(hass, "cave_notify_battery_below", "30")
    await set_helper(hass, "cave_notify_battery_clear_above", "50")
    hass.states.async_set("sensor.a_battery", "25", {**BATTERY, "friendly_name": "A"})

    await advance(hass, freezer, 2)
    assert shown.messages == ["A is at 25 %"]

    hass.states.async_set("sensor.a_battery", "45", {**BATTERY, "friendly_name": "A"})
    await advance(hass, freezer, 2)
    assert shown.messages == ["A is at 25 %"], "still there: not clear before 50"

    hass.states.async_set("sensor.a_battery", "50", {**BATTERY, "friendly_name": "A"})
    await advance(hass, freezer, 2)
    assert shown.current == {}


async def test_the_durations_the_owner_enters_are_the_ones_used(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    shown = await load_package(hass, freezer)
    await set_helper(hass, "cave_notify_window_open_minutes", "5")
    await set_helper(hass, "cave_notify_problem_minutes", "2")
    await set_helper(hass, "cave_notify_unavailable_minutes", "10")
    room(hass, "Wohnzimmer", "living_room")
    hass.states.async_set(WINDOW, "off")
    hass.states.async_set(PROBLEM, "off")
    add_device(hass, "Gate contact", {"binary_sensor.gate": "off"})
    await advance(hass, freezer, 1)
    hass.states.async_set(WINDOW, "on")
    hass.states.async_set(PROBLEM, "on", {"messages": ["x"]})
    hass.states.async_set("binary_sensor.gate", "unavailable")

    await advance(hass, freezer, 3)
    assert shown.titles == ["Heating problem"]
    await advance(hass, freezer, 3)
    assert shown.titles == ["Heating problem", "Window left open"]
    await advance(hass, freezer, 5)

    assert shown.titles == ["Device unavailable", "Heating problem", "Window left open"]
    assert "more than 10 minutes" in " ".join(shown.messages)


async def test_the_humidity_limits_the_owner_enters_are_the_ones_used(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    shown = await load_package(hass, freezer)
    await set_helper(hass, "cave_notify_humidity_above", "60")
    room(hass, "Bad", "bathroom", humidity=61)

    await advance(hass, freezer, 2)

    assert shown.messages == ["Bad: humidity is 61 %, time to air the room"]


async def test_the_group_limit_travels_with_the_entries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The group limit travels with the entries, so the automation needs no second copy."""
    await load_package(hass, freezer)
    await advance(hass, freezer, 1)
    state: Any = hass.states.get("sensor.cave_attention_battery")
    assert state is not None
    assert state.attributes["group_limit"] == 3


THRESHOLDS = (
    "cave_notify_battery_below",
    "cave_notify_battery_clear_above",
    "cave_notify_unavailable_minutes",
    "cave_notify_window_open_minutes",
    "cave_notify_humidity_above",
    "cave_notify_humidity_clear_below",
    "cave_notify_problem_minutes",
)


@pytest.mark.parametrize("helper", THRESHOLDS)
async def test_a_threshold_left_empty_survives_a_restart(hass: HomeAssistant, helper: str) -> None:
    """Untouched, a threshold's state is `unknown`, and Home Assistant saves that for restore.

    Restoring the word as the helper's value would fail its number pattern, and Home Assistant
    would refuse to add the entity ("Error adding entity", which the autouse check catches). Found
    by the end-to-end job, which restarts Home Assistant.
    """
    mock_restore_cache_with_extra_data(hass, [(State(f"input_text.{helper}", "unknown"), {})])

    await load_package(hass)

    assert hass.states.get(f"input_text.{helper}") is not None


async def test_a_threshold_typed_in_is_kept_across_a_restart(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State("input_text.cave_notify_battery_below", "25"), {})]
    )

    await load_package(hass)

    assert hass.states.get("input_text.cave_notify_battery_below").state == "25"
