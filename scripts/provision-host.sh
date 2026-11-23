#!/usr/bin/env bash
# Prepare a Debian 13 or Raspberry Pi OS 64-bit host for cave_system (concept section 11):
#
#   * Docker Engine and the Compose plugin, from Docker's own package repository
#   * Git, an SSH client and Python 3 (the `cave` command needs Python 3.11 or newer)
#   * the service user (home /var/lib/cave-home), /opt/cave (the checkout) and /var/lib/cave (the state)
#   * the systemd units of scripts/systemd: the nightly update and backup, which run as root, the
#     recovery of an interrupted update at boot, and the watch that alerts through CAVE_ALERT_URL
#     every five minutes, which runs as the service user (docs/operations.md)
#
# Idempotent: every step first looks whether it is already done. Run it as root:
#
#   sudo scripts/provision-host.sh [--dry-run] [--user NAME]
#
# --dry-run prints what would be done and changes nothing; it needs no root and works on any
# system, so it can be tried anywhere.

set -euo pipefail

SERVICE_USER=cave
APP_DIR=/opt/cave
# Outside the checkout: the deploy key and Git's and SSH's state go here, and a clone needs an
# empty target directory.
HOME_DIR=/var/lib/cave-home
DATA_DIR=/var/lib/cave
BACKUP_DIR=/var/backups/cave
DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
# The fingerprint Docker publishes for its package signing key.
DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
DOCKER_SOURCES=/etc/apt/sources.list.d/docker.sources
# The units next to this script; absent when the script was copied to the server on its own.
UNITS_DIR=$(cd "$(dirname "$0")" && pwd)/systemd
UNIT_FILES="cave-update.service cave-update.timer cave-backup.service cave-backup.timer cave-recover.service cave-watch.service cave-watch.timer"
ENABLED_UNITS="cave-update.timer cave-backup.timer cave-recover.service cave-watch.timer"
DOCKER_PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: provision-host.sh [--dry-run] [--user NAME]

  --dry-run    print what would be done, change nothing, need no root
  --user NAME  the service user that owns the checkout and the state (default: cave)
  --help       show this text
EOF
}

say() { printf '%s\n' "$*"; }
die() {
  printf 'provision-host: %s\n' "$*" >&2
  exit 1
}

# Run a command, or only say that it would run.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: $*"
  else
    "$@"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --user)
      [ $# -ge 2 ] || die "--user needs a name"
      SERVICE_USER=$2
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
  shift
done

# --- what this host is
OS_ID=unknown
OS_VERSION=unknown
OS_CODENAME=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  OS_ID=$(. /etc/os-release && printf '%s' "${ID:-unknown}")
  # shellcheck disable=SC1091
  OS_VERSION=$(. /etc/os-release && printf '%s' "${VERSION_ID:-unknown}")
  # shellcheck disable=SC1091
  OS_CODENAME=$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")
fi

SUPPORTED=1
case "$OS_ID" in
  debian | raspbian) [ "$OS_VERSION" = "13" ] || SUPPORTED=0 ;;
  *) SUPPORTED=0 ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: nothing will be changed."
else
  [ "$(id -u)" -eq 0 ] || die "run as root (sudo), or use --dry-run to see what would happen"
fi

say "Host: $OS_ID $OS_VERSION ${OS_CODENAME:+($OS_CODENAME)}"
if [ "$SUPPORTED" -eq 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    say "Note: this is not Debian 13 or Raspberry Pi OS 64-bit. A real run would stop here;"
    say "      the plan below is for Debian 13."
    OS_CODENAME=trixie
  else
    die "unsupported system '$OS_ID $OS_VERSION': Debian 13 and Raspberry Pi OS 64-bit (Debian 13) only"
  fi
fi
[ -n "$OS_CODENAME" ] || OS_CODENAME=trixie

# Docker's repository has one path for Debian; Raspberry Pi OS 64-bit uses it too.
DOCKER_DEB_URL=https://download.docker.com/linux/debian
ARCH=$(dpkg --print-architecture 2>/dev/null || echo arm64)

# --- packages
say "[1/6] Base packages"
missing=""
for pkg in ca-certificates curl git gnupg openssh-client python3; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    say "  $pkg: installed"
  else
    missing="$missing $pkg"
  fi
done
if [ -n "$missing" ]; then
  say "  to install:$missing"
  run apt-get update
  # shellcheck disable=SC2086  # word splitting of the package list is intended
  run apt-get install -y $missing
fi

# The first fingerprint of a key file (the primary key).
key_fingerprint() {
  gpg --show-keys --with-colons "$1" 2>/dev/null | awk -F: '/^fpr:/ { print $10; exit }'
}

say "[2/6] Docker's package repository"
SOURCES_TEXT="Types: deb
URIs: $DOCKER_DEB_URL
Suites: $OS_CODENAME
Components: stable
Architectures: $ARCH
Signed-By: $DOCKER_KEYRING"
if [ -f "$DOCKER_KEYRING" ]; then
  if [ "$DRY_RUN" -eq 0 ] && [ "$(key_fingerprint "$DOCKER_KEYRING")" != "$DOCKER_KEY_FINGERPRINT" ]; then
    die "$DOCKER_KEYRING is not Docker's signing key (fingerprint $DOCKER_KEY_FINGERPRINT); remove it and run again"
  fi
  say "  signing key: present"
else
  say "  signing key: to be downloaded to $DOCKER_KEYRING and checked against $DOCKER_KEY_FINGERPRINT"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: curl -fsSL $DOCKER_DEB_URL/gpg, compare its fingerprint, install it as $DOCKER_KEYRING"
  else
    install -m 0755 -d /etc/apt/keyrings
    key_tmp=$(mktemp)
    trap 'rm -f "$key_tmp"' EXIT
    curl -fsSL "$DOCKER_DEB_URL/gpg" -o "$key_tmp"
    [ "$(key_fingerprint "$key_tmp")" = "$DOCKER_KEY_FINGERPRINT" ] ||
      die "the key downloaded from $DOCKER_DEB_URL/gpg has an unexpected fingerprint; nothing was installed"
    install -m 0644 "$key_tmp" "$DOCKER_KEYRING"
  fi
fi
if [ -f "$DOCKER_SOURCES" ] && [ "$(cat "$DOCKER_SOURCES")" = "$SOURCES_TEXT" ]; then
  say "  $DOCKER_SOURCES: up to date"
else
  say "  $DOCKER_SOURCES: to be written"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  would write $DOCKER_SOURCES:"
    printf '%s\n' "$SOURCES_TEXT" | sed 's/^/    /'
  else
    printf '%s\n' "$SOURCES_TEXT" >"$DOCKER_SOURCES"
  fi
fi

say "[3/6] Docker Engine and the Compose plugin"
if dpkg -s docker-ce docker-compose-plugin >/dev/null 2>&1; then
  say "  installed"
else
  # Always refresh the package lists first: an earlier run may have been interrupted between
  # writing the repository and updating, and then apt would not know the packages.
  run apt-get update
  # shellcheck disable=SC2086  # word splitting of the package list is intended
  run apt-get install -y $DOCKER_PACKAGES
fi
if command -v systemctl >/dev/null 2>&1; then
  if [ "$DRY_RUN" -eq 1 ] || ! systemctl is-enabled --quiet docker 2>/dev/null; then
    run systemctl enable --now docker
  else
    say "  docker service: enabled"
  fi
fi

say "[4/6] Service user '$SERVICE_USER'"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "  exists"
else
  run useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if id -nG "$SERVICE_USER" 2>/dev/null | grep -qw docker; then
  say "  is in the docker group"
else
  run usermod -aG docker "$SERVICE_USER"
fi

say "[5/6] Directories"
# The checkout is readable by everyone; the state and the backups hold secrets and are not.
for entry in "$APP_DIR:0755" "$DATA_DIR:0750" "$BACKUP_DIR:0750"; do
  dir=${entry%%:*}
  mode=${entry##*:}
  if [ -d "$dir" ] && [ "$(stat -c '%U' "$dir" 2>/dev/null || echo '?')" = "$SERVICE_USER" ]; then
    say "  $dir: ready"
  else
    run install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m "$mode" "$dir"
  fi
done

# A unit as it is installed: one that runs as the service user (cave-watch.service names `cave`)
# names the one given with --user.
unit_text() {
  sed "s/^User=cave\$/User=$SERVICE_USER/" "$UNITS_DIR/$1"
}

say "[6/6] systemd units for the nightly update and backup, and the watch"
UNITS_INSTALLED=0
if [ ! -d "$UNITS_DIR" ]; then
  say "  skipped: $UNITS_DIR does not exist (the script was copied on its own)."
  say "  Run $APP_DIR/scripts/provision-host.sh again once the repository is cloned there."
elif ! command -v systemctl >/dev/null 2>&1; then
  say "  skipped: this system has no systemd"
else
  changed=0
  for unit in $UNIT_FILES; do
    if [ "$(unit_text "$unit")" = "$(cat "/etc/systemd/system/$unit" 2>/dev/null)" ]; then
      say "  $unit: up to date"
    elif [ "$(unit_text "$unit")" = "$(cat "$UNITS_DIR/$unit")" ]; then
      run install -m 0644 "$UNITS_DIR/$unit" "/etc/systemd/system/$unit"
      changed=1
    elif [ "$DRY_RUN" -eq 1 ]; then
      say "  would run: install -m 0644 $UNITS_DIR/$unit (with User=$SERVICE_USER) /etc/systemd/system/$unit"
      changed=1
    else
      unit_tmp=$(mktemp)
      unit_text "$unit" >"$unit_tmp"
      install -m 0644 "$unit_tmp" "/etc/systemd/system/$unit"
      rm -f "$unit_tmp"
      changed=1
    fi
  done
  if [ "$changed" -eq 1 ]; then
    run systemctl daemon-reload
  fi
  for unit in $ENABLED_UNITS; do
    if [ "$DRY_RUN" -eq 0 ] && systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      say "  $unit: enabled"
    else
      # Timers are started as well; the recovery runs at every boot and after every update.
      case "$unit" in
        *.timer) run systemctl enable --now "$unit" ;;
        *) run systemctl enable "$unit" ;;
      esac
    fi
  done
  UNITS_INSTALLED=1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run finished."
  exit 0
fi

# --- verify what the rest of the system relies on
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
  die "python3 is older than 3.11; the cave command needs 3.11 or newer"
docker compose version >/dev/null || die "docker compose does not work"

if [ "$UNITS_INSTALLED" -eq 0 ]; then
  cat <<EOF

Done. Next, in this order (docs/install.md, "Get the code, and install the timers" and after):

  1. clone the repository into the empty $APP_DIR as $SERVICE_USER
  2. run $APP_DIR/scripts/provision-host.sh again: it installs the nightly update and backup
  3. then, as $SERVICE_USER in $APP_DIR: ./cave init, edit .env, ./cave check, ./cave up,
     ./cave bootstrap
EOF
else
  cat <<EOF

Done. Next, as $SERVICE_USER in $APP_DIR (docs/install.md, "Create the configuration" and after):

  ./cave init, edit .env, ./cave check, ./cave up, ./cave bootstrap
EOF
fi
