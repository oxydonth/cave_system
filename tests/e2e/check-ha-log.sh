#!/usr/bin/env bash
# Fails when Home Assistant logged an ERROR or CRITICAL line that is not on the allow-list below.
# The e2e job runs it for each stack it starts. Usage, from the repository root:
#
#   tests/e2e/check-ha-log.sh [docker compose options...]
#
# for example `tests/e2e/check-ha-log.sh -p cave-demo -f compose.yaml -f compose.sim.yaml
# --env-file data/demo/demo.env` for the demo.
set -euo pipefail

work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT

# Home Assistant colours its log lines; remove the escape sequences before matching.
docker compose "$@" logs --no-color --no-log-prefix homeassistant 2>&1 |
  sed 's/\x1b\[[0-9;]*m//g' > "${work}/ha.log"

# Log lines look like: 2026-11-15 12:00:00.000 ERROR (MainThread) [logger] message. If Home
# Assistant changed that format the filter below would find nothing and pass without looking.
line_format='^[0-9-]+ [0-9:.]+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) '
lines=$(grep -cE "${line_format}" "${work}/ha.log" || true)
if [ "${lines}" -lt 10 ]; then
  echo "::error::only ${lines} log lines match the expected format; has Home Assistant's log format changed?"
  exit 1
fi

# Errors that are expected. Every entry is an extended regular expression matched against the
# whole log line, and says why it is harmless. Do not add one without a reason. There is none now.
allowed=(
  '^$'
)

# Errors and criticals of Home Assistant's logging, and every traceback: one printed to standard
# error outside logging, and also one that Home Assistant attaches to a line of any level,
# a WARNING logged with its exception included. That is stricter than ERROR alone, on purpose;
# allow-list a traceback below, with its reason, if one is ever harmless.
grep -E '^[0-9-]+ [0-9:.]+ (ERROR|CRITICAL) |^Traceback \(most recent call last\)' \
  "${work}/ha.log" > "${work}/errors.log" || true
for pattern in "${allowed[@]}"; do
  grep -vE "${pattern}" "${work}/errors.log" > "${work}/errors.next" || true
  mv "${work}/errors.next" "${work}/errors.log"
done
if [ -s "${work}/errors.log" ]; then
  echo "::error::Home Assistant logged errors that are not on the allow-list:"
  cat "${work}/errors.log"
  exit 1
fi
echo "no unexpected errors in ${lines} log lines"
