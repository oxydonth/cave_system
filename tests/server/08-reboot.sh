#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "reboot": `sudo reboot` while the stack runs normally. Afterwards the stack runs again by
# itself, the recovery unit ran after Docker and found nothing to do, the timers are back with
# their next runs at night, and nothing was alerted (hardware-acceptance 6.3).

. "$(dirname "$0")/lib.sh"
part reboot

boot_id() {
  vm 'cat /proc/sys/kernel/random/boot_id'
}

reboot_the_machine() {
  vm 'sudo systemctl reboot'
  wait_until 300 3 new_boot
}

new_boot() {
  local now
  now=$(vm 'cat /proc/sys/kernel/random/boot_id' 2> /dev/null) && [ -n "${now}" ] &&
    [ "${now}" != "${boot}" ]
}

healthy() {
  vm 'cd /opt/cave && sudo -u cave ./cave status > /dev/null'
}

# "<monotonic microseconds> <monotonic microseconds>": when Docker became active, and when the
# recovery started.
docker_then_recovery() {
  vm 'echo $(systemctl show -p ActiveEnterTimestampMonotonic --value docker.service) $(systemctl show -p ExecMainStartTimestampMonotonic --value cave-recover.service)'
}

recovery_after_docker() {
  local docker recovery
  read -r docker recovery < <(docker_then_recovery)
  echo "Docker active at ${docker} us, the recovery started at ${recovery} us"
  [ "${recovery}" -gt 0 ] && [ "${recovery}" -ge "${docker}" ]
}

# The time of day of a timer's next run, HH:MM, in the machine's time zone.
next_run() {
  vm "systemctl show -p NextElapseUSecRealtime --value $1" | awk '{ print substr($3, 1, 5) }'
}

# within FIRST LAST COMMAND...: the command prints a time of day HH:MM from FIRST to LAST.
within() {
  local first=$1 last=$2 got
  shift 2
  got=$("$@")
  echo "next run at ${got}, expected from ${first} to ${last}"
  [[ "${got}" > "${first}" || "${got}" == "${first}" ]] && [[ "${got}" < "${last}" || "${got}" == "${last}" ]]
}

alerts_before=$(alert_count)
boot=$(boot_id)
step "sudo reboot" reboot_the_machine
check "the stack is healthy again by itself (cave status exits 0 within ten minutes)" \
  wait_until 600 10 healthy
check "Home Assistant and the broker run" \
  equals "cave-homeassistant-1 cave-mosquitto-1" vm "sudo docker ps --format '{{.Names}}' | sort | xargs"
check "the recovery unit ran and exited 0" equals "success 0" unit_outcome cave-recover.service
check "it found nothing to recover" \
  contains '^No interrupted update: nothing to recover\.$' \
  vm 'sudo journalctl -b -u cave-recover.service --no-pager -o cat'
check "the recovery started after Docker was up (the boot order)" recovery_after_docker
check "the journal of the boot before is kept (journalctl -b -1)" \
  contains 'cave' vm 'sudo journalctl -b -1 -t cave --no-pager -o cat | tail -n 3'
check "the three timers are back, each with a next run" \
  equals "cave-backup.timer cave-update.timer cave-watch.timer" \
  vm 'for t in cave-backup.timer cave-update.timer cave-watch.timer; do [ -n "$(systemctl show -p NextElapseUSecRealtime --value $t)" ] && echo $t; done | xargs'
check "the backup's next run is at 02:30, within its random delay of ten minutes" \
  within 02:30 02:40 next_run cave-backup.timer
check "the update's next run is at 03:30, within its random delay of fifteen minutes" \
  within 03:30 03:45 next_run cave-update.timer
check "the watch's next run is at most five minutes away" \
  vm 'n=$(systemctl show -p NextElapseUSecRealtime --value cave-watch.timer); [ $(( $(date -d "$n" +%s) - $(date +%s) )) -le 300 ]'
check "the watch, run now, finds the stack healthy" \
  contains 'The stack is healthy\.' \
  vm 'sudo systemctl start cave-watch.service; sudo journalctl -b -u cave-watch.service -n 3 --no-pager -o cat'
check "the reboot sent no alert" equals "${alerts_before}" alert_count
