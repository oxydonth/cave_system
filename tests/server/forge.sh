#!/usr/bin/env bash
# The machine's stand-in for GitHub, run as root inside the machine. It serves one repository
# over the Git protocol on 127.0.0.1 (git daemon, as its own user), so that the server clones and
# fetches over the network as it does from GitHub, and holds the releases made by releases.sh in
# a separate repository until they are published.
#
#   forge.sh setup BUNDLE    create both repositories and start the service
#   forge.sh publish NAME    publish r1 ... r4 as the pipeline does: main moves to the commit and
#                            the release job's planner (ops/cave_ops/commands/_releases.py, of the
#                            commit under test) names its tag; prints the tag
#
# The URL to clone is git://127.0.0.1/cave_system.git.

set -euo pipefail

FORGE=/srv/forge
SERVED=${FORGE}/cave_system.git
STAGING=${FORGE}/staging.git
PLANNER=${FORGE}/planner

as_forge() {
  runuser -u forge -- "$@"
}

case $1 in
  setup)
    id -u forge > /dev/null 2>&1 ||
      useradd --system --home-dir "${FORGE}" --create-home --shell /usr/sbin/nologin forge
    install -m 0644 "$2" "${FORGE}/releases.bundle"
    as_forge git init --quiet --bare --initial-branch=main "${SERVED}"
    as_forge git init --quiet --bare --initial-branch=main "${STAGING}"
    as_forge git -C "${STAGING}" fetch --quiet "${FORGE}/releases.bundle" \
      'refs/rehearsal/*:refs/rehearsal/*'
    as_forge mkdir -p "${PLANNER}"
    as_forge git -C "${STAGING}" archive refs/rehearsal/r1 ops | as_forge tar -x -C "${PLANNER}"
    cat > /etc/systemd/system/forge.service << EOF
[Unit]
Description=The rehearsal's stand-in for GitHub: ${SERVED} over git://127.0.0.1
After=network.target

[Service]
User=forge
ExecStart=/usr/bin/git daemon --reuseaddr --export-all --listen=127.0.0.1 --base-path=${FORGE} ${SERVED}

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now forge.service
    for _ in $(seq 50); do
      git ls-remote git://127.0.0.1/cave_system.git > /dev/null 2>&1 && exit 0
      sleep 0.2
    done
    echo "the forge does not answer" >&2
    exit 1
    ;;
  publish)
    commit=$(as_forge git -C "${STAGING}" rev-parse --verify "refs/rehearsal/$2^{commit}")
    as_forge git -C "${STAGING}" push --quiet "${SERVED}" "${commit}:refs/heads/main"
    plan=$(cd "${SERVED}" && as_forge env PYTHONPATH="${PLANNER}/ops" python3 -m \
      cave_ops.commands._releases plan --head "${commit}" --date "$(date -u +%F)")
    tag=$(sed -n 's/^tag=//p' <<< "${plan}")
    if [ -z "${tag}" ]; then
      echo "the planner creates no release: ${plan}" >&2
      exit 1
    fi
    as_forge git -C "${SERVED}" tag "${tag}" "${commit}"
    echo "${tag}"
    ;;
  *)
    echo "usage: forge.sh setup BUNDLE | publish NAME" >&2
    exit 2
    ;;
esac
