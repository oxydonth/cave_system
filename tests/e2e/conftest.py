"""End-to-end tests against the running stack (concept section 8), run by the ``e2e`` job.

They need a checkout where ``./cave init`` and ``./cave up`` have run, and are skipped unless
``CAVE_E2E=1``, so ``make test`` passes over them on a machine without Docker. They talk to Home
Assistant only as any other client would: through its REST and WebSocket APIs with the script
token, and through ``./cave``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from cave_ops.envfile import load_env
from cave_ops.ha import HAClient
from tools.bootstrap import WebSocket

REPO = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8123"


@pytest.fixture(scope="session", autouse=True)
def e2e_only() -> None:
    if os.environ.get("CAVE_E2E") != "1":
        pytest.skip("end-to-end tests run against a running stack: CAVE_E2E=1 (the e2e job)")


@dataclass
class Stack:
    """The checkout with its running stack."""

    root: Path

    @property
    def env(self) -> dict[str, str]:
        return load_env(self.root / ".env")

    @property
    def data(self) -> Path:
        return Path(self.env["CAVE_DATA"])

    @property
    def token_file(self) -> Path:
        return self.data / "homeassistant" / ".cave" / "token"

    @property
    def secrets(self) -> list[str]:
        env = self.env
        return [env[name] for name in ("CAVE_HA_OWNER_PASSWORD", "CAVE_MQTT_PASSWORD") if env[name]]

    def cave(self, *args: str, timeout: float = 900) -> subprocess.CompletedProcess[str]:
        """Run ``./cave``; its output is printed, so the job log shows it."""
        done = subprocess.run(
            ["./cave", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        print(f"$ ./cave {' '.join(args)}  (exit status {done.returncode})")  # noqa: T201
        print(done.stdout + done.stderr)  # noqa: T201
        return done

    def token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    def api(self) -> HAClient:
        return HAClient(self.token(), base_url=URL, timeout=30)

    def ws(self, *commands: tuple[str, dict[str, Any]]) -> list[Any]:
        """Run WebSocket commands with the script token; return their results in order."""

        async def run() -> list[Any]:
            async with (
                aiohttp.ClientSession() as session,
                WebSocket(session, URL, self.token()) as ws,
            ):
                return [await ws.call(kind, **fields) for kind, fields in commands]

        return asyncio.run(run())

    def state(self) -> dict[str, Any]:
        """Everything the bootstrap could change, comparable between two runs.

        Times of last use are left out: using the token updates them, and that is not a change
        the bootstrap makes.
        """
        users, tokens, schedules, registry, persons = self.ws(
            ("config/auth/list", {}),
            ("auth/refresh_tokens", {}),
            ("schedule/list", {}),
            ("config/entity_registry/list", {}),
            ("person/list", {}),
        )
        api = self.api()
        config = api.config()
        info = self.token_file.stat()
        return {
            "users": sorted(
                (u["id"], u["username"], u["name"], u["is_owner"], u["is_active"])
                for u in users
                if not u["system_generated"]
            ),
            "owner_tokens": sorted(
                (t["id"], t["client_name"], t["client_id"], t["type"]) for t in tokens
            ),
            "entries": sorted(
                (e["entry_id"], e["domain"], e["title"], e["source"], e["state"])
                for e in api.request("GET", "/api/config/config_entries/entry")
            ),
            "schedules": sorted(schedules, key=lambda item: item["id"]),
            "schedule_entities": sorted(
                (e["entity_id"], e["platform"], e["unique_id"])
                for e in registry
                if e["entity_id"].startswith("schedule.")
            ),
            "persons": persons,
            "core": {
                key: config[key] for key in ("time_zone", "country", "currency", "unit_system")
            },
            "onboarding": api.request("GET", "/api/onboarding"),
            "token_file": (
                hashlib.sha256(self.token_file.read_bytes()).hexdigest(),
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_mtime_ns,
            ),
        }


@pytest.fixture(scope="session")
def stack() -> Stack:
    return Stack(REPO)


def _wait_until(condition: Callable[[], Any], what: str, timeout: float = 60.0) -> Any:
    """Poll ``condition`` until it is truthy; fail naming ``what`` after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last: object = None
    while True:
        try:
            result = condition()
            if result:
                return result
        except Exception as error:
            last = error
        if time.monotonic() >= deadline:
            pytest.fail(f"gave up after {timeout:.0f} s waiting for {what} (last: {last!r})")
        time.sleep(0.5)


@pytest.fixture(scope="session")
def wait_until() -> Callable[..., Any]:
    """``wait_until(condition, what, timeout=60)``: no fixed sleeps, a condition and a limit."""
    return _wait_until
