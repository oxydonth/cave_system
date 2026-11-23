#!/usr/bin/env bash
# Run as root inside the machine: wait until an update in flight has reached a point of its switch,
# then print "now" and end. The rehearsal cuts the power the moment it reads that line.
#
#   when.sh stopping DATA    the update record says "before-switch": the update is about to stop
#                            the stack or is stopping it, and the snapshot is not complete
#   when.sh starting DATA    the record says "switching" (the snapshot is complete) and the new
#                            release is checked out (its own file is there): it is being started
#
# Gives up after ten minutes with exit status 1.

set -euo pipefail

point=$1
record=$2/.cave/ops/update.json

reached() {
  case "${point}" in
    stopping) grep -q '"phase": "before-switch"' "${record}" 2> /dev/null ;;
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
