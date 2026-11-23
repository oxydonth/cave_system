# Simulated devices (`sim/`)

Software stand-ins for every device of concept section 3.1 that is marked as simulated, faithful
enough that Home Assistant's own MQTT and FRITZ!SmartHome integrations accept them. They power the
tests (in-process, through `cave_sim.testing`), the demo and the end-to-end tests (as the `sim`
container of `compose.sim.yaml`). They are never used at home.

| Part | What it stands in for | Module |
|---|---|---|
| Zigbee2MQTT stand-in | Zigbee2MQTT 2.14.3 with Sonoff TRV-ZBT, TRVZB, SNZB-02P, SNZB-04P and Bosch BTH-RA | `cave_sim.zigbee` |
| Fake FRITZ!Box | The FRITZ!Box's login and smart-home HTTP API with FRITZ!Smart Thermo 302 thermostats | `cave_sim.fritz` |
| Room model | Rooms whose temperature follows valve openings, open windows and the outdoor temperature | `cave_sim.room` |
| Control API | HTTP endpoints to steer and read the simulation | `cave_sim.control_api` |
| Home description | Which rooms and devices exist (`sim/demo/home.yaml`) | `cave_sim.home` |
| Test interface | A simulated home inside Home Assistant's test harness | `cave_sim.testing` |

Everything timed (reporting intervals, the room model, the FRITZ! reporting delay, the Sonoff's and
the Bosch's sensor time-outs, Zigbee2MQTT's republish after Home Assistant's birth message) runs on
a clock (`cave_sim.clock`) that tests drive; nothing sleeps.

## Running it

In the demo and the pipeline it runs as the `sim` service of `compose.sim.yaml`, built from
`sim/Dockerfile` (build context: the repository root). By hand, from the repository root:

```sh
CAVE_FRITZ_PASSWORD=some-password CAVE_SIM_MQTT_HOST=127.0.0.1 PYTHONPATH=sim uv run python -m cave_sim
```

| Variable | Meaning | Default |
|---|---|---|
| `CAVE_SIM_HOME` | The home description | `sim/demo/home.yaml` |
| `CAVE_SIM_MQTT_HOST` | MQTT broker host | `mosquitto` |
| `CAVE_SIM_MQTT_PORT` | MQTT broker port | `1883` |
| `CAVE_MQTT_USER`, `CAVE_MQTT_PASSWORD` | Broker login (concept section 4.2) | none |
| `CAVE_FRITZ_USER` | User the fake FRITZ!Box accepts (concept section 4.2) | `cave` |
| `CAVE_FRITZ_PASSWORD` | Its password; required | — |
| `CAVE_SIM_BIND` | Address the HTTP servers listen on | `0.0.0.0` |
| `CAVE_SIM_FRITZ_PORT` | Fake FRITZ!Box port | `8480` |
| `CAVE_SIM_CONTROL_PORT` | Control API port | `8481` |
| `CAVE_SIM_LOG_LEVEL` | Log level | `INFO` |

`python -m cave_sim.smoke` (same variables, plus `CAVE_SIM_SMOKE_HTTP_HOST`, default `127.0.0.1`)
checks a running simulator; the simulator check of the pipeline's `e2e` job uses it.

Home Assistant connects the FRITZ!SmartHome integration to `http://127.0.0.1:8480` with
`CAVE_FRITZ_USER` and `CAVE_FRITZ_PASSWORD`. The simulator publishes into the broker as
Zigbee2MQTT would, so Home Assistant's MQTT integration needs nothing special.

## The home description

```yaml
outdoor_temperature: 5.0          # optional, °C
fritzbox:
  report_delay: 0                 # seconds until a new setpoint is reported; 0 = at once
  window_target: unchanged        # what <tsoll> shows during window open mode: unchanged or off
  locked_command: refuse          # sethkrtsoll while locked: refuse (HTTP 400) or ignore (200, no effect)
zigbee2mqtt:                      # optional
  passive_timeout_minutes: 1500   # until a silent battery device shows as unavailable
  device_options: {}              # Zigbee2MQTT's device_options; default: those of compose.yaml
rooms:
  - id: living_room               # used by the control API
    name: Wohnzimmer
    temperature: 19.0             # optional start values
    humidity: 50
    heating_per_hour: 2.0         # optional room-model rates, see below
    loss_per_hour: 0.1
    window_loss_per_hour: 2.0
    devices:
      - model: TRV-ZBT            # TRV-ZBT, TRVZB, BTH-RA, SNZB-02P, SNZB-04P or "FRITZ!Smart Thermo 302"
        name: Wohnzimmer Heizung Fenster   # the Zigbee2MQTT friendly name or the FRITZ! device name
        ieee: "0x00158d0000000101"         # optional (Zigbee); derived from the name otherwise
        ain: "13979 0000001"               # optional (FRITZ!); derived from the name otherwise
```

A device's ID in the control API is its name in lower case with runs of whitespace replaced by an
underscore, the same rule Zigbee2MQTT applies to build entity IDs. A contact sensor is a window of
its room.

### The demo home and its entity IDs

`sim/demo/home.yaml` has three rooms that together use every simulated model and both device
families. The names follow the example of concept section 5.1 and fix the entity IDs below, which
`sim/demo/heating.yaml` (WP6) and the device tests (WP7) refer to. Change names only together with
those files. A test (`tests/sim/test_home_assistant_mqtt.py`) asserts the main ones.

| Room (`id`) | Device (control-API ID) | Model | Entity IDs the heating logic uses |
|---|---|---|---|
| Wohnzimmer (`living_room`) | Wohnzimmer Heizung Fenster (`wohnzimmer_heizung_fenster`) | TRV-ZBT | `climate.wohnzimmer_heizung_fenster`, `number.wohnzimmer_heizung_fenster_external_temperature_input`, `select.wohnzimmer_heizung_fenster_temperature_sensor_select`, `switch.wohnzimmer_heizung_fenster_open_window`, `sensor.wohnzimmer_heizung_fenster_battery` |
| | Wohnzimmer Heizung Tuer (`wohnzimmer_heizung_tuer`) | TRVZB | `climate.wohnzimmer_heizung_tuer`, `number.wohnzimmer_heizung_tuer_external_temperature_input`, `select.wohnzimmer_heizung_tuer_temperature_sensor_select`, `switch.wohnzimmer_heizung_tuer_open_window`, `sensor.wohnzimmer_heizung_tuer_battery` |
| | Wohnzimmer (`wohnzimmer`) | SNZB-02P | `sensor.wohnzimmer_temperature`, `sensor.wohnzimmer_humidity`, `sensor.wohnzimmer_battery` |
| | Wohnzimmer Fenster (`wohnzimmer_fenster`) | SNZB-04P | `binary_sensor.wohnzimmer_fenster_contact`, `sensor.wohnzimmer_fenster_battery`, `binary_sensor.wohnzimmer_fenster_battery_low` |
| Schlafzimmer (`bedroom`) | Schlafzimmer Heizung (`schlafzimmer_heizung`) | BTH-RA | `climate.schlafzimmer_heizung`, `number.schlafzimmer_heizung_remote_temperature`, `switch.schlafzimmer_heizung_window_detection`, `select.schlafzimmer_heizung_operating_mode`, `sensor.schlafzimmer_heizung_battery`, `binary_sensor.schlafzimmer_heizung_battery_low` |
| | Schlafzimmer (`schlafzimmer`) | SNZB-02P | `sensor.schlafzimmer_temperature`, `sensor.schlafzimmer_humidity`, `sensor.schlafzimmer_battery` |
| | Schlafzimmer Fenster (`schlafzimmer_fenster`) | SNZB-04P | `binary_sensor.schlafzimmer_fenster_contact`, `sensor.schlafzimmer_fenster_battery`, `binary_sensor.schlafzimmer_fenster_battery_low` |
| Bad (`bathroom`) | Bad Heizung (`bad_heizung`), AIN `13979 0000001` | FRITZ!Smart Thermo 302 | `climate.bad_heizung`, `sensor.bad_heizung_temperature`, `sensor.bad_heizung_battery`, `binary_sensor.bad_heizung_summer_mode`, `binary_sensor.bad_heizung_holiday_mode`, `binary_sensor.bad_heizung_button_lock_via_ui`, `binary_sensor.bad_heizung_button_lock_on_device`, `binary_sensor.bad_heizung_open_window_detected` |
| | Bad (`bad`) | SNZB-02P | `sensor.bad_temperature`, `sensor.bad_humidity`, `sensor.bad_battery` |

Settings entries for `heating.yaml` (concept sections 5.1 and 5.6), with the option names the
devices show:

| Model | Sensor selection | Window detection off |
|---|---|---|
| TRV-ZBT, TRVZB | `select.<name>_temperature_sensor_select: remote_temperature` | `switch.<name>_open_window: "off"` |
| BTH-RA | none: the remote temperature is used whenever it is fresher than 30 minutes | `switch.<name>_window_detection: "off"` |

Every Zigbee device also has the entities Zigbee2MQTT creates for all its exposes (the full lists,
model by model, are in `tests/sim/test_home_assistant_mqtt.py`), a `sensor.<name>_last_seen`
(enabled by this project's `device_options`, see below), a `sensor.<name>_linkquality` (disabled
by default) and an `update.<name>`. The bridge adds
`binary_sensor.zigbee2mqtt_bridge_connection_state` and its siblings.

## MQTT topics

Zigbee2MQTT's settings as the stand-in assumes them: base topic `zigbee2mqtt`, discovery prefix
`homeassistant`, availability on, `last_seen: ISO_8601`, `cache_state: true`, output `json`, and
the `device_options` of `compose.yaml` (`ZIGBEE2MQTT_CONFIG_DEVICE_OPTIONS`):
`{"homeassistant": {"last_seen": {"enabled_by_default": true}}}`. A test keeps the two equal.

**Device options.** The fixtures hold the discovery a device's definition yields; the stand-in
applies the options when it publishes, as Zigbee2MQTT 2.14.3 does: a device's options are
`device_options` merged shallowly with its own (`lib/model/device.ts`, `get options()`), and in
`lib/extension/homeassistant.ts` a falsy `homeassistant` option discovers nothing (`discover`), an
entity whose entry is `null` is dropped (`getConfigs`), and after the converter's own override the
scalar values of `homeassistant` go into every payload (`name` excepted) and those of
`homeassistant.<object id>` into that entity's (`add` in `discover`), where `null` deletes a key
and `device` is merged. Renaming an entity through `type` or `object_id` is not transcribed; the
stand-in refuses such options. `Zigbee2Mqtt.set_device_options` stands for Zigbee2MQTT started
again with other options: changed discovery is published again, and discovery that is gone is
cleared. The stand-in's devices have no options of their own. The real Zigbee2MQTT applies its
`ZIGBEE2MQTT_CONFIG_*` variables by writing them into its `configuration.yaml` at every start
(`lib/util/onboarding.ts`, `onboard`, calls `settings.write()`, which applies them in
`lib/util/settings.ts`, `applyEnvironmentVariables`); `tests/sim/test_zigbee2mqtt_settings.py`
runs the pinned image in the pipeline to check that nothing else in that file changes.

| Topic | Direction | Content |
|---|---|---|
| `zigbee2mqtt/bridge/state` | published, retained | `{"state":"online"}`; `offline` as last will and on shutdown |
| `zigbee2mqtt/bridge/info` | published, retained | `version`, `coordinator`, `log_level`, `permit_join`, `restart_required` |
| `homeassistant/<component>/<ieee>/<object id>/config` | published, retained | Discovery, one message per entity |
| `homeassistant/<component>/2122...104_<coordinator>/<object id>/config` | published, retained | The bridge's entities (the key is the base topic's character codes) |
| `zigbee2mqtt/<name>/availability` | published, retained | `{"state":"online"}` or `{"state":"offline"}` |
| `zigbee2mqtt/<name>` | published | The device's whole state, with `last_seen`, `linkquality` and `update` |
| `zigbee2mqtt/<name>/set` | subscribed | A JSON object of properties to set |
| `zigbee2mqtt/<name>/set/<property>` | subscribed | One property; the payload is the value (JSON or bare) |
| `zigbee2mqtt/<name>/get`, `.../get/<property>` | subscribed | The device answers with the current values |
| `homeassistant/status` | subscribed | `online` makes the bridge republish its state and every device's state 30 s later |
| `zigbee2mqtt/bridge/request/permit_join`, `restart`, `options` | subscribed | Answered on `zigbee2mqtt/bridge/response/<request>` |
| `zigbee2mqtt/bridge/request/<anything else>` | subscribed | Answered with `{"status":"error",...}` |

## HTTP endpoints

### Fake FRITZ!Box (port 8480)

| Path | Parameters | Does |
|---|---|---|
| `/login_sid.lua` (GET or POST) | `version=2`; `username` and `response`; `sid`; `logout` or `security:command/logout` | Session login with the PBKDF2 challenge-response (offered with or without `version=2`; there is no MD5 login), session check, logout. Sessions last 20 minutes and are extended by use. |
| `/webservices/homeautoswitch.lua` (GET or POST) | `sid`, `switchcmd`, `ain`, `param`, `endtimestamp` | `getdevicelistinfos`, `getdeviceinfos`, `gettemplatelistinfos`, `gettriggerlistinfos`, `getswitchlist`, `getswitchpresent`, `getswitchname`, `gettemperature`, `gethkrtsoll`, `gethkrkomfort`, `gethkrabsenk`, `sethkrtsoll`, `sethkrboost`, `sethkrwindowopen`. Without a valid session: 403. Unknown command, AIN or value: 400. |

### Control API (port 8481)

Requests and responses are JSON. Unknown room or device: 404; invalid request: 400 with
`{"error": "..."}`. A change answers with the new state of what was changed.

| Method and path | Body | Does |
|---|---|---|
| `GET /api/health` | | `{"status":"ok"}` once the devices are published and while the broker connection stands; otherwise 503 with `{"status":"unavailable","reason":...}`. The container's health check uses it. |
| `GET /api/state` | | Time, outdoor temperature, FRITZ!Box and Zigbee2MQTT settings, rooms, every device's state (see below) |
| `GET /api/devices/{id}` | | One device (see below) |
| `POST /api/devices/{id}/window` | `{"open": true}` | Open or close the window a contact sensor sits on |
| `POST /api/devices/{id}/silent` | `{"silent": true}` | A Zigbee device stops answering (dead battery, out of range): it stays available until `passive_timeout_minutes` after its last message, commands to it fail without a trace, and what changes on it meanwhile is reported when it answers again (`"silent": false`) |
| `POST /api/devices/{id}/availability` | `{"online": false}` | A shortcut: the device stops answering and shows as unavailable at once; `true` brings it back. FRITZ!: `present` 0 or 1 |
| `POST /api/devices/{id}/mode` | `{"mode": "off"}` | Change a Zigbee thermostat's mode on the device: `system_mode` (`off`, `auto`, `heat`) for TRV-ZBT and TRVZB, `operating_mode` (`schedule`, `manual`, `pause`) for BTH-RA |
| `POST /api/devices/{id}/window-detection` | `{"minutes": 15}` | A FRITZ! thermostat's own window detection starts its window open mode |
| `POST /api/devices/{id}/dial` | `{"target": 22.5}`; FRITZ! also `"off"`, `"on"` | Turn a thermostat's dial |
| `POST /api/devices/{id}/battery` | `{"level": 15, "low": true}` | Battery level, and the low flag where the model has one |
| `POST /api/devices/{id}/fritz-lock` | `{"lock": true, "device_lock": false}` | A FRITZ! thermostat's key lock via UI/API and on the device |
| `POST /api/devices/{id}/sonoff` | `{"returns_on_feed": false}` | Whether a Sonoff goes back to the external sensor by itself |
| `POST /api/rooms/{id}/temperature` | `{"temperature": 21.0}` | Set a room's temperature |
| `POST /api/rooms/{id}/humidity` | `{"humidity": 70}` | Set a room's humidity |
| `POST /api/outdoor` | `{"temperature": -5}` | Set the outdoor temperature |
| `POST /api/fritzbox` | `{"report_delay": 180, "summer": true, "holiday": false, "txbusy_while_pending": false, "window_target": "off", "locked_command": "ignore"}` | Change the FRITZ!Box's behaviour; omitted fields stay |
| `POST /api/zigbee2mqtt` | `{"passive_timeout_minutes": 5}` | Zigbee2MQTT's `availability.passive.timeout`, in minutes, counted from each device's last message; answers with the Zigbee2MQTT part of `/api/state`, `device_options` included |

### What the control API returns

`GET /api/state` (the same object as `SimulatedHome.state()`):

```json
{
  "time": 1767225600.0,
  "outdoor_temperature": 5.0,
  "fritzbox": {"report_delay": 0.0, "summer": false, "holiday": false, "txbusy_while_pending": false,
               "window_target": "unchanged", "locked_command": "refuse"},
  "zigbee2mqtt": {"passive_timeout_minutes": 1500.0,
                  "device_options": {"homeassistant": {"last_seen": {"enabled_by_default": true}}}},
  "rooms": {"bedroom": {"name": "Schlafzimmer", "temperature": 18.0, "humidity": 55.0, "open_windows": 0,
                        "devices": ["schlafzimmer_heizung", "schlafzimmer", "schlafzimmer_fenster"]}},
  "devices": {"<device id>": "<as GET /api/devices/{id}>"}
}
```

`GET /api/devices/{id}` (the same object as `SimulatedHome.device_state(id)`) and every answer of a
`POST /api/devices/{id}/...`:

| Field | Devices | Meaning |
|---|---|---|
| `id`, `family`, `model`, `name`, `room` | all | Control-API ID, `zigbee` or `fritz`, model, friendly or FRITZ! name, room ID |
| `ieee` | Zigbee | IEEE address |
| `ain` | FRITZ! | Actor identification number |
| `answering` | all | The device receives commands and its messages arrive (`false` while silent or unavailable; FRITZ!: `present`) |
| `available` | all | What Zigbee2MQTT publishes on `<name>/availability` (FRITZ!: `present`) |
| `state` | all | Zigbee: the device's own registers, keyed like Zigbee2MQTT's properties. FRITZ!: `target_temperature` (°C, `"off"` or `"on"`), `reported_target_temperature` (what the box reports), `comfort_temperature`, `eco_temperature`, `temperature`, `battery`, `battery_low`, `lock` (as reported, including summer and holiday), `device_lock`, `window_open_until` (Unix time or `null`) |
| `zigbee2mqtt_state` | Zigbee | What Zigbee2MQTT has cached and publishes on `zigbee2mqtt/<name>`, without `last_seen`, `linkquality` and `update`. It differs from `state` while the device is silent, and after a value the device ignored or rounded |
| `valve_opening` | thermostats | 0 to 1 |
| `set_commands` | FRITZ! thermostats | How many `sethkrtsoll` requests the box has received for it since the simulator started, refused ones included; a dial turn is not one. The end-to-end tests use it to tell one command from a resent one. |
| `internal_temperature`, `regulation_temperature` | Zigbee thermostats | What the own sensor reads; what the valve regulates on (and the device reports as `local_temperature`) |
| `external_received`, `returns_on_feed` | TRV-ZBT, TRVZB | Unix time of the last accepted external temperature (or `null`); whether the device returns to it by itself |
| `remote_received` | BTH-RA | Unix time of the last accepted remote temperature (or `null`) |

### Renamed since the first version of this interface

The first version of this pull request (before review) used other names. Nothing in `main` used
them, but anyone who read that version should take the new ones:

- A device's `online` is now two fields: `answering` (it receives commands and is heard) and
  `available` (what Zigbee2MQTT publishes).
- A Zigbee device's `state` now means the device's own registers; Zigbee2MQTT's cache is
  `zigbee2mqtt_state` (in Python, `ZigbeeDevice.cache`).
- `ZigbeeDevice.online` and `ZigbeeDevice.set_online()` became `ZigbeeDevice.answering` and
  `ZigbeeDevice.set_answering()`. `SimulatedHome.set_online()` keeps its name and now means
  "unavailable at once"; `SimulatedHome.set_silent()` is the realistic case.
- The passive timeout is `passive_timeout_minutes` everywhere: in `home.yaml`, in
  `POST /api/zigbee2mqtt`, in its answer and in `/api/state`, and in
  `SimulatedHome.configure_zigbee2mqtt()`.

## `cave_sim.testing`: a simulated home inside Home Assistant's tests

For the simulator tests here, the demo test (WP6) and the device tests (WP7). Import the fixture
into the `conftest.py` of a Home Assistant test directory (see `tests/sim/conftest.py`):

```python
from cave_sim.testing.fixtures import simulated_home  # noqa: F401
```

```python
from cave_sim.testing import DEMO_HOME, async_advance


async def test_window_pause(hass, simulated_home, freezer):
    sim = await simulated_home(DEMO_HOME)  # or a HomeSpec, or a path; setup_fritzbox=False
    sim.home.set_window("wohnzimmer_fenster", True)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.wohnzimmer_fenster_contact").state == "on"
    await async_advance(hass, freezer, 3600)  # one hour, in 60 s steps
    assert sim.home.rooms["living_room"].temperature < 15
```

| Name | What it is |
|---|---|
| `simulated_home` (fixture) | `await simulated_home(home=DEMO_HOME, *, setup_fritzbox=True)` returns a `SimulatedHomeHandle`; everything is stopped after the test. One per test. |
| `SimulatedHomeHandle.home` | The `SimulatedHome`: the control functions and the state, below |
| `SimulatedHomeHandle.broker` | The in-process broker; `broker.messages("zigbee2mqtt/#")` lists what was published, `broker.retained` what is retained |
| `SimulatedHomeHandle.fritz_url`, `.fritz_entry` | The fake box's URL on 127.0.0.1 and Home Assistant's config entry for it |
| `async_refresh_fritzbox()` | Make Home Assistant poll the FRITZ!Box now instead of after its 30 s interval |
| `async_flush_subscriptions()` | Let Home Assistant's MQTT client send queued subscriptions (it waits 0.5 s otherwise); needed after anything that makes Home Assistant subscribe anew, such as reloading the MQTT entry |
| `async_start_simulated_home(hass, home, *, mqtt_client_mock, mqtt_mock_entry, setup_fritzbox=True)` | The same without the fixture; needs `mock_hass_config` and `socket_enabled`, and `await handle.async_stop()` at the end |
| `async_advance(hass, freezer, seconds, step=60)` | Move Home Assistant's and the simulators' time forward, firing timers each step |
| `HassClock`, `connect_hass_to_broker` | The building blocks: Home Assistant's clock, and the bridge between the mocked paho client and the broker |
| `DEMO_HOME` | The path of `sim/demo/home.yaml` |

How it works: the Zigbee2MQTT stand-in and Home Assistant's real MQTT integration share an
in-process broker with retained messages; the test library's mocked paho client is routed through
it. The fake FRITZ!Box serves HTTP on `127.0.0.1` (the only address the test library lets sockets
reach), and Home Assistant's `fritzbox` integration is set up through its config flow, so
`pyfritzhome` talks real HTTP to it. The simulators run on Home Assistant's clock. On start, after
Home Assistant has processed discovery, every device's state is published once more, which is what
Zigbee2MQTT does 30 s after Home Assistant's birth message.

Control functions of `SimulatedHome` (the control API calls the same ones):
`set_window(device_id, is_open)`, `set_room_temperature(room_id, t)`, `set_room_humidity(room_id, h)`,
`set_outdoor_temperature(t)`, `set_silent(device_id, silent)`, `set_online(device_id, online)`,
`configure_zigbee2mqtt(passive_timeout_minutes=)`, `turn_dial(device_id, target | "off" | "on")`,
`set_device_mode(device_id, mode)`, `set_battery(device_id, level, low=None)`,
`configure_fritzbox(report_delay=, summer=, holiday=, txbusy_while_pending=, window_target=, locked_command=)`,
`detect_open_window(device_id, minutes)`, `set_fritz_lock(device_id, lock=, device_lock=)`,
`set_returns_on_feed(device_id, returns)`;
state: `device_state(device_id)`, `state()`, and the objects in `home.rooms`, `home.devices`,
`home.zigbee.devices`, `home.fritzbox`.

## Models, fixtures and recordings

The models are data: `cave_sim/zigbee/models.py` lists each model's exposes, transcribed from
zigbee-herdsman-converters v26.125.0 (the version Zigbee2MQTT 2.14.3 pins), with the source next to
each model, plus the reporting configuration its `configure` sets up. `cave_sim/zigbee/discovery.py`
is a transcription of Zigbee2MQTT 2.14.3's `lib/extension/homeassistant.ts` that turns them into
discovery messages. Adding a model means adding its description, a behaviour class if it needs one,
and regenerating the fixtures.

At run time the discovery messages and the initial state come from `cave_sim/zigbee/fixtures/<model>.json`,
with the device's name and addresses as placeholders (`@FRIENDLY_NAME@`, `@OBJECT_ID@`, `@IEEE@`,
`@COORDINATOR_IEEE@`). The shipped fixtures are generated:

```sh
cd sim && uv run python -m cave_sim.zigbee.fixtures          # regenerate
cd sim && uv run python -m cave_sim.zigbee.fixtures --check  # what a test runs
```

**Recording a real device.** With a real Zigbee2MQTT (Home Assistant discovery on) and the device
paired under a friendly name containing a capital letter or a space:

```sh
cd sim && uv run python -m cave_sim.zigbee.record --host 127.0.0.1 --username "$CAVE_MQTT_USER" \
    --password-env CAVE_MQTT_PASSWORD --friendly-name "Wohnzimmer Heizung Fenster" --model TRV-ZBT --seconds 120
```

Touch the device (or publish `{"state": ""}` to its `/get` topic) so that it reports within the
time. The recording replaces `fixtures/<model>.json`, with a `source` starting `recorded:`; the
simulator then publishes exactly the recorded messages, and the generator leaves the file alone.
The model still decides how the device reacts to commands, so the two must agree: `--check`
(which a test runs) compares every recorded fixture with its model (entities, modes, ranges, steps,
options, payloads, command topics) and names what to change in `models.py`.

## Sources

| What | Source |
|---|---|
| Discovery, entity IDs, topics, availability, state publishing, command parsing | Zigbee2MQTT 2.14.3: `lib/extension/homeassistant.ts`, `lib/extension/publish.ts`, `lib/extension/availability.ts`, `lib/controller.ts`, `lib/mqtt.ts`, `lib/model/device.ts`, `lib/util/stringify.ts` |
| Model exposes and reporting | zigbee-herdsman-converters v26.125.0: `src/devices/sonoff.ts`, `src/devices/bosch.ts`, `src/lib/bosch.ts`, `src/lib/modernExtend.ts`, `src/lib/exposes.ts`, `src/lib/ewelink.ts`, `src/lib/reporting.ts`, `src/converters/toZigbee.ts` |
| Sonoff fallback after two hours, return on the next value | zigbee-herdsman-converters pull request 8873 (comments of its author), and the `temperature_sensor_select` description in sonoff.ts |
| Bosch remote temperature at least every 30 min, boost 5 min, window mode | `src/lib/bosch.ts` descriptions |
| FRITZ! login | FRITZ! "HTTP_Session-ID_EN.pdf" (via fritz.com/en/pages/interfaces), whose worked examples are tests |
| FRITZ! smart-home API | FRITZ! "AHA-HTTP-Interface.pdf", 05.08.25 |
| What Home Assistant asks of the box | Home Assistant 2026.10.4 `homeassistant/components/fritzbox` and `pyfritzhome` 0.6.21 |

## Known differences from the real devices

General (Zigbee2MQTT):

- Only the parts of Zigbee2MQTT that face Home Assistant exist: no Zigbee network, pairing, groups,
  scenes, OTA, `bridge/devices` or the web UI. `bridge/info` carries only what the bridge entities
  read; the bridge's `log_level` select, network map and coordinator version entities are left out.
- Availability follows Zigbee2MQTT's passive check (`availability.ts`): a silent device stays
  available until `passive_timeout_minutes` (default 1500) after its last message. A device that
  answers counts as heard all the time, since real devices check in regularly; the moment it falls
  silent is its last message. Commands to a silent device fail without anything being published;
  the device keeps regulating with its own settings, and its own registers (`state`) stay separate
  from Zigbee2MQTT's cache (`zigbee2mqtt_state`), so a dial turned meanwhile is reported on return.
  `availability` (`online: false`) is a shortcut that shows a device unavailable at once.
- After a write the device's own report follows only where the stored value differs from the sent
  one (a setpoint rounded to 0.5 K). A real device reports when the attribute changed by at least
  its reportable change (0.1 °C for the setpoint), which makes no difference to Home Assistant.
- A setpoint outside the device's range is refused, as the ZCL attribute's limits make the device
  answer `INVALID_VALUE`. Other numeric values outside their range are published optimistically,
  because Zigbee2MQTT's `numeric` converter does no range check, and ignored by the device, as
  zigbee-herdsman-converters PR 8873 observed for the TRVZB's external temperature (assumed for
  the others).
- Attribute reporting follows each model's configured minimum, maximum and reportable change, checked
  on every 60 s step of the room model and after a change made through the control API. Battery
  levels change only through the control API.
- `linkquality` is fixed at 120; device `sw_version` is "Zigbee2MQTT 2.14.3" because the devices
  report no software build ID; the coordinator is "EmberZNet 8.0.2.0" at `0x00124b0000000001`.

Thermostats (all):

- The valve is proportional: fully open 1 K below the setpoint. The own sensor reads the room plus
  up to 2 K next to an open valve (with a 10-minute lag), so regulating on it under-heats the room,
  as the concept expects. On-device schedules, PID modes, temperature accuracy, valve positions,
  calibration of the motor and the device's own window detection are stored and reported but have
  no effect. Mode `auto` heats to the current setpoint.

TRV-ZBT and TRVZB:

- The external temperature is used while `temperature_sensor_select` is `remote_temperature` and
  the last value is less than two hours old; then the device reports `remote_source_offline` and
  regulates on its own sensor; the next value brings it back (switchable with
  `POST /api/devices/{id}/sonoff`, since our own hardware has not confirmed it). While the external
  value is in use it is reported as `local_temperature`. The two hours and the return are observed
  for the TRVZB only (zigbee-herdsman-converters PR 8873); applying them to the TRV-ZBT is an
  assumption.
- Frost protection opens the valve below `frost_protection_temperature` while the mode is `off`.

BTH-RA:

- The remote temperature is used for 30 minutes after it was written. `operating_mode: pause` and
  `window_detection: ON` close the valve, `boost_heating: ON` opens it for 5 minutes. A setpoint
  written over Zigbee is reported with `setpoint_change_source: externally`, a dial turn with
  `manual` (an assumption). Frost protection below 5 °C is assumed.

SNZB-02P and SNZB-04P:

- The SNZB-02P reports by its configured reporting (10 s, 1 h, 1 °C and 1 %); real ones may
  report smaller changes. Humidity is not modelled: it stays where it was set.
- The SNZB-04P has no settable property; its contact is the window of its room. Tamper never
  triggers.

FRITZ!Smart Thermo 302 and the FRITZ!Box:

- One delay (`report_delay`) governs both when the box reports a new setpoint and when the valve
  applies it; whether a real box reports before the valve has it is open (concept section 12). A
  dial turn is known to the box at once.
- While summer mode, holiday mode or the key lock via UI/API is active, the box reports `lock` set.
  Home Assistant refuses commands itself, by the values it last polled, with a
  `HomeAssistantError`. In the up to 30 s between the lock being set and the next poll the command
  reaches the box. What a real box then answers is not documented, so `locked_command` chooses:
  `refuse` (HTTP 400, which reaches the caller of the service as `requests.HTTPError`, not as a
  `HomeAssistantError`) or `ignore` (HTTP 200 and no effect, so only the missing confirmation shows
  it). Both are tested.
- The thermostat's own window detection can be triggered (`window-detection`); it closes the valve
  and reports `windowopenactiv`. Whether a real box shows the target unchanged or as OFF (253)
  meanwhile is open (concept section 12): `window_target` chooses; with `off` Home Assistant
  shows the thermostat as off.
- `BlockTime` is always 0 (a real box blocks logins after failures; pyfritzhome would sleep).
  `txbusy` is 0 unless `txbusy_while_pending` is set, because pyfritzhome waits with real sleeps
  while it is 1. The timetable is neutral: no next change (`endperiod` 0, `tchange` 255).
- The measured temperature is reported live; a real thermostat reports every few minutes. There
  are no statistics (`getbasicdevicestats`), switches, templates or triggers.
