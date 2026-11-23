#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "restore": docs/operations.md, "Restoring a backup" and "A restore that cannot finish".
#
# 1. A backup by hand, then a restore of it onto the same state directory: every file keeps its
#    owner and mode (hardware-acceptance 6.2), and the service user can still apply.
# 2. A restore that stops half-way: an entry of the state that cannot be moved (made immutable,
#    which a rename cannot pass) stops it while it moves the state into place. Update, backup and
#    apply wait; `restore --undo` puts the state from before back and starts the stack.
# 3. A restore onto a new state directory (--data): .env then names it, and the stack runs from it.
#    The rest of the rehearsal runs on that directory.

. "$(dirname "$0")/lib.sh"
part restore

NEW=/var/lib/cave-restored

# The owners and modes of a state directory, relative paths, without the commands' own records
# (hardware-acceptance 6.2), into a file in the machine.
owners() {
  vm "sudo find $1 -path '*/.cave/ops' -prune -o -printf '%u:%g %m %P\n' | sort -k3 > $2 && wc -l < $2"
}

# The paths in both lists whose owner or mode differ. Files that Home Assistant wrote or removed
# meanwhile (its log, the database's journal) are in only one of them and are not compared.
owner_changes() {
  vm "join -1 3 -2 3 -o 1.1,1.2,2.1,2.2,0 $1 $2 | awk '\$1 != \$3 || \$2 != \$4'"
}

newest_archive() {
  vm 'sudo ls /var/backups/cave' | grep -E '^cave-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz$' | tail -n 1
}

backup_by_hand() {
  sudo_cave backup || return 1
  remember archive-2 "$(newest_archive)"
  echo "the archive: $(recall archive-2)"
}

log() {
  vm "cat $1"
}

config_entries() {
  vm 'sudo sha256sum /var/lib/cave/homeassistant/.storage/core.config_entries'
}

alerts_before=$(alert_count)

# --- 1. onto the same state directory
step "sudo ./cave backup, by hand" backup_by_hand
step "the owners and modes before (hardware-acceptance 6.2)" owners /var/lib/cave owners-before
step "sudo ./cave restore the archive" \
  vm "set -o pipefail; cd /opt/cave && sudo ./cave restore /var/backups/cave/$(recall archive-2) 2>&1 | tee ~/restore-1.log"
check "it restored into /var/lib/cave, because .env names it, and the stack is healthy" \
  contains "^Restored $(recall archive-2) \(taken .* with $(recall tag2)\) into /var/lib/cave\. .*The stack is healthy\.\$" \
  log restore-1.log
check "it chose the state directory from CAVE_DATA in .env and said so" \
  contains '^Restoring into /var/lib/cave \(CAVE_DATA in /opt/cave/\.env\)\.$' log restore-1.log
step "the owners and modes after" owners /var/lib/cave owners-after
check "no file or directory changed its owner or mode" equals "" owner_changes owners-before owners-after
check "the state from before is kept in a before-restore directory" \
  contains '^before-restore-[0-9]{8}T[0-9]{6}Z$' vm 'sudo ls /var/lib/cave/.cave/ops'
check "the .env from before is kept, the service user's, mode 600" \
  contains '^cave 600 /opt/cave/\.env\.before-restore-[0-9]{8}T[0-9]{6}Z$' \
  vm "stat -c '%U %a %n' /opt/cave/.env.before-restore-*"
check ".env is the service user's, mode 600" equals "cave 600" vm "stat -c '%U %a' /opt/cave/.env"
check ".cave and its lock are the service user's" \
  equals "cave cave" vm "sudo stat -c '%U' /var/lib/cave/.cave /var/lib/cave/.cave/lock | xargs"
check "nothing in /opt/cave is root's after the restore (6.4)" equals "" vm 'sudo find /opt/cave -user root'
check "the mount points packages and custom_components/cave_heating are not root's (6.4)" \
  equals "cave cave" vm "sudo stat -c '%U' /var/lib/cave/homeassistant/packages /var/lib/cave/homeassistant/custom_components/cave_heating | xargs"
check "the service user can apply after the restore" exits 0 cave apply
check "cave status exits 0" exits 0 cave status
check "a restore that worked sends no alert" equals "${alerts_before}" alert_count

# --- 2. a restore that stops half-way, and --undo
entries_before=$(config_entries)
step "make the state's zigbee2mqtt entry immutable, so that it cannot be moved" \
  vm 'sudo chattr +i /var/lib/cave/zigbee2mqtt && sudo lsattr -d /var/lib/cave/zigbee2mqtt'
step "sudo ./cave restore the archive again (it is meant to stop half-way)" \
  vm "cd /opt/cave && sudo ./cave restore /var/backups/cave/$(recall archive-2) > ~/restore-2.log 2>&1; echo \"exit status \$?\" >> ~/restore-2.log; cat ~/restore-2.log"
check "the restore exits 1 and says it stopped half-way" \
  contains '^cave: the restore stopped half-way while moving the state into place: .*Operation not permitted' \
  log restore-2.log
check "it alerted: restore stopped half-way" \
  equals "cave-server: restore stopped half-way (token: true)" alert_titles_after "${alerts_before}"
check "cave status exits 1 and names the interrupted restore" \
  contains '^INTERRUPTED: +a restore of /var/backups/cave/cave-backup-.* was interrupted' cave status
check "cave status exits 1" exits 1 cave status
check "apply refuses: Not applied" contains '^Not applied: a restore of .* stopped half-way' cave apply
check "a backup refuses and alerts: no backup" \
  contains '^No backup was made: a restore of .* was interrupted' sudo_cave backup
check "the alerts so far: restore stopped half-way, no backup" \
  equals "$(printf '%s\n' "cave-server: restore stopped half-way (token: true)" \
    "cave-server: no backup: a restore is unfinished (token: true)")" \
  alert_titles_after "${alerts_before}"
step "the cause is removed" vm 'sudo chattr -i /var/lib/cave/zigbee2mqtt'
step "sudo ./cave restore --undo" \
  vm 'set -o pipefail; cd /opt/cave && sudo ./cave restore --undo 2>&1 | tee ~/undo.log'
check "the undo says the state from before is back and the stack is healthy" \
  contains '^The restore that stopped half-way was undone: /var/lib/cave holds the state from before\. The stack is healthy\.$' \
  log undo.log
check "cave status exits 0 again" exits 0 cave status
check "the .env put back is the service user's, mode 600" \
  equals "cave 600" vm "stat -c '%U %a' /opt/cave/.env"
check "no restore marker is left" vm 'sudo test ! -e /var/lib/cave/.cave/ops/restore.json'
step "the owners and modes after the undo" owners /var/lib/cave owners-after-undo
check "every file has the owner and mode it had before the half-way restore" \
  equals "" owner_changes owners-after owners-after-undo
check "Home Assistant's configuration entries are those from before" \
  equals "${entries_before}" config_entries
# The restore's alert was about the stack and went out while it was stopped, so it counts as the
# incident's alert: the watch's next run (every five minutes) sends the all-clear.
check "the watch sends the all-clear at its next run" \
  wait_until 360 10 alert_count_is "$((alerts_before + 3))"
check "the alerts of this part: restore stopped half-way, no backup, healthy again" \
  equals "$(printf '%s\n' "cave-server: restore stopped half-way (token: true)" \
    "cave-server: no backup: a restore is unfinished (token: true)" \
    "cave-server: healthy again (token: true)")" \
  alert_titles_after "${alerts_before}"

# --- 3. onto a new state directory
step "sudo ./cave restore the archive --data ${NEW}" \
  vm "set -o pipefail; cd /opt/cave && sudo ./cave restore /var/backups/cave/$(recall archive-2) --data ${NEW} 2>&1 | tee ~/restore-3.log"
check "it restored into the new directory and the stack is healthy" \
  contains "^Restored $(recall archive-2) .* into ${NEW}\. .*The stack is healthy\.\$" log restore-3.log
check "the new directory is the service user's, mode 750, like the provisioned one" \
  equals "cave:cave 750" vm "sudo stat -c '%U:%G %a' ${NEW}"
check ".env names the new directory" equals "CAVE_DATA=${NEW}" vm 'sudo -u cave grep "^CAVE_DATA=" /opt/cave/.env'
check "Home Assistant runs from the new directory" \
  contains "^${NEW}/homeassistant /config\$" \
  vm "sudo docker inspect --format '{{range .Mounts}}{{println .Source .Destination}}{{end}}' cave-homeassistant-1"
step "the owners and modes in the new directory" owners "${NEW}" owners-new
check "every file has the owner and mode it had in /var/lib/cave" equals "" owner_changes owners-after owners-new
check "the service user can apply on the new directory" exits 0 cave apply
check "cave status exits 0" exits 0 cave status
step "the rest of the rehearsal runs on ${NEW}" remember data "${NEW}"
