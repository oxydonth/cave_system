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

from .paths import CONTAINER_CONFIG

if TYPE_CHECKING:
    from .context import Context

VALIDATOR_MODULE = "custom_components.cave_heating.validate"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of ``validate_and_store``."""

    ok: bool
    """The file is valid. When false nothing was stored."""
    messages: tuple[str, ...] = ()
    """What the validator said: the errors naming room and field, or empty."""
    changed: bool = False
    """The applied copy now differs from what it was."""


def store_applied(ctx: Context, source: Path) -> bool:
    """Copy ``source`` to the applied copy, atomically and unvalidated. Returns whether it changed.

    Does nothing, not even touching the modification time, when the content is the same.
    Callers other than ``init`` must validate first: use ``validate_and_store``.
    """
    target = ctx.paths.heating_applied
    content = source.read_bytes()
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


def validate(ctx: Context, source: Path | None = None) -> tuple[bool, tuple[str, ...]]:
    """Run the validator on ``source`` (default: the edited file) in a one-off container.

    Returns ``(valid, messages)``. A file outside the state directory is copied there for the
    run, because the container sees only ``/config``. Any non-zero exit status counts as
    invalid, including a container that could not run, and its output is the message.
    """
    source = ctx.paths.heating if source is None else source
    staged: Path | None = None
    container_file = ctx.paths.container_path(source)
    if container_file is None:
        staged = ctx.paths.ha_config / "cave" / ".validating.yaml"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(source.read_bytes())
        container_file = f"{CONTAINER_CONFIG}/cave/{staged.name}"
    try:
        result = ctx.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--workdir",
            CONTAINER_CONFIG,
            "--entrypoint",
            "python3",
            "homeassistant",
            "-m",
            VALIDATOR_MODULE,
            container_file,
        )
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    messages = tuple(line.rstrip() for line in result.output.splitlines() if line.strip())
    return result.ok, messages


def validate_and_store(ctx: Context, source: Path | None = None) -> ApplyResult:
    """Validate ``source`` (default: the edited file); only if valid, store it as the applied copy.

    An invalid file changes nothing. ``apply`` and ``demo`` call this.
    """
    source = ctx.paths.heating if source is None else source
    ok, messages = validate(ctx, source)
    if not ok:
        return ApplyResult(False, messages)
    return ApplyResult(True, messages, store_applied(ctx, source))
