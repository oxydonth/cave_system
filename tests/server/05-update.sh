#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "update": a newer release appears, and the nightly update installs it, started by its
# systemd unit as the timer would start it (docs/operations.md, "How an update works"). Release 2
# names an older broker image, so the update pulls a second image and the first stays behind as
# the previous release's.

. "$(dirname "$0")/lib.sh"
part update

publish_release_2() {
  local tag
  tag=$(vm 'sudo bash forge.sh publish r2') || return 1
  echo "release 2 is ${tag}"
  remember tag2 "${tag}"
}

# The unit is a oneshot with ExecStopPost: `systemctl start` returns once cave update has ended,
# and the recovery after it may still be running. Wait for the unit to be inactive again.
run_the_nightly_update() {
  vm 'sudo systemctl start cave-update.service'
  local status=$?
  wait_until 600 2 vm '[ "$(systemctl show -p ActiveState --value cave-update.service)" != deactivating ]'
  return "${status}"
}

update_journal() {
  vm 'sudo journalctl -u cave-update.service --no-pager -o cat'
}

broker_image() {
  vm "sudo docker inspect --format '{{.Config.Image}}' cave-mosquitto-1"
}

broker_images() {
  vm "sudo docker image ls eclipse-mosquitto --format '{{.Repository}}:{{.Tag}}' | sort | xargs"
}

previous_release() {
  vm "sudo python3 -c 'import json; print(json.load(open(\"/var/lib/cave/.cave/ops/update.json\"))[\"previous\"][\"release\"])'"
}

alerts_before=$(alert_count)
step "publish release 2: main moves on, the release job tags it" publish_release_2
check "cave status, before the update, does not know release 2 yet (it does not fetch)" \
  lacks "$(recall tag2)" cave status
step "start cave-update.service, as its timer does" run_the_nightly_update
check "the unit ran and exited 0" equals "success 0" unit_outcome cave-update.service
check "the journal says: Installed release 2 (was release 1)" \
  contains "^Installed $(recall tag2) \(was $(recall tag1)\); the stack is healthy\." update_journal
check "the recovery after the run (ExecStopPost) found nothing to recover" \
  contains '^No interrupted update: nothing to recover\.$' update_journal
check "the fetch as the service user, started by root, left no message about /root (6.1)" \
  lacks '/root' update_journal
check "cave status: release 2 installed, healthy, the update's result" \
  contains "^Last update: .*Installed $(recall tag2) \(was $(recall tag1)\)" cave status
check "cave status exits 0 and names release 2" contains "^Release: +$(recall tag2)\$" cave status
check "the broker runs release 2's image" equals "$(broker_tag_of "$(recall tag2)")" broker_image
check "release 1's broker image is kept as the previous release's" \
  equals "$(printf '%s\n' "$(broker_tag_of "$(recall tag1)")" "$(broker_tag_of "$(recall tag2)")" | sort | xargs)" \
  broker_images
check "the update record names release 1 as the previous release" \
  equals "$(recall tag1)" previous_release
check "the snapshot of the state from before the update is kept" \
  equals "0001-$(recall tag1)-to-$(recall tag2)" vm 'sudo ls /var/lib/cave/.cave/ops/snapshots'
check "the checkout is clean and has no file of root's" \
  equals "" vm 'sudo -u cave git -C /opt/cave status --porcelain; sudo find /opt/cave -user root'
check ".cave and its lock are the service user's" \
  equals "cave cave" vm "sudo stat -c '%U' /var/lib/cave/.cave /var/lib/cave/.cave/lock | xargs"
check "the service user can apply after the update" exits 0 cave apply
check "an installed update sends no alert" equals "${alerts_before}" alert_count
