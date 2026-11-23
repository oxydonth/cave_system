"""Removing the container images of releases that are no longer needed (``cave update``).

Each release pulls its images and nothing removes them, so after many releases the disk check of
``update`` would refuse every update. ``prune`` removes them:

* Candidates are every image of the repositories that this project's Compose files
  (``compose.yaml``, ``compose.sim.yaml``) name, in the releases it keeps (below), whoever pulled
  it: also an image of such a repository that other software on the server pulled. Images of
  other repositories are never touched.
* Kept are the images that these releases name: the checked-out one; the one the last installed
  update came from (``previous`` in the update record); and the one the last switch started
  from (``last_switch``), which a way back that was not clean tells the owner to check out by
  hand. Also kept is any image ``compose.override.yaml`` names. An image line is kept by its tag
  and, for a digest pin (``name:tag@sha256:…``), by its digest too.
* Each image is removed by its name with ``docker image rm``, never with ``--force``: Docker then
  refuses an image that a container still uses, also a stopped one. Never
  ``docker image prune -a``, never ``docker system prune``.

Nothing is removed unless the caller holds the lock (only ``update`` calls it, at the end of its
run); no update is in flight; no restore is unfinished (its marker, half-way or start owed); the
last update did not end ``broken`` (exit status 3: the owner may have to put the previous release
back by hand, and needs its images offline); and the stack runs and is healthy. Without a record
of the previous release (no update has installed one since this was added), or when an
``image:`` line cannot be read, nothing is removed either.
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
LISTING = "{{.Repository}}\t{{.Tag}}\t{{.Digest}}"
"""What ``docker image ls --digests`` prints per image: one line per repository and tag (an
image pulled only by its digest has the tag ``<none>``), its digest in the last column."""
_IMAGE = re.compile(r"^[ \t]*image:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.MULTILINE)


@dataclass
class Pruned:
    """What ``prune`` did: ``skipped`` says why it removed nothing, if it did not try."""

    removed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    skipped: str | None = None


def normalise(reference: str) -> set[str]:
    """``reference`` as ``docker image ls`` can name it: ``repository:tag``, and for a digest
    ``repository@digest`` (a pin ``name:tag@digest`` gives both); Docker Hub's ``docker.io/`` and
    ``library/`` left out, ``:latest`` if neither tag nor digest is given."""
    name, at, digest = reference.partition("@")
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if name.startswith("library/") and name.count("/") == 1:
        name = name[len("library/") :]
    tagged = ":" in name.rsplit("/", 1)[-1]
    repository_name = name.rsplit(":", 1)[0] if tagged else name
    names = {f"{repository_name}@{digest}"} if at else set()
    if tagged:
        names.add(name)
    elif not at:
        names.add(f"{name}:latest")
    return names


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
        found |= normalise(value)
    return found


def _why_not(ctx: Context, record: state.UpdateRecord) -> str | None:
    if record.switch is not None:
        return "an update is interrupted"
    if _server.ops_dir(ctx).joinpath(_server.RESTORE_MARKER).exists():
        return "a restore is unfinished"
    if (record.last_result or {}).get("outcome") == "broken":
        return (
            "the last update ended with exit status 3, and putting the previous release back by "
            "hand needs its images; they stay until an update ends otherwise"
        )
    if not record.previous:
        return "the previous release is not recorded yet (the next installed update records it)"
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


def kept_releases(record: state.UpdateRecord) -> list[str]:
    """The commits whose images are kept: the checkout, the previous release, the last switch's
    starting point."""
    commits = ["HEAD"]
    for entry in (record.previous, record.last_switch):
        commit = (entry or {}).get("commit")
        if commit and commit not in commits:
            commits.append(commit)
    return commits


def prune(ctx: Context) -> Pruned:
    """Remove the images described in the module's documentation. Never raises for Docker or
    Git failing: it then removes nothing and says why."""
    record = state.load(ctx)
    why = _why_not(ctx, record)
    if why is not None:
        return Pruned(skipped=why)
    keep: set[str] = set()
    repositories: set[str] = set()
    for commit in kept_releases(record):
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

    listing = ctx.docker("image", "ls", "--digests", "--format", LISTING, timeout=DOCKER_TIMEOUT)
    if not listing.ok:
        return Pruned(skipped=f"Docker could not list the images: {listing.output.strip()[-200:]}")
    result = Pruned()
    seen: set[str] = set()
    for line in listing.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in repositories:
            continue
        name, tag, digest = fields
        by_tag = f"{name}:{tag}" if tag and tag != "<none>" else None
        by_digest = f"{name}@{digest}" if digest.startswith("sha256:") else None
        if (by_tag is not None and by_tag in keep) or (by_digest is not None and by_digest in keep):
            continue
        reference = by_tag or by_digest
        if reference is None or reference in seen:
            continue
        seen.add(reference)
        removed = ctx.docker("image", "rm", reference, timeout=DOCKER_TIMEOUT)
        (result.removed if removed.ok else result.refused).append(reference)
    return result
