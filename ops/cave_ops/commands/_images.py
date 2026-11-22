"""Removing the container images of releases that are no longer needed (``cave update``).

Each release pulls its images and nothing removes them, so after many releases the disk check of
``update`` would refuse every update. ``prune`` removes them, and nothing else:

* Candidates are only images of the repositories that this project's Compose files
  (``compose.yaml``, ``compose.sim.yaml``) name, in the checked-out release or the previous one.
* Kept are the images that those two releases name (the running one, and the one before it that
  the last update's snapshot belongs to), and any image ``compose.override.yaml`` names.
* Each image is removed by its name with ``docker image rm``, never with ``--force``: Docker then
  refuses an image that a container still uses. Never ``docker image prune -a``, never
  ``docker system prune``; nothing else on the host is touched.

Nothing is removed unless the caller holds the lock (only ``update`` calls it, at the end of its
run), no update is in flight, no restore is unfinished (its marker, half-way or start owed), and
the stack runs and is healthy. Without a record of the previous release (no update has installed
one since this was added), or when an ``image:`` line cannot be read, nothing is removed either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..context import Context
from ..health import check_health
from . import _server
from . import _update_state as state

COMPOSE_FILES = ("compose.yaml", "compose.sim.yaml")
OVERRIDE = "compose.override.yaml"
DOCKER_TIMEOUT = 120.0
_IMAGE = re.compile(r"^[ \t]*image:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.MULTILINE)


@dataclass
class Pruned:
    """What ``prune`` did: ``skipped`` says why it removed nothing, if it did not try."""

    removed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    skipped: str | None = None


def normalise(reference: str) -> str:
    """``reference`` as ``docker image ls`` names it: ``repository:tag`` or
    ``repository@digest``; Docker Hub's ``docker.io/`` and ``library/`` left out, ``:latest`` if
    no tag is given."""
    name, at, digest = reference.partition("@")
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if name.startswith("library/") and name.count("/") == 1:
        name = name[len("library/") :]
    if at:
        repository = name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name
        return f"{repository}@{digest}"
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    return name


def repository(reference: str) -> str:
    """The repository of a normalised reference."""
    if "@" in reference:
        return reference.split("@", 1)[0]
    return reference.rsplit(":", 1)[0]


def image_references(text: str) -> set[str] | None:
    """The normalised images a Compose file names; ``None`` when one cannot be read without
    Compose (a variable in it), so that nothing is removed on a guess."""
    found: set[str] = set()
    for match in _IMAGE.finditer(text):
        value = match.group(1).strip().strip("'\"")
        if not value or "$" in value:
            return None
        found.add(normalise(value))
    return found


def _why_not(ctx: Context) -> str | None:
    if state.load(ctx).switch is not None:
        return "an update is interrupted"
    if _server.ops_dir(ctx).joinpath(_server.RESTORE_MARKER).exists():
        return "a restore is unfinished"
    if not check_health(ctx).healthy:
        return "the stack is not running and healthy"
    return None


def _named(ctx: Context, commit: str) -> tuple[set[str], set[str]] | str:
    """The images the Compose files of ``commit`` name, and their repositories; or why not."""
    images: set[str] = set()
    for name in COMPOSE_FILES:
        shown = _server.git(ctx, "show", f"{commit}:{name}")
        if not shown.ok:
            if name == "compose.yaml":
                return f"the Compose file of {commit[:12]} could not be read"
            continue  # an older release without the simulator overlay
        found = image_references(shown.stdout)
        if found is None:
            return f"an image in {name} of {commit[:12]} names a variable"
        images |= found
    return images, {repository(image) for image in images}


def prune(ctx: Context, previous: str | None) -> Pruned:
    """Remove the images described in the module's documentation. Never raises for Docker or
    Git failing: it then removes nothing and says why."""
    why = _why_not(ctx)
    if why is None and not previous:
        why = "the previous release is not recorded yet (the next installed update records it)"
    if why is not None:
        return Pruned(skipped=why)
    keep: set[str] = set()
    repositories: set[str] = set()
    for commit in ("HEAD", previous or "HEAD"):
        named = _named(ctx, commit)
        if isinstance(named, str):
            return Pruned(skipped=named)
        keep |= named[0]
        repositories |= named[1]
    override = ctx.paths.root / OVERRIDE
    if override.is_file():
        found = image_references(override.read_text(encoding="utf-8", errors="replace"))
        if found is None:
            return Pruned(skipped=f"an image in {OVERRIDE} names a variable")
        keep |= found

    listing = ctx.docker(
        "image", "ls", "--format", "{{.Repository}}\t{{.Tag}}\t{{.Digest}}", timeout=DOCKER_TIMEOUT
    )
    if not listing.ok:
        return Pruned(skipped=f"Docker could not list the images: {listing.output.strip()[-200:]}")
    result = Pruned()
    seen: set[str] = set()
    for line in listing.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in repositories:
            continue
        name, tag, digest = fields
        if tag and tag != "<none>":
            reference = f"{name}:{tag}"
        elif digest and digest != "<none>":
            reference = f"{name}@{digest}"
        else:
            continue
        if reference in keep or reference in seen:
            continue
        seen.add(reference)
        removed = ctx.docker("image", "rm", reference, timeout=DOCKER_TIMEOUT)
        (result.removed if removed.ok else result.refused).append(reference)
    return result
