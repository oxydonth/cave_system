#!/usr/bin/env bash
# Run as root inside the machine: wait until an update or a restore in flight has reached a point,
# then print "now" and end. The rehearsal then acts at once: it changes the state that the way back
# has to put back, cuts the power, or kills the restore.
#
#   when.sh stopping DATA    the update record says "before-switch": the update is about to stop
#                            the stack or is stopping it, and the snapshot is not complete
#   when.sh switching DATA   the record says "switching": the snapshot is complete
#   when.sh starting DATA    the record says "switching" and release 4 is checked out (its own file
#                            is there): it is being started
#   when.sh start-owed DATA  a restore has put the state and .env in place and is about to start
#                            the stack (its marker says "start_owed")
#
# Gives up after ten minutes with exit status 1.

set -euo pipefail

point=$1
data=$2
record=${data}/.cave/ops/update.json

reached() {
  case "${point}" in
    stopping) grep -q '"phase": "before-switch"' "${record}" 2> /dev/null ;;
    switching) grep -q '"phase": "switching"' "${record}" 2> /dev/null ;;
    start-owed) grep -q '"start_owed": true' "${data}/.cave/ops/restore.json" 2> /dev/null ;;
    starting)
      grep -q '"phase": "switching"' "${record}" 2> /dev/null &&
        test -e /opt/cave/rehearsal-release-4.txt
      ;;
    *) exit 2 ;;
  esac
}

for _ in $(seq 6000); do
  if reached; then
    echo now
    exit 0
  fi
  sleep 0.1
done
exit 1
