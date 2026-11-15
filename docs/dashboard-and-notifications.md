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
| Rooms needing attention | Each room's problem sensor: `ok`, or `needs attention` with the sensor's `messages` and the state of each thermostat from its `thermostats` attribute |

A home without rooms still has both views; the room view says that no rooms are configured yet.

The texts of the dashboard are English. Room names are the `name` from the file.

### Generating it

`./cave apply` generates the dashboard after it has validated and stored your `heating.yaml`
(the command ships in a later pull request of work package 5). The generator can also be run
by hand:

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
what the documentation lists.

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
message to that service as well.

| Field | Meaning |
|---|---|
| `title` | Headline |
| `message` | What happened |
| `notification_id` | Optional. A later call with the same id replaces the earlier notification |

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
Helpers**, choose **Notify service** and enter the name of the phone's notify service, which
you find under **Developer tools, Actions** by searching for `notify.mobile_app`. For example
`notify.mobile_app_pixel_8`; the part before the dot may be left out. Leave the helper empty to
send nothing but the persistent notification.

A wrong or unknown name does not stop anything: the persistent notification is created first,
the script finishes normally, and the failed attempt is written to the Home Assistant log as an
error of the automation "Cave: send a notification copy".

### What notifies, and when

The thresholds and durations are the first block of
`homeassistant/packages/cave_notifications.yaml`, with a comment each. Change a value there and
restart Home Assistant. Five sensors named `sensor.cave_attention_*` check once a minute, so a
notification appears within a minute of its condition being met. Each notification appears once,
is dismissed again when the condition ends, and does not return once you have dismissed it,
unless the condition ends and starts again.

| Notification | Condition | Default |
|---|---|---|
| Low battery | A battery sensor of any device, in percent or as a low-battery flag, is at or below the limit | 20 % |
| Device unavailable | Every entity of a device has been unavailable for this long. Zigbee2MQTT itself waits 25 hours before it marks a battery device unavailable | 60 minutes |
| Window left open | A room's `binary_sensor.cave_<room>_window_open` has been on for this long. That sensor turns on only after the room's window delay | 30 minutes |
| High humidity | A humidity sensor is above the limit; it clears, and may notify again, only when it is below the second value | 70 %, clears below 65 % |
| Heating problem | A room's `binary_sensor.cave_<room>_problem` has been on for this long; the message lists what is wrong | 10 minutes |

Texts are in English and name the room by its Home Assistant area where the entity has one.
Restarting Home Assistant clears persistent notifications; conditions that still hold are not
announced again, because the sensors remember what they reported.

### Testing it

`tests/packages/test_cave_notifications.py` loads the package into a Home Assistant test
instance, creates stand-in entities with the IDs of concept section 5.2, changes their states and
moves the clock, and asserts which notifications appear and which do not:

```sh
uv run pytest tests/packages
```
