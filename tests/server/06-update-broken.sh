#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "update-broken": release 3 passes every check before the switch, but its broker never
# becomes healthy. The owner runs the update by hand (`sudo ./cave update`); it has to roll back
# to release 2 with the state from before, remember release 3 as failed, remove release 3's image
# and alert (docs/operations.md, the rows "Update to v… failed: … Rolled back" and "Old images").
#
# Release 3 fails when it starts, before Home Assistant runs on it, so it changes nothing in the
# state itself. So that the rollback has to put the state back for the check to pass, a marker is
# written into the state once the switch has begun (the snapshot is complete): the way back must
# take it away with the rest of what the switch left.
#
# Before it, an image of the broker's repository is pulled by its digest only, as other software
# might: Docker lists it with the tag <none> (hardware-acceptance 6.8), and the clean-up at the
# end of the update must read that line and remove it, since no release here names it.

. "$(dirname "$0")/lib.sh"
part update-broken

DIGEST_ONLY=eclipse-mosquitto@sha256:d12c8f80dfc65b768bb9acecc7ef182b976f71fb681640b66358e5e0cf94e9e9
MARKER=/var/lib/cave/homeassistant/written-after-the-snapshot

publish_release_3() {
  local tag
  tag=$(vm 'sudo bash forge.sh publish r3') || return 1
  echo "release 3 is ${tag}"
  remember tag3 "${tag}"
}

the_update() {
  vm 'cat update-3.log'
}

broker_images() {
  vm "sudo docker image ls eclipse-mosquitto --format '{{.Repository}}:{{.Tag}}' | sort | xargs"
}

# A file the update must give back as it was: Home Assistant's core configuration, written by the
# bootstrap. Its checksum before the update and after the rollback.
state_sum() {
  vm 'sudo sha256sum /var/lib/cave/homeassistant/.storage/core.config_entries'
}

alerts_before=$(alert_count)
before=$(state_sum)
step "publish release 3" publish_release_3
step "copy the helper that reports the point of the switch" vm_put "${SERVER_TESTS}/when.sh" when.sh
step "pull an image of the broker's repository by its digest only" \
  vm "sudo docker pull --quiet ${DIGEST_ONLY}"
check "docker image ls --digests lists it with the tag <none> and its digest (6.8)" \
  contains "^eclipse-mosquitto"$'\t'"<none>"$'\t'"${DIGEST_ONLY#*@}\$" \
  vm "sudo docker image ls --digests --format '{{.Repository}}\t{{.Tag}}\t{{.Digest}}'"
# The update runs in the step and its exit status is checked below: it is meant to fail. Beside
# it, as root, the marker is written and flushed to the disk once the snapshot is complete.
step "sudo ./cave update, by hand (release 3 does not become healthy); a marker in the state once the switch has begun" \
  vm "rm -f ~/marker-3.log
      (sudo bash when.sh switching /var/lib/cave && sudo touch ${MARKER} && sync &&
        echo 'marker written' > ~/marker-3.log) > /dev/null 2>&1 &
      cd /opt/cave && sudo ./cave update > ~/update-3.log 2>&1; echo \"exit status \$?\" >> ~/update-3.log
      cat ~/update-3.log"
check "the marker was written while the switch ran" equals "marker written" vm 'cat ~/marker-3.log'
check "the update exits 2: rolled back" contains '^exit status 2$' the_update
check "the rollback took the marker away with the rest of the switch's state" \
  vm "sudo test ! -e ${MARKER}"
check "it says that release 3 failed and was rolled back to release 2" \
  contains "^Update to $(recall tag3) failed: .*Rolled back: the previous release $(recall tag2) with the state from before the update .* is running and healthy\." \
  the_update
check "it removed release 3's broker image and the one with only a digest, which no kept release needs" \
  contains "^cave update: removed 2 image\(s\) that no release here needs any more: ($(broker_tag_of "$(recall tag3)"), ${DIGEST_ONLY}|${DIGEST_ONLY}, $(broker_tag_of "$(recall tag3)"))\.\$" \
  the_update
check "the image with only a digest is gone" lacks "${DIGEST_ONLY#*@}" \
  vm "sudo docker image ls --digests --format '{{.Repository}}\t{{.Tag}}\t{{.Digest}}'"
check "the broker images left are release 1's and release 2's" \
  equals "$(printf '%s\n' "$(broker_tag_of "$(recall tag1)")" "$(broker_tag_of "$(recall tag2)")" | sort | xargs)" \
  broker_images
check "the state is the one from before the update" equals "${before}" state_sum
check "the used-up snapshot is gone, the one of the installed update is kept" \
  equals "0001-$(recall tag1)-to-$(recall tag2)" vm 'sudo ls /var/lib/cave/.cave/ops/snapshots'
check "cave status: release 2, healthy, release 3 failed" \
  contains "^Failed: +$(recall tag3) on .*; skipped until a newer release or \`sudo \./cave update --force\`\$" \
  cave status
check "cave status names release 2" contains "^Release: +$(recall tag2)\$" cave status
check "cave status exits 0" exits 0 cave status
check "the checkout is clean and has no file of root's" \
  equals "" vm 'sudo -u cave git -C /opt/cave status --porcelain; sudo find /opt/cave -user root'
check "one alert arrived: update rolled back, with the URL's token" \
  equals "cave-server: update rolled back (token: true)" alert_titles_after "${alerts_before}"
check "the alert says the command and the stack's state, nothing of the home" \
  contains '^Command: cave update$' last_alert_body
check "the alert's body is only the fixed words" equals "$(
  printf '%s\n' \
    "A new release failed and was rolled back: the previous release runs again, with the state from before the update." \
    "" "Server: cave-server" "Command: cave update" "Stack: running and healthy" "" \
    "On the server, \`./cave status\` shows more; docs/operations.md says what to do."
)" last_alert_body
check "the URL's token is in no journal" equals 0 token_in_the_journal
check "the service user can apply after the rollback" exits 0 cave apply
check "the next update skips the failed release and changes nothing" \
  contains "^$(recall tag3) failed on .* and is skipped until a newer release appears" \
  sudo_cave update
