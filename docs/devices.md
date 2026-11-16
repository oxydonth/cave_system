# Devices: what is tested, and checking a real one

Every product of [concept section 3.1](concept.md#31-recommended-products) is tested through Home
Assistant's own integrations with the heating logic on top: now against the simulators of
[`sim/`](../sim/README.md), and with `./cave device-check` against the real device once it is
paired (requirement R3).

- [The device integration tests](#the-device-integration-tests)
- [Per device](#per-device)
- [Checking a real thermostat: `cave device-check`](#checking-a-real-thermostat-cave-device-check)

## The device integration tests

`tests/devices/` runs Home Assistant in-process with its real MQTT integration, fed by the
Zigbee2MQTT stand-in, and its real FRITZ!SmartHome integration, talking HTTP to the fake
FRITZ!Box; `cave_heating` is set up from an applied `heating.yaml` exactly as on the server. There
are no stand-in entities: what a device receives is read where it arrives, as MQTT messages on
`zigbee2mqtt/<name>/set...` and as `sethkrtsoll` requests at the FRITZ!Box, and every test counts
the commands, because these devices run on two AA cells. Time is simulated, so the suite runs in
under a minute although it covers days of heating.

The tests are parametrised by device, so their names read as a matrix:
`test_<capability>[<device>]`. A capability that does not apply to a device is still listed for
it, skipped with the reason. The list of capabilities and where each applies is
`tests/devices/capabilities.py`.

```sh
make test                                   # the whole suite, these included
uv run pytest tests/devices -v              # only these, by name
uv run pytest tests/devices -k FRITZ-302    # only one device
```

**The matrix.** Every pipeline run shows the devices against the capabilities in its job summary
(job `test`, step "Device matrix"). A cell says `pass`, `FAIL`, `n/a` (with the reason in
`capabilities.py`), `GAP` (the capability applies but no test ran for it, which fails the step),
or ⚠️ with a finding number. To make it locally:

```sh
uv run pytest --junitxml=test-results.xml
uv run python -m tests.devices.matrix test-results.xml
```

**Findings.** Where the integration or a simulator does not do what the concept requires, the
test still asserts the concept and is marked as an expected failure whose reason starts with
`finding:` and a number (F1, F2, ...). The mark is strict: once the finding is fixed, the test
passes, the run fails, and the mark has to go.

**Behaviours not confirmed on real hardware** (concept section 12) are tested in every variant the
simulators can play, and each test says which one it covers: a Sonoff that does or does not go
back to the external temperature by itself; a FRITZ!Box that reports a new setpoint at once, after
3 minutes (302) or after 15 (301); a locked FRITZ! thermostat whose box refuses a command or
accepts it without effect; and FRITZ! window detection shown with the target unchanged or as OFF.

## Per device

Each section lists what is tested; the matrix shows the result. The known differences between the
simulator and the real device that matter here are listed with it; the full list is in
[`sim/README.md`](../sim/README.md#known-differences-from-the-real-devices).

### Every thermostat

Discovery (entity IDs, range and step); heat at a target, rounded half up to the step and clamped
to the range; off and back to heat, with the command forms of concept 5.5; confirmation by the
device's report; an unanswered command resent three times, then `unresponsive` with a repair
notice and retried every 30 minutes; a dial turned on the device becoming the room's override; a
mode changed on the device put back; the mode never set to heat without the target; unavailable
and back, with a dial turned meanwhile not adopted; the battery level; the hold `device-check`
uses; and a room built from the model: comfort and eco by schedule, window pause, away, boost, and
an override that ends with the schedule. A mixed room with all four models stays in step whichever
dial is turned.

### Sonoff TRV Gen2 (TRV-ZBT) and Sonoff TRVZB

Also: the room temperature reaches `number.<name>_external_temperature_input`, at once at the
start, after 10 minutes for a change and at least every 25; the sensor selection
(`select.<name>_temperature_sensor_select: remote_temperature`) is written once and left alone
when the device switches to `remote_source_offline` by itself two hours after the feed stops;
when the device does not go back by itself, it is written again five minutes after the feed
resumes; the on-device window detection (`switch.<name>_open_window: "off"`) is switched off once
and again when the device returns from being unavailable; a dial turned while the device is
silent (dead battery: Zigbee2MQTT keeps it available for 25 hours) is adopted when it answers.

Differences: the two-hour fallback and the return on the next value are documented for the TRVZB
only; for the Gen2 they are assumed. Frost protection, PID regulation and valve positions are not
modelled.

### Bosch Thermostat II (BTH-RA)

Also: the room temperature reaches `number.<name>_remote_temperature` (rounded to its 0.2 K step)
at least every 25 minutes, so the device, which falls back to its own sensor after 30 minutes
without a value, never does; the window detection (`switch.<name>_window_detection: "off"`) is
switched off. Its mode in Home Assistant is its `operating_mode`: `manual` is heat, `pause` off,
`schedule` auto; both changed on the device are put back.

Differences: a setpoint written over Zigbee is assumed to be reported with
`setpoint_change_source: externally`; boost and window mode close or open the valve as documented.

### FRITZ!Smart Thermo 302

Also: a new setpoint reported at once, after 3 minutes or after 15, each confirmed with one
command (it is a slow thermostat: 20 minutes before a resend); a late report of an earlier command
while a later one waits is not taken for a dial turn; a command refused in summer mode, holiday
mode or key lock makes it `blocked`, including a lock the box has but Home Assistant has not yet
polled (both answers of the box); its own window detection with the target shown unchanged
(nothing is sent) or as OFF (put back with one command); the dial turned all the way down (off)
or up (ON) put back. It has no external input and no settings entities: it regulates on its own
sensor.

Differences: one delay governs when the box reports a setpoint and when the valve applies it; the
measured temperature is reported live; the timetable is always neutral; Home Assistant shows
summer mode as off and, in holiday mode, no target at all.

### Sonoff SNZB-02P (room temperature and humidity)

The room shows its temperature and humidity; it feeds the thermostats' inputs; when it falls
silent, the room counts it as stale three hours after its device last reported, shows the
average of the thermostats' readings and stops the feed, which writes again at once when the
sensor answers. It reports by its configured reporting (1 °C or 1 %, at least hourly); real ones
may report smaller changes.

### Sonoff SNZB-04P (window contact)

It starts the window pause after a minute open and ends it two minutes after closing; a shorter
airing sends nothing. A contact that falls silent while open keeps reading open until Zigbee2MQTT
declares it unavailable (25 hours by default), then the pause lasts 30 minutes more and ends with
the problem sensor on.

## Checking a real thermostat: `cave device-check`

Once a thermostat is paired and its room is in the applied `heating.yaml`, run on the server:

```sh
./cave device-check climate.bad_heizung
./cave device-check climate.wohnzimmer_heizung_fenster --timeout 300
```

It needs the script token (`./cave bootstrap` creates it) and a running Home Assistant. It asks
the real device what the tests ask the simulators:

1. It reads the thermostat's entry from the applied `heating.yaml`, with the integration's own
   loader in a one-off Home Assistant container: its room, external input and settings.
2. It holds the thermostat with `cave_heating.hold`, so the controller neither reacts to the
   check nor passes it on to the room. The hold is longer than the check can take (three times
   `--timeout`, plus 6 minutes), and ends by itself if the command is killed. A thermostat that is
   not in the file is checked without a hold: the controller does not drive it.
3. It checks that the entity exists, is available and reports a temperature; that it accepts a
   target half a degree higher and reports it back, and **how long that took**; where the file
   names them, that the external input accepts the room's temperature and that the settings
   entities exist with the configured options (it does not change the settings).
4. Whatever happens, also on an error or Ctrl-C, it sends the original target back, waits until
   the thermostat reports it (if the half degree was never reported, it watches for a late report
   for another `--timeout` and undoes it), and releases the hold. Nothing is changed permanently.

| Option | Default | Meaning |
|---|---|---|
| `--timeout SECONDS` | 1200 | How long to wait for the thermostat to report a target: 20 minutes, enough for a FRITZ! thermostat. A Zigbee thermostat answers within seconds; 300 is plenty. |
| `--interval SECONDS` | 2 | Time between two looks at the thermostat. |

It prints a table of the checks with pass, FAIL or skipped and the time each took, and exits
with 0 when every check passed, 1 when one failed, and 2 when **the check could not complete**:
the thermostat is off (its room is switched off, paused or in summer mode; run it while the room
heats), or the hold disappeared during the check, which happens when Home Assistant restarts. In
that case it holds the thermostat again to restore it, and the result says so rather than
blaming the device.

The time a thermostat takes to report a target back is one of the open points of concept section
12 (does a FRITZ!Box report a setpoint before the valve has it?). Note it for each thermostat in
the hardware acceptance checklist.
