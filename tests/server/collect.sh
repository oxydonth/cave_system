#!/usr/bin/env bash
# shellcheck disable=SC1091 # lib.sh is found at run time
# After the rehearsal, whatever happened: the machine's journals, the update record, the alerts
# received and the serial console, into $REHEARSAL_DIR/logs for the job's artifact. Nothing here
# is secret: every password in the machine was generated in it for this run and dies with it.
# Then the machine is switched off.

. "$(dirname "$0")/lib.sh"

logs=${REHEARSAL_DIR}/logs
mkdir -p "${logs}"
cp "${VM_CONSOLE}" "${logs}/console.log" 2> /dev/null || true
cp "${RESULTS}" "${logs}/results.tsv" 2> /dev/null || true

if vm_running && vm true 2> /dev/null; then
  vm 'sudo journalctl --list-boots --no-pager' > "${logs}/boots.txt" 2>&1
  vm 'sudo journalctl -b 0 --no-pager' > "${logs}/journal-this-boot.txt" 2>&1
  vm 'sudo journalctl --no-pager -t cave -u "cave-*" -u alert-receiver.service -u forge.service' \
    > "${logs}/journal-cave.txt" 2>&1
  vm 'cd /opt/cave && sudo -u cave ./cave status; sudo docker ps -a; sudo docker image ls --digests' \
    > "${logs}/status.txt" 2>&1
  vm 'sudo cat /var/lib/cave*/.cave/ops/update.json /var/lib/alert-receiver/alerts.jsonl' \
    > "${logs}/records.txt" 2>&1
  echo "Collected the machine's journals into ${logs}."
else
  echo "The machine does not answer; only the console log is kept."
fi
if vm_running; then
  vm_cut_power
fi
ls -l "${logs}"
