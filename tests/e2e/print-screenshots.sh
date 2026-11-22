#!/usr/bin/env bash
# Prints the small copies of the dashboard screenshots into the job log, as base64 between marker
# lines. The e2e job runs it when the screenshot artifact could not be uploaded (the account's
# artifact storage has been full), so that the screenshots can still be seen. To get the pictures
# back from the log of a run:
#
#   gh run view RUN_ID --job JOB_ID --log | python3 tests/e2e/extract_screenshots.py OUT_DIR
#
# Usage: tests/e2e/print-screenshots.sh DIRECTORY   (the directory with the .jpg files)
set -euo pipefail

directory=${1:?usage: print-screenshots.sh DIRECTORY}

shopt -s nullglob
files=("${directory}"/*.jpg)
if [ ${#files[@]} -eq 0 ]; then
  echo "No screenshots in ${directory}: the browser check did not get as far as taking them."
  exit 0
fi

for file in "${files[@]}"; do
  name=$(basename "${file}")
  echo "-----BEGIN SCREENSHOT ${name} ($(wc -c < "${file}" | tr -d ' ') bytes, base64)-----"
  base64 < "${file}" | tr -d '\n' | fold -w 100
  echo
  echo "-----END SCREENSHOT ${name}-----"
done
