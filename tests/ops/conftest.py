"""Fixtures for testing the ``cave`` command and its core modules.

Nothing here imports Home Assistant: these tests also run on Python 3.11 in an environment that
has only pytest and PyYAML (``make test-py311``). Test modules use the fixtures and never import
this file, because ``--import-mode=importlib`` does not allow it. Later work packages build on:

``repo_root``
    The real repository, for tests of tracked files.
``checkout``
    A scratch checkout (a copy of the tracked files that ``init`` and ``version`` read) in
    ``tmp_path``. Its state directory is ``checkout / "data"``.
``fake_runner``
    Replaces ``docker`` and ``git``. Declare answers with ``fake_runner.on(prefix, ...)``; an
    unexpected command fails the test. See ``FakeRunner``.
``fake_ha``
    Replaces Home Assistant's REST API. See ``FakeHomeAssistant``.
``ctx``
    A ``Context`` wired to all of the above, with captured output, a journal list and a fake clock.
``run_cave``
    ``run_cave("up", "--no-wait")`` runs ``cave_ops.cli.main`` with ``ctx`` and returns a
    ``CaveRun`` (exit status, captured standard output and error).
``write_token``
    ``write_token("secret")`` creates the script token file, so that ``ctx.ha()`` works.
``snapshot``
    ``snapshot(path)`` lists every file and directory below ``path`` with its mode, size and
    modification time, to prove that a command changed nothing.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cave_ops import cli
from cave_ops.context import Context
from cave_ops.envfile import NAMES
from cave_ops.paths import Paths
from cave_ops.runner import CommandError, Result

REPO_ROOT = Path(__file__).resolve().parents[2]

# What the checkout needs from the real repository: the files that commands read.
TRACKED_FILES = (
    "compose.yaml",
    "homeassistant/heating.example.yaml",
    "zigbee2mqtt/configuration.template.yaml",
)


# --------------------------------------------------------------------------------------- runner
@dataclass(frozen=True)
class Call:
    """One command that a command under test ran."""

    args: tuple[str, ...]
    cwd: Path | None
    capture: bool
    check: bool
    input_text: str | None


@dataclass
class _Rule:
    prefix: tuple[str, ...]
    responses: list[tuple[int, str, str]]
    side_effect: Callable[[Call], None] | None
    used: int = 0


class FakeRunner:
    """A ``Runner`` that runs nothing. Declare what each command prints, then inspect ``calls``.

    ``on(prefix, rc=0, out="", err="")`` answers every call whose arguments start with
    ``prefix``. Later rules win over earlier ones, so a test can override a default.
    ``sequence=[(rc, out, err), ...]`` answers successive calls in order, repeating the last.
    ``side_effect(call)`` runs when the rule matches, to create files the command would create.
    A command with no matching rule raises ``AssertionError``.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._rules: list[_Rule] = []

    def on(
        self,
        prefix: Sequence[str],
        *,
        rc: int = 0,
        out: str = "",
        err: str = "",
        sequence: Sequence[tuple[int, str, str]] | None = None,
        side_effect: Callable[[Call], None] | None = None,
    ) -> None:
        responses = list(sequence) if sequence else [(rc, out, err)]
        self._rules.append(_Rule(tuple(prefix), responses, side_effect))

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        check: bool = False,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        call = Call(tuple(args), cwd, capture, check, input_text)
        self.calls.append(call)
        for rule in reversed(self._rules):
            if call.args[: len(rule.prefix)] == rule.prefix:
                index = min(rule.used, len(rule.responses) - 1)
                rule.used += 1
                rc, out, err = rule.responses[index]
                if rule.side_effect is not None:
                    rule.side_effect(call)
                result = Result(call.args, rc, out, err)
                if check and not result.ok:
                    raise CommandError(result)
                return result
        raise AssertionError(f"unexpected command: {' '.join(call.args)}")

    def ran(self, *prefix: str) -> bool:
        """Whether any call started with ``prefix``."""
        return bool(self.matching(*prefix))

    def matching(self, *prefix: str) -> list[Call]:
        return [call for call in self.calls if call.args[: len(prefix)] == prefix]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``CAVE_*`` setting of the developer's shell may leak into a test."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


# ------------------------------------------------------------------------------ Home Assistant
class FakeHomeAssistant:
    """Home Assistant's REST API as a ``Transport``.

    Set ``components``, ``services`` (``{domain: [service, ...]}``) and ``states``
    (``{entity_id: {...}}``) to what the test needs. ``down = True`` makes every request fail like
    a refused connection. ``requests`` records ``(method, path, body)``; ``service_calls`` the
    services that were called, as ``(domain, service, data)``.
    """

    def __init__(self) -> None:
        self.token = "fake-token-for-tests"
        self.components: list[str] = ["homeassistant", "cave_heating"]
        self.services: dict[str, list[str]] = {
            "persistent_notification": ["create", "dismiss"],
            "homeassistant": ["reload_all"],
        }
        self.states: dict[str, dict[str, Any]] = {}
        self.down = False
        self.requests: list[tuple[str, str, Any]] = []
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        if self.down:
            raise ConnectionRefusedError("connection refused")
        path = re.sub(r"^https?://[^/]+", "", url)
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, b"401: Unauthorized"
        if method == "GET" and path == "/api/config":
            return 200, json.dumps({"components": self.components}).encode()
        if method == "GET" and path == "/api/services":
            listing = [
                {"domain": domain, "services": {name: {} for name in names}}
                for domain, names in self.services.items()
            ]
            return 200, json.dumps(listing).encode()
        if method == "GET" and path.startswith("/api/states/"):
            state = self.states.get(path.removeprefix("/api/states/"))
            if state is None:
                return 404, b'{"message": "Entity not found."}'
            return 200, json.dumps(state).encode()
        service = re.fullmatch(r"/api/services/([^/]+)/([^/]+)", path)
        if method == "POST" and service:
            domain, name = service.groups()
            if name not in self.services.get(domain, []):
                return 400, b"Service not found."
            self.service_calls.append((domain, name, payload or {}))
            return 200, b"[]"
        return 404, b"404: Not Found"


@pytest.fixture
def fake_ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()


# --------------------------------------------------------------------------------- the context
@dataclass
class FakeClock:
    """``sleep`` advances ``now`` instead of waiting."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@dataclass
class CaveRun:
    """The outcome of ``run_cave``."""

    code: int
    out: str
    err: str


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A scratch checkout holding copies of the tracked files that commands read."""
    root = tmp_path / "checkout"
    for name in TRACKED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / name, target)
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def journal() -> list[str]:
    """The lines written to the journal by ``ctx``."""
    return []


@pytest.fixture
def ctx(
    checkout: Path,
    fake_runner: FakeRunner,
    fake_ha: FakeHomeAssistant,
    clock: FakeClock,
    journal: list[str],
) -> Context:
    return Context(
        paths=Paths(root=checkout, data=checkout / "data"),
        env={},
        runner=fake_runner,
        out=io.StringIO(),
        err=io.StringIO(),
        transport=fake_ha,
        journal=journal.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.fixture
def run_cave(ctx: Context) -> Callable[..., CaveRun]:
    def run(*argv: str) -> CaveRun:
        # Fresh buffers for every call, so a test can run several commands in a row.
        ctx.out, ctx.err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), ctx=ctx)
        return CaveRun(code, ctx.out.getvalue(), ctx.err.getvalue())

    return run


@pytest.fixture
def write_token(ctx: Context, fake_ha: FakeHomeAssistant) -> Callable[..., None]:
    def write(token: str | None = None) -> None:
        path = ctx.paths.token
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((token or fake_ha.token) + "\n", encoding="utf-8")
        path.chmod(0o600)

    return write


def _snapshot(path: Path) -> dict[str, tuple[int, int, int]]:
    entries: dict[str, tuple[int, int, int]] = {}
    for current, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            full = Path(current) / name
            info = full.lstat()
            entries[str(full.relative_to(path))] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return entries


@pytest.fixture
def snapshot() -> Callable[[Path], dict[str, tuple[int, int, int]]]:
    return _snapshot
