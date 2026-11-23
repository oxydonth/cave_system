#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "power-cut": the machine is switched off hard in the middle of an update's switch, twice,
# and started again. docs/operations.md, "Interrupted updates", says what the recovery at boot
# (cave-recover.service) must then do for the phase that was hit:
#
# 1. While stopping the stack (the record says "before-switch", the snapshot is not complete):
#    start the previous release again; the release is not marked as failed.
# 2. After the snapshot, while the new release is being started ("switching"): the full rollback,
#    with the new release's own file removed from the checkout; the release is marked as failed.
#
# Release 4 is healthy; only the power cut stops it. The nightly unit runs both updates.

. "$(dirname "$0")/lib.sh"
part power-cut

DATA=$(state_dir)
MARKER=${DATA}/homeassistant/written-during-the-switch

publish_release_4() {
  local tag
  tag=$(vm 'sudo bash forge.sh publish r4') || return 1
  echo "release 4 is ${tag}"
  remember tag4 "${tag}"
}

boot_id() {
  vm 'cat /proc/sys/kernel/random/boot_id'
}

# start_update_and_cut_power_when POINT: the nightly update starts; the power goes the moment the
# machine reports POINT (when.sh). With "starting", first a marker is written into the state and
# flushed to the disk (the way back has to take it away again), and the update's journal so far
# is kept.
start_update_and_cut_power_when() {
  vm 'sudo systemctl start --no-block cave-update.service' || return 1
  if [ "$1" = starting ]; then
    # While the update is busy with its checks: who holds the lock, as cave status says it.
    sleep 5
    cave status > "${REHEARSAL_DIR}/status-during-update.txt" 2>&1
    vm "sudo bash when.sh starting ${DATA} > /dev/null && sudo touch ${MARKER} && sync && echo now" |
      grep -qx now || return 1
  else
    vm "sudo bash when.sh $1 ${DATA}" | grep -qx now || return 1
  fi
  if [ "$1" = starting ]; then
    vm 'sudo journalctl -u cave-update.service --since "-10min" --no-pager -o cat' \
      > "${REHEARSAL_DIR}/journal-before-the-cut.txt" 2>&1
  fi
  vm_cut_power
  echo "power cut at $1: $(vm_running && echo 'QEMU still runs' || echo 'QEMU is gone')"
}

power_on_after_the_cut() {
  sleep 5
  vm_start && vm_wait_ssh 300
}

# The recovery at boot is a oneshot unit; wait until it has finished.
recovery_done() {
  [ "$(vm 'systemctl show -p ActiveState --value cave-recover.service')" != activating ] &&
    [ "$(vm 'systemctl show -p ExecMainExitTimestampMonotonic --value cave-recover.service')" != 0 ]
}

recovery_journal() {
  vm 'sudo journalctl -b -u cave-recover.service --no-pager -o cat'
}

release_4s_file_is_gone() {
  local journal
  journal=$(recovery_journal)
  if grep -qF "rehearsal-release-4.txt" <<< "${journal}"; then
    echo "the file was on the disk after the cut; the recovery says:"
    grep -E "Note: removed, because they are $(recall tag4)'s own files, byte for byte .*: rehearsal-release-4\.txt\.\$" <<< "${journal}" ||
      return 1
  else
    echo "the file did not survive the power cut; the recovery had nothing to remove"
  fi
  vm 'test ! -e /opt/cave/rehearsal-release-4.txt'
}

snapshots() {
  vm "sudo ls ${DATA}/.cave/ops/snapshots 2> /dev/null | xargs"
}

alerts_before=$(alert_count)
step "copy the helper that reports the point of the switch" vm_put "${SERVER_TESTS}/when.sh" when.sh
step "publish release 4" publish_release_4

# --- 1. while stopping
boot=$(boot_id)
step "start the nightly update; cut the power while it stops the stack" \
  start_update_and_cut_power_when stopping
step "power on again" power_on_after_the_cut
check "it is a new boot" lacks "${boot}" boot_id
step "the recovery at boot has finished" wait_until 900 5 recovery_done
check "the recovery says the update was interrupted before the snapshot was complete" \
  contains "^An update from $(recall tag2) to $(recall tag4), started .*, was interrupted \(before-switch\)\. Finishing it first\.\$" \
  recovery_journal
check "it started the previous release again, nothing changed" \
  contains "^Update to $(recall tag4) stopped before the switch: it was interrupted \(killed or a power cut\) before the snapshot was complete\. Nothing was changed: the previous release $(recall tag2) is running again and healthy\." \
  recovery_journal
check "the recovery unit exited 1: not updated" equals "exit-code 1" unit_outcome cave-recover.service
check "no partial snapshot is left" lacks '\.partial' snapshots
check "release 4 is not marked as failed" lacks "^Failed: +$(recall tag4)" cave status
check "cave status: release 2, healthy, nothing interrupted" \
  contains "^Release: +$(recall tag2)\$" cave status
check "cave status exits 0" exits 0 cave status

# --- 2. while starting the new release
boot=$(boot_id)
step "start the nightly update again; write a marker into the state and sync; cut the power while it starts release 4" \
  start_update_and_cut_power_when starting
check "while the update ran, cave status named it as the lock's holder" \
  contains '^Running now: +`cave update` \(pid [0-9]+, since [0-9-]+ [0-9:]+\)$' \
  cat "${REHEARSAL_DIR}/status-during-update.txt"
check "while the update ran, its journal already showed how far it got" \
  contains "^Stopping the stack to switch from $(recall tag2) to $(recall tag4) \.\.\.\$" \
  cat "${REHEARSAL_DIR}/journal-before-the-cut.txt"
step "power on again" power_on_after_the_cut
check "it is a new boot" lacks "${boot}" boot_id
step "the recovery at boot has finished" wait_until 900 5 recovery_done
check "the recovery says the update was interrupted during the switch" \
  contains "^An update from $(recall tag2) to $(recall tag4), started .*, was interrupted \(switching\)\. Finishing it first\.\$" \
  recovery_journal
check "it rolled back to release 2 with the state from before" \
  contains "^Update to $(recall tag4) failed: it was interrupted \(killed or a power cut\) during the switch\. Rolled back: the previous release $(recall tag2) with the state from before the update .* is running and healthy\." \
  recovery_journal
check "the state is the one from before the update: the marker written during the switch is gone" \
  vm "sudo test ! -e ${MARKER}"
# Git does not flush the files of a checkout to the disk, so the power cut may have taken release
# 4's file with it; if it was still there, the recovery removed it and said so.
check "release 4's own file is not in the checkout, and the recovery named it if it removed it" \
  release_4s_file_is_gone
check "the recovery unit exited 2: rolled back" equals "exit-code 2" unit_outcome cave-recover.service
check "the checkout is exactly release 2: clean, no Git lock file, no file of root's" \
  equals "" vm 'sudo -u cave git -C /opt/cave status --porcelain --untracked-files=all; ls /opt/cave/.git/*.lock 2> /dev/null; sudo find /opt/cave -user root'
check "the checkout's HEAD is release 2" \
  equals "$(recall tag2)" vm 'sudo -u cave git -C /opt/cave describe --tags --exact-match'
check "the used-up snapshot is gone, and no partial one is left" equals "" snapshots
check "cave status: release 4 failed and is skipped" \
  contains "^Failed: +$(recall tag4) on " cave status
check "cave status exits 0" exits 0 cave status
check "one alert in this part: update rolled back (none for the power cut itself)" \
  equals "cave-server: update rolled back (token: true)" alert_titles_after "${alerts_before}"
check "the service user can apply after the recovery" exits 0 cave apply
