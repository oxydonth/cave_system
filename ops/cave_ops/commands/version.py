"""``cave version``: the release that is checked out and the image versions."""

from __future__ import annotations

import argparse
import re

from ..context import Context

HELP = "Print the release that is checked out and the image versions"

_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)


def images(compose_text: str) -> list[str]:
    """The ``image:`` entries of a Compose file, without digest."""
    return [match.group(1).split("@")[0] for match in _IMAGE.finditer(compose_text)]


def run(ctx: Context, args: argparse.Namespace) -> int:
    release = ctx.git("describe", "--tags", "--always", "--dirty")
    ctx.say(f"release: {release.stdout.strip() if release.ok else 'unknown (not a Git checkout)'}")
    try:
        text = ctx.paths.compose_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        ctx.warn(f"{ctx.paths.compose_file} not found")
        return 1
    for image in images(text):
        ctx.say(f"image:   {image}")
    return 0
