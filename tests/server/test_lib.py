"""The server rehearsal's own bookkeeping (tests/server/lib.sh): known findings and results.

The rehearsal itself runs only on GitHub (docs/operations.md, "The server rehearsal"); these
tests run its helpers here, without a machine.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent / "lib.sh"


def run_part(tmp_path: Path, findings: str, *checks: tuple[str, bool]) -> tuple[int, list[str]]:
    """Run a part named "provision" with the given checks (name, passes) and a known-findings
    file; return the exit status and the result lines."""
    known = tmp_path / "known-findings.tsv"
    known.write_text(findings)
    lines = [f". '{LIB}'", "part provision"]
    lines += [f"check '{name}' {'true' if passes else 'false'}" for name, passes in checks]
    done = subprocess.run(
        ["bash", "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REHEARSAL_DIR": str(tmp_path / "run"),
            "REHEARSAL_PARTS": "all",
            "KNOWN_FINDINGS": str(known),
        },
        check=False,
    )
    results = (tmp_path / "run" / "results.tsv").read_text().splitlines()
    return done.returncode, results


def outcome(results: list[str], name: str) -> str:
    [line] = [line for line in results if line.split("\t")[2] == name]
    return line.split("\t")[3]


@pytest.fixture(autouse=True)
def _bash_available() -> None:
    if subprocess.run(["bash", "-c", "true"], check=False).returncode != 0:
        pytest.skip("no bash")


def test_a_listed_finding_that_fails_is_reported_and_does_not_fail_the_part(tmp_path):
    status, results = run_part(
        tmp_path, "provision\tbroken thing\t#7\n", ("broken thing", False), ("fine", True)
    )

    assert status == 0
    assert outcome(results, "broken thing") == "FINDING"
    assert outcome(results, "(whole part)") == "PASS"


def test_a_listed_finding_that_passes_fails_the_part(tmp_path):
    status, results = run_part(tmp_path, "provision\tbroken thing\t#7\n", ("broken thing", True))

    assert status == 1
    assert outcome(results, "broken thing") == "FAIL"


def test_any_other_failure_fails_the_part(tmp_path):
    status, results = run_part(tmp_path, "provision\tbroken thing\t#7\n", ("another thing", False))

    assert status == 1
    assert outcome(results, "another thing") == "FAIL"


@pytest.mark.parametrize(
    "line",
    [
        "provision\tbroken thing\n",
        "provision\tbroken thing\tissue 7\n",
        "provision broken thing #7\n",
    ],
)
def test_a_malformed_line_of_known_findings_fails_the_part(tmp_path, line):
    status, results = run_part(tmp_path, f"# a comment\n\n{line}", ("broken thing", False))

    assert status == 1
    assert outcome(results, "(known-findings.tsv)") == "FAIL"


def test_comments_and_blank_lines_are_no_findings(tmp_path):
    status, results = run_part(tmp_path, "# provision\tfine\t#7\n\n", ("fine", True))

    assert status == 0
    assert outcome(results, "fine") == "PASS"
