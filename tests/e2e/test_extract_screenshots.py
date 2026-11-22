"""The screenshots that go into the job log come back out of it unchanged."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e import extract_screenshots

HERE = Path(__file__).resolve().parent


def _printed(directory: Path) -> str:
    return subprocess.run(
        ["bash", str(HERE / "print-screenshots.sh"), str(directory)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def shots(tmp_path: Path) -> Path:
    directory = tmp_path / "log"
    directory.mkdir()
    (directory / "desktop-rooms.jpg").write_bytes(os.urandom(30_001))
    (directory / "phone-rooms.jpg").write_bytes(b"\xff\xd8 small \xff\xd9")
    return directory


def test_screenshots_survive_the_log(shots: Path) -> None:
    printed = _printed(shots)
    assert "-----BEGIN SCREENSHOT desktop-rooms.jpg (30001 bytes, base64)-----" in printed
    assert all(len(line) <= 100 for line in printed.splitlines())
    # As GitHub shows them: behind a timestamp, in `gh run view --log` also a job and a step.
    stamped = "\n".join(f"2026-11-22T12:00:00.1234567Z {line}" for line in printed.splitlines())
    cli = "\n".join(f"e2e\tPrint screenshots\t{line}" for line in stamped.splitlines())
    for log in (printed, stamped, cli):
        found = extract_screenshots.extract(log)
        assert found == {path.name: path.read_bytes() for path in shots.iterdir()}


def test_a_cut_off_log_is_an_error(shots: Path) -> None:
    printed = _printed(shots)
    cut = printed[: printed.rindex("-----END")]
    with pytest.raises(ValueError, match="ends inside the block"):
        extract_screenshots.extract(cut)


def test_without_screenshots_the_script_says_so(tmp_path: Path) -> None:
    assert "No screenshots" in _printed(tmp_path)
