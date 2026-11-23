# Hardware acceptance: what to verify on real devices

Everything in this repository was proven against simulated devices and Home Assistant's own
integrations. A simulator is a model built from documentation, so a few things can only be found
out on real devices. [`cave device-check`](devices.md#checking-a-real-thermostat-cave-device-check)
asks each thermostat the questions the tests ask the simulators; this checklist is what it cannot
ask: how the real devices behave where the simulators had to guess, and what depends on your
building and your network.

Work through it once, after the trial room of [pairing.md](pairing.md) is running, and again for
whatever you add later that behaves differently (another model). Tick each box when you have the
answer, write the answer in the line below it, and do what the point says with it. Most points
take a few minutes of watching and some a few hours of waiting; the longest, the timetable, takes
a day.

## Before you start

- [ ] The trial room is paired, described in `cave/heating.yaml`, applied, and its schedule exists
  ([pairing.md](pairing.md#5-describe-the-rooms)).
- [ ] `./cave device-check` passed for each of its thermostats. Keep its output: the times are used below.
- [ ] You can open Home Assistant's **History** and **Logbook** (sidebar) and Zigbee2MQTT's web page
  (`http://<server>:8080`).
- [ ] You know the entity IDs of the room (`climate.cave_<room>` is the room, `sensor.cave_<room>_reason`
  says why it is where it is, `binary_sensor.cave_<room>_problem` says whether something needs attention).

Some observations take hours and run through the night. The nightly update (03:30) restarts the
stack when it installs a new release: the History then has a gap, and for five minutes after the
restart a dial turned on a thermostat is not taken over (`confirm_timeout`). For the length of this
checklist set `CAVE_AUTO_UPDATE=0` in `/opt/cave/.env` (`sudo -u cave editor /opt/cave/.env`), which
stops only the timer-started updates, and **set it back to `1` when you are done**; check afterwards that
`.env` says `1` again. Run `cave device-check` by day for the same reason.

Every observation below is a look at the **History** of an entity (Home Assistant, sidebar, pick the
entity and a time span), and the controller's own words are in `sensor.cave_<room>_reason` and the
problem sensor's attribute `messages`.

## 1. The open points of concept section 12

[Concept section 12](concept.md#12-risks-and-open-points) lists what is not confirmed without
hardware. The controller is built so that it works whichever way each turns out; the checks tell
you which way it is, so that the documentation and the simulators say what the real devices do.

### 1.1 When the FRITZ!Box reports a new setpoint

Whether the FRITZ!Box reports a new target before the valve has it, or only after the valve has it
(about three minutes on the 302, up to fifteen on the 301), is not known. The simulator does both.

- **Observe.** `cave device-check` printed how long the FRITZ! thermostat took to report a target half
  a degree higher: a time near zero means the box reports at once. Then read the thermostat's own
  display: set a target in Home Assistant (the room's card, any change) and note after how many
  minutes the number on the thermostat's display follows. Do it three times and write down the
  longest. A target set by hand is an override that lasts until the room's next schedule change or six
  hours; press **Resume schedule** on the room's card when you have the numbers, and check that
  the room reads its schedule's temperature again.
- **Result:** report time ______ , valve time ______ .
- **Do with it.** If the box reports within the time the valve takes (the first case), nothing to
  change. If the valve needs longer than 20 minutes, which is the controller's patience for a FRITZ!
  thermostat (`timing: confirm_timeout_slow`, [the integration's README](../custom_components/cave_heating/README.md#timing)),
  raise that value in `heating.yaml` above the time you measured, and apply. Either way, a result
  that the simulators cannot play, for instance a report that comes before the valve has the
  value but never at a fixed delay, is a defect to report as an issue in the repository, with the
  numbers, so that the fake FRITZ!Box is changed (`sim/README.md` lists its known differences).

### 1.2 What the FRITZ!Box reports during its own window detection

The FRITZ! thermostat can detect an open window by a fast temperature drop and close its valve. The
box may then show the target unchanged, or as OFF. The simulator plays both.

- **Observe.** On a cold day, in a room that is heating, open a window wide. The thermostat reacts to
  a fast fall of the temperature, so it may take a few tries; it shows a window symbol when it has
  detected it, and the FRITZ!Box reports it as `binary_sensor.<name>_open_window_detected`. Watch
  `climate.<name>`: does it keep its target and mode, or show `off`? Watch the room's problem sensor
  and its `messages` for a minute after. If the detection never fires, write that down and move on.
  Close the window again, and if the room has a window contact its pause ends two minutes later: check that the
  room is back at its target.
- **Result:** target unchanged / shows off (cross out one).
- **Do with it.** Unchanged: nothing. Shown as `off`: the controller takes it for a mode changed by
  hand and puts the thermostat back to heat, which is a command to the thermostat every time the
  detection fires, up to 20 an hour before it holds back. If the room's problem sensor then comes on
  or the thermostat keeps flapping, switch the thermostat's own window detection off in its
  profile in the FRITZ!Box (the room's window contact does the job anyway), and report the behaviour as an issue.

### 1.3 A neutral FRITZ!Box timetable

What a timetable has to look like so that the box never changes the thermostat's target by itself
is not confirmed. [pairing.md](pairing.md#give-it-a-timetable-that-changes-nothing) says: no switching times, equal
comfort and saving temperature.

- **Observe.** Touch nothing for a whole day and night. Open the **History** of the FRITZ! thermostat
  `climate.<name>` and of its room `climate.cave_<room>` for the 24 hours. The thermostat's target
  may only change when the room's does (the controller's schedule at work). A step at any other
  time came from the box, most likely at a time that used to be a switching time. Also look at
  `sensor.cave_<room>_reason`: `override` appearing without you touching anything means the thermostat
  changed its target and the controller took it for a dial turn.
- **Result:** neutral / changed at ______ .
- **Do with it.** A step means the timetable still switches: open the thermostat's profile in the
  FRITZ!Box, find that time and remove it, and run the day again. When you have the recipe that
  works, write it into [pairing.md](pairing.md#give-it-a-timetable-that-changes-nothing) in a pull request, replacing the
  intention with what you found.

### 1.4 Whether and when a Sonoff leaves the external temperature, and whether it comes back

A Sonoff thermostat regulates on the room temperature the controller writes to it. The TRVZB is
documented to go back to its own sensor after two hours without a value, showing
`remote_source_offline` as its sensor selection, and to return when the next value arrives. For
the TRV Gen2 that is assumed. It matters because, if a thermostat never lets go, a dead room-sensor
battery leaves it regulating on a number that no longer changes.

- **Observe.** Do this on a mild day, or in a room you do not need warm: for three hours the room is
  switched off, which means its radiators heat only against frost. In a room with a Sonoff, a room
  sensor and the feed running, switch the room off in the dashboard (the feed stops while it is off).
  Open the History of `select.<name>_temperature_sensor_select`. Wait three hours. Then switch the
  room back on: the feed starts again at once. Watch the select for ten minutes, and check that
  the room's thermostat is in the mode `heat` and at the room's target again.
- **Result:** went to `remote_source_offline` after ______ ; came back by itself / only after the controller's
  write (5 minutes after the feed resumed).
- **Do with it.** Both ways are covered: the controller leaves the switch alone and writes the
  selection again after the feed resumed. If the select **never** leaves `remote_temperature`, the
  thermostat holds the last value for good: report it as an issue (the answer decides whether a
  stale room sensor has to be handled differently), and in the meantime watch the battery level
  of the room sensors on the Maintenance view.

### 1.5 What a Sonoff reports while its own frost protection is heating

With the thermostat in the mode `off` in a cold room, a Sonoff opens its valve below its frost
protection temperature (5 to 15 °C on the TRV Gen2, per its Zigbee2MQTT page). The simulator opens
the valve and reports `off`. What a real one reports is not known.

- **Observe.** Switch the room off in the dashboard: its radiators now heat only against frost. Let
  the room cool below the thermostat's frost protection temperature (read it on its Zigbee2MQTT
  page: `frost_protection_temperature`; the room is cold only on a cold day or in an unheated room)
  and watch `climate.<name>` in the History: does the mode stay `off`, does `hvac_action` show
  heating? You may raise `frost_protection_temperature` to 15 °C to shorten the wait, but **write down
  its value first**. The controller does not manage that setting, so if you leave it at 15 °C every
  room you switch off later is heated to 15 °C and nothing on the dashboard says why. When you have
  the result, set it back to the value you wrote down, and look at the device's Zigbee2MQTT page to
  check that it is back. Switch the room on again.
- **Result:** mode stays off / flips to ______ .
- **Do with it.** Staying `off`: nothing. A mode that flips to `heat` is taken for a change by hand
  and put back, a command each time. The command budget (20 an hour) bounds it and turns the
  room's problem sensor on. Report what you saw as an issue.

### 1.6 The server's absence

Requirement R9: if the server is down, the radiators keep their last setpoint and their frost
protection, and a radiator that was off stays off.

- **Observe.** With one room heating and another switched off, run `sudo -u cave ./cave down`,
  and wait ten minutes: Home Assistant records nothing while it is down, so watch the thermostats'
  displays (the targets they show, and whether the switched-off room's radiator stays cold) and
  write them down. Then `sudo -u cave ./cave up`, wait until `sudo -u cave ./cave status` says
  healthy, and check that the rooms are back at their targets. The radiators are not controlled
  for those ten minutes.
- **Result:** the thermostats kept their targets / did something else: ______ .
- **Do with it.** Anything else than keeping their last setpoint is a defect to report.

### 1.7 A room sensor in a perfectly steady room

Home Assistant records a state only when a value changed, so a sensor that is alive and reports
every hour, but whose temperature, humidity and battery level all stay exactly the same, would look
silent ([issue #21](https://github.com/oxydonth/cave_system/issues/21)). The room therefore judges
the sensor by its `sensor.<name>_last_seen`, which Zigbee2MQTT moves with every message (concept
section 5.4). This was proven against the simulators only: whether a real SNZB-02P sends a message
at least every three hours when nothing changes, and whether Zigbee2MQTT then moves `last_seen`, is
what this point checks.

- **Observe.** After pairing, open the room sensor's device page in Home Assistant: `Last seen`
  is listed under Diagnostic, enabled, with a time. If it is disabled, open the entity's settings:
  when Home Assistant says it was disabled by the integration, run `./cave apply` (or restart Home
  Assistant), which enables it; when it says disabled by the user, enable it there by hand
  (`cave_heating` leaves an owner's choice alone). Then look again a minute later: about half a
  minute after it was enabled every Zigbee device shows unavailable for a moment (Home Assistant
  reloads its MQTT integration), and their values return within another half minute. Note how
  long it took, and whether a thermostat received anything but its settings meanwhile
  (`sudo -u cave ./cave logs --tail 200 zigbee2mqtt`). Then, after a night in which the room was
  empty and steady, open the **History** of the sensor's temperature and humidity. Is there a
  stretch of three hours or more in which neither changed? If so, did the room's
  `climate.cave_<room>` show the sensor's temperature throughout (not the thermostats' average)?
  `Last seen` has no history (the recorder leaves it out, `homeassistant/configuration.yaml`):
  how often the sensor sent during the stretch shows in Zigbee2MQTT's log, one `MQTT publish`
  line on `zigbee2mqtt/<name>` per message. For a whole night:
  `sudo -u cave ./cave logs --since 12h --tail all zigbee2mqtt | grep "topic 'zigbee2mqtt/<name>'"`.
  A restart of Home Assistant or Zigbee2MQTT adds one such line that is not a message from the
  sensor: Zigbee2MQTT sending its states again.
- **Result:** longest steady stretch ______ ; longest gap between two messages of the sensor
  ______ ; the room showed the thermostats' reading: yes / no.
- **Do with it.** A gap between messages longer than three hours means the sensor reports less often
  than assumed: raise `sensor_stale` in the `timing:` block above that gap, and report it on the
  issue. The room showing the thermostats' reading while `Last seen` moved is a defect to report.

## 2. Radio range

Battery-powered Zigbee devices do not relay for each other, so each one has to reach the
coordinator, or a mains-powered Zigbee device, directly.

- **Observe.** For every Zigbee device note its link quality in Zigbee2MQTT's **Devices** list (the
  LQI column). Compare the farthest rooms with the nearest. Then watch for a week: **Unavailable devices**
  on the Maintenance view, and the problem sensors, must stay quiet; a device that is lost and
  found again repeatedly is at the edge.
- **Result:** the lowest LQI ______ in the room ______ ; devices lost in the week: ______ .
- **Do with it.** A device that drops out is the reason for one of two things: put a mains-powered
  Zigbee plug between it and the coordinator as a relay, or use a FRITZ!Smart Thermo 302 in that
  room ([hardware.md](hardware.md#where-the-coordinator-goes)). Moving the coordinator is the third:
  it is on a cable, and the best place is central. Concrete walls and floors are the usual cause.

## 3. The coordinator

The SMLIGHT SLZB-06MG24 is attached by network. Check that it holds.

- [ ] **It reconnects after a reboot of the server.** `sudo reboot`; when the server is back, `sudo -u cave ./cave status`
  is healthy, Zigbee2MQTT's page opens and the devices are available without pairing again. While the
  server reboots, nothing controls the radiators; they keep their last setpoint.
- [ ] **It survives its own power loss.** Unplug the coordinator for a minute and plug it back; meanwhile the Zigbee thermostats keep their
  setpoint and receive nothing, and the room sensors and window contacts are not heard. Look at
  `sudo -u cave ./cave logs --tail 100 zigbee2mqtt`: does Zigbee2MQTT reconnect by itself, or exit? The
  stack restarts it if it exits, but note how long the devices were unavailable, and check that every
  device is available again afterwards (**Unavailable devices** on the Maintenance view reads "All devices are available.").
- [ ] **Its address is fixed.** The FRITZ!Box always gives it the same address ([install.md](install.md#2-give-the-server-a-fixed-address)),
  the same as `CAVE_Z2M_SERIAL_PORT` in `.env`.
- [ ] **The network key is backed up.** `CAVE_DATA/zigbee2mqtt/configuration.yaml` is in the latest
  backup ([operations.md](operations.md#backups)); without the key every device has to be paired again.
- Result and the coordinator's firmware version (Zigbee2MQTT, **Settings**, **About**): ______ .

## 4. Presence through the FRITZ!Box

The demo cannot test this: the simulated FRITZ!Box has no TR-064 interface, which FRITZ!Box Tools
needs, so presence from the FRITZ!Box is unproven until it runs against a real one.

- [ ] Your phone's tracker is enabled and reads `home` on the Wi-Fi, and is listed under `presence`
  ([phone-and-remote.md](phone-and-remote.md#3-presence-setting-the-heating-back-when-nobody-is-home)).
- [ ] **Leaving.** Turn the phone's Wi-Fi off (if someone else's phone is listed and at home, the home
  stays occupied; do this when you are the one at home or take theirs off too). After `away_delay` the
  rooms heat at the away temperature, until the Wi-Fi is back on, so do not leave it off for longer than the
  test. Note how long the tracker takes to read `not_home`, and
  then how long `binary_sensor.cave_home_occupied` takes to go off (the tracker's delay plus `away_delay`).
  The rooms' reason (`sensor.cave_<room>_reason`) should read `away`.
- [ ] **Returning.** Turn the Wi-Fi on again: the home is occupied and the rooms go back to their
  schedule within moments. Check that `sensor.cave_<room>_reason` no longer reads `away`.
- [ ] **A sleeping phone.** Leave the phone idle on a table for an hour with the screen off. The home
  must stay occupied. If it flips to away, raise `away_delay` in `heating.yaml` or the tracker's
  **consider home** option ([phone-and-remote.md](phone-and-remote.md#35-what-the-away-delay-does)).
- Result: tracker takes ______ to leave; occupied changes after ______ .

## 5. Recording a real device's messages for the simulators

The simulators publish what their models say a device publishes. A recording of a real device
replaces that, so the simulators converge on reality. It is optional, and it goes through a pull
request like any other change. The tool is in `sim/cave_sim/zigbee/record.py`; the details are in
[sim/README.md](../sim/README.md#models-fixtures-and-recordings).

You need a development checkout (`make setup`), and the real Zigbee2MQTT's broker. The broker
listens on the server's loopback address only, so open a tunnel from your development machine:

```sh
ssh -N -L 1883:127.0.0.1:1883 <your user>@<server>
```

Pair the device under a friendly name with a capital letter or a space in it (the names of
[pairing.md](pairing.md#1-choose-the-names) have both), then record, in a second terminal, with
the broker login from the server's `.env`:

```sh
export CAVE_MQTT_USER=cave CAVE_MQTT_PASSWORD='<from /opt/cave/.env>'
cd sim && uv run python -m cave_sim.zigbee.record --host 127.0.0.1 --username "$CAVE_MQTT_USER" \
    --password-env CAVE_MQTT_PASSWORD --friendly-name "Wohnzimmer Heizung Fenster" \
    --model TRV-ZBT --seconds 120
```

`--model` is one of `TRV-ZBT`, `TRVZB`, `BTH-RA`, `SNZB-02P` and `SNZB-04P`. While it records, make the
device report: turn the dial, open the window, or publish `{"state": ""}` to `zigbee2mqtt/<name>/get`.
The recording replaces `sim/cave_sim/zigbee/fixtures/<model>.json`. Then:

```sh
cd sim && uv run python -m cave_sim.zigbee.fixtures --check
make check
```

The check compares the recording with the model (entities, modes, ranges, steps, options,
payloads, command topics) and names what to change in `sim/cave_sim/zigbee/models.py` where they
differ. Fix the model, run `make check` again, and open a pull request with the recording and
the change (`CLAUDE.md` has the delivery rules). Read the recording before you commit it: it is
a real device's messages. There is no recorder for the FRITZ!Box: what a real box reports is noted by hand in
[devices.md](devices.md) and `sim/README.md` under the known differences.

## 6. The server

The update, backup, restore and watch are tested with real files and real Git, but with Docker
faked, as an ordinary user, without systemd and without pulling a plug. What only the server can
show is here. Do it once after the install, and again after a release that changes
[operations.md](operations.md). Run each command in `/opt/cave`.

### 6.1 Git as the service user, started by root

`update` runs as root and starts Git as the owner of the checkout with `runuser -u cave -- git …`,
so that the fetch uses the service user's deploy key. `runuser` may keep root's `HOME`; OpenSSH
takes the key from the service user's own home either way, but Git also reads its configuration
from `HOME`.

- **Observe.** `sudo ./cave update`, with or without a new release, and read its first lines and
  `sudo -u cave ./cave status` ("Last check"). Also
  `sudo runuser -u cave -- sh -c 'echo $HOME; git -C /opt/cave fetch --dry-run'`.
- **Result:** the fetch worked: yes / no; `HOME` was ______ ; messages about `/root`: ______ .
- **Do with it.** A fetch that works is all that matters. "Could not fetch releases" with
  "Permission denied" for a path below `/root`, or a host key question, is a defect: report it with
  the output. Until it is fixed, updates are by hand (`sudo -u cave git -C /opt/cave fetch --tags`
  works; `update` does the rest).

### 6.2 Files that belong to root and to the containers

The state holds files of root and of the containers' users. A backup must keep their owners, a
restore must give them back, and the lock must end up the service user's whoever took it first.

- **Observe.** `sudo find /var/lib/cave -path '*/.cave/ops' -prune -o -printf '%u:%g %m %p\n' | sort > /tmp/owners-before`,
  then `sudo ./cave backup` and `sudo ./cave restore /var/backups/cave/<the archive just written>`,
  and the same `find` into `/tmp/owners-after`; `diff /tmp/owners-before /tmp/owners-after`. Then
  `ls -l /var/lib/cave/.cave` (the lock file and `.cave` belong to `cave`), and
  `sudo -u cave ./cave apply` (it takes the lock as the service user).
- **Result:** the owners and modes differ in: ______ ; `.cave` and its lock belong to: ______ ;
  `apply` took the lock: yes / no.
- **Do with it.** Only the times of files Home Assistant wrote meanwhile may differ. Any owner or
  mode that changed, or `apply` refusing with "cannot take the lock", is a defect: report it.
  Delete the `before-restore-*` directory and the `.env.before-restore-*` file the restore names
  once all is well.

### 6.3 The timers and the boot

- **Observe.** `systemctl list-timers 'cave-*'` lists `cave-backup.timer`, `cave-update.timer` and
  `cave-watch.timer` with their next runs; the next morning, `journalctl -u cave-backup.service -u
  cave-update.service --since yesterday` shows both nightly runs; `journalctl -u
  cave-watch.service --since '-30min'` shows a run every five minutes, as the user `cave`, without
  "permission denied" for Docker. Then `sudo reboot`, and afterwards `systemctl status
  cave-recover.service` (ran and exited 0) and `sudo -u cave ./cave status` (healthy).
- **Result:** the three timers are listed: yes / no; the watch runs every five minutes: yes / no;
  after the reboot the stack is healthy: yes / no.
- **Do with it.** A timer that is missing: run `sudo /opt/cave/scripts/provision-host.sh` again.
  Anything else is a defect to report.

### 6.4 Docker creating a missing bind-mount source

Docker creates a bind mount's missing source as root. `init` and `restore` create the ones in the
checkout and the state directory first, so that none of them becomes root's and blocks a later
update of the checkout.

- **Observe.** After the first `./cave up`, and again after a restore,
  `sudo find /opt/cave /var/lib/cave/homeassistant -maxdepth 2 -user root`.
- **Result:** listed: ______ .
- **Do with it.** Within `/opt/cave` nothing may be listed, `tools` least of all. In
  `/var/lib/cave/homeassistant` files Home Assistant wrote are expected, but not the mount points
  `packages` and `custom_components/cave_heating`. One of those listed is a defect: report it, and
  `sudo chown cave: <it>`.

### 6.5 A real power cut

- **Observe.** While the stack runs normally, pull the plug, wait a minute, and plug it in again.
  Ten minutes after the start, `sudo -u cave ./cave status`. If you want to see
  an update interrupted: when a new release is out, run `sudo ./cave update` and pull the plug as
  soon as it says "Stopping the stack"; after the start, `sudo -u cave ./cave status` and
  `journalctl -t cave --since '-1h'`.
- **Result:** the stack came back healthy by itself: yes / no; the interrupted update was rolled
  back at boot ("An update from … was interrupted"): yes / no; an alert arrived (with
  `CAVE_ALERT_URL` set): ______ .
- **Do with it.** The stack has to come back by itself, and an interrupted update has to be rolled
  back by `cave-recover.service` at boot. An alert for the minutes of the power cut itself is a
  defect (two runs far apart start the watch's count afresh); one for an update that was
  interrupted is right if its way back could not finish. Report anything else.

### 6.6 The alert and the watch

- **Observe.** Set `CAVE_ALERT_URL` to a topic of your ntfy server and subscribe to it on the phone.
  `sudo -u cave ./cave down`, wait 20 minutes: nothing arrives. `sudo -u cave ./cave up`. Then stop
  one container without `cave`, `sudo docker stop cave-homeassistant-1`: after 15 to 20 minutes
  one alert arrives ("the stack is not healthy" or "the stack is stopped"), and no second one.
  `sudo -u cave ./cave up`: within five minutes "healthy again". Read the alert's text.
- **Result:** no alert after `down`: yes / no; one alert, then one all-clear: yes / no; the text
  holds the server's name, the command and the stack's state, and nothing else: yes / no.
- **Do with it.** Anything else is a defect to report. If your receiver needs a token, check that
  it is not in `journalctl -u cave-watch.service` nor in `journalctl -t cave`.

### 6.7 The disk over many releases

- **Observe.** After the second and every later installed update, `sudo docker image ls` and
  `df -h /var/lib/cave`.
- **Result:** versions of the Home Assistant, broker and Zigbee2MQTT images listed: ______ .
- **Do with it.** At most two versions of each (the running release's and the one before) are
  expected, three after a rollback, plus any your `compose.override.yaml` names. Every other
  image of these three repositories is removed, also one that other software pulled, unless a
  container of it exists; images of other repositories are never touched. More than that after
  an update that said "Installed": look for `Old images were not removed` in
  `journalctl -u cave-update.service`, and report it if the reason does not explain it.

### 6.8 What Docker lists, and what it refuses to remove

The removal of old images relies on two things the tests can only fake: what
`docker image ls --digests --format '{{.Repository}}\t{{.Tag}}\t{{.Digest}}'` prints (one line
per repository and tag, `<none>` as the tag of an image that has only a digest, the image's
`sha256:` digest in the last column), and that `docker image rm` without `--force` refuses an
image that a stopped container uses.

- **Observe.** `sudo docker image ls --digests --format '{{.Repository}}\t{{.Tag}}\t{{.Digest}}'`.
  Then stop one container without removing it, `sudo docker stop cave-mosquitto-1`, try
  `sudo docker image rm eclipse-mosquitto:<the version it runs>`, and start it again with
  `sudo -u cave ./cave up`.
- **Result:** the lines look as described: yes / no (paste one) ______ ; the removal was refused
  ("conflict … is using its referenced image"): yes / no.
- **Do with it.** Both as described is right. A listing that looks different (another column
  order, an empty digest for an image that was pulled) or a removal that went through is a
  defect: report it with the output, because the update could then remove an image it means to
  keep. (After `./cave down` there are no containers at all, so nothing protects the stack's
  images; that is why the update removes nothing while the stack is not running.)

## When you are done

What turned out as the controller expects is now known behaviour. What did not is a defect: open
an issue in the repository for each, with the numbers you wrote down. Where this guide told you to
change another guide (the neutral timetable in [pairing.md](pairing.md)), do it in a pull request,
so that the next owner does not have to find out again.
