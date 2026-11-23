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
quiet_apt_timers() {
  vm 'sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service &&
      sudo systemctl disable apt-daily.timer apt-daily-upgrade.timer 2> /dev/null; systemctl is-active apt-daily.service || true'
}

update_the_system() {
  vm 'sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -qq > /dev/null && echo upgraded'
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
step "Debian's daily apt timers are stopped" quiet_apt_timers
step "the owner brings the system up to date (docs/install.md, step 1)" update_the_system
