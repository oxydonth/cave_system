""" "Healthy", as defined in concept section 4.5.

Every container of the active profiles is running and its Docker health check passes. Home
Assistant's own check (compose.yaml) needs its frontend to answer and the line "Home Assistant
initialized" in its log, so it is not healthy while it is still starting; it cannot tell recovery
mode from normal operation without a login. If the script token exists, additionally
``GET /api/config`` returns 200 and reports ``state`` RUNNING, neither ``recovery_mode`` nor
``safe_mode``, and ``cave_heating`` among its ``components``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import CaveError

if TYPE_CHECKING:
    from .context import Context

HEATING_COMPONENT = "cave_heating"
DOCKER_TIMEOUT = 60.0  # seconds for each Docker question; a hung daemon is "not healthy"


@dataclass(frozen=True)
class Health:
    """The verdict and, when it is negative, what is wrong, one problem per entry."""

    healthy: bool
    problems: tuple[str, ...] = ()

    def describe(self) -> str:
        return "healthy" if self.healthy else "not healthy: " + "; ".join(self.problems)


def parse_compose_ps(text: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json``: one JSON object per line, or one JSON array."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        loaded = json.loads(text)
        return [item for item in loaded if isinstance(item, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


def check_health(ctx: Context, *, api: bool = True) -> Health:
    """Ask Docker, and Home Assistant if a script token exists, whether the stack is healthy.

    ``api=False`` asks Docker only. ``cave bootstrap`` waits that way, because it must be able to
    repair a script token that Home Assistant no longer accepts.
    """
    problems: list[str] = []

    expected = ctx.compose("config", "--services", timeout=DOCKER_TIMEOUT)
    if not expected.ok:
        return Health(False, (f"docker compose config failed: {expected.output.strip()}",))
    services = [line.strip() for line in expected.stdout.splitlines() if line.strip()]

    listing = ctx.compose("ps", "--all", "--format", "json", timeout=DOCKER_TIMEOUT)
    if not listing.ok:
        return Health(False, (f"docker compose ps failed: {listing.output.strip()}",))
    try:
        containers = {item.get("Service"): item for item in parse_compose_ps(listing.stdout)}
    except ValueError:
        return Health(False, ("docker compose ps gave output that is not JSON",))

    for service in services:
        container = containers.get(service)
        if container is None:
            problems.append(f"{service}: no container")
            continue
        state = container.get("State", "")
        health = container.get("Health", "")
        if state != "running":
            problems.append(f"{service}: {state or 'not running'}")
        elif health not in ("", "healthy"):
            problems.append(f"{service}: health check {health}")

    if not problems and api:
        problems.extend(_check_api(ctx))
    return Health(not problems, tuple(problems))


def _check_api(ctx: Context) -> list[str]:
    try:
        client = ctx.ha()
        if client is None:
            return []
        config = client.config()
    except CaveError as error:
        return [f"Home Assistant API: {error}"]
    problems = []
    if config.get("state") != "RUNNING":
        problems.append(
            f"Home Assistant is {config.get('state', 'in an unknown state')}, not RUNNING"
        )
    if config.get("recovery_mode") or config.get("safe_mode"):
        problems.append("Home Assistant is in recovery or safe mode: its configuration is broken")
    if HEATING_COMPONENT not in config.get("components", []):
        problems.append(f"Home Assistant has not set up {HEATING_COMPONENT}")
    return problems


def wait_healthy(
    ctx: Context, *, timeout: float = 300.0, interval: float = 5.0, api: bool = True
) -> Health:
    """Poll ``check_health`` until it passes or ``timeout`` seconds pass; return the verdict."""
    deadline = ctx.monotonic() + timeout
    while True:
        health = check_health(ctx, api=api)
        if health.healthy or ctx.monotonic() >= deadline:
            return health
        ctx.sleep(interval)
