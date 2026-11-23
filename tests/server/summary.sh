#!/usr/bin/env bash
# The rehearsal's summary: every part with its outcome and duration, every failed step or check,
# every known finding, and the parts that did not run. Written to the job summary (or standard
# output) from $REHEARSAL_DIR/results.tsv. Exit status 1 when anything failed or did not run;
# a part left out by the workflow's input is reported, and is no failure.

set -uo pipefail

REHEARSAL_DIR=${REHEARSAL_DIR:-/mnt/rehearsal}
RESULTS=${REHEARSAL_DIR}/results.tsv
out=${GITHUB_STEP_SUMMARY:-/dev/stdout}

if [ ! -s "${RESULTS}" ]; then
  echo "## Server rehearsal: no results" >> "${out}"
  echo "No part recorded a result: the rehearsal did not start." >> "${out}"
  exit 1
fi

minutes() {
  awk -v s="$1" 'BEGIN { printf "%dm%02ds", s / 60, s % 60 }'
}

failed=$(awk -F '\t' '$4 == "FAIL" || $4 == "NOT RUN"' "${RESULTS}" | wc -l)
total=$(awk -F '\t' '$2 == "part" && $3 == "(whole part)" { s += $5 } END { print s + 0 }' "${RESULTS}")
{
  if [ "${failed}" -eq 0 ]; then
    echo "## Server rehearsal: passed in $(minutes "${total}")"
  else
    echo "## Server rehearsal: FAILED"
  fi
  echo
  echo "| Part | Outcome | Steps and checks | Duration |"
  echo "|---|---|---|---|"
  awk -F '\t' '
    $2 != "part" { count[$1]++; if ($4 == "FAIL") bad[$1]++; if ($4 == "FINDING") found[$1]++ }
    $2 == "part" && $3 == "(whole part)" { order[++n] = $1; outcome[$1] = $4; took[$1] = $5 }
    $2 == "part" && $3 == "(not run)" { order[++n] = $1; outcome[$1] = "not run"; took[$1] = 0 }
    $2 == "part" && $3 == "(left out)" { order[++n] = $1; outcome[$1] = "left out"; took[$1] = 0 }
    END {
      for (i = 1; i <= n; i++) {
        p = order[i]
        note = count[p] + 0
        if (bad[p]) note = note ", " bad[p] " failed"
        if (found[p]) note = note ", " found[p] " known finding(s)"
        printf "| %s | %s | %s | %dm%02ds |\n", p, outcome[p], note, took[p] / 60, took[p] % 60
      }
    }' "${RESULTS}"
  echo
  echo "Total: $(minutes "${total}")."
  if awk -F '\t' '$4 == "FAIL" { found = 1 } END { exit !found }' "${RESULTS}"; then
    echo
    echo "### Failed"
    awk -F '\t' '$4 == "FAIL" && $3 != "(whole part)" {
      printf "- %s, %s: %s%s\n", $1, $2, $3, ($6 == "" ? "" : " (" $6 ")") }' "${RESULTS}"
  fi
  if awk -F '\t' '$4 == "FINDING" { found = 1 } END { exit !found }' "${RESULTS}"; then
    echo
    echo "### Known findings (tests/server/known-findings.tsv)"
    awk -F '\t' '$4 == "FINDING" { printf "- %s, %s: %s, issue %s\n", $1, $2, $3, $6 }' "${RESULTS}"
  fi
  if awk -F '\t' '$4 == "NOT RUN" || $4 == "LEFT OUT" { found = 1 } END { exit !found }' "${RESULTS}"; then
    echo
    echo "### Not run"
    awk -F '\t' '$4 == "NOT RUN" || $4 == "LEFT OUT" { printf "- %s: %s\n", $1, $6 }' "${RESULTS}"
  fi
} >> "${out}"

[ "${failed}" -eq 0 ]
