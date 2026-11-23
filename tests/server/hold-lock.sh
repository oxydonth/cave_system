#!/usr/bin/env bash
# Run as the service user inside the machine: hold the cave lock for a few seconds under a name
# written the way a cave command writes it, "`cave update` (pid N, since YYYY-MM-DD HH:MM:SS)",
# with this process's number. SINCE "now" writes the time after this process started; an older
# time makes the process look younger than its name, as a later process with a reused number does.
#
#   hold-lock.sh SINCE LOCK_FILE [SECONDS]

set -euo pipefail

since=$1
lock=$2
[ "${since}" = now ] && since=$(date '+%Y-%m-%d %H:%M:%S')
# shellcheck disable=SC2016 # the backquotes are part of the name
printf '`cave update` (pid %s, since %s)\n' "$$" "${since}" > "${lock}"
exec 9< "${lock}"
flock -x 9
sleep "${3:-8}"
