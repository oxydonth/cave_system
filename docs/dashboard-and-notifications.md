# Heating dashboard and notifications

Two things a person sees of the heating system: the dashboard at `/cave-heating`, generated from the
room description, and the notifications that tell you when something needs attention. The
concept (section 6) says what they contain; this page says how to use and change them.

## The dashboard

### What it shows

The dashboard has two views.

**Rooms** has one section for the home and one per room, in the order of `cave/heating.yaml`.

| Section | Cards |
|---|---|
| Home | Home mode (`auto`, `away`, `off`), the away temperature, whether the home counts as occupied, and, if `presence` is set in the file, the entities that say who is home |
| Each room | A heading with the room's name and two badges (window pause, problem). The room thermostat with its modes and presets. A list with the reason the room is at its target, the window pause, the comfort and eco temperatures, the button that resumes the schedule, the room's schedule and window contacts if the file names them. A history graph of the room thermostat and the room's temperature and humidity sensors if the file names them |

The comfort, eco and away temperatures are changed here, on the cards, and not in `heating.yaml`
(concept section 5.1).

**Maintenance** has three sections.

| Section | Shows |
|---|---|
| Batteries | Every battery sensor Home Assistant knows, lowest first: percentage sensors and low-battery flags. The generator knows no device names; the card asks Home Assistant, so a newly paired device appears without regenerating |
| Unavailable devices | Every device all of whose entities are unavailable |
| Rooms needing attention | The rooms whose problem sensor is not `off`: `needs attention` with the sensor's `messages` and the state of each thermostat from its `thermostats` attribute. "All rooms are ok." when there is none |

A home without rooms still has both views; the room view says that no rooms are configured yet.

The texts of the dashboard are English. Room names are the `name` from the file.

**How rows are named.** Home Assistant names an entity after its device and then itself, which
in a room's list repeats the room: "Wohnzimmer Comfort temperature", cut to "Wohnzimmer Comf..."
on a phone. So:

- The room's own entities (reason, window pause, comfort and eco temperature, resume button) and
  the three tiles of the home are named by the entity's own name alone: "Comfort temperature",
  "Away temperature". The frontend reads it from Home Assistant, so it is in the language the
  integration names it in, and a name you give the entity in Home Assistant is shown instead.
- A window contact is named "Window contact", or "Window contact 1", "Window contact 2", ... in
  the order of `windows` when the room has several. Home Assistant's own name for it comes from
  the device and its device class, "Wohnzimmer Fenster Door" for a Zigbee contact on a window.
  The generator knows a contact only by its entity ID, and the device's name alone would leave
  the row empty for a contact without a device (a group helper, an `input_boolean`), so the row
  says what the file says it is. A contact on a balcony door is a window contact to the heating
  and is shown as one: "Window contact 2" next to the window's "Window contact 1". Its state
  says "Open" or "Closed".
- The schedule keeps the name of its helper, and the history graphs and "Who is home" show
  Home Assistant's names.

### Generating it

`./cave apply` does three things, in this order, after you have edited `cave/heating.yaml`:

1. It validates the file and stores it as the applied copy. An invalid file stops here: the
   errors name the room and field, and nothing changes, not the applied copy, not the dashboard
   and not Home Assistant.
2. It regenerates `dashboards/heating.yaml` from the applied copy, in a one-off Home Assistant
   container.
3. It tells Home Assistant to reload `cave_heating`. Without a script token (before
   `./cave bootstrap`) it says that Home Assistant has to be restarted instead
   (`./cave restart homeassistant`).

It ends with three lines that say what state each thing is in, for example:

```text
Applied copy:    updated
Dashboard:       regenerated (dashboards/heating.yaml, shown at /cave-heating)
Home Assistant:  cave_heating reloaded
```

Once the applied copy is stored, a later failure cannot undo it, so the command carries on and
reports exactly what is left, and exits with status 1:

| What failed | State afterwards | What to do |
|---|---|---|
| The dashboard could not be generated | The applied copy is new and the heating follows it. The dashboard file is untouched: it is the previous one and does not match. The generator's message is shown | Fix the cause it names and run `./cave apply` again |
| Home Assistant is not running, or has no `cave_heating.reload` | Applied copy and dashboard are new. Home Assistant loads the applied copy when it starts | `./cave up`, or `./cave restart homeassistant` |
| Home Assistant refused the reload | Applied copy and dashboard are new; the running configuration stays in effect, and its answer is shown | Fix what it names, or restart Home Assistant |
| Home Assistant did not accept the script token | Applied copy and dashboard are new; Home Assistant was not told | `./cave bootstrap` creates a new token; or restart Home Assistant |

`apply` holds the one lock of `update`, `backup` and `restore` for its whole run, so two runs
cannot interleave and `apply` cannot change the applied copy between an update's check of it and
the restart that follows. When another command holds the lock, `apply` says which one and
changes nothing. When the state directory cannot be read or written, it says so in one line and
changes nothing.

Running `./cave apply` again with an unchanged file regenerates the dashboard and reloads
anyway, which repairs a dashboard that went missing.

The generator can also be run by hand:

```sh
# On the server, in the Home Assistant container:
python /cave/tools/render_dashboard.py /config/cave/.applied/heating.yaml /config/dashboards/heating.yaml

# In a development checkout; `-` prints to the terminal instead of writing a file:
uv run python tools/render_dashboard.py path/to/heating.yaml -
```

The output is the same for the same input, byte for byte, so a changed dashboard file always
means a changed `heating.yaml`. The file is replaced in one step, and when the input cannot be
used the existing file stays as it was. The generator prints one line, for example
`render_dashboard: heating.yaml: room living_room, humidity_sensor: 'x' is not an entity ID`, and
exits with status 1.

It reads only `presence`, and per room `id`, `name`, `temperature_sensor`, `humidity_sensor`,
`windows` and `schedule`. Everything else in the file, including keys of later releases, is ignored.
Entity IDs of the cave entities (`climate.cave_<room>` and the others of concept section 5.2)
come from the room `id`. Do not edit `dashboards/heating.yaml`; the next `apply` overwrites it.

### Built-in cards only

The dashboard uses Home Assistant's own cards (`heading`, `thermostat`, `tile`, `entities`,
`history-graph`, `markdown`), so no third-party frontend code is loaded. The generator checks
what it produces against a list of card types, features, badge types and options
(`ALLOWED_CARD_OPTIONS` and its companions in `tools/render_dashboard.py`), taken from the
Home Assistant 2026.10 dashboard documentation, and refuses to write anything else. To use
another built-in card, add it to those lists from the documentation of that card, and to the
list in `tests/tools/test_render_dashboard.py`, which exists so that widening the list is
deliberate. A card option that does not exist shows as an error card in the browser, so add only
what the documentation lists. A `name` that is not a text is a name item the frontend fills in
from Home Assistant (`{type: entity}`, the entity's own name); the item types it may use are
listed in `ALLOWED_NAME_ITEM_OPTIONS`, checked against the frontend of Home Assistant 2026.10
(`home-assistant-frontend` 20260930.5), which knows `entity`, `device`, `parent_device`,
`area`, `floor` and `text`.

### Changing the generator

The expected output for four example homes is in `tests/tools/golden/<home>/dashboard.yaml`
next to the `heating.yaml` it comes from. After a deliberate change, rewrite them, review the
diff, and commit it:

```sh
UPDATE_GOLDEN=1 uv run pytest tests/tools/test_render_dashboard.py
```

When a golden file differs without `UPDATE_GOLDEN`, the test fails with a unified diff.
The maintenance cards are templates that Home Assistant runs; `tests/tools/templates/` renders
them in a real Home Assistant against stand-in entities.

## Notifications

### script.cave_notify

Everything that tells a person something goes through `script.cave_notify`. It always creates a
persistent notification (the bell in the Home Assistant sidebar), and when the helper
**Notify service** (`input_text.cave_notify_service`) is not empty it sends the same title and
message to that notify action as well.

| Field | Meaning |
|---|---|
| `title` | Headline |
| `message` | What happened |
| `notification_id` | Optional. A later call with the same id replaces the earlier notification |
| `copy` | Optional, true by default. `false` shows the notification in the UI only |

From an automation or script in Home Assistant:

```yaml
- action: script.cave_notify
  data:
    title: Backup
    message: The nightly backup finished
```

From a shell on the server, with the script token of concept section 4.5:

```sh
curl -sS -X POST \
  -H "Authorization: Bearer $(cat "$CAVE_DATA/homeassistant/.cave/token")" \
  -H "Content-Type: application/json" \
  -d '{"title": "Backup", "message": "The nightly backup finished"}' \
  http://127.0.0.1:8123/api/services/script/cave_notify
```

### Sending a copy to the phone

Install the Home Assistant app on the phone, then open **Settings, Devices & services,
Helpers**, choose **Notify service** and enter the name of the phone's notify action, which
you find under **Developer tools, Actions** by searching for `notify.mobile_app`. For example
`notify.mobile_app_pixel_8`; the part before the dot may be left out. Leave the helper empty to
send nothing but the persistent notification.

Only a notify action is ever called. Any other name (`script.cave_notify`, which would notify
for ever, or `homeassistant.restart`) is written to the log as a warning and ignored. A notify
action that does not exist or fails does not stop anything either: the persistent notification is
created first, the script finishes normally, and the failed attempt is written to the log as an
error of the automation "Cave: send a notification copy".

### What notifies, and when

Five sensors named `sensor.cave_attention_*` check once a minute what needs attention, so a
notification appears within a minute of its condition being met. Each notification appears once,
is dismissed again when the condition ends, and does not return while Home Assistant keeps
running once you have dismissed it, unless the condition ends and starts again. A restart is
different: see "Restarts" below, where what you dismissed comes back.

**A first install pushes nothing** for conditions that already exist when the package starts
for the first time: a battery that is already low, a device that is already gone. They have
nothing to be compared with, so they appear in the bell only, and the phone hears of what
happens from then on. The same holds for a kind of notification that a new release adds.

| Notification | Condition | Default |
|---|---|---|
| Low battery | A battery sensor of a device in the home, in percent or as a low-battery flag, is at or below the limit. It ends when the level is at or above the clearing level. The battery of a phone with the Home Assistant app is not checked | 20 %, clears at 30 % |
| Device unavailable | Every entity of a device has been unavailable for this long. Zigbee2MQTT itself waits 25 hours before it marks a battery device unavailable | 60 minutes |
| Window left open | A room's `binary_sensor.cave_<room>_window_open` has been on for this long. That sensor turns on only after the room's window delay | 30 minutes |
| High humidity | The humidity of a room is above the limit; it ends, and may notify again, only when it is below the second value. The room's humidity is the `current_humidity` of `climate.cave_<room>`, which is the reading of the `humidity_sensor` named for the room in `heating.yaml`, so no area has to be assigned to the sensor. A room without a `humidity_sensor`, a sensor that belongs to no room (the garden, the cellar) and an unavailable sensor give no notification. A notification whose room has had no reading for an hour ends, also across a restart: the sensor was taken out of the room in `heating.yaml`, or its battery is dead. A shorter gap, such as a Zigbee sensor that reports a minute or two after a start, keeps it | 70 %, ends below 65 % |
| Heating problem | A room's `binary_sensor.cave_<room>_problem` has been on for this long; the message lists what is wrong. `held` and `pending` thermostats do not turn that sensor on | 10 minutes |

Texts are in English and name the room by its Home Assistant area where the entity has one.

**Changing the limits.** Under **Settings, Devices & services, Helpers** there is one helper per
number, named `Notify: ...`, with the default in its name. Type a number to change it; leave it
empty to use the default. The helpers have no `initial` value, so what you enter survives
restarts and updates, and the package itself, which every update replaces, is never edited. The
defaults are the first block of `homeassistant/packages/cave_notifications.yaml`.

| Helper | Default | Meaning |
|---|---|---|
| `input_text.cave_notify_battery_below` | 20 | Battery at or below this percentage notifies |
| `input_text.cave_notify_battery_clear_above` | 30 | A battery notification ends at or above this |
| `input_text.cave_notify_unavailable_minutes` | 60 | Minutes before an unavailable device notifies |
| `input_text.cave_notify_window_open_minutes` | 30 | Minutes of window pause before it notifies |
| `input_text.cave_notify_humidity_above` | 70 | Humidity above this percentage notifies |
| `input_text.cave_notify_humidity_clear_below` | 65 | A humidity notification ends below this |
| `input_text.cave_notify_problem_minutes` | 10 | Minutes of room problem before it notifies |

**Many at once.** When more than three new entries of one kind appear in the same minute, which
is what a Zigbee coordinator going offline looks like, the phone gets one summary
("12 new: ...") and the UI still gets one notification per entry. Recoveries are never sent to
the phone.

**Restarts.** Home Assistant keeps persistent notifications in memory only, so every restart,
and with it every nightly update, empties the bell. What happens next:

- The sensors remember what they had reported. When Home Assistant has started, the
  notifications of everything that is still reported are created again, in the UI only: the
  phone is not told a second time. This includes a notification you had dismissed: the bell
  shows everything that still holds after every restart, and so after every nightly update,
  until the condition ends. Dismissals are not remembered across a restart.
- An entry that was reported stays while its entity is `unknown` or `unavailable`, which every
  Zigbee device is for a minute or two after a start, and a window pause or a room problem that
  was reported stays while its sensor is on, however young its state looks after the restart.
  So a problem that goes on is not announced again because the restart made it look new.
- A condition that ended while Home Assistant was down is dismissed at the first check. A new
  condition is announced as usual, with the full time limit counted from the start; a condition
  that began while Home Assistant was down is pushed once.
- After a restart, a device that was silent before the restart counts its hour from the start
  again if it was not reported before.
- All of this holds wherever in its minute the start lands. The minute is counted by the
  automation "Cave: notify about what needs attention" itself, which fires the event
  `cave_attention_check` that the sensors check on, so the sensors are never checked before that
  automation listens to them (issue #31).

**Reloading.** Reloading the template entities (Developer tools, YAML) removes and adds the five
sensors. That is treated like a start: what they list is shown, nothing is dismissed and nothing
is sent to the phone.

### Entities of the package

Besides `script.cave_notify`, `input_text.cave_notify_service` and the seven limit helpers above,
the package creates the sensors `sensor.cave_attention_battery`, `_unavailable`, `_windows`,
`_humidity` and `_problems` (state: how many entries; attribute `entries`: key to sentence;
`sensor.cave_attention_humidity` also has `missing_since`, since when each reported room has had
no reading), and three automations: "Cave: notify about what needs attention", which also fires
the event `cave_attention_check` once a minute that the sensors check on, "Cave: show reported
notifications again after a restart" and "Cave: send a notification copy", which listens for the
event `cave_notify_copy`. Turning off "Cave: notify about what needs attention" stops the checks
as well, so nothing is missed while it is off: what changed meanwhile is announced at the first
check after it is turned on again.

### Testing it

`tests/packages/test_cave_notifications.py` loads the package into a Home Assistant test
instance, creates stand-in entities with the IDs of concept section 5.2, changes their states and
moves the clock, and asserts which notifications appear and which do not.
`tests/packages/test_restarts_and_limits.py` does the same across simulated restarts (entities
`unknown`, restored sensors, young `last_changed`, an empty UI), with thirty devices at once, and
with the helpers:

```sh
uv run pytest tests/packages
```

`tests/packages/test_real_integration.py` does the same with the real `cave_heating` on a small
`heating.yaml`: the window and problem notifications come from the integration's own
`binary_sensor.cave_<room>_window_open` and `_problem`, every `cave_*` entity of the generated
dashboard exists in the integration, and the maintenance card shows the real `messages` and
`thermostats` attributes. The pipeline's `stack` job runs `./cave apply` with the real validator
and generator: the starter configuration, a valid file with a room, a well-formed invalid file,
and a held lock.
