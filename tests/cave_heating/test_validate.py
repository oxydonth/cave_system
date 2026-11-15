"""The validator of concept section 4.5, run the way the cave command runs it.

``python -m custom_components.cave_heating.validate <file>`` in Home Assistant's configuration
directory, without Home Assistant running. The configuration directory here is laid out like
``/config`` in the container: the integration under ``custom_components/cave_heating``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import custom_components.cave_heating as integration

REPOSITORY_INTEGRATION = Path(integration.__file__).parent
EXAMPLE = Path(__file__).with_name("fixtures") / "versions" / "v1.yaml"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A configuration directory with the integration in it and a cave/ directory."""
    (tmp_path / "custom_components").mkdir()
    (tmp_path / "custom_components" / "cave_heating").symlink_to(REPOSITORY_INTEGRATION)
    (tmp_path / "cave").mkdir()
    return tmp_path


def run(config_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the validator with the configuration directory as the working directory."""
    return subprocess.run(  # noqa: S603  (a fixed command line)
        [sys.executable, "-m", "custom_components.cave_heating.validate", *args],
        cwd=config_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        # A clean environment: only the working directory puts the integration on the path,
        # as in the container.
        env={"PATH": "/usr/bin:/bin"},
    )


def test_valid_file_exits_0_and_says_so_on_standard_output(config_dir: Path) -> None:
    """Exit status 0 only for a valid file."""
    (config_dir / "cave" / "heating.yaml").write_text(EXAMPLE.read_text())
    result = run(config_dir, "cave/heating.yaml")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "cave/heating.yaml is valid: version 1, 2 rooms, 3 thermostats.\n"
    assert result.stderr == ""


def test_starter_file_without_rooms_is_valid(config_dir: Path) -> None:
    """The starter written by cave init has no rooms."""
    (config_dir / "cave" / "heating.yaml").write_text("version: 1\nrooms: []\n")
    result = run(config_dir, "cave/heating.yaml")
    assert result.returncode == 0
    assert "0 rooms" in result.stdout


def test_invalid_file_exits_1_with_one_sentence_per_problem_on_standard_error(
    config_dir: Path,
) -> None:
    """The messages name the room and the field."""
    (config_dir / "cave" / "heating.yaml").write_text(
        "version: 1\n"
        "rooms:\n"
        "  - id: living_room\n"
        "    name: Wohnzimmer\n"
        "    trvs:\n"
        "      - entity: clima.wohnzimmer\n"
        "  - id: bathroom\n"
        "    name: Bad\n"
        "    trvs:\n"
        "      - entity: climate.bad\n"
        '    window_open_delay: "5 min"\n'
    )
    result = run(config_dir, "cave/heating.yaml")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "cave/heating.yaml is not valid (2 problems):",
        'Room "living_room" (rooms[0]), field "trvs[0].entity": must be an entity of the domain '
        'climate, but is "clima.wohnzimmer".',
        'Room "bathroom" (rooms[1]), field "window_open_delay": must be a duration such as '
        '"00:05:00" (hours:minutes:seconds) or a number of seconds, but is "5 min".',
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "The file cave/heating.yaml does not exist."),
        ("version: 1\nrooms: [\n", "The file is not valid YAML: "),
        ("", 'The file is empty; it needs at least "version: 1".'),
    ],
)
def test_missing_broken_or_empty_file_exits_1(
    config_dir: Path, content: str | None, message: str
) -> None:
    """A file that cannot be read is not valid either."""
    if content is not None:
        (config_dir / "cave" / "heating.yaml").write_text(content)
    result = run(config_dir, "cave/heating.yaml")
    assert result.returncode == 1
    first, problem = result.stderr.splitlines()
    assert first == "cave/heating.yaml is not valid (1 problem):"
    assert problem.startswith(message)


@pytest.mark.parametrize("args", [(), ("a.yaml", "b.yaml"), ("--help",)])
def test_wrong_arguments_exit_2(config_dir: Path, args: tuple[str, ...]) -> None:
    """Called wrongly: the usage, and neither 0 nor 1."""
    result = run(config_dir, *args)
    assert result.returncode == 2
    assert result.stderr.startswith("Usage: python -m custom_components.cave_heating.validate")


def test_main_in_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The same entry point in-process, so coverage sees it."""
    from custom_components.cave_heating.validate import main

    good = tmp_path / "good.yaml"
    good.write_text("version: 1\n")
    assert main([str(good)]) == 0
    assert capsys.readouterr().out == f"{good} is valid: version 1, 0 rooms, 0 thermostats.\n"
    assert main([str(tmp_path / "missing.yaml")]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main([]) == 2
