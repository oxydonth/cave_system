#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "provision": docs/install.md, steps 3 and 4. The provisioning script is copied to the
# server on its own and run for real (its check of the operating system included), the code is
# cloned from the stand-in for GitHub, and the script runs a second time from the checkout, which
# installs the timers.

. "$(dirname "$0")/lib.sh"
part provision

copy_the_script() {
  vm_put "${REPOSITORY}/scripts/provision-host.sh" provision-host.sh && vm 'chmod +x provision-host.sh'
}

make_the_releases() {
  "${SERVER_TESTS}/releases.sh" "${REHEARSAL_DIR}/releases.bundle" &&
    vm_put "${REHEARSAL_DIR}/releases.bundle" releases.bundle &&
    vm_put "${SERVER_TESTS}/forge.sh" forge.sh &&
    vm 'sudo bash forge.sh setup releases.bundle'
}

publish_release_1() {
  local tag
  tag=$(vm 'sudo bash forge.sh publish r1') || return 1
  echo "release 1 is ${tag}"
  remember tag1 "${tag}"
}

# The timers that systemd lists with a time for their next run; the listing goes to the log.
timers_with_a_next_run() {
  vm "systemctl list-timers --all 'cave-*' >&2
      for timer in cave-backup.timer cave-update.timer cave-watch.timer; do
        [ -n \"\$(systemctl show -p NextElapseUSecRealtime --value \$timer)\" ] && echo \$timer
      done | xargs"
}

owner_mode() {
  vm "stat -c '%U:%G %a' $1"
}

step "copy the provisioning script to the server on its own (install.md, step 3)" copy_the_script
check "the dry run says what it would do and changes nothing" \
  contains '^Dry run finished: nothing was changed\.$' vm 'sudo ./provision-host.sh --dry-run'
check "after the dry run there is no Docker and no service user" \
  vm '! command -v docker && ! id cave'
step "provision the host" vm 'set -o pipefail; sudo ./provision-host.sh 2>&1 | tee provision-1.log'
check "copied on its own, the script skips the timers and says to run it again from the checkout" \
  contains 'Run /opt/cave/scripts/provision-host.sh again once the repository is cloned there' \
  vm 'cat provision-1.log'
check "Docker and the Compose plugin work" vm 'sudo docker version && sudo docker compose version'
check "Docker's signing key has the fingerprint Docker publishes" \
  contains 'fpr:+9DC858229FC7DD38854AE2D88D81803C0EBFCD88:' \
  vm 'gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc'
check "the service user cave: home /var/lib/cave-home, no login shell, in the docker group" \
  equals "/var/lib/cave-home /usr/sbin/nologin docker" \
  vm 'echo "$(getent passwd cave | cut -d: -f6) $(getent passwd cave | cut -d: -f7) $(id -nG cave | tr " " "\n" | grep -x docker)"'
check "/opt/cave is the service user's, mode 755" equals "cave:cave 755" owner_mode /opt/cave
check "/var/lib/cave is the service user's, mode 750" equals "cave:cave 750" owner_mode /var/lib/cave
check "/var/backups/cave is the service user's, mode 750" \
  equals "cave:cave 750" owner_mode /var/backups/cave

step "the stand-in for GitHub, with the releases made from the commit under test" make_the_releases
step "publish release 1, the commit under test" publish_release_1
# docs/install.md, step 4, for a public repository: no key, the clone goes over the network.
step "clone into the empty /opt/cave as the service user" \
  vm 'sudo -u cave -H git clone git://127.0.0.1/cave_system.git /opt/cave'
check "every file of the checkout is the service user's" \
  equals "" vm 'sudo find /opt/cave ! -user cave'
step "run the provisioning script again, from the checkout" \
  vm 'set -o pipefail; sudo /opt/cave/scripts/provision-host.sh 2>&1 | tee provision-2.log'
check "the second run installs the units and names the next steps" \
  contains '^  \./cave init, edit \.env, \./cave check, \./cave up, \./cave bootstrap$' \
  vm 'cat provision-2.log'
check "the three timers are listed with their next run" \
  equals "cave-backup.timer cave-update.timer cave-watch.timer" timers_with_a_next_run
check "the timers and the recovery at boot are enabled" \
  equals "enabled enabled enabled enabled" \
  vm 'systemctl is-enabled cave-update.timer cave-backup.timer cave-watch.timer cave-recover.service | xargs'
check "the watch runs as the service user, update, backup and recovery as root" \
  equals "cave root root root" \
  vm 'for u in watch update backup recover; do systemctl show -p User --value cave-$u.service; done | xargs'
check "a third run finds everything done" \
  lacks 'to install:|to be written|to be downloaded|Created symlink' \
  vm 'sudo /opt/cave/scripts/provision-host.sh'
