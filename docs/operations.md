# Operating the server: updates, backups, and what to do when one fails

The server updates itself at night and makes a backup before that. Both report every result
as a notification in Home Assistant (and on the phone, once the notify service is set). This page
is what to read when such a notification says something went wrong.

**First, always:**

```sh
cd /opt/cave
sudo -u cave ./cave status
```

It shows the installed release, whether the stack is healthy, the last update's result, failed
releases, the last backup, and whether `heating.yaml` has changes that were not applied.

## A notification arrived: what now?

Find the start of the message in the left column.

| The message starts with | What happened | The system now | What to do |
|---|---|---|---|
| `Installed v…` | The new release runs and is healthy. | Fine. | Nothing. |
| `Backup cave-backup-… written` | The nightly backup worked. | Fine. | Nothing. |
| `Up to date` (journal only, no notification) | There was no newer release. | Fine. | Nothing. |
| `v… failed on … and is skipped` (journal only, no notification) | The newest release failed before and is not tried again by itself. | Fine. | Nothing, or `sudo ./cave update --force` to try it again. |
| `Not updating to v… automatically: there is no script token yet` (journal only: without the token the notification cannot reach Home Assistant either) | Nightly updates start only once `./cave bootstrap` has created the script token; see [Without the script token](#without-the-script-token). | Unchanged. | Run `./cave bootstrap`. |
| `Not updating to v…: cannot read the script token` / `… is not a single line of printable characters` | The script token file is unreadable or damaged. | Unchanged. | Remove `/var/lib/cave/homeassistant/.cave/token` and run `./cave bootstrap` again. |
| `Not updating to v…: the running stack is not healthy` | The update found the running system unhealthy before it touched anything, and left it alone. | As before the update, still unhealthy. | See [The running stack is not healthy](#the-running-stack-is-not-healthy). |
| `Not updating to v…: the checkout … has local changes` | Someone edited files in `/opt/cave`. | Unchanged. | See [Local changes](#local-changes). |
| `Not updating to v…: the snapshot needs about … MiB` | Not enough disk space for the safety copy, measured after the new images were pulled. | Unchanged. | Free space on the disk that holds `/var/lib/cave`: old files in `/var/backups/cave`, `/var/lib/cave/.cave/ops/before-restore-*` left by a restore, unused images (`sudo docker image prune -a` while the stack runs). The next night tries again. |
| `Not updating to v…: pulling its images failed`, `Could not fetch releases` or `Could not list the releases` | No internet, GitHub or the image registry unreachable, or Git could not read the checkout. | Unchanged. | Nothing; the next night tries again. If it repeats for days, check the internet connection and the deploy key (`sudo -u cave -H git -C /opt/cave fetch`). |
| `Not updating to v…: exporting it failed` | The release could not be unpacked into `/var/lib/cave/.cave/ops/tmp` (disk full, or a damaged repository). | Unchanged. | Free space; if it repeats, `sudo -u cave git -C /opt/cave fsck`. |
| `Not updating to v…: its Compose file does not accept this server's .env and compose.override.yaml` | The new release needs a variable that `.env` lacks, or the local `compose.override.yaml` does not fit it. | Unchanged. Not remembered as failed. | `sudo -u cave ./cave init` (it adds missing variables), check `compose.override.yaml`, then `sudo ./cave update`. |
| `Not updating to v…: the check_config could not run` / `the validator could not run` | The check in the new image did not run at all: Docker failed, it timed out, or the validator is not in the release (see [Before the validator exists](#before-the-validator-exists)). This says nothing about your configuration. | Unchanged. Not remembered as failed. | Nothing; the next night tries again. If it repeats, look at `journalctl -u cave-update.service`. |
| `Not updating to v…: its Home Assistant rejects the configuration` | The new release's Home Assistant does not accept the configuration. | Unchanged. The release is skipped from now on. | Nothing urgent. A newer release will be tried automatically. Report the message. |
| `Not updating to v…: it does not accept the applied heating.yaml` | The new release's validator ran and found an error in your home description. | Unchanged. The release is skipped from now on. | Fix the named room and field in `/var/lib/cave/homeassistant/cave/heating.yaml`, run `sudo -u cave ./cave apply`, then `sudo ./cave update --force`. |
| `Not updating to v…: … heating.yaml does not exist` | The applied copy is missing. | Unchanged. | `sudo -u cave ./cave apply`. |
| `Update to v… stopped before the switch` … `is running again and healthy` | The stack was stopped, but the snapshot could not be taken (or the stack could not be stopped). Nothing was changed. | The previous release runs, healthy. | Read the reason (often a full disk). The next night tries again. |
| `Update to v… failed: …` … `Rolled back` | The new release was installed but did not become healthy, or could not be started. Everything was put back. | The previous release runs, healthy, with the state from just before the update. The failed release is skipped from now on. | Nothing urgent. A newer release is tried automatically; `sudo ./cave update --force` tries this one again. Report the message. |
| `… The previous release v… was started and left running, but it is NOT healthy` | The new release failed, and after putting everything back the previous release does not become healthy either. | The previous release runs, unhealthy. | See [The rollback is not healthy](#the-rollback-is-not-healthy). |
| `… NOTHING IS RUNNING` | Not even the previous release could be started. Home Assistant does not control the heating. | Stack stopped. Thermostats keep their last setpoint and their frost protection. | Act now: see [Nothing is running](#nothing-is-running). |
| `… running and healthy, but the way back was not clean` | Rolled back, but a step of the way back failed (named in the message). | The previous release runs, healthy, but see the message. | See [The way back was not clean](#the-way-back-was-not-clean). |
| `… The way back is tried again at the next start of the server …` | The way back could not finish (the snapshot is not back, or nothing runs). The update stays recorded as in flight. | As the message says; no backups until it is finished. | It repeats by itself at the next boot, after the next nightly run, or now with `sudo ./cave update --recover` once the cause (often Docker, or a full disk) is fixed. |
| `An update from … was interrupted` / `it was interrupted (killed or a power cut)` | The server lost power or the update was killed during an update. The next run (at boot) finished it. | As the rest of the message says. | See [Interrupted updates](#interrupted-updates). |
| `Not updated: … is running` / `No backup was made: … is running` | Update, backup and restore never run at the same time; one was still busy. | Unchanged. | Nothing; the next night runs again. |
| `The backup failed: …` | No archive was written; the older ones are kept. | Fine, but without a fresh backup. | Read the reason (often a full disk). Run `sudo ./cave backup` once fixed. |
| `Backup … written …; WARNING: … could not be read as a SQLite database` | The backup was made, but one database file is damaged (Home Assistant renames a damaged database to `home-assistant_v2.db.corrupt.<time>`). It is archived as it is. | Backup fine. | If the named file is a `….corrupt.*` file, Home Assistant has replaced it already: delete it to save space. Otherwise look at `journalctl -t cave`. |
| `The restore failed while unpacking: …` | A restore could not unpack the archive; nothing was restored. | The stack runs again unchanged (or the message says it could not be started). | Read the reason (often a full disk). |
| `… The copy to CAVE_BACKUP_COPY_TO=… FAILED` | The backup was written locally, but the second location was not reachable (not mounted?). | Local backup fine. | Mount the second location, or fix `CAVE_BACKUP_COPY_TO` in `.env`. |
| `No backup was made: the update to v… was interrupted` | An update was interrupted; the state may be incomplete, so it is not archived. | See [Interrupted updates](#interrupted-updates). | `sudo ./cave update --recover`. |

When Home Assistant itself is down, a notification cannot reach it. Every message is also written
to the system journal: `journalctl -t cave` lists them, `journalctl -u cave-update.service` and
`journalctl -u cave-backup.service` show the full output of the nightly runs.

## How an update works

The timer `cave-update.timer` starts `cave update --auto` at 03:30 (plus up to 15 minutes); the
backup runs at 02:30 before it. `cave update`, holding the one lock that update, backup and restore
share (concept section 4.5):

1. Fetches the release tags from GitHub, so that the server's tags are exactly GitHub's: a tag
   moved there is moved here, a deleted (withdrawn) one is deleted here. It takes the newest
   release (`vYYYY.MM.DD.N`) **whose commit is on `main`**: only the pipeline creates release
   tags, and only for `main`, so a matching tag anywhere else (pushed by hand, on a branch) is
   ignored. Nothing happens if that release is not newer than the installed one, or if it failed
   before (see `--force`).
2. **Before anything is touched:** the running stack must be healthy; the checkout must have no
   local changes; the script token must exist (for the nightly run, see below). The new release
   is exported to a temporary directory (`/var/lib/cave/.cave/ops/tmp`); its Compose file must
   accept this server's `.env` and `compose.override.yaml`; its container images are pulled;
   then there must be room on the disk for the snapshot (measured after the pull, which usually
   fills the same disk); then the new release's Home Assistant checks the configuration and the
   new release's validator checks the applied `heating.yaml`, both in the new image. If anything
   fails here, the system has not been stopped for a second.
3. **The switch:** stop the stack, copy the state (`/var/lib/cave`) into a snapshot while nothing
   writes to it, check out the release, start it, and wait up to 15 minutes until it is healthy.
4. **If the switch fails:** stop (a second try after 10 seconds), put back the snapshot and the
   previous release, start that and wait until it is healthy. The snapshot is put back by
   renaming its files into place, which needs no free disk space and uses the snapshot up. If
   even the previous release is not healthy, it is left running and the message says so. In
   every case the last thing the update does is start the stack; Ctrl-C and `systemctl stop` are
   ignored during the way back, because interrupting it would leave the stack stopped. If the
   way back could not finish (the snapshot is not back, or nothing runs), the update stays
   recorded as in flight and the way back is tried again by the next run.

A release whose Home Assistant rejected the configuration or whose validator rejected
`heating.yaml` (exit status 1 from a check that ran), or that failed in step 3, is remembered and
skipped until a newer release appears or `sudo ./cave update --force` is run. A failed download,
and a check that could not run at all (Docker failed, it timed out, the validator is missing), is
not remembered: the next night tries again.

At most two snapshots are kept in `/var/lib/cave/.cave/ops/snapshots/`, each named after the
releases it was taken between: after a successful update its snapshot stays, after a rollback it
has been used up. The update record (`/var/lib/cave/.cave/ops/update.json`) holds the last
result, the failed releases and, while an update runs, what it is doing.

### Before the validator exists

The validator of `heating.yaml` comes with the heating integration (work package 4). Until a
release contains it, the validator check of every release "could not run": the update stops
before touching anything, sends `Not updating to v…: the validator could not run (the validator
is not in this release …)` every night, and does not remember the release as failed. Nothing is
ever stopped because of it.

### Without the script token

"Healthy" (concept section 4.5) can only see whether Home Assistant is in recovery mode by asking
its API, which needs the script token that `./cave bootstrap` creates. Without it, a release whose
configuration sends Home Assistant into recovery mode would look healthy and would not be rolled
back. So nightly updates start only after `./cave bootstrap`: until then the **nightly** update
does nothing and says so in the journal (a notification cannot reach Home Assistant without the
token either). A manual `sudo ./cave update` still works, with a warning, for someone who watches
the result.

## The commands

| Command | Does |
|---|---|
| `sudo -u cave ./cave status` | Shows what is installed, health, the last update, failed releases, the last backup, unapplied `heating.yaml` changes. Changes nothing. Exit status 1 if unhealthy or an update was interrupted. |
| `sudo ./cave update` | Installs the newest release now, as described above. |
| `sudo ./cave update --force` | The same, also for a release that failed before. |
| `sudo ./cave update --recover` | Only finishes an interrupted update, or a way back that could not finish. Runs at every boot and after every nightly update; waits for a backup that holds the lock. |
| `sudo ./cave update --timeout SECONDS` | How long a started release may take to become healthy (default 900). |
| `sudo ./cave backup` | Writes a backup now. |
| `sudo ./cave restore ARCHIVE [--data DIR] [--no-start] [--force]` | Puts back a backup; see [Restoring](#restoring-a-backup). |

`update` exits with 0 (installed, or nothing to do), 1 (not updated, the previous release runs as
before), 2 (the release failed and was rolled back; the previous release runs, healthy) or 3 (the
previous release is not back healthy, or nothing runs: act now).

### Which user

`update`, `backup` and `restore` run as **root**; the systemd units do. The state directory holds
files that the containers created, owned by root or by the broker's user and readable only by them
(Home Assistant's `.storage`, the broker's data). Only root can copy all of them and give a copy
back its owner. Run as the service user, these commands stop at once with
`` `cave update` must run as root ``, before touching anything; run them with `sudo`. (In a
development checkout, where every file belongs to you, they work without root.)

Git, however, always runs as the owner of `/opt/cave` (the service user `cave`), also when the
update runs as root: the fetch then uses the service user's deploy key in
`/var/lib/cave-home/.ssh`, and no file in the checkout becomes root's (run as root, `cave` also
writes no Python bytecode there). `status` needs no root.

**What this means for security.** Root runs `/opt/cave/cave`, which the service user can change,
and works in directories the service user owns (`/var/backups/cave`, the export directory). So
the service user can become root. That adds nothing today, because the service user is in the
`docker` group, which is equivalent to root anyway. Do not remove it from the `docker` group in
the belief that this "hardens" the server: the hole described here would then be the open one.
Protect the service user's deploy key and login as you would root's.

### Turning automatic updates off

Set `CAVE_AUTO_UPDATE=0` in `/opt/cave/.env` (`sudo -u cave editor /opt/cave/.env`). The nightly
run then does nothing; `sudo ./cave update` still updates by hand. The recovery of an interrupted
update always runs. To stop the timers entirely: `sudo systemctl disable --now cave-update.timer`
(or `cave-backup.timer`).

## What to do

### The running stack is not healthy

The update did nothing, because a rollback could only return to this unhealthy state. Find out
why: `sudo -u cave ./cave status`, `sudo -u cave ./cave ps`, `sudo -u cave ./cave logs --tail 100
homeassistant`. Often `sudo -u cave ./cave up` fixes it. Once it is healthy, the next night
updates as usual.

### Local changes

`/opt/cave` must stay exactly as released, because an update replaces it. See what was changed
with `sudo -u cave git -C /opt/cave status`, and undo it with
`sudo -u cave git -C /opt/cave checkout -- .` (local settings belong in `.env` or
`compose.override.yaml`, which Git ignores).

### The rollback is not healthy

The previous release runs again with the state from just before the update, and is still not
healthy. Look at the logs: `sudo -u cave ./cave logs --tail 200 homeassistant`. A restart may be
enough: `sudo -u cave ./cave up`. If it was healthy before the update and is not now, put back the
last backup (it was taken an hour before the update): see [Restoring](#restoring-a-backup).

### Nothing is running

Home Assistant is not controlling the heating. The thermostats keep their last setpoint and
their own frost protection, and can be set by hand on the device. If the message also says that
the snapshot was NOT put back, the state is the one the failed release left behind (its database
may be migrated); the way back is then tried again by itself, see the row "The way back is tried
again" above. Then:

1. `sudo -u cave ./cave up` and watch what it says. `sudo systemctl status docker` if Docker
   itself is the problem; a reboot often helps (`sudo reboot`; the recovery runs at boot).
2. `sudo -u cave ./cave status` and `sudo -u cave ./cave logs --tail 200`.
3. If the state is damaged, put back the last backup: [Restoring](#restoring-a-backup).

### The way back was not clean

The message names what failed:

* **"the containers could not be stopped … the snapshot was NOT put back"**: the previous release
  runs on the state that the failed release left behind (Home Assistant may have migrated its
  database). The way back is tried again by itself (next boot, next night, or
  `sudo ./cave update --recover`).
* **"the snapshot … could not be put back"**: the same. Putting it back only renames files, so
  this is rare; the message names the error.
* **"the previous release … could not be checked out"**: whatever is checked out was started.
  `sudo -u cave git -C /opt/cave status` shows what. If Git complains about `index.lock`, remove
  `/opt/cave/.git/index.lock` (no other Git may be running); then
  `sudo -u cave git -C /opt/cave checkout --force --detach <previous release>` and
  `sudo -u cave ./cave up` finish the job.

**Putting a snapshot back by hand**, only if the automatic way back keeps failing. The snapshot's
`data/` is a copy of `/var/lib/cave` from just before the update. Move, do not copy: it is on the
same disk, so moving needs no free space, which is often what is missing.

```sh
cd /opt/cave
sudo systemctl stop cave-update.timer
sudo -u cave ./cave down
S=/var/lib/cave/.cave/ops/snapshots/<name>/data      # sudo ls /var/lib/cave/.cave/ops/snapshots
B=/var/lib/cave/.cave/ops/broken                     # same disk as the state
sudo mkdir -p "$B/.cave"
for e in homeassistant zigbee2mqtt mosquitto .cave/mqtt-credentials; do   # those that exist in $S
  sudo mv "/var/lib/cave/$e" "$B/$e"; sudo mv "$S/$e" "/var/lib/cave/$e"
done
sudo -u cave git checkout --force --detach <the commit or release named before "-to-" in the snapshot name>
sudo rm /var/lib/cave/.cave/ops/update.json          # forget the update in flight
sudo -u cave ./cave up
sudo systemctl start cave-update.timer               # delete $B once everything works
```

### Interrupted updates

If the server loses power, or the update is killed, the update record says how far it got. The
next run finishes the job: `cave-recover.service` at boot, the update service itself when it
stops for any reason, or any later `cave update`. `sudo ./cave update --recover` does it by hand.

| Interrupted | What the next run finds | What it does |
|---|---|---|
| Before stopping the stack | No update in flight (only a temporary export, removed) | Nothing special; a normal update. |
| While stopping, or while copying the snapshot | Stack stopped or stopping; a snapshot `….partial`; checkout and state unchanged | Removes the partial snapshot, starts the previous release, waits for healthy. The release is not marked as failed. |
| After the snapshot: during the checkout, before or after starting the new release | Stack stopped or running the new release; the checkout possibly half changed, with Git's lock file left behind; a complete snapshot | The full rollback: stop, put back the snapshot, remove Git's stale lock files, check out exactly the previous release (files that only the new release has are removed), start, wait. The release is marked as failed (an update that died during the switch is suspect); `--force` tries it again. |
| In the instant after the new release became healthy, before the result was recorded | The new release running, the record still saying "switching" | Rolls back, as above. Conservative: an update is only done once it is recorded. |
| While rolling back (also half-way through putting the snapshot back) | State possibly half replaced | Rolls back again: entries the snapshot still holds are moved into place, those already moved stay. Safe to repeat until it completes. |
| A way back that could not finish | The update still recorded as in flight | Tries the way back again. |

A backup does not run while an update is interrupted, so that a half-switched state is never
archived. A restore, on the other hand, supersedes an interrupted update: see
[Restoring](#restoring-a-backup). The lock is released by the operating system when its holder dies, so a lock file left
behind blocks nothing.

## Backups

`cave-backup.timer` runs `cave backup` at 02:30. It archives `/var/lib/cave` (without the update's
own snapshots) and `.env` into `CAVE_BACKUP_DIR` (default `/var/backups/cave`) as
`cave-backup-YYYYMMDDTHHMMSSZ.tar.gz`, mode 0600, because it holds every password. Nothing is
stopped: Home Assistant's database (and any other SQLite database in the state) is copied with
SQLite's own online backup, which gives a consistent copy while Home Assistant writes, and the
copy is checked before it goes into the archive. A database that cannot be read that way (a
damaged one) is archived as it is and named in the notification, so that it cannot stop the
backup of everything else. Other files are copied as they are at that moment.

| `.env` | Meaning |
|---|---|
| `CAVE_BACKUP_DIR` | Where the archives go. Created if missing. |
| `CAVE_BACKUP_KEEP` | How many archives are kept (default 14); the oldest by the time they were written are deleted, never the one just written. |
| `CAVE_BACKUP_COPY_TO` | Optional second directory, for example storage on the FRITZ!Box mounted at `/mnt/fritz`. Each archive is copied there, with the same number kept. It must exist and be mounted: it is never created, so that an unmounted share does not fill the local disk. |

A backup is only useful somewhere else as well: set `CAVE_BACKUP_COPY_TO`, or copy the newest
archive from time to time.

## Restoring a backup

`sudo ./cave restore ARCHIVE` checks the whole archive first, then stops the stack, puts back
the state and `.env`, and starts the stack. The state it replaces is kept in
`/var/lib/cave/.cave/ops/before-restore-<time>` and the replaced `.env` as
`/opt/cave/.env.before-restore-<time>`; delete them once everything works (each
`before-restore-*` is a full copy of the state and counts against the update's disk check). If a
restore is interrupted, run the same command again. If it fails while unpacking (for example a
full disk; it checks the space first), nothing is restored and the stack is started again.

Every member of the archive is checked before anything is touched, and again where it would
really land: nothing in an archive can make the restore write outside the state directory and
`.env`, by absolute paths, `..`, links (also chains of links), hard links or device files. Files
of the old server's service user get this server's service user as owner, whatever its user
number.

If an update was interrupted (`cave status` shows it), the restore supersedes it: it checks out
the release from before that update, restores the archive, marks that update's release as
failed and forgets the update, so that no later recovery can put the update's snapshot back over
the restored state. The archive is then compared with that release.

* `--data DIR` restores into another state directory (`.env` is changed to name it).
* `--no-start` does not start the stack afterwards.
* An archive taken with a newer release than the one checked out is refused, because an older
  Home Assistant may not read a newer database. Check out that release first
  (`sudo -u cave git -C /opt/cave checkout --detach <release>`), or pass `--force`.

### Onto a new server

1. Provision the new server and clone the repository as in [install.md](install.md), steps 1 and 2,
   but do **not** run `./cave init`.
2. Copy the archive onto the server, for example to `/var/backups/cave/`.
3. `cd /opt/cave && sudo ./cave restore /var/backups/cave/cave-backup-….tar.gz`.
4. `sudo -u cave ./cave init`: it creates what this release needs and the archive lacks (a
   variable a newer release added to `.env`, the `tools/` directory) and changes nothing else.
   Without it, a release that needs a new variable would be refused at the Compose check every
   night.
5. If the coordinator's address or the FRITZ!Box changed, edit `.env` and run
   `sudo -u cave ./cave up`.
6. Run `sudo /opt/cave/scripts/provision-host.sh` once more: it installs the nightly update and
   backup now that the checkout is there.
