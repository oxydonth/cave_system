#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "install": docs/install.md, steps 5 to 8, as the service user in /opt/cave: init, .env,
# pull, check, up, bootstrap, then apply with a room. The machine has no coordinator and no
# FRITZ!Box, which is a home before its devices: COMPOSE_PROFILES and the FRITZ! variables stay
# empty, exactly as install.md says for that case. CAVE_ALERT_URL points at the alert receiver
# in the machine from here on, so every later part also shows which alerts went out.

. "$(dirname "$0")/lib.sh"
part install

# The alert receiver, as a service of its own that comes back after a reboot or a power cut. The
# token in the URL is made here; the receiver refuses a request without it.
start_the_alert_receiver() {
  local token
  token=$(openssl rand -hex 16)
  remember alert-token "${token}"
  vm_put "${SERVER_TESTS}/alert_receiver.py" alert_receiver.py || return 1
  vm_script << EOF
set -e
sudo install -d -m 0755 /opt/alert-receiver /var/lib/alert-receiver
sudo install -m 0644 alert_receiver.py /opt/alert-receiver/alert_receiver.py
sudo tee /etc/systemd/system/alert-receiver.service > /dev/null << 'UNIT'
[Unit]
Description=The rehearsal's alert receiver (CAVE_ALERT_URL)
After=network.target

[Service]
Environment=ALERT_TOKEN=${token}
ExecStart=/usr/bin/python3 /opt/alert-receiver/alert_receiver.py /var/lib/alert-receiver/alerts.jsonl

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now alert-receiver.service
for _ in \$(seq 50); do
  curl -s -o /dev/null http://127.0.0.1:8090/ && exit 0
  sleep 0.2
done
exit 1
EOF
}

# docs/install.md, step 6: the owner edits .env as the service user. Here: the alert URL. It
# reaches the service user's sed on standard input, so that sudo's log of the command line, which
# goes to the journal, does not hold the token: a later check looks for it there.
set_the_alert_url() {
  vm 'sudo -u cave sh -c '\''url=$(cat); sed -i "s|^CAVE_ALERT_URL=.*|CAVE_ALERT_URL=${url}|" /opt/cave/.env'\'' &&
      sudo -u cave grep -c "^CAVE_ALERT_URL=http://:" /opt/cave/.env' \
    <<< "http://:$(recall alert-token)@127.0.0.1:8090/cave"
}

apply_a_room() {
  vm 'sudo -u cave cp /opt/cave/tests/tools/golden/one_room/heating.yaml /var/lib/cave/homeassistant/cave/heating.yaml &&
      cd /opt/cave && sudo -u cave ./cave apply'
}

memory_and_containers() {
  vm 'free -m; sudo docker stats --no-stream; sudo docker image ls; df -h /'
}

step "start the alert receiver in the machine" start_the_alert_receiver
step "cave init" cave init
check ".env is the service user's, mode 600" equals "cave 600" vm "stat -c '%U %a' /opt/cave/.env"
check "init created the state directory's entries as the service user's" \
  equals "" vm 'sudo find /var/lib/cave ! -user cave'
check "init recorded that the stack was not started yet (stopped on purpose)" \
  vm 'sudo test -f /var/lib/cave/.cave/stopped.json'
check "a second init changes nothing" contains '^Nothing to do: already initialised\.$' cave init
step "fill in .env: CAVE_ALERT_URL" set_the_alert_url
step "pull the images (docker compose pull, as the service user)" \
  vm 'cd /opt/cave && time sudo -u cave docker compose pull --quiet'
step "cave check" cave check
step "cave up" cave up
step "cave ps" cave ps
check "cave status before the first-run setup names the release" \
  contains "^Release: +$(recall tag1)\$" cave status
check "cave status before the first-run setup says the stack is healthy" \
  contains '^Stack: +healthy$' cave status
step "cave bootstrap" cave bootstrap
check "the script token is the service user's, mode 600" \
  equals "cave 600" vm "sudo stat -c '%U %a' /var/lib/cave/homeassistant/.cave/token"
step "describe a room and apply it" apply_a_room
check "a second bootstrap changes nothing and leaves the stack healthy" \
  contains '^Stack healthy\.$' cave bootstrap
check "cave status exits 0: healthy, the starter release, heating.yaml applied" exits 0 cave status
check "cave status: heating.yaml applied, no changes since" \
  contains '^heating\.yaml: +applied, no changes since$' cave status
check "nothing in /opt/cave is root's after the first up (hardware-acceptance 6.4)" \
  equals "" vm 'sudo find /opt/cave -user root'
check "the mount points packages and custom_components/cave_heating are not root's (6.4)" \
  equals "cave cave" vm "sudo stat -c '%U' /var/lib/cave/homeassistant/packages /var/lib/cave/homeassistant/custom_components/cave_heating | xargs"
check "the watch, run by its unit as the service user, finds the stack healthy" \
  contains 'The stack is healthy\.' \
  vm 'sudo systemctl start cave-watch.service; sudo journalctl -u cave-watch.service -n 5 --no-pager -o cat'
check "the watch's journal has no permission problem with Docker" \
  lacks '[Pp]ermission denied' vm 'sudo journalctl -u cave-watch.service --no-pager -o cat'
step "memory, containers and images, for the size budget" memory_and_containers
