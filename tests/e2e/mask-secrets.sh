#!/usr/bin/env bash
# Registers the secrets of an .env-style file, and optionally a script token file, with the
# GitHub Actions runner, which then masks them in everything the job prints afterwards. The e2e
# job runs it straight after `./cave init` and `./cave demo` have generated them. Usage:
#
#   tests/e2e/mask-secrets.sh ENV_FILE [TOKEN_FILE]
set -euo pipefail

env_file=$1
token_file=${2:-}

grep -E '^CAVE_(MQTT_PASSWORD|HA_OWNER_PASSWORD|Z2M_FRONTEND_TOKEN|FRITZ_PASSWORD)=' "${env_file}" |
  cut -d= -f2- | while read -r secret; do
    if [ -n "${secret}" ]; then
      echo "::add-mask::${secret}"
    fi
  done
if [ -n "${token_file}" ] && [ -s "${token_file}" ]; then
  echo "::add-mask::$(tr -d '\n' < "${token_file}")"
fi
