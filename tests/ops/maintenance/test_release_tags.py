"""The release job's decision (``.github/workflows/release.yml``), on real Git repositories.

The job runs ``python3 -m cave_ops.commands._releases plan --head SHA --date YYYY-MM-DD`` in a
full clone; these tests run the same entry point in a repository built for each case.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cave_ops.commands import _releases

TODAY = "2026-11-15"


def git(repo, *args):
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def commit(repo, message):
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def plan(repo, head, date=TODAY):
    """Run the job's command exactly as the workflow does and parse what it prints."""
    ops = Path(__file__).resolve().parents[3] / "ops"
    command = [sys.executable, "-m", "cave_ops.commands._releases"]
    done = subprocess.run(
        [*command, "plan", "--head", head, "--date", date],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(ops)},
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_first_release_of_a_day(repo):
    commit(repo, "yesterday")
    git(repo, "tag", "v2026.11.14.1")
    git(repo, "tag", "v2026.11.14.2")
    head = commit(repo, "today")

    assert plan(repo, head) == {"tag": "v2026.11.15.1", "reason": f"{head} becomes v2026.11.15.1"}


def test_the_very_first_release(repo):
    head = commit(repo, "initial")

    assert plan(repo, head)["tag"] == "v2026.11.15.1"


def test_a_second_release_the_same_day(repo):
    commit(repo, "morning")
    git(repo, "tag", "v2026.11.15.1")
    head = commit(repo, "afternoon")

    assert plan(repo, head)["tag"] == "v2026.11.15.2"


def test_the_tenth_release_of_a_day_counts_numerically(repo):
    for number in range(1, 11):
        commit(repo, f"change {number}")
        git(repo, "tag", f"v2026.11.15.{number}")
    head = commit(repo, "eleventh")

    assert plan(repo, head)["tag"] == "v2026.11.15.11"


def test_a_commit_that_is_already_released_gets_nothing(repo):
    head = commit(repo, "released")
    git(repo, "tag", "v2026.11.15.1")

    assert plan(repo, head) == {
        "tag": "",
        "reason": f"{head} is already released as v2026.11.15.1",
    }


def test_an_annotated_release_tag_counts_as_released(repo):
    head = commit(repo, "released")
    git(repo, "tag", "-a", "-m", "release", "v2026.11.15.1")

    assert plan(repo, head)["tag"] == ""


def test_a_commit_contained_in_a_newer_release_gets_nothing(repo):
    older = commit(repo, "first merge")
    commit(repo, "second merge")
    git(repo, "tag", "v2026.11.15.1")  # the second merge's run finished first

    result = plan(repo, older)

    assert result["tag"] == ""
    assert result["reason"] == f"{older} is already part of the newer release v2026.11.15.1"


def test_other_tags_are_ignored(repo):
    commit(repo, "old")
    git(repo, "tag", "v1.0")
    git(repo, "tag", "v2026.13.40.1")  # not a date
    head = commit(repo, "new")

    assert plan(repo, head)["tag"] == "v2026.11.15.1"


def test_versions_order_by_date_then_number():
    names = ["v2026.11.15.10", "v2026.11.15.9", "v2026.12.01.1", "v2025.12.31.3", "nonsense"]

    assert [v.tag for v in _releases.releases(names)] == [
        "v2025.12.31.3",
        "v2026.11.15.9",
        "v2026.11.15.10",
        "v2026.12.01.1",
    ]
    assert _releases.next_tag(names, datetime.date(2026, 11, 15)) == "v2026.11.15.11"
    assert _releases.next_tag(names, datetime.date(2026, 11, 16)) == "v2026.11.16.1"
