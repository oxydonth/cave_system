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
| A second computer | With a checkout of this repository, for the one file you copy to the server. The repository is public: no GitHub account is needed. |
| The FRITZ!Box | Its login, to give the server a fixed address and, if you use FRITZ! thermostats or presence, to create a user. |
| A Zigbee coordinator | The SLZB-06MG24 or another. It is not needed to install: without it the system starts without Zigbee2MQTT. |

## 1. Put the operating system on the machine

The server runs Debian 13, which on a Raspberry Pi is Raspberry Pi OS Lite 64-bit. No desktop.

**Mini PC.** Install Debian 13 from the installer image on a USB stick. In the step "Software
selection", untick the desktop environment and the print server, and keep "SSH server" and
"standard system utilities". **Leave the root password empty** when the installer asks for it: that
disables the root account and makes the user you create next an administrator who can use `sudo`,
which every command of this guide does. If you set a root password, the installer does not install
`sudo` and your user cannot use it. To fix that afterwards, log in as that user and run `su -`, then
`apt-get install sudo` and `usermod -aG sudo <your user>`, `exit`, and log in again. The system uses
a separate service user, which a later step creates.

**Raspberry Pi.** Write Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its options set a
host name, your user name and password (that user can use `sudo`), and switch on SSH. Write it to an SSD if you can: an SD card
works for a trial but wears out under the database writes (see
[the server section of hardware.md](hardware.md#the-server)). If you stay on a card, use a high-endurance one. The first start takes several minutes.

Connect the machine to the FRITZ!Box with an Ethernet cable. Its address is in the FRITZ!Box under
**Home Network** (Heimnetz), **Network** (Netzwerk); on the machine itself `ip -4 addr` shows it.
Then log in over SSH and bring the system up to date:

```sh
sudo apt-get update
sudo apt-get full-upgrade
uname -m                 # x86_64 on a mini PC, aarch64 on a Raspberry Pi
python3 --version        # 3.11 or newer; Debian 13 has it
```

## 2. Give the server a fixed address

Everything reaches the server by its address: your browser, the phone app, and later the
VPN. In the FRITZ!Box open **Home Network**, **Network**, the tab **Network Connections** (German
interface: **Heimnetz**, **Netzwerk**, **Netzwerkverbindungen**), click the edit button (Bearbeiten)
beside the server and tick "Always assign this network device the same IPv4 address" (German: "Diesem
Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen"). AVM's help page
[Netzwerkgerät immer die gleiche IP-Adresse von FRITZ!Box zuweisen lassen](https://ch.fritz.com/service/wissensdatenbank/dok/FRITZ-Box-4020/201_Netzwerkgerat-immer-die-gleiche-IP-Adresse-von-FRITZ-Box-zuweisen-lassen/)
has this for several models, and notes that the box gives a device the same address by default as long
as there are addresses left; the setting makes it a promise. If you intend to change the
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

At its end it lists what follows. The nightly update and backup are not installed yet, and that is
expected: their units come with the repository, which you clone next, and **you run the script a
second time after that**. Run again, it only does what is still missing. The commands below use the user
name `cave`; use yours if you gave `--user`.

## 4. Get the code, and install the timers

The repository is public, so the server needs no key and no GitHub account: clone it over HTTPS,
as the service user, into the empty `/opt/cave`. The nightly update fetches its releases the same
way.

```sh
sudo -u cave -H git clone https://github.com/oxydonth/cave_system.git /opt/cave
cd /opt/cave
```

A server cloned before 23 November 2026, while the repository was private, fetches over SSH with
its deploy key. That keeps working while the key is listed on GitHub. To do without it, switch the
checkout to HTTPS, check that a fetch works, then delete the deploy key on GitHub (the
repository's **Settings**, **Deploy keys**) and on the server:

```sh
sudo -u cave git -C /opt/cave remote set-url origin https://github.com/oxydonth/cave_system.git
sudo -u cave -H git -C /opt/cave fetch --tags
sudo rm /var/lib/cave-home/.ssh/id_ed25519 /var/lib/cave-home/.ssh/id_ed25519.pub
```

Now the second run of the script, this time from the checkout. It finds the units in
`scripts/systemd/` and installs and starts the timers: the backup at 02:30 and the update at 03:30
(each with a few minutes of random delay), the recovery of an interrupted update at every
boot, and the watch every five minutes, which alerts you when the stack is down once you set
`CAVE_ALERT_URL` (step 6) and does nothing until then. The watch runs as the service user (the one
given with `--user`), the others as root. The units do nothing until `.env` exists, which [creating the configuration](#5-create-the-configuration) does. [operations.md](operations.md) explains
what they do and what to do when one reports a failure.

```sh
sudo /opt/cave/scripts/provision-host.sh
systemctl list-timers 'cave-*'       # cave-backup.timer, cave-update.timer and cave-watch.timer
```

A server installed before the watch existed gets it the same way: run the script again from the
checkout after an update; it installs only what is missing.

The nightly update starts to install releases only after [the first-run setup](#8-first-run-setup): it needs the script token that
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
| `CAVE_ALERT_URL` | Optional, empty by default: then nothing leaves the server. Set it to a topic of an [ntfy](https://ntfy.sh) server, `http://<ntfy server>/<topic>`, and the server posts a short alert there when the stack has been down for 15 minutes (and when it is back), or an update, backup or restore needs you, also while Home Assistant is down. An ntfy server on the home network keeps everything local; a public one (ntfy.sh) receives the server's name and the stack's state, and anyone who knows the topic can read them. A token goes into the URL, `http://:<token>@<ntfy server>/<topic>`; write the value in single quotes if it contains `#` or `$`. [operations.md](operations.md#alerts-without-home-assistant) says what is sent and when. |

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

It takes the one lock that `apply`, `update`, `backup` and `restore` also take. If it says that another
command holds the lock (the nightly backup or update is running), wait for it and run it again; if it
says "not bootstrapping" because an update is interrupted or a restore stopped half-way, that is
something [operations.md](operations.md#a-notification-arrived-what-now) describes. Nothing was changed
either way. On a fresh install neither happens.

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
`/cave-heating`. At this point it is only a placeholder: one view with the text "No rooms yet.
Describe them in `cave/heating.yaml` and run `./cave apply`." That is right. The real dashboard, with
a Rooms and a Maintenance view, appears when [pairing.md](pairing.md#apply-it) has you run
`./cave apply` for the first rooms. Run
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
