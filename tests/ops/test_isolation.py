"""``ops/`` uses the standard library only; ``tests/ops`` never touches Home Assistant.

Both must hold so that the command and its tests run on a server's own Python 3.11 with nothing
installed (``make test-py311`` runs the tests in an environment with only pytest and PyYAML).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_IN_TESTS = ("homeassistant", "custom_components", "pytest_homeassistant_custom_component")


def imported_modules(path: Path) -> set[str]:
    """Top-level names of everything a Python file imports; relative imports are not included."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_ops_imports_only_the_standard_library(repo_root):
    files = [*(repo_root / "ops").rglob("*.py"), repo_root / "cave"]
    assert len(files) > 10

    for path in files:
        outside = {
            name
            for name in imported_modules(path)
            if name not in sys.stdlib_module_names and name not in ("cave_ops", "__future__")
        }
        assert not outside, f"{path.relative_to(repo_root)} imports {sorted(outside)}"


def test_tests_of_the_cave_command_import_nothing_from_home_assistant(repo_root):
    files = list((repo_root / "tests" / "ops").glob("*.py"))
    assert len(files) > 5

    for path in files:
        found = imported_modules(path) & set(FORBIDDEN_IN_TESTS)
        assert not found, f"{path.name} imports {sorted(found)}"


def test_the_demo_takes_the_dashboard_paths_from_a_core_module(repo_root):
    """Issue #16: `cave demo` imported three path constants from the `apply` command."""
    tree = ast.parse((repo_root / "ops/cave_ops/commands/demo.py").read_text(encoding="utf-8"))
    from_commands = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
    }

    assert "apply" not in from_commands
