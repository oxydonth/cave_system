"""Release tags ``vYYYY.MM.DD.N``: parsing, ordering, and planning the next one.

Used by ``update`` and ``status`` to compare releases, and by the pipeline's release job
(``.github/workflows/release.yml``), which runs

    PYTHONPATH=ops python3 -m cave_ops.commands._releases plan --head SHA --date YYYY-MM-DD

in a full clone and appends the printed ``tag=`` and ``reason=`` lines to ``$GITHUB_OUTPUT``. An
empty ``tag`` means: create nothing. The decision is ``plan_release``; the rest only asks Git.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

TAG_PATTERN = "v*"
_TAG = re.compile(r"^v(\d{4})\.(\d{2})\.(\d{2})\.([1-9]\d*)$")


@dataclass(frozen=True, order=True)
class Version:
    """A release, ordered by date and then by its number on that day."""

    year: int
    month: int
    day: int
    number: int

    @property
    def tag(self) -> str:
        return f"v{self.year:04d}.{self.month:02d}.{self.day:02d}.{self.number}"

    def __str__(self) -> str:
        return self.tag


def parse_tag(name: str) -> Version | None:
    """The version of a release tag, or ``None`` for any other tag."""
    match = _TAG.match(name.strip())
    if match is None:
        return None
    year, month, day, number = (int(part) for part in match.groups())
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return Version(year, month, day, number)


def releases(names: Iterable[str]) -> list[Version]:
    """The release tags among ``names``, oldest first."""
    return sorted(version for version in map(parse_tag, names) if version is not None)


def newest(names: Iterable[str]) -> Version | None:
    found = releases(names)
    return found[-1] if found else None


def next_tag(existing: Iterable[str], today: datetime.date) -> str:
    """The tag of the next release made on ``today``: ``.1``, or one more than the day's highest."""
    numbers = [
        v.number
        for v in releases(existing)
        if (v.year, v.month, v.day) == (today.year, today.month, today.day)
    ]
    return Version(today.year, today.month, today.day, max(numbers, default=0) + 1).tag


@dataclass(frozen=True)
class ReleasePlan:
    """What the release job does: create ``tag``, or nothing (``tag`` is ``None``) and why."""

    tag: str | None
    reason: str


def plan_release(
    head: str,
    tags: Mapping[str, str],
    today: datetime.date,
    contains: Callable[[str, str], bool],
) -> ReleasePlan:
    """Decide the release for commit ``head``.

    ``tags`` maps every tag name to the commit it points at. ``contains(newer, older)`` says
    whether commit ``newer`` has ``older`` in its history. Nothing is released when ``head``
    already has a release tag, or when the newest release already contains ``head`` (a later
    commit's run finished first; releasing ``head`` then would give older code a newer tag).
    """
    versions = {v: commit for name, commit in tags.items() if (v := parse_tag(name)) is not None}
    released = sorted(v for v, commit in versions.items() if commit == head)
    if released:
        return ReleasePlan(None, f"{head} is already released as {released[-1]}")
    if versions:
        latest = max(versions)
        if contains(versions[latest], head):
            return ReleasePlan(None, f"{head} is already part of the newer release {latest}")
    tag = next_tag(tags, today)
    return ReleasePlan(tag, f"{head} becomes {tag}")


# ---------------------------------------------------------------------------- command line (CI)
def _git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _tag_commits() -> dict[str, str]:
    """Every tag with the commit it points at, peeling annotated tags."""
    done = _git(
        ["tag", "--list", TAG_PATTERN, "--format=%(refname:short) %(objectname) %(*objectname)"]
    )
    if done.returncode != 0:
        raise SystemExit(f"git tag failed: {done.stderr.strip()}")
    commits: dict[str, str] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            commits[parts[0]] = parts[2] if len(parts) == 3 else parts[1]
    return commits


def _contains(newer: str, older: str) -> bool:
    return _git(["merge-base", "--is-ancestor", older, newer]).returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m cave_ops.commands._releases")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="print the tag the release job creates, if any")
    plan.add_argument("--head", required=True, help="the commit to release")
    plan.add_argument("--date", required=True, help="today, YYYY-MM-DD (UTC)")
    args = parser.parse_args(argv)

    head = _git(["rev-parse", "--verify", f"{args.head}^{{commit}}"])
    if head.returncode != 0:
        raise SystemExit(f"unknown commit {args.head}: {head.stderr.strip()}")
    result = plan_release(
        head.stdout.strip(),
        _tag_commits(),
        datetime.date.fromisoformat(args.date),
        _contains,
    )
    sys.stdout.write(f"tag={result.tag or ''}\nreason={result.reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
