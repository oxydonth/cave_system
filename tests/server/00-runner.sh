#!/usr/bin/env bash
# shellcheck disable=SC1091 # lib.sh is found at run time
# Part "runner": what the GitHub runner offers. Without KVM there is no rehearsal: a virtual
# machine without it would be far too slow for Home Assistant, so this part stops the run.
# The numbers go into the log, for the time and size budget in docs/operations.md.

. "$(dirname "$0")/lib.sh"
part runner

show_runner() {
  echo "CPUs: $(nproc)"
  free -m
  df -h / /mnt
  uname -a
  ls -l /dev/kvm
  id
}

kvm_opens() {
  python3 -c 'import os; os.close(os.open("/dev/kvm", os.O_RDWR)); print("/dev/kvm opens read-write")'
}

# The runner's user is not in the group that owns /dev/kvm; a udev rule opens it to everyone on
# this throw-away runner, as GitHub's own documentation for Android emulators does.
open_kvm_to_the_runner() {
  echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' |
    sudo tee /etc/udev/rules.d/99-kvm-rehearsal.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger --name-match=kvm
  ls -l /dev/kvm
}

step "the runner's CPUs, memory and disks" show_runner
step "the runner has /dev/kvm" test -c /dev/kvm
# A fact for the log, not a requirement: the rule below opens it either way.
kvm_as_it_is() {
  kvm_opens || echo "/dev/kvm does not open for $(id -un) without the rule below"
}
step "whether the runner's user can open /dev/kvm as it is" kvm_as_it_is
step "a udev rule lets the runner's user open /dev/kvm" open_kvm_to_the_runner
step "/dev/kvm opens for the runner's user" kvm_opens
step "/mnt has room for the machine (20 GB free)" \
  test "$(df --output=avail -B1G /mnt | tail -n 1 | tr -d ' ')" -ge 20
