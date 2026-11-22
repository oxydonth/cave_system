"""The matrix script and the capability registry it reads. No Home Assistant needed."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.devices import matrix
from tests.devices.capabilities import CAPABILITIES, DEVICES, check_registry

HERE = Path(__file__).parent


def _case(name: str, result: str = "", module: str = "test_thermostats") -> str:
    return (
        f'<testcase classname="tests.devices.{module}" name="{name}" time="0.1">{result}</testcase>'
    )


NA = '<skipped type="pytest.skip" message="n/a: a sensor, not a thermostat"/>'


def _junit(tmp_path: Path, *cases: str) -> Path:
    path = tmp_path / "results.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(cases)
        + _case("test_something_else", module="other")
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _all_passing() -> list[str]:
    """Every case the registry declares, passing; the inapplicable ones skipped as n/a."""
    cases = []
    for name, entry in CAPABILITIES.items():
        for device in DEVICES:
            if device not in entry.applies:
                cases.append(_case(f"test_{name}[{device}]", NA))
            cases.extend(_case(f"test_{name}[{test_id}]") for test_id in entry.test_ids(device))
    return cases


def test_registry_explains_every_device_it_does_not_apply_to() -> None:
    assert check_registry() == []


def test_every_capability_has_a_test_function() -> None:
    """A capability without a test function would be a gap in every column it applies to."""
    functions = {
        node.name
        for path in HERE.glob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert {f"test_{name}" for name in CAPABILITIES} <= functions


def test_all_passing_gives_no_gap_and_exit_status_zero(tmp_path: Path, capsys) -> None:
    junit = _junit(tmp_path, *_all_passing())
    assert matrix.main([str(junit)]) == 0
    out = capsys.readouterr().out
    assert "| Discovery: entities, entity IDs, range and step (`discovery`) | ✅ pass" in out
    assert "**GAP**" not in out
    assert "FAIL" not in out


def test_a_missing_test_is_a_gap_not_a_blank(tmp_path: Path, capsys) -> None:
    cases = [case for case in _all_passing() if "test_room_away[BTH-RA]" not in case]
    junit = _junit(tmp_path, *cases)
    assert matrix.main([str(junit)]) == 1
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if "(`room_away`)" in line)
    cells = [cell.strip() for cell in row.strip("|").split("|")][1:]
    assert cells[list(DEVICES).index("BTH-RA")] == "🕳️ **GAP**"
    assert "- `room_away` has no test for BTH-RA" in out


def test_a_missing_variant_is_a_gap(tmp_path: Path, capsys) -> None:
    """The variants are the unconfirmed hardware behaviours: dropping one must show."""
    dropped = "test_fritz_window_detection[FRITZ-302-target-off]"
    cases = [case for case in _all_passing() if dropped not in case]
    assert matrix.main([str(_junit(tmp_path, *cases))]) == 1
    out = capsys.readouterr().out
    assert "- `fritz_window_detection` has no test for FRITZ-302-target-off" in out


def test_an_undeclared_variant_is_reported(tmp_path: Path, capsys) -> None:
    extra = _case("test_fritz_window_detection[FRITZ-302-target-purple]")
    assert matrix.main([str(_junit(tmp_path, *_all_passing(), extra))]) == 1
    assert "FRITZ-302-target-purple" in capsys.readouterr().out


def test_a_skip_for_another_reason_is_a_gap(tmp_path: Path) -> None:
    other = '<skipped type="pytest.skip" message="no network"/>'
    cases = [
        _case("test_room_away[BTH-RA]", other) if "test_room_away[BTH-RA]" in case else case
        for case in _all_passing()
    ]
    built = matrix.build(_junit(tmp_path, *cases))
    assert ("room_away", "BTH-RA") in built.gaps


def test_variants_failures_and_findings(tmp_path: Path, capsys) -> None:
    failure = '<failure message="assert 1 == 2">AssertionError</failure>'
    finding = '<skipped type="pytest.xfail" message="finding: F9: something is off"/>'
    replaced = {
        "test_discovery[TRVZB]": _case("test_discovery[TRVZB]", failure),
        "test_mode_changed_on_device_is_put_back[BTH-RA-schedule]": _case(
            "test_mode_changed_on_device_is_put_back[BTH-RA-schedule]", finding
        ),
    }
    cases = []
    for case in _all_passing():
        name = case.split(' name="')[1].split('"')[0]
        cases.append(replaced.get(name, case))
    assert matrix.main([str(_junit(tmp_path, *cases))]) == 1
    out = capsys.readouterr().out
    assert "❌ **FAIL**" in out
    assert "- `tests.devices.test_thermostats::test_discovery[TRVZB]`" in out
    assert "⚠️ F9 (1 of 2 cases pass)" in out
    assert "- F9: something is off" in out


def test_a_device_test_outside_the_registry_is_reported(tmp_path: Path, capsys) -> None:
    junit = _junit(tmp_path, *_all_passing(), _case("test_teleportation[TRVZB]"))
    assert matrix.main([str(junit)]) == 1
    assert "test_teleportation[TRVZB]" in capsys.readouterr().out


def test_missing_results_fail(tmp_path: Path, capsys) -> None:
    assert matrix.main([str(tmp_path / "nothing.xml")]) == 1
    assert "No test results" in capsys.readouterr().out
