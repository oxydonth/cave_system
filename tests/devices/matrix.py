"""Turn the test results into the device-by-capability matrix (concept section 8, WP7).

Usage, from the repository root, with the JUnit XML that pytest wrote (``--junitxml``)::

    uv run --locked python -m tests.devices.matrix test-results.xml >> "$GITHUB_STEP_SUMMARY"

Rows are the capabilities of ``tests/devices/capabilities.py``, columns the devices. A cell is
``pass`` when every test of that capability and device passed, ``FAIL`` when one failed, a
finding when one is a known finding (a strict expected failure, see ``capabilities.devices``),
``n/a`` when the capability does not apply, and ``GAP`` when it applies but no test ran for it.
The exit status is 1 if there is a gap or a failure, so the pipeline shows it.

Standard library only; it imports nothing from Home Assistant.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tests.devices.capabilities import CAPABILITIES, DEVICES

PASS = "pass"
FAIL = "FAIL"
FINDING = "finding"
NOT_APPLICABLE = "n/a"
SKIPPED = "skipped"
GAP = "GAP"

_NAME = re.compile(r"^test_(?P<capability>\w+)\[(?P<params>.+)\]$")


@dataclass
class Cell:
    """The results of one capability on one device."""

    outcomes: list[str] = field(default_factory=list)
    findings: list[str | None] = field(default_factory=list)  # per test, for FINDING outcomes
    tests: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)  # the parameter IDs, ``<device>[-<variant>]``


@dataclass
class Matrix:
    """The matrix and what it found."""

    cells: dict[tuple[str, str], str]
    findings: dict[str, list[str]]  # finding text -> test IDs
    gaps: list[tuple[str, str]]  # capability, missing test ID (``<device>[-<variant>]``)
    failures: list[str]
    unknown: list[str]  # device tests whose capability or device is not in the registry
    counts: dict[tuple[str, str], tuple[int, int]]  # cases passed, cases run


def _device_of(params: str) -> str | None:
    """The device a parameter ID names: the longest device key it starts with."""
    candidates = [key for key in DEVICES if params == key or params.startswith(f"{key}-")]
    return max(candidates, key=len) if candidates else None


def _outcome(case: ET.Element) -> tuple[str, str | None]:
    for tag in ("failure", "error"):
        if case.find(tag) is not None:
            return FAIL, None
    skipped = case.find("skipped")
    if skipped is None:
        return PASS, None
    message = skipped.get("message", "")
    if skipped.get("type") == "pytest.xfail":
        reason = message.removeprefix("xfail").strip(": ").strip()
        if reason.startswith("finding:"):
            return FINDING, reason.removeprefix("finding:").strip()
        return FAIL, None  # an expected failure that is not a recorded finding
    if "n/a:" in message:
        return NOT_APPLICABLE, None
    return SKIPPED, None


def build(junit: Path) -> Matrix:
    """Read pytest's JUnit XML and build the matrix of the device tests in it."""
    root = ET.parse(junit).getroot()  # noqa: S314  (our own pytest output)
    cells: dict[tuple[str, str], Cell] = defaultdict(Cell)
    unknown: list[str] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        if not classname.startswith("tests.devices."):
            continue
        match = _NAME.match(case.get("name", ""))
        if match is None:
            continue
        capability, params = match["capability"], match["params"]
        device = _device_of(params)
        test_id = f"{classname}::{case.get('name')}"
        entry = CAPABILITIES.get(capability)
        if entry is None or device is None:
            unknown.append(test_id)
            continue
        outcome, finding = _outcome(case)
        if device in entry.applies and params not in entry.test_ids(device):
            unknown.append(test_id)  # a variant the registry does not declare
            continue
        cell = cells[(capability, device)]
        cell.outcomes.append(outcome)
        cell.tests.append(test_id)
        cell.findings.append(finding)
        cell.ids.append(params)

    result: dict[tuple[str, str], str] = {}
    findings: dict[str, list[str]] = defaultdict(list)
    gaps: list[tuple[str, str]] = []
    failures: list[str] = []
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    for capability, entry in CAPABILITIES.items():
        for device in DEVICES:
            cell = cells.get((capability, device), Cell())
            applies = device in entry.applies
            ran = [outcome for outcome in cell.outcomes if outcome not in (NOT_APPLICABLE, SKIPPED)]
            counts[(capability, device)] = (ran.count(PASS), len(ran))
            if FAIL in cell.outcomes:
                status = FAIL
                failures.extend(
                    test
                    for test, outcome in zip(cell.tests, cell.outcomes, strict=True)
                    if outcome == FAIL
                )
            elif not applies:
                status = NOT_APPLICABLE
            elif missing := [
                test_id
                for test_id in entry.test_ids(device)
                if not any(
                    ran_id == test_id and outcome not in (NOT_APPLICABLE, SKIPPED)
                    for ran_id, outcome in zip(cell.ids, cell.outcomes, strict=True)
                )
            ]:
                status = GAP
                gaps.extend((capability, test_id) for test_id in missing)
            elif FINDING in ran:
                status = FINDING
                for finding, test in zip(cell.findings, cell.tests, strict=True):
                    if finding is not None:
                        findings[finding].append(test)
            else:
                status = PASS
            result[(capability, device)] = status
    return Matrix(result, dict(findings), gaps, failures, unknown, counts)


def _finding_label(text: str) -> str:
    match = re.match(r"(F\d+)\b", text)
    return match.group(1) if match else "finding"


def render(matrix: Matrix) -> str:
    """The matrix as Markdown, for the job summary."""
    finding_of: dict[tuple[str, str], list[str]] = defaultdict(list)
    for text, tests in matrix.findings.items():
        for test in tests:
            name = test.split("::", 1)[1]
            match = _NAME.match(name)
            if match:
                device = _device_of(match["params"])
                label = _finding_label(text)
                cell = (match["capability"], device or "")
                if label not in finding_of[cell]:
                    finding_of[cell].append(label)
    symbols = {
        PASS: "✅ pass",
        FAIL: "❌ **FAIL**",
        NOT_APPLICABLE: "n/a",
        GAP: "🕳️ **GAP**",
    }
    lines = ["## Devices against capabilities", ""]
    header = ["Capability", *DEVICES.values()]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    section = None
    for name, entry in CAPABILITIES.items():
        if entry.section != section:
            section = entry.section
            lines.append(f"| **{section}** |" + " |" * len(DEVICES))
        cells = []
        for device in DEVICES:
            status = matrix.cells[(name, device)]
            if status == FINDING:
                labels = ", ".join(finding_of.get((name, device), ["finding"]))
                passed, total = matrix.counts[(name, device)]
                cells.append(f"⚠️ {labels} ({passed} of {total} cases pass)")
            else:
                cells.append(symbols[status])
        lines.append(f"| {entry.title} (`{name}`) | " + " | ".join(cells) + " |")
    lines.append("")
    legend = (
        "n/a: the capability does not apply to the device; the reason is the skip message of "
        "its test and is listed in `tests/devices/capabilities.py`. GAP: a case the registry "
        "declares for the device did not run."
    )
    if matrix.findings:
        legend += (
            " ⚠️ Fn (k of n cases pass): the other cases are an open finding, each a test that "
            "asserts the concept and is a strict expected failure until the fix; the finding's "
            "cases are kept to the assertions the finding breaks."
        )
    lines.append(legend)
    if matrix.findings:
        lines += ["", "### Findings", ""]
        for text in sorted(matrix.findings, key=_finding_label):
            lines.append(f"- {text}")
    if matrix.gaps:
        lines += ["", "### Gaps", ""]
        lines += [
            f"- `{capability}` has no test for {test_id}" for capability, test_id in matrix.gaps
        ]
    if matrix.failures:
        lines += ["", "### Failures", ""]
        lines += [f"- `{test}`" for test in matrix.failures]
    if matrix.unknown:
        lines += ["", "### Device tests not in the matrix", ""]
        lines += [f"- `{test}`" for test in matrix.unknown]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    """Print the matrix; exit 1 on a gap, a failure, or a test outside the registry."""
    if len(argv) != 1:
        print("usage: python -m tests.devices.matrix <junit.xml>", file=sys.stderr)  # noqa: T201
        return 2
    junit = Path(argv[0])
    if not junit.is_file():
        print(f"## Devices against capabilities\n\nNo test results: {junit} is missing.")  # noqa: T201
        return 1
    matrix = build(junit)
    print(render(matrix), end="")  # noqa: T201
    return 1 if matrix.gaps or matrix.failures or matrix.unknown else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
