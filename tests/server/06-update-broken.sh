#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "update-broken": release 3 passes every check before the switch, but its broker never
# becomes healthy. The owner runs the update by hand (`sudo ./cave update`); it has to roll back
# to release 2 with the state from before, remember release 3 as failed, remove release 3's image
# and alert (docs/operations.md, the rows "Update to v… failed: … Rolled back" and "Old images").

. "$(dirname "$0")/lib.sh"
part update-broken

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
# The update runs in the step and its exit status is checked below: it is meant to fail.
step "sudo ./cave update, by hand (release 3 does not become healthy)" \
  vm 'cd /opt/cave && sudo ./cave update > ~/update-3.log 2>&1; echo "exit status $?" >> ~/update-3.log; cat ~/update-3.log'
check "the update exits 2: rolled back" contains '^exit status 2$' the_update
check "it says that release 3 failed and was rolled back to release 2" \
  contains "^Update to $(recall tag3) failed: .*Rolled back: the previous release $(recall tag2) with the state from before the update .* is running and healthy\." \
  the_update
check "it removed release 3's broker image, which no kept release needs" \
  contains "^cave update: removed 1 image\(s\) that no release here needs any more: $(broker_tag_of "$(recall tag3)")\.\$" \
  the_update
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
