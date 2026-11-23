#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "watch": docs/operations.md, "Alerts without Home Assistant", in real time. One container
# is stopped without cave, as the guide says to test the receiver; the watch's timer runs it every
# five minutes as the service user; after fifteen minutes one alert arrives and no second one;
# after `cave up` the all-clear. Nothing else touches the machine while the incident lasts, except
# what only reads or cannot change the stack: the answers to hardware-acceptance 6.1 and 6.8.

. "$(dirname "$0")/lib.sh"
part watch

DATA=$(state_dir)
HOME_ASSISTANT=$(vm "sudo docker inspect --format '{{.Config.Image}}' cave-homeassistant-1")

machine_time() {
  vm 'date +%s'
}

watch_memory() {
  vm "sudo cat ${DATA}/.cave/watch.json"
}

watch_says_alerted_already() {
  vm "sudo journalctl -u cave-watch.service --since @${alerted_at} --no-pager -o cat" |
    grep -q '^The stack is not healthy: alerted already\.$'
}

body_after_its_first_line() {
  last_alert_body | tail -n +2
}

# The seconds between the incident's start and the alert's arrival.
alert_delay() {
  local arrived
  arrived=$(alerts | tail -n 1 | jq -r '.time | floor')
  echo $((arrived - incident_started))
}

# within LOW HIGH COMMAND...: the command prints a number from LOW to HIGH.
within() {
  local low=$1 high=$2 got
  shift 2
  got=$("$@")
  echo "got ${got}, expected from ${low} to ${high}"
  [ "${got}" -ge "${low}" ] && [ "${got}" -le "${high}" ]
}

# The gaps in seconds between the watch's runs since the incident started.
watch_run_gaps() {
  vm "sudo journalctl -u cave-watch.service --since @${incident_started} -o short-unix --no-pager |
      awk '/Finished cave-watch.service|cave-watch.service: Deactivated successfully/ { print int(\$1) }' | sort -n -u |
      awk 'NR > 1 { print \$1 - previous } { previous = \$1 }' | xargs"
}

gaps_are_five_minutes() {
  local gaps gap
  gaps=$(watch_run_gaps)
  echo "gaps between the runs: ${gaps}"
  [ -n "${gaps}" ] || return 1
  for gap in ${gaps}; do
    [ "${gap}" -ge 240 ] && [ "${gap}" -le 360 ] || return 1
  done
}

# hardware-acceptance 6.8: one line per repository and tag, the digest in the last column.
image_listing() {
  vm "sudo docker image ls --digests --format '{{.Repository}}\t{{.Tag}}\t{{.Digest}}'"
}

listing_as_documented() {
  local listing
  listing=$(image_listing) || return 1
  printf '%s\n' "${listing}"
  # Every image here was pulled, so every line has a digest; Home Assistant's has its own line.
  awk -F '\t' 'NF != 3 || $3 !~ /^sha256:[0-9a-f]{64}$/ { bad = 1; print "not as documented: " $0 } END { exit bad }' \
    <<< "${listing}" &&
    grep -qE "^${HOME_ASSISTANT%:*}"$'\t'"${HOME_ASSISTANT##*:}"$'\t'"sha256:[0-9a-f]{64}\$" <<< "${listing}"
}

removal_refused() {
  vm "sudo docker image rm ${HOME_ASSISTANT}"
  local status=$?
  echo "--- exit status ${status}"
  [ "${status}" -ne 0 ]
}

# hardware-acceptance 6.1: what HOME runuser gives Git, and a fetch as the service user started by
# root, from the checkout and from the public repository on GitHub, without any key.
runuser_home() {
  vm "sudo runuser -u cave -- sh -c 'echo \$HOME'"
}

alerts_before=$(alert_count)
check "the watch has no incident open" lacks '"alerted": [0-9]' watch_memory
incident_started=$(machine_time)
step "stop Home Assistant's container without cave (sudo docker stop cave-homeassistant-1)" \
  vm 'sudo docker stop cave-homeassistant-1'

# While the incident lasts, only what reads or is refused.
check "docker image ls --digests prints one line per repository and tag, digest last (6.8)" \
  listing_as_documented
check "docker image rm refuses the image of a stopped container (6.8)" \
  contains 'conflict: unable to remove repository reference .*container [0-9a-f]+ is using its referenced image' \
  removal_refused
check "and the image is still there" vm "sudo docker image inspect ${HOME_ASSISTANT} > /dev/null"
check "runuser gives Git the service user's HOME (6.1)" equals /var/lib/cave-home runuser_home
check "root fetches as the service user from the checkout without messages about /root (6.1)" \
  lacks '/root|[Pp]ermission denied' vm 'cd /opt/cave && sudo runuser -u cave -- git -C /opt/cave fetch --dry-run 2>&1'
check "the public repository clones over HTTPS without a key, as the service user" \
  vm 'sudo -u cave -H git clone --quiet --depth 1 https://github.com/oxydonth/cave_system.git /tmp/public-clone'
check "and root fetches from it as the service user (6.1)" \
  lacks '/root|[Pp]ermission denied' vm 'sudo runuser -u cave -- git -C /tmp/public-clone fetch --dry-run 2>&1'

step "wait for the watch's alert (at most 22 minutes)" \
  wait_until 1320 15 alert_count_is "$((alerts_before + 1))"
check "the alert arrived 15 to 20 minutes (plus the timer's accuracy) after the stop" \
  within 900 1260 alert_delay
check "its title: the stack is not healthy" \
  equals "cave-server: the stack is not healthy (token: true)" alert_titles_after "${alerts_before}"
check "its body says what happened, in fixed words" \
  contains '^The stack runs, but it is not healthy\. This has lasted 1[5-9] minutes, since [0-9]{2}:[0-9]{2} UTC\.$' \
  last_alert_body
check "and otherwise only the server, the command and the stack's state" \
  equals "$(printf '%s\n' "" "Server: cave-server" "Command: cave watch" \
    "Stack: running, but NOT healthy" "" \
    "On the server, \`./cave status\` shows more; docs/operations.md says what to do.")" \
  body_after_its_first_line
alerted_at=$(machine_time)
step "wait for the watch's next run" wait_until 400 15 watch_says_alerted_already
check "no second alert for the same incident" equals "$((alerts_before + 1))" alert_count
check "the watch ran every five minutes" gaps_are_five_minutes
check "the watch ran as the service user" \
  equals "_UID=$(vm 'id -u cave')" \
  vm "sudo journalctl _SYSTEMD_UNIT=cave-watch.service --since @${incident_started} -o export | grep '^_UID=' | sort -u"
up_at=$(machine_time)
step "cave up" cave up
step "wait for the all-clear (at most five and a half minutes)" \
  wait_until 330 10 alert_count_is "$((alerts_before + 2))"
check "the alerts of this part: not healthy, then healthy again" \
  equals "$(printf '%s\n' "cave-server: the stack is not healthy (token: true)" \
    "cave-server: healthy again (token: true)")" \
  alert_titles_after "${alerts_before}"
check "the URL's token is in no journal" equals 0 token_in_the_journal
check "every alert of the rehearsal, in order" \
  equals "$(printf '%s\n' \
    "cave-server: update rolled back (token: true)" \
    "cave-server: restore stopped half-way (token: true)" \
    "cave-server: no backup: a restore is unfinished (token: true)" \
    "cave-server: healthy again (token: true)" \
    "cave-server: update rolled back (token: true)" \
    "cave-server: the stack is not healthy (token: true)" \
    "cave-server: healthy again (token: true)")" \
  alert_titles_after 0
echo "The all-clear came $(( $(alerts | tail -n 1 | jq -r '.time | floor') - up_at )) s after cave up."
