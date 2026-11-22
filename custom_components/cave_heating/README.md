# cave_heating

Per-room heating control for Home Assistant: one thermostat entity per room on top of the
radiator thermostats, schedules, window pause, away setback and boost. The design is in
[concept section 5](../../docs/concept.md#5-heating-logic-cave_heating); this page is the reference
for the file, the validator, the entities and the services.

## Setting it up

`configuration.yaml` contains only an empty entry:

```yaml
cave_heating:
```

On the first start the integration creates its config entry by itself. Adding it from the UI ends
with a message saying it is configured through the file.

The home is described in `cave/heating.yaml` in Home Assistant's configuration directory and
applied with `./cave apply`, which validates it and stores the **applied copy**
`cave/.applied/heating.yaml`. The integration reads only the applied copy: at start-up and on
`cave_heating.reload`. A missing or invalid applied copy at start-up means no rooms and a repair
notice; an invalid one on reload leaves the running configuration in effect, raises a repair
notice, and makes the service call fail with the problems in its message. Neither ever keeps Home
Assistant from starting.

## The file

```yaml
version: 1                      # required; the format of the file
presence:                       # optional; one entity or a list
  - device_tracker.max_phone
away_delay: "00:30:00"
away_temperature: 16.0
defaults:                       # for every room unless the room sets its own
  comfort_temperature: 21.0
  eco_temperature: 17.5
  boost_temperature: 25.0
  boost_duration: "00:30:00"
  override_max: "06:00:00"
  window_open_delay: "00:01:00"
  window_close_delay: "00:02:00"
rooms:
  - id: living_room             # required; lower-case letters, digits, single underscores
    name: Wohnzimmer            # required; the device name and the Home Assistant area
    trvs:                       # required; at least one
      - entity: climate.wohnzimmer_heizung
        external_temperature: number.wohnzimmer_heizung_external_temperature_input
        settings:
          select.wohnzimmer_heizung_temperature_sensor_select: external
          switch.wohnzimmer_heizung_open_window: "off"
        slow: false
    temperature_sensor: sensor.wohnzimmer_temperature
    humidity_sensor: sensor.wohnzimmer_humidity
    windows: [binary_sensor.wohnzimmer_fenster]
    schedule: schedule.living_room_heating
timing: {}                      # optional; see below
```

| Key | Default | Meaning |
|---|---|---|
| `version` | — | Format of the file. Every earlier version stays accepted and is converted on load. |
| `presence` | none | Someone is home if any says so: a zone with a count above zero, a person or device tracker `home`, anything else `on`. An entity that becomes unavailable keeps its last reading for `presence_unavailable_grace`, then counts as home; one never heard from counts as home at once. |
| `away_delay` | `00:30:00` | How long presence must report nobody home before the home is away. |
| `away_temperature` | 16.0 | First value of `number.cave_away_temperature`; afterwards the UI value wins. |
| `comfort_temperature`, `eco_temperature` | 21.0, 17.5 | First values of the room's two numbers; afterwards the UI values win. A differing file value is logged on load. |
| `boost_temperature`, `boost_duration` | 25.0, `00:30:00` | The preset `boost` and the default of `cave_heating.boost`. |
| `override_max` | `06:00:00` | An override ends at the latest after this long. |
| `window_open_delay`, `window_close_delay` | `00:01:00`, `00:02:00` | The window pause starts when a contact has been open this long, and ends when all have been closed this long. |
| `trvs[].entity` | — | The thermostat's `climate` entity. A thermostat belongs to one room. |
| `trvs[].external_temperature` | none | A `number` or `input_number` the room sensor's reading is written to. |
| `trvs[].settings` | none | Device settings (`select`, `switch`, `number` entities and the value each should have), written when the file is loaded, when the thermostat returns from being unavailable, and `confirm_timeout` after the temperature feed starts again; never watched in between. |
| `trvs[].slow` | `true` for the FRITZ!Box integration, else `false` | Slow thermostats get `confirm_timeout_slow`. |
| `temperature_sensor`, `humidity_sensor` | none | Without a fresh room sensor (see `sensor_stale`) the room shows the average of its thermostats' readings and the feed stops. |
| `windows` | none | Window contacts (`binary_sensor` or `input_boolean`), one entity or a list. |
| `schedule` | none | A `schedule` (or `input_boolean`, `binary_sensor`); on means comfort, off means eco. Without one the room is at comfort. |

Temperatures are in °C, from 5 to 30 in steps of 0.5. Durations are `"HH:MM:SS"` or `"HH:MM"`
in quotes, or a number of seconds.

### `timing`

| Key | Default | Meaning |
|---|---|---|
| `debounce` | 2 s | Changes within this time are sent once. |
| `confirm_timeout` | 5 min | How long a command may take to be confirmed by the thermostat's report. |
| `confirm_timeout_slow` | 20 min | The same for slow thermostats. |
| `resend_attempts` | 3 | Resends before the thermostat is `unresponsive`. |
| `retry_interval` | 30 min | How often an `unresponsive` or `blocked` thermostat is tried again. |
| `manual_settle` | 45 s | Dial turns are collected this long before the last one becomes the room's override. |
| `command_budget_per_hour` | 20 | Mode and target commands per thermostat per hour; more wait. |
| `budget_exhausted_hours` | 3 | A thermostat whose budget is used up this many hours in a row is `unresponsive`: tried again only every `retry_interval`, with a repair notice, until it has matched for a whole `retry_interval`. |
| `feed_threshold` | 0.2 K | Change of the room temperature that is written to the thermostat... |
| `feed_min_interval` | 10 min | ...once this long has passed since the last write. |
| `feed_max_interval` | 25 min | The temperature is written at least this often. |
| `sensor_stale` | 3 h | A room sensor counts as unavailable when its device has not been heard from for this long: by the value of its Zigbee2MQTT `last_seen` sensor where it has one, otherwise by the newest report of any entity of its device (its humidity or battery reading, say). Home Assistant does not record a repeated identical value, so in a steady room every reading can look silent; `last_seen` changes with every message. See "The room sensor's `last_seen`" below. |
| `window_unavailable_grace` | 30 min | A window contact that goes silent while open keeps the pause this long. |
| `presence_unavailable_grace` | 30 min | A presence entity that becomes unavailable keeps its last reading this long. |

## Turning a dial

A dial turned on a radiator thermostat becomes the room's override `manual_settle` after the last
turn, and the room's other thermostats follow.

For `confirm_timeout` (5 minutes) after Home Assistant or the integration starts, after a
thermostat returns from being unavailable, and after a hold ends, a dial turn is not taken over:
the controller puts the thermostat back to the room's target with one command, because a report in
that time can still be the late answer to a command sent before. Turn the dial again once the five
minutes are over, or set the temperature in the app. A short radio outage starts the five minutes
again for that thermostat.

The window is the same five minutes for slow thermostats (FRITZ!), although their commands may
take up to `confirm_timeout_slow` to arrive. The cost: a late report of a command from before the
restart or the outage that arrives after the five minutes on a slow thermostat can be taken for a
dial turn, and the room then takes that target as its override.

## The validator

Run in Home Assistant's configuration directory, also while Home Assistant is down:

```
python -m custom_components.cave_heating.validate cave/heating.yaml
```

| Exit status | Output |
|---|---|
| 0 | The file is valid. One line on standard output: `cave/heating.yaml is valid: version 1, 2 rooms, 3 thermostats.` |
| 1 | The file is not valid, missing, or not YAML. On standard error: `cave/heating.yaml is not valid (2 problems):`, then one complete sentence per problem, each naming the room and the field, for example `Room "bathroom" (rooms[1]), field "window_open_delay": must be a duration such as "00:05:00" (hours:minutes:seconds) or a number of seconds, but is "5 min".` |
| 2 | Called wrongly; the usage on standard error. |

## Entities

Entity IDs are fixed by the room `id`; unique IDs are `<room id>_<key>` (`home_<key>` for the
home). Each room is a device in the area named like the room.

| Entity | Purpose |
|---|---|
| `climate.cave_<room>` | The room thermostat: modes `heat` and `off`; 5 to 30 °C in 0.5 K steps; presets `comfort`, `eco`, `away`, `boost`, `none`; attributes `reason`, `override_until`, `boost_until` (ISO time or null). Unavailable while none of its thermostats is available. |
| `number.cave_<room>_comfort_temperature`, `number.cave_<room>_eco_temperature` | The schedule temperatures. |
| `button.cave_<room>_resume_schedule` | Ends a boost or override. |
| `binary_sensor.cave_<room>_window_open` | On exactly while the window pause is in effect. |
| `binary_sensor.cave_<room>_problem` | On when something needs attention. Attribute `thermostats`: thermostat entity ID → `ok`, `pending`, `unresponsive`, `blocked`, `held`, `unavailable` or `missing`. Attribute `messages`: a list of sentences. `held` and `pending` are not problems. |
| `sensor.cave_<room>_reason` | `summer`, `room_off`, `window`, `boost`, `override`, `away`, `comfort` or `eco`. |
| `select.cave_home_mode` | `auto`, `away` (forced setback), `off` (summer). |
| `number.cave_away_temperature` | The setback temperature of the whole home. |
| `binary_sensor.cave_home_occupied` | Presence after the away delay. |

## Services

| Service | Fields | Does |
|---|---|---|
| `cave_heating.boost` | target: room thermostats (entity, device or area); `duration`, `temperature` optional | Boosts the rooms; a `temperature` is rounded to the 0.5 K step, half up. A target that is not a room thermostat, or that reaches none, is refused with a message. |
| `cave_heating.resume_schedule` | target: room thermostats, refused like `boost` | Ends a boost or override. |
| `cave_heating.reload` | none | Reads the applied copy again. Fails with the problems in its message (HTTP 400 over the REST API) if the file is not valid; the running configuration stays. |
| `cave_heating.hold` | `entity_id`: a thermostat named in the file; `duration` optional, 15 minutes by default, more than zero and at most 4 hours | The controller sends the thermostat nothing and adopts nothing from it. Not kept across a restart. |
| `cave_heating.release` | `entity_id` | Ends a hold (also one that has already ended); the thermostat is brought in line with its room. |

Setting a temperature or preset, or boosting, is refused with a message while the home mode is
`off` or the room is switched off.

## Repair notices

`applied_file_missing`, `applied_file_invalid` (at start-up), `reload_invalid` (on reload),
`missing_<room>_<entity>` for an entity named in the file that Home Assistant does not have, and
`unresponsive_<entity>` for a thermostat that does not confirm its commands, or that kept differing
from what is wanted for `budget_exhausted_hours`.

## The room sensor's `last_seen`

A Zigbee2MQTT room sensor is judged by its device's `sensor.<name>_last_seen` (concept section
5.4): the time Zigbee2MQTT last heard it, which moves with every message, also when all its
readings repeat. Its value counts, not the time Home Assistant wrote it, so a sensor that died
before a restart of Home Assistant or Zigbee2MQTT is stale at once afterwards instead of
`sensor_stale` later; a value later than its arrival counts from its arrival. Without a value
(before Zigbee2MQTT's first message, or with `last_seen` disabled) the newest report of any entity
of the device counts, as before. The entity is recognised by Zigbee2MQTT's unique ID
(`<IEEE address>_last_seen_<base topic>`), so renaming it changes nothing.

`compose.yaml` makes Zigbee2MQTT enable it for every device it discovers. One that Home Assistant
registered disabled before that is enabled by the integration when Home Assistant has started,
on each reload (`./cave apply`), and when the sensor's device gains it later; only one disabled by
its integration, never one the owner disabled. Home Assistant then reloads its MQTT integration
about 30 seconds later, once for all of them: every MQTT entity is unavailable for a moment and
unknown until Zigbee2MQTT sends the states again, 30 seconds after Home Assistant's birth message.
The controller treats that like any short outage: a window pause in effect continues (the grace
for a contact that goes silent while open is 30 minutes), a thermostat that comes back is not
sent a command if it matches, and its device settings are written once more where their entities
were still unknown when it came back.

Thermostats and FRITZ! devices are not judged by age at all: a thermostat's reading is used while
the thermostat is available, and the FRITZ! integration polls the box every 30 seconds.

## Restarts

The state file keeps each room's mode, temperatures, boost and override (with the schedule state
it was set under), whether its window pause is in effect, since when nobody has been home, and the
presence entities' last readings. Holds are not kept. Nothing is sent to the thermostats until Home
Assistant has finished starting, so a restart sends nothing to a thermostat that is where it
should be.
