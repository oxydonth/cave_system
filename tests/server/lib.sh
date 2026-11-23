#!/usr/bin/env bash
# The server rehearsal's helpers (docs/operations.md, "The server rehearsal"). Every part,
# tests/server/NN-*.sh, sources this file and then reads as a list of what happens:
#
#   step NAME COMMAND...   something the owner or the server does. If it fails, the part stops
#                          there and the parts after it do not run (their state would be noise).
#   check NAME COMMAND...  an assertion: what docs/operations.md promises. A failure is recorded
#                          and the part goes on, so that one run shows every broken promise.
#
# Both print their name, the output of their command (folded into a group) and how long they took,
# and append the result to $REHEARSAL_DIR/results.tsv, from which summary.sh writes the job
# summary. A step or check named in known-findings.tsv is a known finding: its failure is reported
# as that finding, and its success fails the part, because the finding has to be taken out of the
# list (as the device tests do with their findings).
#
# The commands of a check are usually `equals`, `contains`, `lacks` or `exits` (below) around
# `vm`, which runs a shell command in the virtual machine as its administrator, the user an owner
# logs in with. Like the owner, that user reaches root and the service user through sudo.

set -uo pipefail

REHEARSAL_DIR=${REHEARSAL_DIR:-/mnt/rehearsal}
SERVER_TESTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC2034 # used by the parts
REPOSITORY=$(cd "${SERVER_TESTS}/../.." && pwd)
RESULTS=${REHEARSAL_DIR}/results.tsv
KNOWN_FINDINGS=${KNOWN_FINDINGS:-${SERVER_TESTS}/known-findings.tsv}
# Where the parts leave what a later part needs (the release tags, the archive of a backup).
FACTS=${REHEARSAL_DIR}/facts

# shellcheck source=tests/server/vm.sh
. "${SERVER_TESTS}/vm.sh"

PART=""
PART_STARTED=0
PART_FAILED=0

# part NAME: begin a part. Ends the script at once when an earlier part's step failed (the state
# it left would make this part's results noise) or when the workflow's input leaves it out.
part() {
  PART=$1
  PART_STARTED=${SECONDS}
  mkdir -p "${FACTS}"
  touch "${RESULTS}"
  if [ -f "${REHEARSAL_DIR}/stopped" ]; then
    # The failed step has failed its own part already, and the summary fails the job for it and
    # lists this part as not run.
    echo "Part ${PART} does not run: $(cat "${REHEARSAL_DIR}/stopped")."
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${PART}" part "(not run)" "NOT RUN" 0 \
      "$(cat "${REHEARSAL_DIR}/stopped")" >> "${RESULTS}"
    exit 0
  fi
  if ! selected "${PART}"; then
    echo "Part ${PART} is left out by the workflow's input (parts: ${REHEARSAL_PARTS})."
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${PART}" part "(left out)" "LEFT OUT" 0 \
      "the input parts: ${REHEARSAL_PARTS}" >> "${RESULTS}"
    exit 0
  fi
  echo "=== Part ${PART}"
  trap part_end EXIT
  local malformed
  malformed=$(malformed_findings)
  if [ -n "${malformed}" ]; then
    echo "::error title=known-findings.tsv::lines that are not part, name and #issue: ${malformed}"
    record part "(known-findings.tsv)" FAIL 0 "malformed: ${malformed}"
    PART_FAILED=1
    exit 1
  fi
}

# selected NAME: whether REHEARSAL_PARTS ("all", or the last part to run) includes the part.
selected() {
  local wanted=${REHEARSAL_PARTS:-all} name
  [ "${wanted}" = all ] && return 0
  for name in ${PARTS_IN_ORDER}; do
    [ "${name}" = "$1" ] && return 0
    [ "${name}" = "${wanted}" ] && return 1
  done
  return 1
}
PARTS_IN_ORDER="runner machine provision install backup update update-broken restore reboot power-cut down-up watch"

part_end() {
  local status=$?
  local took=$((SECONDS - PART_STARTED))
  if [ "${status}" -ne 0 ] && [ "${PART_FAILED}" -eq 0 ]; then
    # Something outside a step or check failed (a helper, the shell itself).
    record part "(the part's script)" FAIL "${took}" "exit status ${status}"
    PART_FAILED=1
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${PART}" part "(whole part)" \
    "$([ "${PART_FAILED}" -eq 0 ] && echo PASS || echo FAIL)" "${took}" "" >> "${RESULTS}"
  echo "=== Part ${PART}: $([ "${PART_FAILED}" -eq 0 ] && echo passed || echo FAILED) in $(duration "${took}")"
  [ "${PART_FAILED}" -eq 0 ] || exit 1
}

duration() {
  printf '%dm%02ds' $(($1 / 60)) $(($1 % 60))
}

# The issue of a known finding, or nothing.
known_finding() {
  local name=$1
  [ -f "${KNOWN_FINDINGS}" ] || return 0
  awk -F '\t' -v part="${PART}" -v name="${name}" \
    '!/^#/ && $1 == part && $2 == name { print $3 }' "${KNOWN_FINDINGS}"
}

# The lines of known-findings.tsv that are not "part<TAB>name<TAB>#issue": such a line would
# silently not be a finding, so the part fails instead.
malformed_findings() {
  [ -f "${KNOWN_FINDINGS}" ] || return 0
  awk -F '\t' '!/^#/ && !/^[[:space:]]*$/ && (NF != 3 || $1 == "" || $2 == "" || $3 !~ /^#[0-9]+$/)' \
    "${KNOWN_FINDINGS}"
}

record() {
  local kind=$1 name=$2 outcome=$3 took=$4 note=${5:-}
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${PART}" "${kind}" "${name}" "${outcome}" "${took}" \
    "${note//$'\t'/ }" >> "${RESULTS}"
}

# _run KIND NAME COMMAND...: run, time and record a step or check. Returns 0 when the part may go
# on: the command passed, or it is a check, or it failed as a known finding.
_run() {
  local kind=$1 name=$2
  shift 2
  local started=${SECONDS} status finding took outcome
  echo "::group::${kind} ${PART}: ${name}"
  "$@" < /dev/null 2>&1
  status=$?
  echo "::endgroup::"
  took=$((SECONDS - started))
  finding=$(known_finding "${name}")
  if [ "${status}" -eq 0 ] && [ -z "${finding}" ]; then
    outcome=PASS
    echo "PASS     ${kind} ${PART}: ${name} ($(duration "${took}"))"
  elif [ "${status}" -ne 0 ] && [ -n "${finding}" ]; then
    outcome=FINDING
    echo "FINDING  ${kind} ${PART}: ${name} ($(duration "${took}")): known finding ${finding}"
    echo "::warning title=Known finding ${finding}::${PART}: ${name}"
  elif [ "${status}" -eq 0 ]; then
    outcome=FAIL
    echo "FAIL     ${kind} ${PART}: ${name} ($(duration "${took}")): passes, but it is listed" \
      "as the known finding ${finding}; take it out of tests/server/known-findings.tsv"
    echo "::error title=A known finding passes::${PART}: ${name} (${finding})"
    PART_FAILED=1
  else
    outcome=FAIL
    echo "FAIL     ${kind} ${PART}: ${name} ($(duration "${took}")): exit status ${status}"
    echo "::error title=${kind} failed::${PART}: ${name}"
    PART_FAILED=1
  fi
  record "${kind}" "${name}" "${outcome}" "${took}" "${finding}"
  if [ "${kind}" = step ] && [ "${outcome}" = FAIL ] && [ "${status}" -ne 0 ]; then
    return 1
  fi
  return 0
}

step() {
  local name=$1
  if ! _run step "$@"; then
    echo "${PART}: the step '${name}' failed" > "${REHEARSAL_DIR}/stopped"
    exit 1
  fi
}

check() {
  _run check "$@"
}

# ------------------------------------------------------------------------------------ assertions
# equals EXPECTED COMMAND...: the command succeeds and prints exactly EXPECTED (surrounding blank
# space aside).
equals() {
  local expected=$1 got status
  shift
  got=$("$@")
  status=$?
  printf 'expected: %s\n     got: %s\n' "${expected}" "${got}"
  [ "${status}" -eq 0 ] || {
    echo "(the command failed with exit status ${status})"
    return 1
  }
  [ "$(trim "${got}")" = "$(trim "${expected}")" ]
}

trim() {
  local text=$1
  text=${text#"${text%%[![:space:]]*}"}
  printf '%s' "${text%"${text##*[![:space:]]}"}"
}

# contains PATTERN COMMAND...: the command's output has a line matching the extended regular
# expression PATTERN. The command's exit status does not matter (use exits for that).
contains() {
  local pattern=$1 got
  shift
  got=$("$@" 2>&1)
  printf '%s\n' "${got}"
  echo "--- looking for: ${pattern}"
  grep -qE -- "${pattern}" <<< "${got}"
}

# lacks PATTERN COMMAND...: no line of the command's output matches PATTERN, and the command
# succeeded (a command that failed proves nothing about what it would have printed).
lacks() {
  local pattern=$1 got status
  shift
  got=$("$@" 2>&1)
  status=$?
  printf '%s\n' "${got}"
  echo "--- must not contain: ${pattern}"
  [ "${status}" -eq 0 ] || {
    echo "(the command failed with exit status ${status})"
    return 1
  }
  ! grep -qE -- "${pattern}" <<< "${got}"
}

# exits STATUS COMMAND...: the command ends with exit status STATUS.
exits() {
  local expected=$1 status
  shift
  "$@"
  status=$?
  echo "--- exit status ${status}, expected ${expected}"
  [ "${status}" -eq "${expected}" ]
}

# ------------------------------------------------------------------------------ the machine
# cave ARGS: `cd /opt/cave && sudo -u cave ./cave ARGS`, the owner's everyday form.
cave() {
  vm "cd /opt/cave && sudo -u cave ./cave $*"
}

# sudo_cave ARGS: `cd /opt/cave && sudo ./cave ARGS`, for update, backup and restore.
sudo_cave() {
  vm "cd /opt/cave && sudo ./cave $*"
}

# unit_outcome UNIT: "<result> <exit status>" of the unit's last run, as systemd recorded them.
unit_outcome() {
  vm "echo \$(systemctl show -p Result --value $1) \$(systemctl show -p ExecMainStatus --value $1)"
}

# state_dir: the state directory in use, /var/lib/cave until the restore part moves it.
state_dir() {
  cat "${FACTS}/data" 2> /dev/null || echo /var/lib/cave
}

# broker_tag_of RELEASE: the broker image that the release's compose.yaml names.
broker_tag_of() {
  vm "sudo -u cave git -C /opt/cave show $1:compose.yaml" |
    sed -nE 's|^ *image: (eclipse-mosquitto:[0-9.]+)$|\1|p'
}

# alerts: every request the alert receiver got, one JSON object per line, oldest first.
# A machine that cannot be asked fails the read, so that no "sent no alert" check passes on it.
alerts() {
  # shellcheck disable=SC2016 # expands in the machine
  vm 'f=/var/lib/alert-receiver/alerts.jsonl; if sudo test -e $f; then sudo cat $f; fi'
}

alert_count() {
  local all
  all=$(alerts) || {
    echo "the alert receiver's record could not be read" >&2
    return 1
  }
  if [ -z "${all}" ]; then echo 0; else printf '%s\n' "${all}" | wc -l | tr -d ' '; fi
}

alert_count_is() {
  local count
  count=$(alert_count) && [ "${count}" -ge "$1" ]
}

# alert_titles_after N: the titles of the alerts after the first N, one per line, each with
# whether it carried the URL's token.
alert_titles_after() {
  local all
  all=$(alerts) || return 1
  [ -z "${all}" ] || printf '%s\n' "${all}" | tail -n "+$(($1 + 1))" |
    jq -r '"\(.title) (token: \(.authorised))"'
}

last_alert_body() {
  local all
  all=$(alerts) || return 1
  printf '%s\n' "${all}" | tail -n 1 | jq -r .body
}

# token_in_the_journal: how many lines of the machine's whole journal hold the alert URL's token.
token_in_the_journal() {
  vm "sudo journalctl --no-pager | grep -c -F '$(recall alert-token)' || true"
}

# remember NAME VALUE / recall NAME: facts that a later part needs.
remember() {
  printf '%s' "$2" > "${FACTS}/$1"
}

recall() {
  cat "${FACTS}/$1"
}

# wait_until SECONDS INTERVAL COMMAND...: repeat the command until it succeeds; fail after SECONDS.
wait_until() {
  local limit=$1 interval=$2 started=${SECONDS}
  shift 2
  until "$@"; do
    if [ $((SECONDS - started)) -ge "${limit}" ]; then
      echo "--- still not so after $(duration "${limit}")"
      return 1
    fi
    sleep "${interval}"
  done
  echo "--- so after $(duration $((SECONDS - started)))"
}
