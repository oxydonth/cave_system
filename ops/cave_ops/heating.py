"""The home description: validating ``heating.yaml`` and storing its applied copy.

Concept section 4.5: the edited file is ``cave/heating.yaml``; the applied copy
``cave/.applied/heating.yaml`` is the only file ``cave_heating`` reads. It is written only by
``store_applied``, and everything but ``init`` reaches it through ``validate_and_store``
(``apply`` and ``demo`` use it).

The validator is ``python -m custom_components.cave_heating.validate <file>``, run in a one-off
Home Assistant container with ``/config`` as the working directory. This module only runs it
through the runner and never imports it.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .container import run_in_homeassistant
from .paths import CONTAINER_CONFIG
from .runner import Result

if TYPE_CHECKING:
    from .context import Context

VALIDATOR_MODULE = "custom_components.cave_heating.validate"
STAGED_PREFIX = ".validating-"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of ``validate_and_store``."""

    ok: bool
    """The file is valid. When false nothing was stored."""
    messages: tuple[str, ...] = ()
    """What the validator said: the errors naming room and field, or empty."""
    changed: bool = False
    """The applied copy now differs from what it was."""
    applied_path: Path | None = None
    """The applied copy on the host (``ctx.paths.heating_applied``), set when ``ok``."""


def store_applied(ctx: Context, source: Path | bytes) -> bool:
    """Store ``source`` (a file, or the bytes themselves) as the applied copy, atomically and
    without validating. Returns whether the applied copy changed.

    Does nothing, not even touching the modification time, when the content is the same.
    Callers other than ``init`` must validate first: use ``validate_and_store``.
    """
    target = ctx.paths.heating_applied
    content = source if isinstance(source, bytes) else source.read_bytes()
    if target.is_file() and target.read_bytes() == content:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".heating-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return True


def _validate_snapshot(ctx: Context, content: bytes, label: str) -> tuple[Result, tuple[str, ...]]:
    """Validate ``content`` by running the validator on a staged copy of exactly these bytes.

    The staged file has a name of its own for every run, so two runs at the same time (``apply``
    does not hold the lock) never validate each other's bytes or delete each other's file.
    """
    cave_dir = ctx.paths.ha_config / "cave"
    cave_dir.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(dir=cave_dir, prefix=STAGED_PREFIX, suffix=".yaml")
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(staged, 0o644)
        container_file = f"{CONTAINER_CONFIG}/cave/{staged.name}"
        result = run_in_homeassistant(
            ctx, ["-m", VALIDATOR_MODULE, container_file], workdir=CONTAINER_CONFIG
        )
    finally:
        staged.unlink(missing_ok=True)
    # The person edited `label`, not the staged copy: name the file they know.
    lines = (line.rstrip().replace(container_file, label) for line in result.output.splitlines())
    return result, tuple(line for line in lines if line.strip())


def validation_result(ctx: Context, source: Path | None = None) -> tuple[Result, tuple[str, ...]]:
    """Like ``validate``, but returns the validator's ``Result`` itself, for callers that must
    tell a rejection (exit status 1 from a validator that ran) from a validator that could not
    run at all (a Docker failure, a timeout, a release without the validator): ``update``."""
    source = ctx.paths.heating if source is None else source
    return _validate_snapshot(ctx, source.read_bytes(), str(source))


def validate(ctx: Context, source: Path | None = None) -> tuple[bool, tuple[str, ...]]:
    """Run the validator on ``source`` (default: the edited file) in a one-off container.

    Returns ``(valid, messages)``. The file is read once and a copy of those bytes is staged in
    the state directory (``cave/.validating-<random>.yaml``, removed afterwards), because the
    container sees only ``/config`` and the file may change while the container starts. Any non-zero
    exit status counts as invalid, including a container that could not run; its output is
    the message, with the staged file's name replaced by ``source``.
    """
    result, messages = validation_result(ctx, source)
    return result.ok, messages


def validate_and_store(ctx: Context, source: Path | None = None) -> ApplyResult:
    """Validate ``source`` (default: the edited file); only if valid, store it as the applied copy.

    The file is read once: the bytes that were validated are the bytes that are stored, even
    if the file is saved again while the validator runs. An invalid file changes nothing.
    ``apply`` and ``demo`` call this.
    """
    source = ctx.paths.heating if source is None else source
    content = source.read_bytes()
    result, messages = _validate_snapshot(ctx, content, str(source))
    ok = result.ok
    if not ok:
        return ApplyResult(False, messages)
    changed = store_applied(ctx, content)
    return ApplyResult(True, messages, changed, ctx.paths.heating_applied)
