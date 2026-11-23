#!/usr/bin/env bash
# The rehearsal's virtual machine: Debian 13 from Debian's official cloud image, started with QEMU
# and KVM on the runner, set up by cloud-init with a key pair made in the job. Sourced by lib.sh.
#
# Everything lives in $REHEARSAL_DIR (on the runner's large disk, /mnt): the image, the machine's
# disk (a copy-on-write layer over the image), the key pair, QEMU's process ID and the serial
# console's log. QEMU runs in the background between the workflow's steps.

# The image: a dated build, never "latest", checked against the SHA-512 kept in the repository
# (tests/server/debian-13-genericcloud-amd64.sha512, copied from the build's SHA512SUMS).
IMAGE_BUILD=20261111-2659
IMAGE_FILE=debian-13-genericcloud-amd64-${IMAGE_BUILD}.qcow2
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/${IMAGE_BUILD}/${IMAGE_FILE}
IMAGE_SUM=${SERVER_TESTS}/debian-13-genericcloud-amd64.sha512

# What the machine gets. The disk is sized for Home Assistant's image (about 2 GB), the broker's,
# a second set when a release changes a tag, the snapshots, the backups and the state.
VM_CPUS=${VM_CPUS:-4}
VM_MEMORY=${VM_MEMORY:-6144}
VM_DISK_SIZE=${VM_DISK_SIZE:-40G}
VM_HOSTNAME=cave-server
VM_USER=owner
VM_SSH_PORT=2222

VM_DIR=${REHEARSAL_DIR}/machine
VM_KEY=${VM_DIR}/id_ed25519
VM_PIDFILE=${VM_DIR}/qemu.pid
VM_CONSOLE=${VM_DIR}/console.log

_ssh_options() {
  printf '%s\n' -i "${VM_KEY}" -p "${VM_SSH_PORT}" \
    -o IdentitiesOnly=yes -o BatchMode=yes -o LogLevel=ERROR \
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${VM_DIR}/known_hosts" \
    -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=8
}

# vm COMMAND: run a shell command in the machine as its administrator (the owner's login).
vm() {
  local options
  mapfile -t options < <(_ssh_options)
  # shellcheck disable=SC2029 # the command is meant to expand in the machine
  ssh "${options[@]}" "${VM_USER}@127.0.0.1" "$@"
}

# vm_put LOCAL REMOTE: copy a file into the machine (into the administrator's reach).
vm_put() {
  local options
  mapfile -t options < <(_ssh_options)
  options=("${options[@]/#-p/-P}")
  scp -q "${options[@]}" "$1" "${VM_USER}@127.0.0.1:$2"
}

# vm_script: run the shell script on standard input in the machine, as the administrator.
vm_script() {
  local options
  mapfile -t options < <(_ssh_options)
  ssh "${options[@]}" "${VM_USER}@127.0.0.1" bash -s
}

vm_get_image() {
  mkdir -p "${VM_DIR}/image"
  if [ ! -f "${VM_DIR}/image/${IMAGE_FILE}" ]; then
    curl --fail --silent --show-error --location --retry 3 \
      -o "${VM_DIR}/image/${IMAGE_FILE}.download" "${IMAGE_URL}" || return 1
    mv "${VM_DIR}/image/${IMAGE_FILE}.download" "${VM_DIR}/image/${IMAGE_FILE}"
  fi
  # The checksum is verified before every use, not only after the download.
  (cd "${VM_DIR}/image" && sha512sum --strict --check "${IMAGE_SUM}")
}

# vm_create: the machine's disk, its key pair and its cloud-init seed. The key pair is made here,
# in the job, and never leaves the runner; nothing secret is used.
vm_create() {
  mkdir -p "${VM_DIR}"
  rm -f "${VM_KEY}" "${VM_KEY}.pub" "${VM_DIR}/known_hosts"
  ssh-keygen -q -t ed25519 -N '' -C rehearsal -f "${VM_KEY}" || return 1
  qemu-img create -q -f qcow2 -F qcow2 -b "${VM_DIR}/image/${IMAGE_FILE}" \
    "${VM_DIR}/disk.qcow2" "${VM_DISK_SIZE}" || return 1
  cat > "${VM_DIR}/user-data" << EOF
#cloud-config
hostname: ${VM_HOSTNAME}
users:
  - name: ${VM_USER}
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "${VM_KEY}.pub")
ssh_pwauth: false
EOF
  printf 'instance-id: %s\nlocal-hostname: %s\n' "${VM_HOSTNAME}" "${VM_HOSTNAME}" \
    > "${VM_DIR}/meta-data"
  cloud-localds "${VM_DIR}/seed.img" "${VM_DIR}/user-data" "${VM_DIR}/meta-data"
}

# vm_start: power the machine on. QEMU keeps running in the background after the step.
vm_start() {
  rm -f "${VM_PIDFILE}"
  echo "--- powered on at $(date -u +%H:%M:%S) UTC" >> "${VM_CONSOLE}"
  qemu-system-x86_64 \
    -name "${VM_HOSTNAME}" \
    -machine q35,accel=kvm -cpu host -smp "${VM_CPUS}" -m "${VM_MEMORY}" \
    -drive "file=${VM_DIR}/disk.qcow2,if=virtio,format=qcow2,discard=unmap" \
    -drive "file=${VM_DIR}/seed.img,if=virtio,format=raw,readonly=on" \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${VM_SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -rtc base=utc \
    -chardev "file,id=console,path=${VM_CONSOLE},append=on" -serial chardev:console \
    -display none -daemonize -pidfile "${VM_PIDFILE}"
}

vm_running() {
  [ -f "${VM_PIDFILE}" ] && kill -0 "$(cat "${VM_PIDFILE}")" 2> /dev/null
}

# vm_wait_ssh SECONDS: wait until the machine answers over SSH.
vm_wait_ssh() {
  local limit=${1:-300} started=${SECONDS}
  until vm true 2> /dev/null; do
    if ! vm_running; then
      echo "QEMU is not running"
      return 1
    fi
    if [ $((SECONDS - started)) -ge "${limit}" ]; then
      echo "no SSH after ${limit} s; the end of the console:"
      tail -n 40 "${VM_CONSOLE}"
      return 1
    fi
    sleep 2
  done
  echo "SSH answers after $((SECONDS - started)) s"
}

# vm_cut_power: switch the machine off hard, as a power cut does: QEMU is killed, so nothing the
# guest had not written to its disk survives, and nothing in the guest is told.
vm_cut_power() {
  local pid
  pid=$(cat "${VM_PIDFILE}")
  kill -KILL "${pid}"
  while kill -0 "${pid}" 2> /dev/null; do sleep 0.1; done
  echo "--- power cut at $(date -u +%H:%M:%S) UTC" >> "${VM_CONSOLE}"
}
