# Operating the server: updates, backups, and what to do when one fails

The server updates itself at night and makes a backup before that. Both report every result
as a notification in Home Assistant (and on the phone, once the notify service is set). When Home
Assistant itself is down it cannot tell you, so the server can also send short alerts that do not
depend on it ([Alerts without Home Assistant](#alerts-without-home-assistant)). This page is what
to read when such a notification or alert says something went wrong.

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
| `v… failed on … and is skipped` (journal only, no notification) | The newest release failed before and is not tried again by itself, and no release between it and the installed one is left that has not failed. | Fine. | Nothing, or `sudo ./cave update --force` to try the newest again. |
| `Installed v… (was v…) … Note: v… failed before and was skipped; v… is the newest release that has not failed` | The newest release failed before, so the newest one between it and the installed release that has not failed was installed instead. | The release named first runs, healthy. | Nothing. A release newer than the failed one is installed by itself; `sudo ./cave update --force` tries the failed one again. |
| `Not updating to v… now: \`cave device-check\` holds …` | A device check held a thermostat when the update was about to stop the stack, which would have spoiled the check. Asked right before the switch, after every other check passed. | Unchanged. Not remembered as failed. | Nothing; the next run tries again. Run `cave device-check` by day. A check that was killed leaves its hold until the hold ends by itself (at most four hours). |
| `Not updating to v… automatically: there is no script token yet` (journal only: without the token the notification cannot reach Home Assistant either) | Nightly updates start only once `./cave bootstrap` has created the script token; see [Without the script token](#without-the-script-token). | Unchanged. | Run `./cave bootstrap`. |
| `Not updating to v…: cannot read the script token` / `… is not a single line of printable characters` | The script token file is unreadable or damaged. | Unchanged. | Remove `/var/lib/cave/homeassistant/.cave/token` and run `./cave bootstrap` again. |
| `Not updating to v…: the running stack is not healthy` | The update found the running system unhealthy before it touched anything, and left it alone. | As before the update, still unhealthy. | See [The running stack is not healthy](#the-running-stack-is-not-healthy). |
| `Not updating to v…: the checkout … has local changes` | Someone edited files in `/opt/cave`. | Unchanged. | See [Local changes](#local-changes). |
| `Not updating to v…: the snapshot needs about … MiB` | Not enough disk space for the safety copy, measured after the new images were pulled. | Unchanged. | Free space on the disk that holds `/var/lib/cave`: old files in `/var/backups/cave`, `/var/lib/cave/.cave/ops/before-restore-*` left by a restore that finished (never while `status` shows an unfinished restore: the directory named in `/var/lib/cave/.cave/ops/restore.json` is then the only copy of the state from before), images: the update removes the images of this system's repositories that no kept release needs by itself ([Old images](#old-images)), so `sudo docker image ls` should show at most two or three versions of each of them; images of other repositories are yours to remove. The next night tries again. |
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
| `Backup … written …; … symbolic link(s) pointing outside their directory were left out` | The state contained links that are absolute or contain `..`. A restore refuses such links, so the backup leaves them out to stay restorable; the manifest lists them. | Backup fine. | None of the services makes such links; look at the named ones. |
| `Backup … written …; WARNING: … could not be read as a SQLite database` | The backup was made, but one database file is damaged (Home Assistant renames a damaged database to `home-assistant_v2.db.corrupt.<time>`). It is archived as it is. | Backup fine. | If the named file is a `….corrupt.*` file, Home Assistant has replaced it already: delete it to save space. Otherwise look at `journalctl -t cave`. |
| `Not updating to v…: the validator could not run (the validator could not read the file it was given …)` | The validator ran but could not open the staged copy of `heating.yaml` (it judged nothing). | Unchanged. Not remembered as failed. | Nothing; the next night tries again. If it repeats, look at `journalctl -u cave-update.service`. |
| `… had put the state in place but was cut off before it started the stack. The stack was started …` | A restore or an `--undo` was cut off (or its `up` failed) after the state was in place; this run (often the boot's) started the stack. | Running, as the message says. | Nothing, or see the stack's health in the message. |
| `cave restore`: `… is neither empty nor a cave state directory` | The chosen target holds something else. | Nothing was changed. | Check `--data` (or `CAVE_DATA` in `.env`); `--force` only if you mean it. |
| `cave restore`: `refusing archive member …` | The archive breaks a rule (see [What an archive may contain](#restoring-a-backup)). | Nothing was stopped or changed. | Use another archive; a backup written by `cave backup` never breaks these rules. |
| `cave restore`: `… Variables in the archive's .env that cave does not know were left out: …` / `From the archive's .env, check: …` | The restore wrote only the known variables and names the settings that decide where root writes. | Restored. | Check the named settings in `/opt/cave/.env`. |
| `cave restore`: `… CAVE_ALERT_URL is now the archive's (set / empty: no alerts); check it in .env.` | The restored `.env` names another alert URL than the one before, or none. Its value is never printed, because it may hold a token. This restore's own alerts still went to the one before. | Restored. | Check `CAVE_ALERT_URL` in `/opt/cave/.env`. |
| `The restore failed while unpacking: … The stack was not started again (--no-start)` | A restore with `--no-start` could not unpack its archive; nothing was restored. If a restore before it had put its state in place without starting it, that state is still complete, and the message says so. | Stack stopped on purpose ([Stopped on purpose](#stopped-on-purpose)); nothing refuses because of it, the boot starts nothing. | `sudo -u cave ./cave up` when you want it to run. |
| `The restore failed while unpacking: … The update to v… is still interrupted; with --no-start its way back, which starts the stack, was not run` | A restore with `--no-start` could not unpack its archive while an update was interrupted. | Stack stopped; the update still in flight. | `sudo ./cave update --recover` finishes the update's way back and starts the stack; the next start of the server does it too. |
| `The restore failed while unpacking: … The state is still half restored from the earlier attempt, so the stack was NOT started` | A repeat of a restore that stopped half-way failed before it moved anything. | Stack stopped; the state half restored; the marker stays. | See [A restore that cannot finish](#a-restore-that-cannot-finish). |
| `Not applied: …` (`cave apply`) / `cave: not bootstrapping: …` | An update is interrupted or a restore stopped half-way; a change made now would be undone or land in a half-restored state. | Nothing was changed. | Finish that first (`sudo ./cave update --recover`, or see [A restore that cannot finish](#a-restore-that-cannot-finish)), then run it again. |
| `cannot take the lock …/.cave/lock …` | `.cave` belongs to root (left by an earlier version) and the lock file is missing. | Nothing was changed. | The next root command that takes the lock (the nightly backup, or `sudo ./cave backup`) gives it back to the service user. |
| `The restore failed while unpacking: …` | A restore could not unpack the archive; nothing was restored. | The stack runs again unchanged (or the message says it could not be started). If an update was interrupted, its way back was finished instead (`… was finished instead`), so the release and the state belong together; its own notification says how that went. If an earlier restore had not been started yet, the stack was started on that restore's state (`… started on the state of the restore before this one`). | Read the reason (often a full disk). |
| `Not updating: a restore of … was interrupted` / `No backup was made: a restore of … was interrupted` | A restore was cut off while moving the state into place. Update and backup wait for it. | The stack is stopped; the state may be half restored. | Run the same `sudo ./cave restore …` again; if it stops at the same place again, restore another archive. See [A restore that cannot finish](#a-restore-that-cannot-finish). |
| `Update to v… failed earlier and was rolled back … That could not be recorded then` | An earlier way back had finished, but the disk was full when it wanted to record that. | The previous release runs, healthy; nothing was stopped. | Free space on the disk. |
| `… Note: left in place, because they are not the new release's files: …` | After a rollback, files in `/opt/cave` that the new release would have added, but whose content is not the release's, were kept: they are yours. | As the rest of the message says. | Look at the named files; move them out of `/opt/cave` if they are in the way of the next release. |
| `… Note: removed, because they are v…'s own files, byte for byte (Git gives them back with v…): …` | After an interrupted checkout, files the new release adds were in `/opt/cave` with exactly the release's content, so they were removed with the rest of the release. A file of yours with the same content is among them. | As the rest of the message says. | Nothing; if you had put such a file there on purpose, `sudo -u cave git -C /opt/cave show v…:<path>` gives its content back. |
| `cave update: removed … image(s) that no release here needs any more: …` (journal and output) | After the update, old images of this system were removed ([Old images](#old-images)). | Fine. | Nothing. |
| `Old images were not removed: …` (output only) | The update removed no images this time, for the reason named (the stack is not healthy, an update is interrupted, a restore unfinished, the last update ended with exit status 3, the previous release not recorded yet, an image line it cannot read). | Fine. | Nothing; a later update tries again. |
| `Could not ask Home Assistant whether a thermostat is held: …` (`cave update`, output only) | Right before the switch, Home Assistant did not answer the question whether `cave device-check` holds a thermostat. | The update goes ahead. | Nothing. It had answered the health check with the same token a moment before; holding back every update because this one list could not be read would stop updates for good, while a check the switch spoils only has to be run again. |
| `The alert to CAVE_ALERT_URL could not be sent: …` | An alert could not be delivered: the receiver refused, did not answer within 10 seconds, or answered with an error or a redirect (which is not followed). | As the rest of the message says; the command's exit status is unchanged. | Check the receiver and `CAVE_ALERT_URL` ([Alerts](#alerts-without-home-assistant)). `cave watch` tries its own alerts again every five minutes. |
| `Not updating to v…: … is on another file system than the rest of the state` | Something is mounted at an entry of `/var/lib/cave` (for example a separate disk at `homeassistant`). Snapshots are put back by renaming, which cannot cross file systems. | Unchanged. | Keep all of `/var/lib/cave` on one file system: mount the disk at `/var/lib/cave` itself instead. |
| `… The copy to CAVE_BACKUP_COPY_TO=… FAILED` | The backup was written locally, but the second location was not reachable (not mounted?). | Local backup fine. | Mount the second location, or fix `CAVE_BACKUP_COPY_TO` in `.env`. |
| `No backup was made: the update to v… was interrupted` | An update was interrupted; the state may be incomplete, so it is not archived. | See [Interrupted updates](#interrupted-updates). | `sudo ./cave update --recover`. |

When Home Assistant itself is down, a notification cannot reach it. Every message is also written
to the system journal: `journalctl -t cave` lists them, `journalctl -u cave-update.service` and
`journalctl -u cave-backup.service` show the full output of the nightly runs. With
`CAVE_ALERT_URL` set, the outcomes that need you also arrive as an alert, as does a stack that is
down: see the next section.

## Alerts without Home Assistant

Set `CAVE_ALERT_URL` in `/opt/cave/.env` (`sudo -u cave editor /opt/cave/.env`) to a URL, and the
server posts a short text to it when it needs you, also while Home Assistant is down. Empty, which
is how `./cave init` writes it, nothing leaves the server and nothing is sent.

**The format** is [ntfy](https://ntfy.sh)'s: an HTTP POST with the text as a plain UTF-8 body and
the title in a `Title` header. A self-hosted ntfy on the home network, with its app on the phone,
is the intended receiver (`CAVE_ALERT_URL=http://<ntfy server>/<topic>`); most webhook receivers
take the same, other servers are not targeted. A token goes into the URL: ntfy takes an access
token as the password of basic authentication with an empty user name,
`http://:<token>@<ntfy server>/<topic>` (a user name and password work the same way), or the
`?auth=` parameter its documentation describes. Write the value in single quotes if it contains
`#` or `$`.

**What it says**, and nothing else: the server's name, the command (`cave watch`, `cave update`,
`cave backup`, `cave restore`), what happened in fixed words, and the stack's state in fixed words
(`running and healthy`, `running, but NOT healthy`, `stopped: nothing runs`, `unknown`). No room, no
device, no reading, and not the command's own message: that stays in the notification, the journal
and `./cave status`.

**Where it goes.** A receiver on the home network keeps everything in the home. A public one, such
as ntfy.sh, receives the server's name and those words, and anyone who knows the topic can read
them. The URL, with any token in it, is never printed or logged, and goes only to that URL: no
proxy from the environment is used, and a redirect is not followed (it counts as a failed send).

**When.**

* `cave watch`, every five minutes (`cave-watch.timer`, as the service user): once when an incident
  has lasted **15 minutes**, and once when the stack is **healthy again**. An incident is the stack
  not healthy or stopped, an update that was interrupted (its way back has not finished), or a
  restore that stopped half-way. While another command holds the lock (an update, a backup, a
  restore, `apply`, `bootstrap`), it says nothing and starts counting afresh afterwards; it never
  takes the lock itself, so it cannot make the nightly update refuse. A stack that is stopped on
  purpose is not an incident ([Stopped on purpose](#stopped-on-purpose)). Two runs more than eleven
  minutes apart (the server was off) start the count afresh too, so a power cut is not reported
  for the hours it lasted while the stack is still starting.
* `cave update`, `cave backup` and `cave restore`, at once, for each outcome whose message is a
  warning, in addition to the notification: an update rolled back (exit status 2) or left not
  healthy or not running (3); a backup that failed, was not made (an interrupted update, an
  unfinished restore, another command at work) or could not be copied; a restore that failed while
  unpacking, stopped half-way, or whose stack could not be started or is not healthy. Outcomes
  that are no warning send nothing: installed, up to date, not updated because a check or the
  download failed (the next night tries again).

An alert of a command about the stack (an update left it not healthy or not running; a restore
stopped half-way, or its stack was not started or is not healthy) that is delivered while the
stack is not healthy counts as that incident's alert: `cave watch` does not send another for it,
and sends the all-clear once the stack is healthy. An alert about something else, such as a
backup that was not made, does not: the watch still reports the stack. What the watch has sent is
in `/var/lib/cave/.cave/watch.json`; an alert that could not be delivered is tried again at its
next run. A send takes at most 10 seconds, whatever the receiver does, and never changes a
command's exit status.

**When the watch cannot write its memory** (a full disk, or `/var/lib/cave/.cave` not writable by
the service user), it trusts nothing it has remembered, because it could not remember what it does
next either. Each run tries the write first. While it fails:

* every run with an incident sends that incident's alert, at once, without waiting fifteen
  minutes, and whatever the memory says was sent before; each says "this repeats every five
  minutes while the incident lasts, and no all-clear follows";
* no all-clear is sent: without a memory it would go out again every five minutes for as long as
  the stack is healthy and the disk full. When the stack is healthy again, the repeated alerts
  simply stop;
* once the memory can be written again, the watch works as usual: an incident it had alerted
  and remembered before the disk filled gets its all-clear then.

So an unwritable memory brings more alerts, never fewer, and never silence during an incident. A
full disk is a likely reason for Home Assistant to fail, and exactly when you need to hear. A
memory that cannot be read (a damaged file) counts as empty: the next incident is alerted after its
fifteen minutes as usual, and the file is written anew.

The title is `<server>: <what happened>`:

| The alert's title ends with | From | What to do |
|---|---|---|
| `the stack is stopped`, `the stack is not healthy`, `Docker does not answer` | `cave watch`, after 15 minutes | `sudo -u cave ./cave status`; see [Nothing is running](#nothing-is-running) or [The running stack is not healthy](#the-running-stack-is-not-healthy). |
| `an update was interrupted` | `cave watch`, after 15 minutes | See [Interrupted updates](#interrupted-updates). |
| `a restore stopped half-way` | `cave watch`, after 15 minutes | See [A restore that cannot finish](#a-restore-that-cannot-finish). |
| `healthy again` | `cave watch` | Nothing. |
| `update rolled back` | `cave update` | As for its notification: nothing urgent. |
| `update failed, act now` | `cave update` | Read its notification (or `journalctl -t cave`): the rows "NOT healthy", "NOTHING IS RUNNING" and "the way back was not clean" above. |
| `restored stack not started`, `restored stack not healthy` | `cave update` (a restore's owed start), `cave restore` | `sudo -u cave ./cave status`; the boot tries a start that is owed again. |
| `backup failed`, `backup copy failed`, `no backup: …` | `cave backup` | The rows of `The backup failed`, `The copy to CAVE_BACKUP_COPY_TO … FAILED` and `No backup was made` above. |
| `restore failed`, `restore failed again`, `restore stopped half-way` | `cave restore` | The rows of `The restore failed while unpacking` above, and [A restore that cannot finish](#a-restore-that-cannot-finish). |

`sudo -u cave ./cave watch` runs the watch by hand and says what it found. To test the receiver,
stop one container without `cave` (`sudo docker stop cave-homeassistant-1`): after 15 to 20
minutes the alert arrives, and after `sudo -u cave ./cave up` the all-clear.

What `cave watch` prints (its journal is `journalctl -u cave-watch.service`), and what it leaves:

| It says | Meaning | It leaves |
|---|---|---|
| `CAVE_ALERT_URL is not set: nothing is watched, and nothing leaves the server.` | The variable is empty. | Nothing: it asked nothing and wrote nothing. |
| `… is at work: nothing to say until it has finished.` | A command holds the lock. | Its count of an incident starts afresh; an incident already alerted stays open. |
| `The stack is healthy.` / `Alert sent: healthy again.` | No incident; the all-clear went out if an incident had been alerted. | Nothing open. |
| `The stack is stopped on purpose: nothing to report.` | Stopped by `down`, `--no-start` or before the first `up`. | An incident alerted before stays open until the stack is healthy. |
| `… (since HH:MM UTC): alerted once it has lasted 15 minutes.` | An incident, not yet 15 minutes old. | The time it was first seen. |
| `Alert sent: …` / `…: alerted already.` | The incident's alert went out now / earlier. | The incident open, until healthy. |
| `The alert (…) could not be sent: …. The next run tries again.` | The receiver did not take it. | The incident not alerted; the next run tries again. |
| `cave watch cannot remember what it has alerted (…): until the memory can be written it alerts an incident at once and at every run, and sends no all-clear.` (exit status 1) | `/var/lib/cave/.cave/watch.json` could not be written (a full disk, `.cave` not the service user's). | An incident is alerted at once and again every five minutes while it lasts, each alert saying so; no all-clear. Free space on the disk that holds `/var/lib/cave`, or give `.cave` back to the service user. |
| `The stack is healthy (no all-clear while the memory cannot be written).` (exit status 1) | The stack is healthy, and the memory cannot be written. | No all-clear; the repeated incident alerts have stopped. |

## Stopped on purpose

`./cave down`, a restore or `restore --undo` with `--no-start`, and `./cave init` for a state it has
just created (before the first `./cave up`) record that the stack is stopped on purpose, in
`/var/lib/cave/.cave/stopped.json`. While it is there and nothing runs, `status` says
`NOT STARTED: … so the stack was not started: on purpose`, and `cave watch` raises no alert. Every
start of the stack by `cave` removes it: `./cave up` once Compose has started the containers, a
restore or `--undo` once it changes the state, and the recovery. A command that refuses or fails
before it has changed anything leaves it as it was: a restore that says "Nothing was changed", an
`up` whose Compose fails, a `down` whose Docker fails (it records the stop first and takes it back
then).

**The record says what was meant; what runs decides.** It can be stale: you started the stack
with `docker compose` directly after `./cave down`, or an `up` or `down` was cut off between its
steps. So no command leaves a running stack stopped, or stays silent about it, because of the
record alone:

* `status` shows `NOT STARTED` only while nothing of the stack runs; over running containers the
  `Stack` line says what is wrong;
* `cave watch` counts only a stack of which nothing runs as stopped on purpose; a stack that runs
  but is unhealthy is reported whatever the record says, and a record that containers outlive by
  five minutes is removed, so that a later stop is reported;
* a restore that cannot unpack its archive leaves the stack stopped only if nothing of it ran
  when the restore began (and the record says it was stopped on purpose); a stack that ran is
  started again;
* the recovery at boot, `update`, `restore --undo` and the way back of an interrupted update
  start the stack without asking the record.

The record does not cover Docker not answering, an interrupted update or a restore that stopped
half-way: those are reported in any case.

An `up` that fails entirely after `./cave down` (Compose refuses, nothing starts) keeps the record:
`cave watch` then stays silent about the stopped stack, although you asked for a start. You saw the
failure in the terminal; run `./cave up` again once its cause is fixed, and the record goes when
Compose has started the containers.

After an update from v2026.11.22.1, the note of a `--no-start` restore of that version may still
be in `/var/lib/cave/.cave/ops/not-started.json`, which the service user cannot remove. A start
by the service user (`./cave up`) then writes `"started": true` into `stopped.json`, which counts
before the old note; the next start by root removes both.

## How an update works

The timer `cave-update.timer` starts `cave update --auto` at 03:30 (plus up to 15 minutes); the
backup runs at 02:30 before it. `cave update`, holding the one lock that update, backup and restore
share (concept section 4.5):

1. Fetches the release tags from GitHub, so that the server's tags are exactly GitHub's: a tag
   moved there is moved here, a deleted (withdrawn) one is deleted here. It takes the newest
   release (`vYYYY.MM.DD.N`) **whose commit is on `main`**: only the pipeline creates release
   tags, and only for `main`, so a matching tag anywhere else (pushed by hand, on a branch) is
   ignored. Nothing happens if no release is newer than the installed one. A release that failed
   before is skipped: the newest release between it and the installed one that has not failed is
   installed instead, and nothing happens if there is none (`--force` takes the newest, failed or
   not).
2. **Before anything is touched:** the running stack must be healthy; the checkout must have no
   local changes; the script token must exist (for the nightly run, see below). The new release
   is exported to a temporary directory (`/var/lib/cave/.cave/ops/tmp`); its Compose file must
   accept this server's `.env` and `compose.override.yaml`; its container images are pulled;
   then there must be room on the disk for the snapshot (measured after the pull, which usually
   fills the same disk); then the new release's Home Assistant checks the configuration and the
   new release's validator checks the applied `heating.yaml`, both in the new image. Last, it asks
   Home Assistant whether `cave device-check` holds a thermostat, and waits for the next run if
   so; if Home Assistant cannot answer that one question, it goes ahead (it answered the health
   check with the same token a moment before, and refusing would stop updates for good). If anything fails here, the system has not been stopped for a second.
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

At the end of every run except `--recover`, still holding the lock, the update removes the
images of releases that are no longer needed: see [Old images](#old-images).

A release whose Home Assistant rejected the configuration or whose validator rejected
`heating.yaml` (exit status 1 from a check that ran), or that failed in step 3, is remembered and
skipped until a newer release appears or `sudo ./cave update --force` is run. A failed download,
and a check that could not run at all (Docker failed, it timed out, the validator is missing), is
not remembered: the next night tries again.

At most two snapshots are kept in `/var/lib/cave/.cave/ops/snapshots/`, each named after the
releases it was taken between: after a successful update its snapshot stays, after a rollback it
has been used up. The update record (`/var/lib/cave/.cave/ops/update.json`) holds the last
result, the failed releases and, while an update runs, what it is doing.

### Old images

Every release pulls its images and leaves the previous ones behind, so after many releases the
disk check above would refuse every update. So the update removes them at the end of its run,
holding the lock:

* Candidates are **every image of the repositories** that this system's Compose files
  (`compose.yaml`, `compose.sim.yaml`) name: `ghcr.io/home-assistant/home-assistant`,
  `eclipse-mosquitto`, `ghcr.io/koenkk/zigbee2mqtt` and the simulator's, whoever pulled it. An
  image of one of these repositories that other software on the server uses is removed too if no
  container of it exists (Docker refuses one that a container, even a stopped one, uses). Images
  of every other repository are never touched.
* Kept are the images that these releases name: the checked-out one; the one the last installed
  update came from; and the one the last switch started from, which a way back by hand returns
  to. Also every image `compose.override.yaml` names. An image pinned by digest
  (`name:tag@sha256:…`) is kept by its tag and by its digest (`docker image ls --digests`). A
  release that failed and was rolled back has its images removed with the rest.
* Each one is removed by its name, `docker image rm`, never forced. `docker image prune -a` and
  `docker system prune` are never run.
* Nothing is removed while an update is interrupted, a restore is unfinished, the last update
  ended with exit status 3 (the owner may have to put the previous release back by hand, which
  needs its images without internet), or the stack is not running and healthy; nor before an
  update has recorded which release it came from (the first update with this version records
  it), nor when an `image:` line names a variable.

Per outcome of the update run (the exit status in brackets):

| The run ended | Images removed? | Kept, besides the override's |
|---|---|---|
| Installed (0) | Yes | The new release's; the one it came from |
| Up to date, or the newest release failed before and is skipped (0) | Yes, unless the last update ended with exit status 3 | The running release's; the previous; the last switch's starting point |
| Automatic updates off, `--auto` (0) | No | All |
| Not updated before anything was touched: no release list, a check failed or could not run, the download failed, local changes, no token, no room for the snapshot, `device-check` holds a thermostat (1) | Yes, if the stack is healthy; the new release's downloaded images go and are downloaded again next time | The running release's; the previous; the last switch's starting point |
| Not updated: the running stack is not healthy (1) | No (not healthy) | All |
| Not updated: a restore is unfinished (1) | No | All |
| Not updated: another command holds the lock (1) | No (it never ran) | All |
| Stopped before the switch, running again unchanged (1) | Yes | The running release's (the switch's starting point); the previous |
| Rolled back (2) | Yes; the failed release's images go | The running release's (the switch's starting point); the previous |
| Not healthy, nothing running, or the way back not clean (3) | No, and not on the nights after either, until an update ends otherwise; even then the last switch's starting point is kept, which is the release to put back by hand | All |
| A restore's owed start: healthy (0) / not (3) | Yes / no | As above / all |
| `--recover`, at boot or by hand (any) | Never | All |

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
| `sudo -u cave ./cave status` | Shows what is installed, health, the last update, failed releases, the last backup, unapplied `heating.yaml` changes, and why the stack does not run if it was stopped on purpose. Changes nothing. Exit status 1 if unhealthy or an update was interrupted. |
| `sudo -u cave ./cave watch` | What `cave-watch.timer` runs every five minutes: alerts through `CAVE_ALERT_URL` ([Alerts](#alerts-without-home-assistant)). Does nothing while it is unset. |
| `sudo ./cave update` | Installs the newest release now, as described above. |
| `sudo ./cave update --force` | The same, also for a release that failed before. |
| `sudo ./cave update --recover` | Only finishes an interrupted update, or a way back that could not finish. Runs at every boot and after every nightly update; waits for a backup that holds the lock. |
| `sudo ./cave update --timeout SECONDS` | How long a started release may take to become healthy (default 900). |
| `sudo ./cave backup` | Writes a backup now. |
| `sudo ./cave restore ARCHIVE [--data DIR] [--no-start] [--force]` | Puts back a backup; see [Restoring](#restoring-a-backup). |
| `sudo ./cave restore --undo [--data DIR] [--no-start]` | Puts back the state from before a restore that stopped half-way; see [A restore that cannot finish](#a-restore-that-cannot-finish). |

`update` exits with 0 (installed, or nothing to do), 1 (not updated, the previous release runs as
before), 2 (the release failed and was rolled back; the previous release runs, healthy) or 3 (the
previous release is not back healthy, or nothing runs: act now).

### One file system

All of `/var/lib/cave` must be on **one file system**: snapshots and restores put state back by
renaming, which needs no free space but cannot cross file systems. An update or restore refuses,
before touching anything, when an entry is on another one. To give the state a disk of its own,
mount it at `/var/lib/cave` itself.

### Which user

`update`, `backup` and `restore` run as **root**; the systemd units do. `apply` and `bootstrap` run
as the service user. All five take the one lock of concept section 4.5 (`/var/lib/cave/.cave/lock`),
in any order: taking it needs no write access to the file, and a root command gives `.cave` and
the lock file to the owner of `/var/lib/cave` (the service user), so whichever ran first, the
other can take it next. A command that finds the lock taken tries for two seconds before it says
who holds it, because `status` and `cave watch` look at the lock for an instant without taking
it. It names the holder only while that process lives: a holder that could not write its name
(the service user, while the lock file is still root's from an earlier version) is "another cave
command". Where Linux says when a process started, a process that started after the name was
written is not taken for the holder either: after a reboot, another process may have the same
number. `cave watch` runs as the service user, who may ask Docker, and takes no lock.
`cave device-check` changes no state of its own and takes no lock; the nightly update does not
stop the stack while a check holds a thermostat, but a check started while an update is under way
can still be spoiled by it, not the home: run checks by day. The state directory holds
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
# forget the update in flight, but keep the record of failed releases:
sudo python3 -c 'import json; p="/var/lib/cave/.cave/ops/update.json"; d=json.load(open(p)); d["switch"]=None; json.dump(d, open(p, "w"), indent=2)'
sudo -u cave ./cave up
sudo systemctl start cave-update.timer               # delete $B once everything works
```

### Giving up on a way back

If the way back keeps failing for a reason that will not go away, no backup is made until it is
finished. To give up deliberately, put the state right by hand (above), then forget the update
in flight with the `python3` line in that recipe; it keeps the record of failed releases.

### Interrupted updates

If the server loses power, or the update is killed, the update record says how far it got. The
next run finishes the job: `cave-recover.service` at boot, the update service itself when it
stops for any reason, or any later `cave update`. `sudo ./cave update --recover` does it by hand.

| Interrupted | What the next run finds | What it does |
|---|---|---|
| Before stopping the stack | No update in flight (only a temporary export, removed) | Nothing special; a normal update. |
| While stopping, or while copying the snapshot | Stack stopped or stopping; a snapshot `….partial`; checkout and state unchanged | Removes the partial snapshot, starts the previous release, waits for healthy. The release is not marked as failed. |
| After the snapshot: during the checkout, before or after starting the new release | Stack stopped or running the new release; the checkout possibly half changed, with Git's lock file left behind; a complete snapshot | The full rollback: stop, put back the snapshot, remove Git's stale lock files, check out exactly the previous release (a file the new release added is removed only if its content is the release's own; anything else, such as a file of yours at that path, is left in place and named in the message), start, wait. The release is marked as failed (an update that died during the switch is suspect); `--force` tries it again. |
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
the state and `.env`, and starts the stack. The `.env` from before is copied to `.env.before-restore-<time>` (`-2`, `-3` … added for a second restore within the same second) and `.env` is then
replaced in one step, so there is always a `.env` that names the state directory. Values are
written back as `./cave init` and Docker Compose read them: in single quotes where they contain
`$`, `#` or spaces; a value that cannot be written so that both read it unchanged is left out and
named.

**Where it restores to** is never taken from the archive: `--data DIR` if given, else `CAVE_DATA`
of the existing `/opt/cave/.env`, else the default state directory (`/var/lib/cave` on a
provisioned server). The restore says which it chose and why, and, if the archive was taken
from another directory, that the restored `.env` names the chosen one. A directory that is
neither empty nor a cave state directory (one with `.cave`, `homeassistant`, `mosquitto` or
`zigbee2mqtt`) is refused unless `--force` is given, so that a mistyped `--data` cannot move a
foreign directory's contents aside. The state it replaces is kept in
`/var/lib/cave/.cave/ops/before-restore-<time>` and the replaced `.env` as
`/opt/cave/.env.before-restore-<time>`; once that restore has finished and everything works, delete them (each
`before-restore-*` is a full copy of the state and counts against the update's disk check). If a
restore is interrupted, run the same command again (or restore another archive; see
[A restore that cannot finish](#a-restore-that-cannot-finish)). If it fails while unpacking (for
example a full disk; it checks the space first), nothing is restored and the stack is started
again.

**What an archive may contain.** Directories, regular files and symbolic links, each once, below
`data/`, none below a link, with no absolute path and no `..` in a name. A link must be relative
and contain no `..` at all, so it can only point at or below its own directory; and once the
whole archive is unpacked, every link in it is resolved and must stay inside. An archive that
breaks a rule is refused before anything is stopped or touched. `data/.cave` must be a plain
directory, and nothing may lie at or below `data/.cave/ops` or `data/.cave/lock` (the commands'
own records and lock, which a backup never contains); no link may point there, nor at a
directory that contains them (`.cave` itself, or the state directory). `cave backup` leaves out
such links too. Modes keep
only the permission bits, so an archive cannot plant a setuid or setgid file. So, with the
target chosen as above, nothing in an archive can make the restore write outside the state
directory and `.env`, nor leave a link there that points outside (which a container's bind
mount would otherwise follow). `cave backup` leaves out links that break the rule and names
them, so every archive it writes can be restored.

**What the archive still decides**, and what it does not: it decides the state's content, file
owners and permission bits, and the values of the `.env` variables of concept section 4.2. The
restored `.env` holds only those variables (any other, such as `COMPOSE_FILE`, is left out and
named), with `CAVE_DATA` set to the directory actually used. The restore names the restored
settings that decide where root writes later (`CAVE_BACKUP_DIR`, `CAVE_BACKUP_COPY_TO`) or what
reaches a container (`CAVE_Z2M_SERIAL_PORT`, `COMPOSE_PROFILES`): check them. Files of the old
server's service user get this server's service user as owner, whatever its user number; root's
files stay root's, whatever the archive's manifest says. The archive does not decide the
release that is checked out (only the update record does, for an interrupted update), nor any
command that is run. Its record of the release it was taken with can only make the restore
refuse, never make it do more.

If an update was interrupted (`cave status` shows it), the restore supersedes it. Only once the
archive is unpacked does it check out the release from before that update, mark that update's
release as failed and forget the update; then it moves the archive's state in. So no later
recovery can put the update's snapshot back over the restored state. If unpacking fails, it
finishes the update's way back instead (the update's release and state belong together), and
says so. The archive is compared with the release from before the update.

While the state is moved in, a marker (`/var/lib/cave/.cave/ops/restore.json`) is kept: if the
restore is cut off there, `status` shows it, and update and backup refuse until the same restore
has been run again.

* `--data DIR` restores into another state directory (`.env` is changed to name it).
* `--no-start` does not start the stack afterwards, also when the restore fails: see
  [Stopped on purpose](#stopped-on-purpose). An interrupted update is the exception: with
  `--no-start` the restore leaves it in flight, and its way back, at the next boot or with
  `sudo ./cave update --recover`, starts the stack; `--undo --no-start` of a restore that had
  superseded an update puts that update back in flight and finishes its way back at once, which
  starts the stack too.
* An archive taken with a newer release than the one checked out is refused, because an older
  Home Assistant may not read a newer database. Check out that release first
  (`sudo -u cave git -C /opt/cave checkout --detach <release>`), or pass `--force`.
  `--force` also allows a target directory that is neither empty nor a state directory.

### A restore that cannot finish

A restore cut off while moving the state in leaves a marker (`/var/lib/cave/.cave/ops/restore.json`);
`status` shows it, and `update` (also `--recover`), `backup`, `apply` and `bootstrap` refuse,
because the state may be half restored. Every attempt of the same series moves the state from
before into one and the same `before-restore-*` directory, named in the marker. **Do not delete
that directory** while the restore is unfinished: it is the only copy of the state from before,
and `--undo` needs it (the messages name it). The ways out:

1. Run the same `sudo ./cave restore <archive>` again. It unpacks again and moves the state in
   from the start.
2. If it stops at the same place again, or there is not enough space, restore another archive
   (`sudo ./cave restore <older archive>`), after freeing space if needed: old archives, but
   keep the one you restore and the one you would fall back to; unused images with
   `sudo docker image prune`, **without** `-a`: with the stack stopped, `-a` also removes the
   stack's own images, and `up` then has to download them again, which fails without internet.
   Any completed restore removes the marker.
3. Or go back to the state from before the first attempt: `sudo ./cave restore --undo`. It
   discards what the attempts moved in, puts back every entry from before (from the
   `before-restore-*` directory, or where no attempt touched it), puts back the `.env` from
   before if an attempt had replaced it, removes the marker and starts the stack (`--no-start`:
   do not). If the restore had superseded an interrupted update, the state from before is the
   one that update left (possibly migrated by its new release): `--undo` then puts that update
   back in flight and finishes its way back, which puts its snapshot and the release from before
   it back together, as `sudo ./cave update --recover` would. If `--undo` is cut off, run it
   again; once it had put the state back, the next `cave update` (at boot) also finishes it,
   starting the stack.
   A marker written by an earlier version does not record the state from before; `--undo`
   and a repeat then refuse, before stopping anything, and say what to do by hand.

If an attempt fails while unpacking (a full disk, an unreadable archive) it changes nothing, but
the state is still half restored from the attempt before: the stack is not started, the marker
stays, and the message says so.

**A restore or `--undo` is finished only once the stack is started.** When the state is in
place, the marker stays (as "start owed") until `up` was issued; if the command is cut off in
between, or `up` fails, `status` shows `NOT STARTED: … put the state in place but was cut off
before it started the stack`, and `sudo ./cave update --recover` (which the boot runs) starts the
stack and says so. With `--no-start` the marker goes at once, since the state is complete, and
`status` shows `NOT STARTED: … finished with --no-start` while the stack is not running; nothing
refuses because of it, the boot does not start what you kept stopped, `cave watch` does not report
it, and `sudo -u cave ./cave up` starts it (which also ends the note, whatever happens after). The only case in which the boot leaves the stack stopped
without you having asked for it is a state that is still half restored: starting that would
start Home Assistant unconfigured, so `status` names the ways out above instead.

A restore run while a start is still owed is a new restore, not a repeat: the state in place is
complete, so it becomes this restore's state from before, in a `before-restore-*` directory of
its own. If this restore stops half-way, the state is half restored as described above, the boot
does not start it, and `--undo` brings back the complete state that was waiting and starts it.
If it cannot unpack its archive, nothing is changed and it starts the stack on the state that
was waiting; should that start fail, the start stays owed. With `--no-start` it starts nothing:
the state that was waiting is complete, so the owed start is given up for "stopped on purpose".

### Onto a new server

1. Do the steps of [install.md](install.md) up to the clone, but not the configuration: put the
   operating system on it and give it a fixed address, [provision the host](install.md#3-provision-the-host),
   and [get the code and install the timers](install.md#4-get-the-code-and-install-the-timers).
   Stop there: do **not** run `./cave init` ([create the configuration](install.md#5-create-the-configuration)),
   because the restore brings `.env` and the state.
2. Copy the archive onto the server, for example to `/var/backups/cave/`.
3. `cd /opt/cave && sudo ./cave restore /var/backups/cave/cave-backup-….tar.gz`. With no `.env`
   yet, it restores into the default state directory, `/var/lib/cave` (it says so), whatever
   directory the archive was taken from; give `--data DIR` for another. A restore is finished only
   once it has started the stack: wait for the command to end, and see
   [A restore that cannot finish](#a-restore-that-cannot-finish) if it does not.
4. `sudo -u cave ./cave init`: it creates what this release needs and the archive lacks (a
   variable a newer release added to `.env`, the `tools/` directory) and changes nothing else.
   Without it, a release that needs a new variable would be refused at the Compose check every
   night.
5. If the coordinator's address or the FRITZ!Box changed, edit `.env` and run
   `sudo -u cave ./cave up`.
