"""One-off commands in the Home Assistant container.

Whatever needs Home Assistant's own Python packages (the validator, the bootstrap, the dashboard
generator, the configuration check) runs in a throw-away container of the pinned image instead
of being imported here. The container has the same mounts as the service: the state directory as
``/config``, the integration at ``/config/custom_components/cave_heating`` and ``tools/`` at
``/cave/tools``, all from ``compose.yaml``. It does not start the broker and works while Home
Assistant itself is down.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .runner import Result

if TYPE_CHECKING:
    from .context import Context

SERVICE = "homeassistant"


def run_in_homeassistant(
    ctx: Context,
    command: Sequence[str],
    *,
    entrypoint: str = "python3",
    workdir: str | None = None,
    capture: bool = True,
    timeout: float | None = None,
) -> Result:
    """Run ``entrypoint command...`` in a one-off Home Assistant container and return the result.

    ``ctx.compose("run", "--rm", "--no-deps", "-T", ...)`` underneath. For example
    ``run_in_homeassistant(ctx, ["-m", "custom_components.cave_heating.validate", file],
    workdir="/config")`` or ``run_in_homeassistant(ctx, ["/cave/tools/bootstrap.py"])``.
    ``capture=False`` lets the output reach the terminal.
    """
    args = ["run", "--rm", "--no-deps", "-T"]
    if workdir is not None:
        args += ["--workdir", workdir]
    args += ["--entrypoint", entrypoint, SERVICE, *command]
    return ctx.compose(*args, capture=capture, timeout=timeout)
