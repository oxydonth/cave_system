# Installing the stack

First version: it brings a server to a running, healthy Home Assistant with its broker. Pairing
devices, describing rooms and the phone apps follow in the complete guide (work package 9).

## What you need

A Debian 13 server (a mini PC) or a Raspberry Pi with Raspberry Pi OS Lite 64-bit, booting from an
SSD, with a network connection and about 2 GB of free memory. See concept section 3.2.

## 1. Provision the host

On the server, as root, from a checkout of this repository (or after copying
`scripts/provision-host.sh` there):

```sh
sudo scripts/provision-host.sh --dry-run   # prints what would be done and changes nothing
sudo scripts/provision-host.sh
```

It installs Docker Engine and the Compose plugin from Docker's package repository, Git and Python,
creates the service user `cave` (`--user NAME` chooses another; its home is `/var/lib/cave-home`,
outside the checkout), and the directories `/opt/cave` (the checkout), `/var/lib/cave` (the state)
and `/var/backups/cave`. It checks Docker's signing key against the fingerprint Docker publishes
(`9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`). Run again, it only does what is still missing.

## 2. Get the code and initialise

The repository is private, so the server needs a read-only deploy key. Create one for the service
user, add its public part under the repository's Settings, Deploy keys (without write access), and
clone over SSH into the empty `/opt/cave`:

```sh
sudo -u cave -H ssh-keygen -t ed25519 -N '' -f /var/lib/cave-home/.ssh/id_ed25519
sudo cat /var/lib/cave-home/.ssh/id_ed25519.pub        # paste this into GitHub
sudo -u cave -H git clone git@github.com:<owner>/cave_system.git /opt/cave
cd /opt/cave
sudo -u cave ./cave init
```

`./cave init` needs only Python 3.11 or newer (Debian 13 has it) and creates:

* the state directory (`/var/lib/cave` on a provisioned server, `./data` in a development checkout;
  `./cave init --data DIR` chooses another for a new install) with Home Assistant's `/config`,
  Zigbee2MQTT's and Mosquitto's data;
* `.env` (mode 0600) with every variable, generating the passwords and tokens;
* the broker's password file, kept in step with `.env`: if `.env` was lost or its password changed,
  `init` writes the file again and says so;
* an empty `tools/` directory, which Home Assistant mounts (if it were missing, Docker would create
  it as root and a later update of the checkout would fail);
* a starter `cave/heating.yaml` with no rooms, and its applied copy.

It changes nothing that already exists, so it is safe to run again; a variable missing from `.env`
is appended.

## 3. Review `.env`

| Variable | Meaning |
|---|---|
| `CAVE_DATA` | State directory |
| `TZ` | Time zone, default `Europe/Berlin` |
| `COMPOSE_PROFILES` | Empty, or `zigbee` once a coordinator is connected |
| `CAVE_MQTT_USER`, `CAVE_MQTT_PASSWORD` | Broker login (password generated) |
| `CAVE_HA_OWNER_NAME`, `CAVE_HA_OWNER_USER`, `CAVE_HA_OWNER_PASSWORD` | Home Assistant's owner account, created by `cave bootstrap` |
| `CAVE_Z2M_SERIAL_PORT`, `CAVE_Z2M_ADAPTER` | Coordinator: `tcp://<address>:6638` or a `/dev/serial/by-id/` path, and the driver `ember` or `zstack` |
| `CAVE_Z2M_FRONTEND_TOKEN` | Token for Zigbee2MQTT's web UI (generated) |
| `CAVE_FRITZ_HOST`, `CAVE_FRITZ_USER`, `CAVE_FRITZ_PASSWORD` | Optional FRITZ!Box login |
| `CAVE_AUTO_UPDATE` | `1` nightly updates, `0` manual only |
| `CAVE_BACKUP_DIR`, `CAVE_BACKUP_KEEP`, `CAVE_BACKUP_COPY_TO` | Backup location, number of archives, optional second location |

`.env.example` lists every variable with its documentation.

A **USB** coordinator also needs its device passed into the Zigbee2MQTT container. Create
`compose.override.yaml` next to `compose.yaml` (Compose merges it automatically; it is not tracked):

```yaml
services:
  zigbee2mqtt:
    devices:
      - /dev/serial/by-id/usb-EXAMPLE-if00:/dev/serial/by-id/usb-EXAMPLE-if00
```

## 4. Check and start

```sh
sudo -u cave ./cave check   # validates compose.yaml and the Home Assistant configuration in the pinned image
sudo -u cave ./cave up      # creates the containers and waits until they are healthy
sudo -u cave ./cave ps
```

`./cave up` recreates the containers every time, because the code is mounted from the checkout and
a changed checkout takes effect only that way. "Healthy" means every container of the active
profiles runs and passes its Docker health check. Home Assistant's check needs its frontend to answer and its log to say that it finished starting,
so `./cave up` does not report healthy while Home Assistant is still starting. Once `cave bootstrap`
has created the script token, `cave` also asks Home Assistant itself: it must report that it is
running, not in recovery or safe mode (a broken configuration), and have set up `cave_heating`.
Without the token a configuration that sends Home Assistant into recovery mode cannot be told
apart from a normal start. Home Assistant is then at `http://<server>:8123`. The first start of a
Raspberry Pi takes several minutes.

Without a coordinator the stack starts without Zigbee2MQTT. Once one is connected, set
`CAVE_Z2M_SERIAL_PORT`, `CAVE_Z2M_ADAPTER` and `COMPOSE_PROFILES=zigbee` in `.env` and run
`./cave up` again; Zigbee2MQTT's web UI is on port 8080, protected by `CAVE_Z2M_FRONTEND_TOKEN`.

Until `./cave bootstrap` has created the owner account, anyone on the network who opens
`http://<server>:8123` can complete Home Assistant's onboarding and become its owner. Run
`./cave bootstrap` straight after the first `./cave up`.

## Other commands

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
  password file again, and `./cave up`. If `init` finds the two out of step it fixes them or stops
  and prints the command to run.
* Zigbee2MQTT generates this home's network key the first time it starts and keeps it in
  `CAVE_DATA/zigbee2mqtt/configuration.yaml`. Back that file up: without the key every Zigbee device
  has to be paired again.
* Files that containers create in the state directory belong to root.
* `./cave init` never overwrites `cave/heating.yaml`, and `./cave apply` (later) is the only way
  a change to it takes effect.
