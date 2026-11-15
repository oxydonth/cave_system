"""The tests in this directory, like the rest of ``tests/ops``, never touch Home Assistant.

``tests/ops/test_isolation.py`` checks the files directly in ``tests/ops``; this checks the ones
here, so that they too run on Python 3.11 with only pytest installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = ("homeassistant", "custom_components", "pytest_homeassistant_custom_component")


def test_these_tests_import_nothing_from_home_assistant():
    files = list(Path(__file__).parent.glob("*.py"))
    assert len(files) >= 7

    for path in files:
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        assert not found & set(FORBIDDEN), f"{path.name} imports {sorted(found & set(FORBIDDEN))}"
