"""Turns the screenshots that ``print-screenshots.sh`` printed into a job log back into files.

Reads a log (standard input, or the file named) and writes each screenshot found between its
``-----BEGIN SCREENSHOT name (...)-----`` and ``-----END SCREENSHOT name-----`` lines into the
directory given. Whatever GitHub put in front of each line (a timestamp, and with
``gh run view --log`` the job and step names) is ignored. Standard library only::

    gh run view RUN_ID --job JOB_ID --log | python3 tests/e2e/extract_screenshots.py OUT_DIR
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import sys
from pathlib import Path

BEGIN = re.compile(r"-----BEGIN SCREENSHOT (?P<name>[\w.-]+) \(")
END = re.compile(r"-----END SCREENSHOT (?P<name>[\w.-]+)-----")
# What GitHub puts before a line: `2026-11-22T12:00:00.1234567Z `, and in `gh run view --log`
# `job<TAB>step<TAB>` before that.
PREFIX = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=]+$")


def extract(log: str) -> dict[str, bytes]:
    """The screenshots in ``log``, by file name. A block that is cut off or damaged is an error."""
    found: dict[str, bytes] = {}
    name: str | None = None
    parts: list[str] = []
    for raw in log.splitlines():
        line = PREFIX.sub("", raw).strip()
        begin = BEGIN.search(line)
        if begin:
            name, parts = begin["name"], []
            continue
        end = END.search(line)
        if end and name is not None:
            if end["name"] != name:
                raise ValueError(f"the block of {name} ends as {end['name']}")
            try:
                found[name] = base64.b64decode("".join(parts), validate=True)
            except binascii.Error as error:
                raise ValueError(f"the block of {name} is not valid base64: {error}") from error
            name = None
        elif name is not None and BASE64_LINE.match(line):
            parts.append(line)
    if name is not None:
        raise ValueError(f"the log ends inside the block of {name}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("out", type=Path, help="the directory to write the pictures to")
    parser.add_argument("log", nargs="?", help="the log file; standard input when omitted")
    args = parser.parse_args(argv)
    text = Path(args.log).read_text(encoding="utf-8") if args.log else sys.stdin.read()
    try:
        screenshots = extract(text)
    except ValueError as error:
        print(f"extract_screenshots: {error}", file=sys.stderr)  # noqa: T201
        return 1
    if not screenshots:
        print("extract_screenshots: no screenshot in this log", file=sys.stderr)  # noqa: T201
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in screenshots.items():
        (args.out / name).write_bytes(data)
        print(f"{args.out / name} ({len(data)} bytes)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
