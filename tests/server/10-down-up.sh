#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016 # lib.sh is found at run time; commands for the machine are in single quotes, to expand there
# Part "down-up": the stack stopped on purpose and started again (docs/operations.md, "Stopped on
# purpose"), and who `cave status` names as the lock's holder, which rests on when Linux says a
# process started (/proc; docs/operations.md, "Which user").

. "$(dirname "$0")/lib.sh"
part down-up

DATA=$(state_dir)

# hold_the_lock SINCE: hold the lock as the service user under a name written at SINCE
# (hold-lock.sh), and print what cave status says about it meanwhile.
hold_the_lock() {
  vm_put "${SERVER_TESTS}/hold-lock.sh" /tmp/hold-lock.sh &&
    vm "sudo -u cave bash /tmp/hold-lock.sh '$1' ${DATA}/.cave/lock & sleep 3
        cd /opt/cave && sudo -u cave ./cave status | grep '^Running now'; status=\$?
        wait; exit \$status"
}

alerts_before=$(alert_count)
step "cave down" cave down
check "nothing runs" equals "" vm "sudo docker ps --quiet"
check "cave status says the stack was stopped on purpose" \
  contains '^NOT STARTED: .*so the stack was not started: on purpose' cave status
check "cave status exits 1 (the stack does not run)" exits 1 cave status
check "the watch, run by its unit, has nothing to report" \
  contains '^The stack is stopped on purpose: nothing to report\.$' \
  vm 'sudo systemctl start cave-watch.service; sudo journalctl -u cave-watch.service -n 3 --no-pager -o cat'
step "cave up" cave up
check "the record of the stop is gone" vm "sudo test ! -e ${DATA}/.cave/stopped.json"
check "cave status exits 0" exits 0 cave status
check "a stop on purpose sent no alert" equals "${alerts_before}" alert_count

check "a lock holder whose process started after its name was written is not named (/proc)" \
  equals "Running now:  another cave command" hold_the_lock "2000-01-01 00:00:00"
check "a lock holder whose process started before its name was written is named" \
  contains '^Running now: +`cave update` \(pid [0-9]+, since [0-9-]+ [0-9:]+\)$' hold_the_lock now
check "the service user can apply once the holder has ended" exits 0 cave apply
