# Installing the system

This guide takes a bare machine, a mini PC or a Raspberry Pi, to a running Home Assistant with its
Zigbee broker, set up and ready for devices. Plan an hour, most of it waiting for downloads. Do
the steps in order, once. What to buy is in [hardware.md](hardware.md). If you want to see the
system work before you buy anything, start with [the demo](demo.md), which needs no server.

When you are done, you can log in at `http://<server>:8123`, the nightly update and backup are
scheduled, and the heating dashboard exists but has no rooms yet. Pairing the devices and describing
the rooms is the next guide, [pairing.md](pairing.md).

## What you need

| | |
|---|---|
| The server | A mini PC (Intel N100 or N150, 8 GB or more, SSD) or a Raspberry Pi 4 or 5 with 4 GB or more, on an Ethernet cable. About 2 GB of memory is used. |
| A second computer | With a checkout of this repository, for the one file you copy to the server, and with access to the repository on GitHub, which is private. |
| The FRITZ!Box | Its login, to give the server a fixed address and, if you use FRITZ! thermostats or presence, to create a user. |
| A Zigbee coordinator | The SLZB-06MG24 or another. It is not needed to install: without it the system starts without Zigbee2MQTT. |

## 1. Put the operating system on the machine

The server runs Debian 13, which on a Raspberry Pi is Raspberry Pi OS Lite 64-bit. No desktop.

**Mini PC.** Install Debian 13 from the installer image on a USB stick. In the step "Software
selection", untick the desktop environment and the print server, and keep "SSH server" and
"standard system utilities". Create your own user; the system uses a separate service user that a
later step creates.

**Raspberry Pi.** Write Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its options set a
host name, your user name and password, and switch on SSH. Write it to an SSD if you can: an SD card
works for a trial but wears out under the database writes (see
[the server section of hardware.md](hardware.md#the-server)). If you stay on a card, use a high-endurance one. The first start takes several minutes.

Then log in over SSH and bring the system up to date:

```sh
sudo apt-get update
sudo apt-get full-upgrade
uname -m                 # x86_64 on a mini PC, aarch64 on a Raspberry Pi
python3 --version        # 3.11 or newer; Debian 13 has it
```

## 2. Give the server a fixed address

Everything reaches the server by its address: your browser, the phone app, and later the
VPN. In the FRITZ!Box open **Home Network**, **Network**, open the server's entry and
switch on "Always assign this network device the same IPv4 address". If you intend to change the
FRITZ!Box's address range, do it now, before the install: [phone-and-remote.md](phone-and-remote.md#41-preparation)
explains why. Give the coordinator a fixed address too, once you have it.

## 3. Provision the host

The repository is not on the server yet, so copy one script there from your own checkout, and run
it as root. It installs what the system needs and creates the service user and directories. Look at
what it would do first:

```sh
scp scripts/provision-host.sh <your user>@<server>:
ssh <your user>@<server>
sudo ./provision-host.sh --dry-run   # prints what would be done, changes nothing
sudo ./provision-host.sh
```

It installs Docker Engine and the Compose plugin from Docker's package repository (checking its
signing key against the fingerprint Docker publishes, `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`),
Git, an SSH client and Python. It creates the service user `cave` (`--user NAME` chooses another;
its home is `/var/lib/cave-home`) and the directories `/opt/cave` (the checkout), `/var/lib/cave`
(the state, where everything the system keeps lives) and `/var/backups/cave`. It stops on a system
other than Debian 13 or Raspberry Pi OS 64-bit.

At its end it says that the nightly update and backup are not installed yet. That is expected: the
units for them come with the repository, which you clone next, and **you run the script a second
time after that**. Run again, it only does what is still missing. The commands below use the user
name `cave`; use yours if you gave `--user`.

## 4. Get the code, and install the timers

The server needs a read-only deploy key for the private repository. Create one for the service
user, add its public part on GitHub (the repository's **Settings**, **Deploy keys**, **Add deploy
key**, without write access), and clone into the empty `/opt/cave`:

```sh
sudo -u cave -H install -d -m 700 /var/lib/cave-home/.ssh
sudo -u cave -H ssh-keygen -t ed25519 -N '' -f /var/lib/cave-home/.ssh/id_ed25519
sudo cat /var/lib/cave-home/.ssh/id_ed25519.pub     # paste this into GitHub
sudo -u cave -H ssh -T git@github.com               # answer yes once the fingerprint matches
                                                    # the one on docs.github.com
sudo -u cave -H git clone git@github.com:oxydonth/cave_system.git /opt/cave
cd /opt/cave
```

Now the second run of the script, this time from the checkout. It finds the units in
`scripts/systemd/` and installs and starts the timers: the backup at 02:30 and the update at 03:30,
and the recovery of an interrupted update at every boot. [operations.md](operations.md) explains
what they do and what to do when one reports a failure.

```sh
sudo /opt/cave/scripts/provision-host.sh
systemctl list-timers 'cave-*'       # cave-backup.timer and cave-update.timer
```

The nightly update starts to install releases only after step 8: it needs the script token that
the first-run setup creates, and says so in the journal until then.

## 5. Create the configuration

```sh
cd /opt/cave
sudo -u cave ./cave init
```

`./cave init` needs only Python 3.11 or newer. It creates:

* the state directory (`/var/lib/cave`; `./cave init --data DIR` chooses another for a new install)
  with Home Assistant's `/config`, Zigbee2MQTT's and Mosquitto's data;
* `.env` (mode 0600) with every variable, generating the passwords and tokens;
* the broker's password file, kept in step with `.env`;
* an empty `tools/` directory, which Home Assistant mounts (Docker would otherwise create it as
  root, and a later update of the checkout would fail);
* a starter `cave/heating.yaml` with no rooms, and its applied copy.

It changes nothing that already exists, so it is safe to run again; a variable missing from `.env`
is appended.

## 6. Fill in `.env`

Edit it as the service user, who owns it: `sudo -u cave editor /opt/cave/.env`. Most of it you
leave alone.

| Variable | What to do |
|---|---|
| `CAVE_DATA`, `TZ` | Leave. `TZ` is `Europe/Berlin`. |
| `CAVE_MQTT_USER`, `CAVE_MQTT_PASSWORD`, `CAVE_Z2M_FRONTEND_TOKEN` | Leave; generated. |
| `CAVE_HA_OWNER_NAME`, `CAVE_HA_OWNER_USER`, `CAVE_HA_OWNER_PASSWORD` | Change the name and user name if you like; the password is generated. This is the account you log in with. |
| `CAVE_FRITZ_HOST`, `CAVE_FRITZ_USER`, `CAVE_FRITZ_PASSWORD` | Set them if you use FRITZ! thermostats or want presence from the FRITZ!Box: the box's address (`fritz.box`), and a user you created for Home Assistant. Do [part 3.1 of the phone guide](phone-and-remote.md#31-prepare-the-fritzbox) first, because the setup in step 8 needs it. Leave them empty otherwise; both integrations are then left out. |
| `CAVE_Z2M_SERIAL_PORT`, `CAVE_Z2M_ADAPTER` | The coordinator, once you have one: `tcp://<address>:6638` for a network coordinator, and the driver `ember` (the default, for the SLZB-06MG24) or `zstack`. The coordinator itself has to be set up first, following its maker's manual, to serve Zigbee over Ethernet on that port; that is not verified here. |
| `COMPOSE_PROFILES` | `zigbee` once the coordinator is connected and its address is in; empty before. |
| `CAVE_AUTO_UPDATE` | `1`: the nightly timer installs new releases. `0`: manual only. |
| `CAVE_BACKUP_DIR`, `CAVE_BACKUP_KEEP`, `CAVE_BACKUP_COPY_TO` | Where backups go, how many are kept (14), and an optional second place. Set the last one: a backup on the same disk does not survive the disk. [operations.md](operations.md#backups) says how. |

`.env.example` lists every variable with its documentation.

A **USB** coordinator also has to be passed into the Zigbee2MQTT container. Create
`compose.override.yaml` next to `compose.yaml` (Compose merges it automatically; it is not tracked):

```yaml
services:
  zigbee2mqtt:
    devices:
      - /dev/serial/by-id/usb-EXAMPLE-if00:/dev/serial/by-id/usb-EXAMPLE-if00
```

## 7. Check and start

```sh
cd /opt/cave
sudo -u cave docker compose pull    # fetches the images; about 2 GB the first time
sudo -u cave ./cave check           # validates compose.yaml and the Home Assistant configuration
sudo -u cave ./cave up              # creates the containers and waits until they are healthy
sudo -u cave ./cave ps
```

`./cave up` recreates the containers every time, because the code is mounted from the checkout and
a changed checkout takes effect only that way. "Healthy" means every container of the active profiles
runs and passes its Docker health check; Home Assistant's check also needs its log to say that it
finished starting, so `./cave up` does not report healthy while Home Assistant is still starting.
The first start of a Raspberry Pi takes several minutes. Home Assistant is then at
`http://<server>:8123`.

Without a coordinator the stack starts without Zigbee2MQTT. Once you have one, set
`CAVE_Z2M_SERIAL_PORT`, `CAVE_Z2M_ADAPTER` and `COMPOSE_PROFILES=zigbee` in `.env`, run
`./cave up` again, and open Zigbee2MQTT's web page at `http://<server>:8080`: it asks for
`CAVE_Z2M_FRONTEND_TOKEN`.

## 8. First-run setup

Until the owner account exists, anyone on the network who opens `http://<server>:8123` can
complete Home Assistant's onboarding and become its owner. So run this right after the first
`./cave up`, and do not open the page in a browser before:

```sh
sudo -u cave ./cave bootstrap
```

It waits until the stack is healthy (`--timeout SECONDS`, default 300), then sets Home Assistant up
without a browser:

* the owner account from `CAVE_HA_OWNER_NAME`, `CAVE_HA_OWNER_USER` and `CAVE_HA_OWNER_PASSWORD`,
  and the rest of onboarding, so the web UI opens with a normal login;
* Home Assistant's time zone (from `TZ`), metric units, country Germany and currency euro, once:
  later changes in the UI are kept, and a time zone that differs from `TZ` is pointed out;
* the MQTT integration, connected to the broker with `CAVE_MQTT_USER` and `CAVE_MQTT_PASSWORD`;
* FRITZ!SmartHome and FRITZ!Box Tools, if `CAVE_FRITZ_HOST`, `CAVE_FRITZ_USER` and
  `CAVE_FRITZ_PASSWORD` are set (the host as `fritz.box`, an address, or a URL);
* a schedule helper for every room in the applied `cave/heating.yaml` that names one, with a default
  week (Monday to Friday 06:00-08:00 and 17:00-22:00, Saturday and Sunday 08:00-22:00; on means
  comfort), which you edit in the UI under Settings, Devices & services, Helpers. It never touches a
  schedule that exists. There are no rooms yet, so none is made now: after you describe rooms in
  [pairing.md](pairing.md), run `./cave apply`, then `./cave bootstrap` again;
* the script token, a long-lived access token named `cave-scripts` of the owner account, in
  `CAVE_DATA/homeassistant/.cave/token` (mode 0600), which `apply`, `status`, `update` and the
  health check use;
* and it removes the integrations Home Assistant's onboarding adds by itself (Radio Browser, Google
  Translate text-to-speech, Shopping list): this system keeps nothing that reaches the internet
  when used. Only the entries onboarding created; the same integration added by you later stays.

Run it again whenever you like: it changes only what is missing, and an interrupted run is finished
by the next. It repairs a deleted or revoked script token and changes the MQTT login to the one in
`.env` if that changed. The owner's password in `.env` is used to create the account and, later,
only to log in when the script token has to be renewed. If you change the password in Home
Assistant, put the same into `.env`, or a token renewal stops with a message saying so.

**If you already created the owner account in the browser**, set `CAVE_HA_OWNER_NAME`,
`CAVE_HA_OWNER_USER` and `CAVE_HA_OWNER_PASSWORD` in `.env` to that account's name, user name and
password first: the bootstrap logs in with them, and stops with a message naming the user name
when Home Assistant refuses the login. It then also removes the Radio Browser, Google Translate and
Shopping list entries your onboarding created.

## 9. Log in and look around

Open `http://<server>:8123` and log in with `CAVE_HA_OWNER_USER` and `CAVE_HA_OWNER_PASSWORD`:

```sh
sudo -u cave grep '^CAVE_HA_OWNER' /opt/cave/.env
```

Save the password in your password manager. The sidebar has **Heizung**, the heating dashboard at
`/cave-heating`: two views, Rooms and Maintenance, both empty until you describe rooms. Run
`sudo -u cave ./cave status` to see the state in one place: the installed release, whether the
stack is healthy, the last update and backup, and whether `heating.yaml` has changes that were not
applied.

## What to do next

| | Guide |
|---|---|
| Pair the thermostats and sensors, describe the rooms, apply them | [pairing.md](pairing.md) |
| The phone app, notifications, presence, access from outside | [phone-and-remote.md](phone-and-remote.md) |
| Check what only real hardware can show | [hardware-acceptance.md](hardware-acceptance.md) |
| Updates, backups, restoring, what to do when one fails | [operations.md](operations.md) |
| The dashboard and the notifications | [dashboard-and-notifications.md](dashboard-and-notifications.md) |

## The other commands

| Command | Does |
|---|---|
| `./cave down` | Stops and removes the containers; the state is kept |
| `./cave restart [service...]` | Restarts services and waits until healthy |
| `./cave ps` | Shows the containers and their health |
| `./cave logs [-f] [--tail N] [service...]` | Shows service logs |
| `./cave version` | Prints the release that is checked out and the image versions |
| `./cave help [command]` | Lists the commands |

## Things to know

* The broker listens on `127.0.0.1:1883` and on the Compose network only; its password file is
  `CAVE_DATA/mosquitto/passwd`. To change the password, edit `CAVE_MQTT_PASSWORD` in `.env` (or
  empty it, to have a new one generated), run `sudo chown -R cave: /var/lib/cave/mosquitto` (the
  broker's container takes ownership of its data directory), then `./cave init`, which writes the
  password file again, `./cave up`, and `./cave bootstrap`, which gives Home Assistant's MQTT
  integration the new login. Change the broker login only this way: once the broker has started,
  `init` cannot read the password file and relies on a record of what it wrote, so a file changed by
  hand would go unnoticed.
* Zigbee2MQTT generates this home's network key the first time it starts and keeps it in
  `CAVE_DATA/zigbee2mqtt/configuration.yaml`. The nightly backup contains it. Without the key every
  Zigbee device has to be paired again.
* Files that containers create in the state directory belong to root.
* `./cave init` never overwrites `cave/heating.yaml`, and `./cave apply` is the only way a change
  to it takes effect.
