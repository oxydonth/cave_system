# Pairing devices and describing rooms

This guide takes you from a running system ([install.md](install.md)) to a heated room: you pair
the devices, tell the heating which devices make up a room, and check each thermostat. Do it for
one room first, the one of your trial purchase ([hardware.md](hardware.md)), and repeat for the
others when it works.

What only real hardware can show is not known yet, and this guide says where. Those points are
listed in [concept section 12](concept.md#12-risks-and-open-points) and checked off in
[hardware-acceptance.md](hardware-acceptance.md).

## The order

1. [Choose the names](#1-choose-the-names) of the devices.
2. [Pair the Zigbee devices](#2-pair-the-zigbee-devices): thermostats, room sensors, window contacts.
3. [Pair a FRITZ! thermostat](#3-register-a-fritz-thermostat), if you have one.
4. [Find the entity IDs](#4-find-the-entity-ids) Home Assistant gave them.
5. [Describe the rooms](#5-describe-the-rooms) in `cave/heating.yaml` and apply it.
6. [Check each thermostat](#6-check-each-thermostat) with `cave device-check`.
7. [Place the sensors](#7-place-the-room-sensors-so-they-read-right).

## 1. Choose the names

Home Assistant builds the entity IDs from the device's name: lower case, spaces as underscores. A
thermostat called `Wohnzimmer Heizung Fenster` becomes `climate.wohnzimmer_heizung_fenster`, and a
room sensor called `Wohnzimmer` becomes `sensor.wohnzimmer_temperature`. You will type these IDs
into `heating.yaml`, so use plain names, without umlauts, and the same scheme everywhere:

| Device | Name | Example |
|---|---|---|
| Thermostat | `<Room> Heizung <Where>`; `<Room> Heizung` if there is one | `Wohnzimmer Heizung Fenster`, `Wohnzimmer Heizung Tuer` |
| Room sensor | `<Room>` | `Wohnzimmer` |
| Window contact | `<Room> Fenster` | `Wohnzimmer Fenster` |

For a FRITZ! thermostat the name is the one you give it in the FRITZ!Box, with the same scheme
(`Bad Heizung`).

## 2. Pair the Zigbee devices

You need the coordinator connected and Zigbee2MQTT running (`COMPOSE_PROFILES=zigbee`, see
[install.md](install.md#7-check-and-start)). Fit a thermostat on its valve **before** pairing it:
the Sonoff and Bosch thermostats calibrate their valve travel when they start, and a head that is
not seated makes the calibration fail ([hardware.md](hardware.md#will-it-fit-your-valve) covers the
adapters). Sit near the coordinator for the first one if you can; range is for
[hardware-acceptance.md](hardware-acceptance.md).

1. Open Zigbee2MQTT's web page at `http://<server>:8080` and enter `CAVE_Z2M_FRONTEND_TOKEN` from
   `.env` (`sudo -u cave grep '^CAVE_Z2M_FRONTEND_TOKEN' /opt/cave/.env`).
2. Click **Permit join (All)** at the top right. The network is open for 254 seconds; click again
   to close it early. Do one device at a time.
3. Put the device in pairing mode (the table below).
4. Watch the log in the web page. A line saying that the device was interviewed and has been paired
   (the wording differs between versions) means it is in. The device then appears under **Devices**
   with its IEEE address as its name.
5. **Rename it** now, to the name from [step 1](#1-choose-the-names): open the device and use the rename button next to its
   name, with the option to update the Home Assistant entity ID switched on. (Zigbee2MQTT's rename
   request has it as `homeassistant_rename`: without it, Home Assistant keeps the old entity IDs.)
6. In Home Assistant the device and its entities appear by themselves, through MQTT discovery.

| Device | Pairing mode | Source |
|---|---|---|
| **Sonoff TRV Gen2** (TRV-ZBT) | Press the middle button: the display shows `Calibrate` while the valve finds its travel, then the room temperature. Set the valve to manual and turn the knob down below 5 °C until the display reads `OFF`, then hold the middle button for 2 seconds. The pairing icon flashes; it stops after 3 minutes. | [localsmarthomeguide](https://localsmarthomeguide.com/articles/sonoff-trv-gen2-home-assistant-setup/), whose author did not test the valve himself; confirm on yours |
| **Sonoff TRVZB** | Wait until the display shows the temperature. If it shows `FS` or `RD`, press the top button. Hold the top button for about 5 seconds until the temperature appears. Turn the dial counter-clockwise until the display shows `OF`, then hold the top button for 3 seconds. A flashing signal icon means it is pairing. | [Zigbee2MQTT device page](https://www.zigbee2mqtt.io/devices/TRVZB.html) |
| **Bosch Thermostat II [+M]** | Switch it from Matter to Zigbee and enter its install code in Zigbee2MQTT first: see [below](#the-bosch-thermostat-matter-to-zigbee-and-its-install-code). | below |
| **Sonoff SNZB-02P** (room sensor), **SNZB-04P** (window contact) | Pull the battery tab: the device is in pairing mode for 3 minutes, its LED flashing slowly. To get there again, hold the button for 5 seconds until the LED flashes slowly. If pairing keeps failing, Sonoff's page for the SNZB-04P says to reset it by holding the button for 10 seconds and to try again. | [Sonoff's help page for the SNZB-04P](https://help.sonoff.tech/docs/snzb-04p); the 02P's manual says the same for pairing |

A thermostat that you pair needs a second look after pairing: the display should show the room
temperature, and the Zigbee2MQTT page should list the device with a link quality (LQI) value. A
device that pairs and then drops out is out of range, or has a flat battery; see
[hardware-acceptance.md](hardware-acceptance.md#2-radio-range).

### The Bosch thermostat: Matter to Zigbee, and its install code

The Bosch Thermostat II [+M] ships in Matter mode and has to be switched to Zigbee. You pair it
with an **install code**, a long string you assemble yourself, because the code printed on the
device is for Matter only. You need the Bosch controller for none of this. (The Zigbee-only Bosch
without "+M" needs no switch; its QR code under the battery cover already is the install code.)

1. **Reset it to factory settings**, which also makes it Matter again: take out one battery, hold
   the main button (marked `O`) while you put it back, and when the LED blinks orange and the display
   shows `RES`, let go and press and hold the button again until the LED is solid green. If the LED
   blinks red, it went wrong: start again.
2. **Switch to Zigbee mode.** Hold the main button until the display shows `Z` and the LED blinks
   orange (Zigbee). Blue means Matter. The sources give 3 seconds and 5 seconds for how long to hold;
   hold until the `Z` appears. If nothing happens, take out a battery, put it back and try again.
3. **Assemble the install code**, from four parts written one after another, without spaces:

   | Part | Characters | Where it comes from |
   |---|---|---|
   | Prefix | 40 | Fixed: `RB01SG0D83101826480080000000000000000000` |
   | IEEE address | 16 | Printed in the battery compartment or on the battery cover, next to the QR code, without colons |
   | `DLK` | 3 | Fixed: the three letters |
   | Install code | 36 | Printed next to the IEEE address; leave out the hyphens |

   The result is 95 characters. A string that starts with `MT:` is the Matter code: it cannot be
   used. Copy the code from the source below if your device's printing differs.
4. In Zigbee2MQTT open **Settings**, **Tools**, click **Add install code**, paste the string and
   confirm. The log says that the install code was added.
5. Click **Permit join (All)**, and put the thermostat in pairing mode by taking out a battery and
   putting it back: the display shows `>o<` (or `>☐<`, the sources differ). Wait for the interview
   to succeed.
6. Press the button on the valve to start the valve adaptation (three blocks move across the display)
   and leave it until the display shows the temperature.

"Interview failed because can not get node descriptor" means the install code is wrong: check how
you copied it. Zigbee2MQTT needs a coordinator that supports install codes: Thing King lists
the Silicon Labs (`ember`) and Texas Instruments (`zstack`) coordinators as able to, and the ConBee as unable. Sources: the [Zigbee2MQTT device page](https://www.zigbee2mqtt.io/devices/BTH-RA.html)
and Thing King on [switching to Zigbee](https://www.thing-king.de/bosch-heizkoerper-thermostat-ii-m-von-matter-thread-zu-zigbee-umstellen/),
[pairing](https://www.thing-king.de/bosch-smart-home-heizkoerper-thermostat-ii-ueber-zigbee2mqtt-mit-home-assistant-verbinden/)
and [the install code](https://www.thing-king.de/bosch-heizkoerper-thermostat-ii-m-install-code-fuer-die-zigbee-verbindung/).
Not tested here: the button timings above are the sources', and the first real Bosch is the test.

## 3. Register a FRITZ! thermostat

A FRITZ!Smart Thermo 302 talks to your FRITZ!Box over DECT. Home Assistant's FRITZ!SmartHome
integration, which `./cave bootstrap` set up if `.env` has the FRITZ!Box login, picks the
thermostat up as soon as the box knows it. That login needs the Smart Home permission:
[phone-and-remote.md](phone-and-remote.md#31-prepare-the-fritzbox) says how to create it.

1. Put the batteries in the thermostat.
2. On the thermostat press **MENU**: it starts the registration ("Anmeldung starten"). Then, within
   the time the thermostat allows, press the **Connect** button on the FRITZ!Box, or hold its **DECT**
   button for 6 seconds. (The box's own pages can start a registration too: **Smart Home**,
   **Devices and Groups** (Geräte und Gruppen), register a device (Gerät anmelden), according to
   [AVM's knowledge base article](https://fritz.com/service/wissensdatenbank/dok/FRITZ-Smart-Thermo-302/1628_FRITZ-Heizkorperregler-einsetzen/).)
3. A symbol on the thermostat's display tells you that it worked; only then mount it on the radiator,
   and let it adapt the valve stroke (it shows a temperature when it has). If the display says the
   registration failed, AVM's "Alternatives Anmelden" is: hold **MENU** for 2 seconds, press the key
   under the registration symbol, then Connect or DECT on the box again. Both are in AVM's quick guide
   ([FRITZ!Smart Thermo 302](https://assets.avm.de/files/docs/fritzsmart/fritzsmart-thermo-302/fritzsmart-thermo-302_qig_de_DE.pdf),
   pages 7 and 8). Menu labels on the box differ a little between FRITZ!OS versions.
4. Give it its name: in **Smart Home**, **Devices and Groups**, the edit icon beside the thermostat
   opens its Smart Home profile (AVM's quick guide, page 11). Use the name from
   [the first step of this guide](#1-choose-the-names). The name decides the entity IDs, so give it
   before Home Assistant has settled on another one; Home Assistant polls the box every 30 seconds, so
   the thermostat shows up there shortly.

### Give it a timetable that changes nothing

The FRITZ!Box has its own timetable (Zeitschaltung) for the thermostat: a comfort temperature
(Komforttemperatur) and a setback temperature (Absenktemperatur), and up to 8 switching points
(Schaltpunkte) a day. This system is the only scheduler. If the box's timetable also switches, the
two fight: AVM's manual says that a temperature set by hand holds only until the next switching point
is reached, so a switching point overwrites the target that Home Assistant set. In the thermostat's
profile under **Smart Home**, **Devices and Groups**:

1. Remove the switching points from the timetable, if the box lets you. This is the intention: a
   timetable without any change.
2. If the box insists on a timetable, give the comfort and the setback temperature the same value.
   That does not guarantee anything: whether a switch to either temperature still overwrites the
   target is not settled by AVM's manual.
3. Look in the profile for a period during which the box turns the heating off ("Heizung aus"), and
   for a holiday setting (Urlaubsschaltung). AVM's manual says that during a holiday setting the
   thermostat cannot be operated by hand, and the concept expects the box to refuse commands while
   its summer or holiday mode or the key lock is active: Home Assistant then shows the thermostat as
   `blocked`. Test reports and forum posts say that the box ships with a heating-off period of about
   1 June to 31 August, with the key lock; I did not find that in AVM's manual for the 302. If you see
   such a period, delete it, and use the home mode `off` in the dashboard for the summer instead.
4. The thermostat's own window detection (Fenster-auf-Erkennung) may stay on in a room without a window
   contact.

What a timetable has to look like to stay neutral is **not confirmed without a real box** (concept
section 12), and neither is the "Heizung aus" period. [hardware-acceptance.md](hardware-acceptance.md#13-a-neutral-fritzbox-timetable)
has the test: the thermostat's target must not change by itself over a day.

## 4. Find the entity IDs

For each device you need the entity IDs. In Home Assistant open **Settings**, **Devices &
services**, **Entities** and search for the device's name. What you need, for a device named
`<name>`:

| Device | Entity IDs you will use |
|---|---|
| Thermostat (Sonoff) | `climate.<name>`, `number.<name>_external_temperature_input`, `select.<name>_temperature_sensor_select`, `switch.<name>_open_window` |
| Thermostat (Bosch) | `climate.<name>`, `number.<name>_remote_temperature`, `switch.<name>_window_detection` |
| Thermostat (FRITZ!) | `climate.<name>` |
| Room sensor | `sensor.<name>_temperature`, `sensor.<name>_humidity` |
| Window contact | `binary_sensor.<name>_contact` |

Every Zigbee device also has a `sensor.<name>_last_seen` (shown as "Last seen" under Diagnostic):
when Zigbee2MQTT last heard from the device. This project's Zigbee2MQTT enables it for every
device it discovers (`device_options` in `compose.yaml`). It does not go into `heating.yaml`, but
`cave_heating` relies on the room sensor's: it tells a sensor whose readings stay the same from
one that has stopped sending. Leave it enabled. If you paired devices before this was set, their
`Last seen` is disabled; `cave_heating` enables the room sensors' itself when it starts or when
you run `./cave apply`, and Home Assistant then reloads its MQTT integration about half a minute
later, which makes every Zigbee device unavailable for a moment. A `Last seen` you disabled
yourself is left alone, and the room then judges the sensor by its readings alone. Further
device options for Zigbee2MQTT go into `compose.override.yaml`, not its web UI, as the comment at
`ZIGBEE2MQTT_CONFIG_DEVICE_OPTIONS` in `compose.yaml` explains.

The names follow Zigbee2MQTT's and the FRITZ! integration's scheme; the entity page in Home
Assistant is the final word, and a name that differs from the table is the one to write into the
file. If the IDs still carry the device's IEEE address, you did not rename the device in
Zigbee2MQTT with the option on: rename it again.

## 5. Describe the rooms

The description of the home is `cave/heating.yaml` in the state directory:
`/var/lib/cave/homeassistant/cave/heating.yaml`. Home Assistant never reads this file directly:
`./cave apply` validates it completely and only then makes it the version the heating uses, and
reloads it, so a typing error cannot stop Home Assistant. Edit it with:

```sh
sudo -u cave editor /var/lib/cave/homeassistant/cave/heating.yaml
```

The file starts with the home-wide values; leave them. It ends with the line `rooms: []`: remove the `[]` and put the rooms below `rooms:`, as here. This is a room
with two Sonoff thermostats, a room sensor and a window contact, and one with a FRITZ! thermostat:

```yaml
rooms:
  - id: living_room                  # lower-case letters, digits, single underscores: fixes entity IDs
    name: Wohnzimmer                 # the room's name, also its Home Assistant area
    trvs:
      - entity: climate.wohnzimmer_heizung_fenster
        external_temperature: number.wohnzimmer_heizung_fenster_external_temperature_input
        settings:
          select.wohnzimmer_heizung_fenster_temperature_sensor_select: remote_temperature
          switch.wohnzimmer_heizung_fenster_open_window: "off"
      - entity: climate.wohnzimmer_heizung_tuer
        external_temperature: number.wohnzimmer_heizung_tuer_external_temperature_input
        settings:
          select.wohnzimmer_heizung_tuer_temperature_sensor_select: remote_temperature
          switch.wohnzimmer_heizung_tuer_open_window: "off"
    temperature_sensor: sensor.wohnzimmer_temperature
    humidity_sensor: sensor.wohnzimmer_humidity
    windows:
      - binary_sensor.wohnzimmer_fenster_contact
    schedule: schedule.living_room_heating
  - id: bathroom
    name: Bad
    trvs:
      - entity: climate.bad_heizung
    schedule: schedule.bathroom_heating
```

Per room only `id`, `name` and one thermostat are required. Without a schedule the room is at
comfort whenever someone is home; without a room sensor it shows the average of its thermostats'
own readings; without window contacts it has no window pause. The complete list of keys, the
timing values and the validator are in [the integration's README](../custom_components/cave_heating/README.md).
**Temperatures are set in the dashboard, not in this file**: the values in the file are only the
first ones.

### The `settings` entries, per model

The controller sets these on the thermostat, so that the thermostat obeys the room's one brain
(concept section 5.6): it regulates on the room sensor, and does not run its own window detection
or schedule. The names are the entity and option names the devices show in Home Assistant.

| Model | `external_temperature` | `settings` |
|---|---|---|
| Sonoff TRV Gen2 (TRV-ZBT), Sonoff TRVZB | `number.<name>_external_temperature_input` | `select.<name>_temperature_sensor_select: remote_temperature` and `switch.<name>_open_window: "off"` |
| Bosch Thermostat II (BTH-RA) | `number.<name>_remote_temperature` | `switch.<name>_window_detection: "off"`; there is no sensor selection, the device uses the remote temperature as long as it is fresher than 30 minutes |
| FRITZ!Smart Thermo 302 | none: it regulates on its own sensor | none: set up its timetable in the box instead (above) |

Write `"off"` in quotes: unquoted, YAML reads it as false. In a room **without a room sensor**,
leave out `external_temperature` and the sensor selection: there is nothing to feed. The first
`remote_temperature` option name comes from the Zigbee2MQTT device pages of both Sonoff models, whose
selection reads `local_temperature`, `remote_temperature` or `remote_source_offline`; the option
list in Home Assistant is the final word. If a name is wrong, Home Assistant refuses it, and the room's
problem sensor says so.

A Sonoff goes back to its own sensor by itself after two hours without a temperature (the
selection then reads `remote_source_offline`), and the controller deliberately does not fight
that. The two hours were observed on the TRVZB only; for the TRV Gen2 they are an assumption.
Whether it returns by itself when the feed resumes is open (concept section 12); the controller
writes the selection again five minutes after the feed resumed, which covers it.

### Apply it

```sh
cd /opt/cave
sudo -u cave ./cave apply
```

`apply` validates the file. If it finds a mistake, it says in which room and field, and changes
nothing: fix the line and run it again. If the file is valid, it stores it as the version in use,
regenerates the dashboard from it and tells Home Assistant to reload the heating. It ends with three
lines that say what happened (applied copy, dashboard, Home Assistant). If it says that Home
Assistant has to be restarted instead, run `./cave bootstrap` once: the script token is missing.
If a room sensor's `Last seen` was still disabled, the Zigbee devices are unavailable for a moment
half a minute later: `cave_heating` enabled it (see [the entity IDs](#4-find-the-entity-ids)).

`apply` and `bootstrap` take the same lock as the nightly backup (02:30) and update (03:30). If one says
that another command holds the lock, wait a few minutes and run it again. If one says "Not applied" or
"not bootstrapping" because an update is interrupted or a restore stopped half-way, nothing was
changed: finish that first, as [operations.md](operations.md#a-notification-arrived-what-now) says
(`./cave status` shows it).

A room that names a `schedule` needs the schedule to exist. Run the first-run setup again, which
creates a schedule for each such room with a default week and never touches one that exists:

```sh
sudo -u cave ./cave bootstrap
```

Open **Heizung** in Home Assistant. Each room has a card; the room's problem sensor (the badge in its
heading) should say OK. If it names an entity as missing, check the spelling against [the entity IDs](#4-find-the-entity-ids), edit and
`./cave apply` again. Change the week's timetable under **Settings**, **Devices & services**,
**Helpers**, on the schedule of the room. Adjust the temperatures on the room's card.

### A room is switched off in the dashboard, not at a FRITZ! dial

To heat no more in a room (a guest room in winter) set its thermostat on the dashboard to the mode
`off`; for the whole home in summer, set **Home Mode** to `off`. **Do not turn a FRITZ! thermostat's
dial down to `OFF` to do it**: the thermostat then reports the mode `off`, which the controller
takes for a change it did not make, and puts back to the room's target. Turning the dial to
another temperature is fine: after 45 seconds it becomes the room's temperature, until the next
change of the room's schedule or six hours, whichever comes first, and the room's other
thermostats follow. The same holds for the dials of the
Zigbee thermostats.

## 6. Check each thermostat

`cave device-check` asks a paired thermostat what the device tests ask the simulated ones, and
measures how long it takes. Run it on the server for every new thermostat, after `./cave apply`
has put its room into the file. It needs the script token, so `./cave bootstrap` must have run.

```sh
cd /opt/cave
sudo -u cave ./cave device-check climate.wohnzimmer_heizung_fenster --timeout 300
sudo -u cave ./cave device-check climate.bad_heizung
```

Name the radiator thermostat as listed under `trvs`, never a room's `climate.cave_<room>` (the command
refuses those). For a
Zigbee thermostat 300 seconds is plenty. For a FRITZ! thermostat leave the default (20 minutes):
the box passes a new target to the valve only about every three minutes (up to fifteen on the older
301). The check holds the thermostat away from the controller meanwhile, sets its target half a
degree higher, waits until it is reported back, restores the target and releases the hold, whatever
happens, also if your SSH session drops. In between, a few seconds on a Zigbee thermostat and
minutes on a FRITZ! one, the thermostat sits half a degree above the room's target and the room's
schedule, window pause and dial handling do not reach it. It prints a table with pass or fail and the time each took, and exits non-zero when a
check failed. What each row means, the exit codes and how a failure looks are in
[devices.md](devices.md#checking-a-real-thermostat-cave-device-check).

Run checks by day: a nightly update that restarts the stack during a check can spoil that check
([operations.md](operations.md#which-user)). Write down the times: the FRITZ! one is an open question that
[hardware-acceptance.md](hardware-acceptance.md#11-when-the-fritzbox-reports-a-new-setpoint) uses.

## 7. Place the room sensors so they read right

A room sensor is only worth having if it reads what you feel in the room. The same goes for
humidity: the system tells you to air the room when it is high, so it has to be right.

* **Away from the radiator and its warm air.** Not above it, not on the wall behind it, not in its
  line of heat. The thermostat's own sensor sits there, which is why you have a room sensor.
* **On an interior wall, at about head height**, in the part of the room where you are. A sensor in
  a corner or on a cold outer wall reads too cold and too humid.
* **Not in the sun, not in a draught.** Not on a window sill, next to a window or a door that
  opens, or above a lamp or a television.
* **Not in a cupboard or behind a curtain**, where the air does not move.
* **In the bathroom,** not in the shower, and not right next to it. A spot a few metres away, on
  the wall opposite the shower, reads the humidity of the room instead of the shower's steam. The
  shower will push the reading up anyway; that is what the prompt to air the room is for.
* **One sensor per room**, even if the room has two radiators: they are one room.

If a sensor reads differently from a reference you trust, correct it in Zigbee2MQTT: the device
has `temperature_calibration` and `humidity_calibration`, offsets of up to 50 in either direction
([Zigbee2MQTT device page](https://www.zigbee2mqtt.io/devices/SNZB-02P.html)). According to the
source of the simulator's model, the SNZB-02P reports when the temperature or the humidity changed
by 1 or at least once an hour, so the reading does not move every minute.

A window contact has two parts: the sensor on the fixed frame and the magnet on the sash, as close
together as the window allows when it is closed. It starts the window pause after a minute open
and ends it two minutes after closing, so airing for shorter than that does not stop the heating.
