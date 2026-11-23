#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "backup": the nightly backup, started by its systemd unit as the timer would start it
# (docs/operations.md, "Backups"), and what it leaves: the archive, the owners of the lock, and a
# service user who can still apply.

. "$(dirname "$0")/lib.sh"
part backup

newest_archive() {
  vm 'sudo ls /var/backups/cave' | grep -E '^cave-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz$' | tail -n 1
}

remember_the_archive() {
  local name
  name=$(newest_archive)
  echo "the archive: ${name}"
  [ -n "${name}" ] && remember archive-1 "${name}"
}

check "the alert receiver has received nothing so far" equals 0 alert_count
step "start cave-backup.service, as its timer does" vm 'sudo systemctl start cave-backup.service'
check "the unit ran and exited 0" equals "success 0" unit_outcome cave-backup.service
step "remember the archive's name" remember_the_archive
check "the unit's journal says the backup was written" \
  contains "^Backup $(recall archive-1) written" \
  vm 'sudo journalctl -u cave-backup.service --no-pager -o cat'
check "the archive is root's, mode 600 (it holds every password)" \
  equals "root 600" vm "sudo stat -c '%U %a' /var/backups/cave/$(recall archive-1)"
check "the archive holds neither the commands' own records nor the lock" \
  lacks '^data/\.cave/(ops|lock)' vm "sudo tar -tzf /var/backups/cave/$(recall archive-1)"
check "the archive has its manifest, .env and Home Assistant's storage" \
  equals "data/homeassistant/.storage/core.config_entries env manifest.json" \
  vm "sudo tar -tzf /var/backups/cave/$(recall archive-1) | grep -E '^(manifest\.json|env|data/homeassistant/\.storage/core\.config_entries)\$' | sort | xargs"
check ".cave and its lock are the service user's after root's backup (hardware-acceptance 6.2)" \
  equals "cave cave" vm "sudo stat -c '%U' /var/lib/cave/.cave /var/lib/cave/.cave/lock | xargs"
check "the service user can apply after root has run (it takes the lock)" exits 0 cave apply
check "cave status names the backup" \
  contains "^Last backup: +/var/backups/cave/$(recall archive-1), [0-9.]+ MiB; 1 kept\$" cave status
check "a successful backup sends no alert" equals 0 alert_count
