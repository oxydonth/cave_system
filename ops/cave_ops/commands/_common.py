"""Helpers shared by the commands of this package (not a command: the name starts with ``_``)."""

from __future__ import annotations

from ..context import Context
from ..errors import CaveError


def require_initialised(ctx: Context) -> None:
    """Stop with a clear message when ``./cave init`` has not run: Compose needs ``.env``."""
    if not ctx.paths.env_file.is_file():
        raise CaveError(f"{ctx.paths.env_file} does not exist; run `./cave init` first")
