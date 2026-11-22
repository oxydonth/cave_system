"""The device-by-capability matrix: which capability applies to which device, and why not.

This module is the single source for the matrix. Each capability is tested by one test function
named ``test_<capability>``, parametrised with :func:`devices` so that the test IDs read as
``test_<capability>[<device>]``, or ``[<device>-<variant>]`` where the capability has variants
(the unconfirmed hardware behaviours of concept section 12, for example). The variants are
declared here too, so the matrix notices one that did not run. A device the capability does not
apply to is still a parameter, skipped with the reason given here, so nothing is silently absent.
``tests/devices/matrix.py`` turns the test results into the table for the pipeline's job summary
and shows a capability, or a variant, that has no test for a device it applies to as a gap.

It imports nothing from Home Assistant, so the matrix script can use it on its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

# Column order of the matrix, with the names shown in its header.
DEVICES: Final[dict[str, str]] = {
    "TRV-ZBT": "Sonoff TRV Gen2 (TRV-ZBT)",
    "TRVZB": "Sonoff TRVZB",
    "BTH-RA": "Bosch Thermostat II (BTH-RA)",
    "FRITZ-302": "FRITZ!Smart Thermo 302",
    "SNZB-02P": "Sonoff SNZB-02P",
    "SNZB-04P": "Sonoff SNZB-04P",
}
THERMOSTATS: Final = ("TRV-ZBT", "TRVZB", "BTH-RA", "FRITZ-302")
ZIGBEE_THERMOSTATS: Final = ("TRV-ZBT", "TRVZB", "BTH-RA")
SONOFF: Final = ("TRV-ZBT", "TRVZB")
SENSORS: Final = ("SNZB-02P", "SNZB-04P")

NOT_A_THERMOSTAT: Final = "a sensor, not a thermostat"
NOT_A_SENSOR: Final = "a thermostat; the room sensors have rows of their own"
ROOM_ROW: Final = (
    "the room's sensor and contact take part in every room scenario; the rows are per thermostat"
)
FRITZ_OWN_SENSOR: Final = (
    "regulates on its own sensor; it has no external temperature input (concept 3.1)"
)
REFUSALS: Final = (
    "summer-mode",
    "holiday-mode",
    "key-lock",
    "lock-not-yet-polled-refused",
    "lock-not-yet-polled-ignored",
)

# Open findings: a case in which the integration or a simulator does not do what the concept
# requires, by name (F1, F2, ...) with its description and the fix that closes it. A capability
# names the cases it marks in its ``findings``; :func:`devices` makes each a strict expected
# failure. F1, F2 and F3 were fixed by pull request #15, F4 (a live room sensor in a perfectly
# steady room taken for stale, issue #21) by work package 10. None is open.
FINDINGS: Final[dict[str, str]] = {}


@dataclass(frozen=True)
class Capability:
    """One row of the matrix."""

    title: str
    section: str
    applies: tuple[str, ...]
    # Reasons for the devices the capability does not apply to; every other device needs one.
    not_applicable: Mapping[str, str] = field(default_factory=dict)
    # The cases each applicable device runs, if more than one; every one must run.
    variants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Test IDs (``<device>`` or ``<device>-<variant>``) that are an open finding: name of FINDINGS.
    findings: Mapping[str, str] = field(default_factory=dict)

    def reason(self, device: str) -> str:
        """Why the capability does not apply to ``device``."""
        return self.not_applicable[device]

    def test_ids(self, device: str) -> tuple[str, ...]:
        """The test IDs that must run for ``device`` (none where it does not apply)."""
        if device not in self.applies:
            return ()
        if self.variants:
            return tuple(f"{device}-{variant}" for variant in self.variants[device])
        return (device,)


def _thermostats(title: str, section: str = "Every thermostat", **extra: Any) -> Capability:
    return Capability(
        title, section, THERMOSTATS, dict.fromkeys(SENSORS, NOT_A_THERMOSTAT), **extra
    )


def _only(
    title: str,
    section: str,
    applies: Sequence[str],
    reasons: Mapping[str, str],
    **extra: Any,
) -> Capability:
    return Capability(title, section, tuple(applies), dict(reasons), **extra)


_SILENT_ZIGBEE = {
    "FRITZ-302": "DECT: the simulator assumes the box learns a dial turn at once (sim/README.md; "
    "not confirmed on hardware), and a thermostat out of range is reported as not present "
    "(covered by offline_and_back)",
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_SONOFF_ONLY = {
    "BTH-RA": "has no sensor selection: it uses the remote value while it is fresher than "
    "30 minutes (sim/README.md)",
    "FRITZ-302": FRITZ_OWN_SENSOR,
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_FEED = {"FRITZ-302": FRITZ_OWN_SENSOR, **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT)}
_FRITZ_ONLY = {
    **dict.fromkeys(
        ZIGBEE_THERMOSTATS, "a behaviour of the FRITZ!Box; Zigbee2MQTT reports a write at once"
    ),
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_FRITZ_REFUSALS = {
    **dict.fromkeys(
        ZIGBEE_THERMOSTATS,
        "Zigbee2MQTT never refuses a command towards Home Assistant: a failed write is only "
        "logged, so it shows as an unanswered command "
        "(covered by unanswered_command_resent_then_flagged)",
    ),
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_LATE_REPORT = {
    **dict.fromkeys(
        ZIGBEE_THERMOSTATS,
        "Zigbee2MQTT reports a write at once or, from a silent device, never: no report of a "
        "command can arrive late",
    ),
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_CLAMP = {
    "TRV-ZBT": "its range, 5 to 30 °C, is the room's: no room target lies outside it",
    "TRVZB": "its range, 4 to 35 °C, contains the room's 5 to 30 °C: no room target lies "
    "outside it",
    "BTH-RA": "its range, 5 to 30 °C, is the room's: no room target lies outside it",
    **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
}
_ROOMS = dict.fromkeys(SENSORS, ROOM_ROW)
_CONTACT_ONLY = {
    **dict.fromkeys(THERMOSTATS, NOT_A_SENSOR),
    "SNZB-02P": "a climate sensor, no contact",
}
_SENSOR_ONLY = {**dict.fromkeys(THERMOSTATS, NOT_A_SENSOR), "SNZB-04P": "a contact, no temperature"}

CAPABILITIES: Final[dict[str, Capability]] = {
    # --- every thermostat model -------------------------------------------------------------
    "discovery": _thermostats("Discovery: entities, entity IDs, range and step"),
    "rounding_to_the_step": _thermostats(
        "Rounded half up to the step: room targets arrive on it, the Bosch input on 0.2 K",
        variants={
            "TRV-ZBT": ("room-step", "boost-off-grid"),
            "TRVZB": ("room-step", "boost-off-grid"),
            "BTH-RA": ("room-step", "boost-off-grid", "input-0.2-step"),
            "FRITZ-302": ("room-step", "boost-off-grid"),
        },
    ),
    "clamping_to_the_range": _only(
        "A target outside the device's range is clamped to it, and compared as such",
        "Every thermostat",
        ("FRITZ-302",),
        _CLAMP,
    ),
    "heat_off_and_back_to_heat": _thermostats("Heat, off, back to heat: the command forms"),
    "command_confirmed_by_report": _thermostats("A command is confirmed by the device's report"),
    "unanswered_command_resent_then_flagged": _thermostats(
        "An unanswered command is resent, then flagged unresponsive"
    ),
    "dial_becomes_room_override": _thermostats("A dial turned on the device is the room override"),
    "mode_changed_on_device_is_put_back": _thermostats(
        "A mode changed on the device is put back",
        variants={
            "TRV-ZBT": ("off", "auto"),
            "TRVZB": ("off", "auto"),
            "BTH-RA": ("pause", "schedule"),
            "FRITZ-302": ("off", "on"),
        },
    ),
    "mode_never_set_to_heat_alone": _thermostats("The mode is never set to heat on its own"),
    "offline_and_back": _thermostats("Offline, and back: nothing adopted, brought in line"),
    "dial_turned_while_silent": _only(
        "A dial turned while the device is silent is adopted when it answers",
        "Every thermostat",
        ZIGBEE_THERMOSTATS,
        _SILENT_ZIGBEE,
    ),
    "hold_for_device_check": _thermostats(
        "Held for device-check: nothing sent, nothing adopted, restored on release"
    ),
    "hold_ends_by_itself": _thermostats(
        "A hold ends by itself after its duration; the thermostat is brought back, not adopted"
    ),
    "late_report_after_release_not_adopted": _only(
        "A late report of the check's own target, after the release, is not adopted",
        "Every thermostat",
        ("FRITZ-302",),
        _LATE_REPORT,
    ),
    # --- every device -----------------------------------------------------------------------
    "battery_level": Capability("The battery level is available", "Every device", tuple(DEVICES)),
    # --- model-specific ---------------------------------------------------------------------
    "feed_reaches_external_input": _only(
        "The room temperature reaches the external input",
        "Model-specific",
        ZIGBEE_THERMOSTATS,
        _FEED,
    ),
    "feed_at_least_every_30_minutes": _only(
        "In a steady room the input is written every 25 minutes, no more; the device never "
        "falls back",
        "Model-specific",
        ZIGBEE_THERMOSTATS,
        _FEED,
    ),
    "feed_stops_while_the_room_is_off": _only(
        "The feed stops while the room is switched off and writes at once when it is back on",
        "Model-specific",
        ZIGBEE_THERMOSTATS,
        _FEED,
    ),
    "sensor_selection_set_once_and_left_alone": _only(
        "Sensor selection set once, left alone when the device switches it by itself",
        "Model-specific",
        SONOFF,
        _SONOFF_ONLY,
    ),
    "sensor_selection_written_again_after_feed_resumes": _only(
        "Sensor selection written again after the feed resumes, if the device did not return",
        "Model-specific",
        SONOFF,
        _SONOFF_ONLY,
    ),
    "window_detection_switched_off": _only(
        "On-device window detection is switched off",
        "Model-specific",
        ZIGBEE_THERMOSTATS,
        {
            "FRITZ-302": "its own window detection may stay on and has no setting entity "
            "(concept 5.6); see fritz_window_detection",
            **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
        },
    ),
    "fritz_reporting_behaviours": _only(
        "A new setpoint reported at once, or only after a delay",
        "Model-specific",
        ("FRITZ-302",),
        _FRITZ_ONLY,
        variants={"FRITZ-302": ("at-once", "after-3-min", "after-15-min")},
    ),
    "late_report_while_a_later_command_waits": _only(
        "A late report of an earlier command is not a hand change",
        "Model-specific",
        ("FRITZ-302",),
        _FRITZ_ONLY,
    ),
    "refused_command_makes_blocked": _only(
        "A command refused in summer mode, holiday mode or key lock makes it blocked",
        "Model-specific",
        ("FRITZ-302",),
        _FRITZ_REFUSALS,
        variants={"FRITZ-302": REFUSALS},
    ),
    "blocked_not_retried_before_retry_interval": _only(
        "A blocked thermostat is tried again only after retry_interval",
        "Model-specific",
        ("FRITZ-302",),
        _FRITZ_REFUSALS,
        variants={"FRITZ-302": REFUSALS},
    ),
    "fritz_window_detection": _only(
        "The FRITZ!Box's own window detection, with both things it may report",
        "Model-specific",
        ("FRITZ-302",),
        {
            **dict.fromkeys(
                ZIGBEE_THERMOSTATS,
                "the FRITZ!Box's own detection; the Zigbee thermostats' on-device window "
                "detection is switched off, see window_detection_switched_off",
            ),
            **dict.fromkeys(SENSORS, NOT_A_THERMOSTAT),
        },
        variants={"FRITZ-302": ("target-unchanged", "target-off")},
    ),
    # --- sensors ----------------------------------------------------------------------------
    "room_temperature_and_humidity": _only(
        "Supplies the room's temperature and humidity", "Sensors", ("SNZB-02P",), _SENSOR_ONLY
    ),
    "sensor_going_stale": _only(
        "Going stale: the room falls back to the thermostats and the feed stops, then resumes",
        "Sensors",
        ("SNZB-02P",),
        _SENSOR_ONLY,
    ),
    "sensor_stays_fresh_while_it_reports": _only(
        "A sensor that keeps reporting in a steady room stays fresh: the feed goes on",
        "Sensors",
        ("SNZB-02P",),
        _SENSOR_ONLY,
        variants={"SNZB-02P": ("humidity-changing", "everything-steady")},
    ),
    "sensor_last_seen_enabled_after_an_upgrade": _only(
        "A last_seen sensor registered disabled before the upgrade is enabled; the MQTT reload "
        "that follows costs nothing",
        "Sensors",
        ("SNZB-02P",),
        _SENSOR_ONLY,
    ),
    "contact_starts_and_ends_window_pause": _only(
        "Starts and ends the window pause", "Sensors", ("SNZB-04P",), _CONTACT_ONLY
    ),
    "contact_silent_while_open": _only(
        "Going silent while open: the pause ends after the grace, the problem sensor turns on",
        "Sensors",
        ("SNZB-04P",),
        _CONTACT_ONLY,
    ),
    "contact_returns_closed_after_grace": _only(
        "Answering again closed after the grace: nothing changes, nothing is sent",
        "Sensors",
        ("SNZB-04P",),
        _CONTACT_ONLY,
    ),
    # --- rooms built from each thermostat model ---------------------------------------------
    "room_comfort_and_eco_by_schedule": Capability(
        "Room: comfort and eco by schedule", "Rooms", THERMOSTATS, _ROOMS
    ),
    "room_window_pause": Capability(
        "Room: window pause, a dial turned meanwhile not adopted", "Rooms", THERMOSTATS, _ROOMS
    ),
    "room_away": Capability("Room: away", "Rooms", THERMOSTATS, _ROOMS),
    "room_boost": Capability("Room: boost", "Rooms", THERMOSTATS, _ROOMS),
    "room_override_expires": Capability(
        "Room: an override that expires", "Rooms", THERMOSTATS, _ROOMS
    ),
    "mixed_room_stays_in_step": Capability(
        "Mixed room (all four thermostats): a dial turned on this one, the others follow",
        "Rooms",
        THERMOSTATS,
        _ROOMS,
    ),
}


def devices(capability: str) -> list[Any]:
    """The parameters of ``test_<capability>``: every device, the inapplicable ones skipped.

    A capability with variants gives several cases per device; its test takes two arguments, the
    device and the variant, with IDs ``<device>-<variant>``. A case named in the capability's
    ``findings`` is a strict expected failure (``xfail``) whose reason starts with ``finding:``:
    the pipeline stays green while the finding is open, the matrix shows it, and the mark has to
    go as soon as the finding is fixed. Keep such a case as narrow as the finding itself.
    """
    import pytest  # here, so that the matrix script needs no pytest

    entry = CAPABILITIES[capability]

    def marks(test_id: str) -> tuple[Any, ...]:
        finding = entry.findings.get(test_id)
        if finding is None:
            return ()
        return (pytest.mark.xfail(reason=f"finding: {FINDINGS[finding]}", strict=True),)

    params: list[Any] = []
    for device in DEVICES:
        if device not in entry.applies:
            skip = pytest.mark.skip(reason=f"n/a: {entry.reason(device)}")
            values = (device, None) if entry.variants else (device,)
            params.append(pytest.param(*values, id=device, marks=skip))
        elif entry.variants:
            for variant in entry.variants[device]:
                test_id = f"{device}-{variant}"
                params.append(pytest.param(device, variant, id=test_id, marks=marks(test_id)))
        else:
            params.append(pytest.param(device, id=device, marks=marks(device)))
    return params


def check_registry() -> list[str]:
    """Problems with the registry itself."""
    problems = []
    for name, entry in CAPABILITIES.items():
        for device in DEVICES:
            if device not in entry.applies and device not in entry.not_applicable:
                problems.append(f"{name}: {device} is neither applicable nor explained")
            if device in entry.applies and device in entry.not_applicable:
                problems.append(f"{name}: {device} is both applicable and explained away")
            if entry.variants and device in entry.applies and device not in entry.variants:
                problems.append(f"{name}: {device} applies but has no variants")
        known = {test_id for device in DEVICES for test_id in entry.test_ids(device)}
        for test_id, finding in entry.findings.items():
            if test_id not in known:
                problems.append(f"{name}: finding for unknown test ID {test_id}")
            if finding not in FINDINGS:
                problems.append(f"{name}: unknown finding {finding}")
    return problems
