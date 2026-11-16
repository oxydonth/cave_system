"""End-to-end tests against a running stack (concept section 8), run by the ``e2e`` job.

``CAVE_E2E`` names the stack that runs, and so which tests apply:

* ``home``: the home's stack after ``./cave init`` and ``./cave up``, never set up
  (``test_first_run.py``);
* ``demo``: the demo after ``./cave demo`` (``test_scenarios.py``).

Without ``CAVE_E2E`` everything here is skipped, so ``make test`` passes over it on a machine
without Docker. The tests talk to Home Assistant only as any other client would, through its REST
and WebSocket APIs with the script token; to the simulators through their control API; and to
the system through ``./cave``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from cave_ops.commands.demo import default_data
from cave_ops.envfile import load_env
from cave_ops.ha import HAClient, HAError
from tools.bootstrap import ONBOARDING_STEPS, WebSocket

REPO = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8123"


STACKS = ("home", "demo")
CONTROL_API = "http://127.0.0.1:8481"


@pytest.fixture(scope="session")
def e2e_stack() -> str:
    """Which stack runs: ``home`` or ``demo`` (``CAVE_E2E``)."""
    value = os.environ.get("CAVE_E2E", "")
    if value not in STACKS:
        pytest.skip("end-to-end tests run against a running stack: CAVE_E2E=home or demo")
    return value


@pytest.fixture(autouse=True)
def network(socket_enabled: None) -> None:
    """Home Assistant's test plugin blocks sockets in every test; these talk to the real stack."""


@dataclass
class Stack:
    """The checkout with its running stack, the home's or the demo's."""

    root: Path
    kind: str = "home"

    @property
    def env_file(self) -> Path:
        if self.kind == "demo":
            # `./cave demo`'s default state directory, or the one it was given with --data.
            data = os.environ.get("CAVE_E2E_DEMO_DATA")
            return (Path(data) if data else default_data()) / "demo.env"
        return self.root / ".env"

    @property
    def env(self) -> dict[str, str]:
        return load_env(self.env_file)

    def control(self, method: str, path: str, body: Any = None) -> Any:
        """The simulators' control API (sim/README.md)."""
        request = urllib.request.Request(  # noqa: S310 - a fixed http://127.0.0.1 URL
            CONTROL_API + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.loads(response.read())

    def device(self, device_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.control("GET", f"/api/devices/{device_id}")
        return result

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
        mask_in_job_log(self.token_file)  # a run may have made a new script token
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

    def onboarding(self) -> dict[str, bool]:
        """Onboarding's steps and whether each is done.

        A Home Assistant started with onboarding complete has no onboarding views: 404.
        """
        try:
            steps = self.api().request("GET", "/api/onboarding")
        except HAError as error:
            if error.status != 404:
                raise
            return dict.fromkeys(ONBOARDING_STEPS, True)
        return {step["step"]: step["done"] for step in steps}

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
            "onboarding": self.onboarding(),
            "token_file": (
                hashlib.sha256(self.token_file.read_bytes()).hexdigest(),
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_mtime_ns,
            ),
        }


def mask_in_job_log(token_file: Path) -> None:
    """In GitHub Actions, have the runner mask the script token in everything printed later."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not token_file.is_file():
        return
    token = token_file.read_text(encoding="utf-8").strip()
    if token:
        print(f"::add-mask::{token}")  # noqa: T201 - a workflow command; the runner hides it


@pytest.fixture(scope="session")
def stack(e2e_stack: str) -> Stack:
    return Stack(REPO, e2e_stack)


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
