#!/usr/bin/env bash
# The releases the rehearsal updates between, made on the runner from the checkout under test and
# written as a Git bundle for the machine's stand-in for GitHub (forge.sh). Each is a commit on
# top of the one before, as releases on main are:
#
#   r1  the commit under test, as it is
#   r2  r1 with an older tag of the broker's image: a harmless change that makes the update pull
#       a second image and leaves the first behind for the image clean-up
#   r3  r2 with yet another broker tag and a broker whose health check cannot log in: a release
#       that does not become healthy, so the update rolls back and removes its image
#   r4  r2's Compose file again, plus a file that only this release has: a healthy release for
#       the switch that the power cut interrupts, whose way back has to remove that file
#
# Usage: releases.sh BUNDLE. Prints the commits. Each edit is checked to have changed exactly what
# it means to, so that a Compose file that has moved on fails here and not in the machine.

set -euo pipefail

bundle=$1
repository=$(git rev-parse --show-toplevel)
work=$(mktemp -d)
trap 'git -C "${repository}" worktree remove --force "${work}" > /dev/null 2>&1 || true' EXIT

git -C "${repository}" worktree add --quiet --detach "${work}" HEAD
cd "${work}"
# commit NAME MESSAGE: commit what changed as release NAME (refs/rehearsal/NAME).
commit() {
  git -c user.name="cave rehearsal" -c user.email="rehearsal@invalid" commit --quiet -am "$2"
  git update-ref "refs/rehearsal/$1" HEAD
}
# The number of lines of compose.yaml an edit changed.
changed_lines() {
  git diff --numstat -- compose.yaml | awk '{ print $1 }'
}

git update-ref refs/rehearsal/r1 HEAD
current=$(sed -nE 's|^ *image: eclipse-mosquitto:([0-9.]+)$|\1|p' compose.yaml)
older=()
for tag in 2.0.22 2.0.21 2.0.20; do
  [ "${tag}" = "${current}" ] || older+=("${tag}")
done
if [ -z "${current}" ] || [ "${#older[@]}" -lt 2 ]; then
  echo "compose.yaml names no eclipse-mosquitto:<version> image that this script knows" >&2
  exit 1
fi

sed -i "s|image: eclipse-mosquitto:${current}\$|image: eclipse-mosquitto:${older[0]}|" compose.yaml
[ "$(changed_lines)" = 1 ]
commit r2 "Rehearsal release 2: the broker's image ${older[0]}"

sed -i "s|image: eclipse-mosquitto:${older[0]}\$|image: eclipse-mosquitto:${older[1]}|" compose.yaml
# shellcheck disable=SC2016 # the line of compose.yaml, literally
sed -i 's|^      CAVE_MQTT_PASSWORD: \${CAVE_MQTT_PASSWORD:?run ./cave init first}$|      CAVE_MQTT_PASSWORD: not-the-password|' compose.yaml
[ "$(changed_lines)" = 2 ]
commit r3 "Rehearsal release 3: the broker's health check cannot log in"

git checkout --quiet refs/rehearsal/r2 -- compose.yaml
echo "A file that only release 4 of the rehearsal has." > rehearsal-release-4.txt
git add rehearsal-release-4.txt
commit r4 "Rehearsal release 4: release 2 and a file of its own"

git bundle create --quiet "${bundle}" refs/rehearsal/r1 refs/rehearsal/r2 refs/rehearsal/r3 \
  refs/rehearsal/r4
git log --oneline --no-decorate -4
