#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "machine": a Debian 13 machine to install on, as an owner has after step 1 of
# docs/install.md. Debian's own cloud image, its checksum verified, started with QEMU and KVM.

. "$(dirname "$0")/lib.sh"
part machine

install_qemu() {
  sudo apt-get update -qq &&
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      qemu-system-x86 qemu-utils cloud-image-utils > /dev/null &&
    qemu-system-x86_64 --version
}

# The owner's first commands on a new server (docs/install.md, step 1), and what cloud-init did.
first_login() {
  vm_script << 'EOF'
set -e
cloud-init status --wait > /dev/null || true
cloud-init status --long
. /etc/os-release && echo "${PRETTY_NAME}"
uname -m
python3 --version
nproc
free -m
df -h /
EOF
}

# Debian's daily apt runs would hold the package lock while the provisioning script needs it;
# on a real server they run at 06:00 and 18:00, here they could fire right after the first boot.
# Stopped for the rehearsal only (docs/operations.md, "What the rehearsal does not cover").
# Debian's daily apt runs stay on, as on an owner's server: apt-daily.timer is persistent and has
# a random delay of up to twelve hours, so it may hold the package lock right after this first
# boot. The owner whose `apt-get` says "Could not get lock" waits and runs it again (install.md);
# here apt waits for the lock itself. The provisioning script waits on its own.
apt_timers() {
  vm 'systemctl list-timers --all apt-daily.timer apt-daily-upgrade.timer; systemctl is-active apt-daily.service apt-daily-upgrade.service || true'
}

update_the_system() {
  vm 'for _ in $(seq 60); do
        sudo apt-get -o DPkg::Lock::Timeout=600 update -qq && break
        echo "the package lists are locked; trying again"; sleep 10
      done &&
      sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 full-upgrade -y -qq > /dev/null &&
      echo upgraded'
}

step "install QEMU on the runner" install_qemu
step "download Debian's cloud image ${IMAGE_BUILD} and verify its SHA-512" vm_get_image
step "the machine's disk, key pair and cloud-init seed" vm_create
step "power the machine on" vm_start
step "the machine answers over SSH" vm_wait_ssh 300
step "the first login: Debian 13, cloud-init done" first_login
check "the machine is Debian 13" equals 13 vm '. /etc/os-release && echo "${VERSION_ID}"'
check "the machine's Python is 3.11 or newer (the cave command needs it)" \
  vm 'python3 -c "import sys; sys.exit(sys.version_info < (3, 11))"'
step "Debian's daily apt timers are on, as on a server (shown, not changed)" apt_timers
step "the owner brings the system up to date (docs/install.md, step 1)" update_the_system
