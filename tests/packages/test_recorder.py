"""What the notifications package leaves in Home Assistant's database, logbook and traces.

The sensors are checked once a minute by a run of the automation "Cave: notify about what needs
attention" (issue #31). Every run changes that automation's `last_triggered` and `current`, and
fires `automation_triggered` and `cave_attention_check`: four rows a minute, 5,760 a day kept for
30 days, on a server that may run from an SD card, and 1,440 logbook lines a day. The recorder
section of homeassistant/configuration.yaml leaves them out. These tests apply that section to what
the package really fires, the way the recorder decides, so a wrong entry fails here. The automation
keeps the traces of two hours of runs, so the run that pushed can be found.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import recorder
from homeassistant.components.trace.util import async_list_traces
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED, MATCH_ALL
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from pytest_homeassistant_custom_component.common import async_mock_service

from tests.packages.helpers import advance, load_package, set_helper

CONFIGURATION = Path(__file__).resolve().parents[2] / "homeassistant" / "configuration.yaml"
BATTERY = {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Hall contact"}
PUSHING = "automation.cave_notify_about_what_needs_attention"


def recorded_by_configuration() -> Callable[[Event], bool]:
    """Whether the recorder configured in configuration.yaml writes an event to its database.

    The same decision as the recorder's own event listener (`Recorder.async_initialize`): an
    excluded event type is dropped, and an event with an `entity_id` is kept only if the entity
    filter keeps that entity.
    """

    class Loader(yaml.SafeLoader):
        """Home Assistant's ``!include`` tags are read as nothing: only ``recorder`` matters."""

    Loader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)
    configuration = yaml.load(CONFIGURATION.read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506
    conf = recorder.CONFIG_SCHEMA({"recorder": configuration["recorder"]})["recorder"]
    excluded = set(conf[recorder.CONF_EXCLUDE].get(recorder.CONF_EVENT_TYPES, []))
    excluded.discard(EVENT_STATE_CHANGED)
    entity_filter = convert_include_exclude_filter(conf).get_filter()

    def recorded(event: Event) -> bool:
        if event.event_type in excluded:
            return False
        entity_id = event.data.get(ATTR_ENTITY_ID)
        if isinstance(entity_id, str):
            return entity_filter(entity_id)
        return True

    return recorded


def _describe(event: Event) -> str:
    entity_id = event.data.get(ATTR_ENTITY_ID)
    return f"{event.event_type} {entity_id}" if entity_id else event.event_type


class Bus:
    """Every event fired from now on, and those of them the recorder would write."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.fired: list[str] = []
        self.written: list[str] = []
        self._recorded = recorded_by_configuration()
        hass.bus.async_listen(MATCH_ALL, self._listen)

    @callback
    def _listen(self, event: Event[Any]) -> None:
        self.fired.append(_describe(event))
        if self._recorded(event):
            self.written.append(_describe(event))


async def test_a_quiet_hour_writes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Sixty checks in which nothing changes leave no row behind.

    Unchanged sensors are only "reported", which the recorder does not write; the runs of the
    pushing automation and the event that starts the check are left out by configuration.yaml.
    """
    await load_package(hass, freezer)
    hass.states.async_set("sensor.hall_contact_battery", "80", BATTERY)
    await advance(hass, freezer, 1)
    bus = Bus(hass)

    await advance(hass, freezer, 60)

    assert bus.written == []
    assert bus.fired.count("cave_attention_check") == 60, "the checks did happen"
    assert bus.fired.count(f"automation_triggered {PUSHING}") == 60


async def test_a_push_is_still_recorded_where_the_owner_looks(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The sensor's new state, the notification script and the copy to the phone are kept.

    The logbook shows "Cave: send a notification copy" triggered, once per push, and the
    sensor's history shows when the entry appeared.
    """
    async_mock_service(hass, "notify", "test_phone")
    await load_package(hass, freezer)
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    bus = Bus(hass)
    written = bus.written
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)

    await advance(hass, freezer, 1)

    assert "state_changed sensor.cave_attention_battery" in written
    assert "automation_triggered automation.cave_send_a_notification_copy" in written
    assert "state_changed script.cave_notify" in written
    assert not [event for event in written if PUSHING in event]
    assert "cave_attention_check" not in written


def test_the_heating_and_the_attention_sensors_keep_their_history() -> None:
    """Only the pushing automation and the check event are left out, nothing a person reads."""
    recorded = recorded_by_configuration()

    def event(event_type: str, entity_id: str | None = None) -> Event:
        return Event(event_type, {ATTR_ENTITY_ID: entity_id} if entity_id else {})

    assert not recorded(event("state_changed", PUSHING))
    assert not recorded(event("automation_triggered", PUSHING))
    assert not recorded(event("cave_attention_check"))
    for entity_id in (
        "sensor.cave_attention_battery",
        "automation.cave_send_a_notification_copy",
        "automation.cave_show_reported_notifications_again_after_a_restart",
        "script.cave_notify",
        "climate.cave_living_room",
    ):
        assert recorded(event("state_changed", entity_id)), entity_id
    assert recorded(event("automation_triggered", "automation.cave_send_a_notification_copy"))
    assert recorded(event("cave_notify_copy"))


async def test_the_run_that_pushed_is_still_traced_an_hour_later(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Sixty quiet checks later the trace of the push is there, in both automations.

    Home Assistant keeps the last 5 runs of an automation by default; with a run every minute
    the push would be gone within five minutes.
    """
    async_mock_service(hass, "notify", "test_phone")
    await load_package(hass, freezer)
    await set_helper(hass, "cave_notify_service", "notify.test_phone")
    hass.states.async_set("sensor.hall_contact_battery", "12", BATTERY)
    await advance(hass, freezer, 1)

    await advance(hass, freezer, 60)

    pushing = await async_list_traces(hass, "automation", "automation.cave_notifications")
    triggers = [trace["trigger"] for trace in pushing]
    assert any("sensor.cave_attention_battery" in trigger for trigger in triggers), triggers
    assert len(pushing) > 60, "more than the default of five runs are kept"
    copies = await async_list_traces(hass, "automation", "automation.cave_notify_copy")
    assert len(copies) == 1
