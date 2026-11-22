"""Fixtures for ``update``, ``status``, ``backup`` and ``restore``: a server with real files.

These build on ``tests/ops/conftest.py`` (``fake_runner``, ``fake_ha``, ``clock``, ``journal``,
``run_cave``, ``write_token``) and replace its ``ctx`` with one for a server:

``server``
    A ``Server``: ``origin`` (a real Git repository, standing in for GitHub, with the release
    ``v2026.11.14.1``), ``root`` (a real clone of it: the checkout, with ``.env``), ``data`` (a
    real state directory with Home Assistant's, Zigbee2MQTT's and Mosquitto's files and the
    script token) and ``stack`` (a ``FakeStack`` running that release). ``server.release(tag)``
    publishes a newer release on ``origin``.
``ctx``
    A ``Context`` for that server. Git runs for real (with a neutral configuration); ``docker
    compose`` reaches the ``FakeStack``; anything else goes to ``fake_runner`` and fails the test.
    Every call is recorded in ``fake_runner.calls``.

Each file of a release says which release it is (``RELEASE``), so the fake knows what was
checked out when ``up`` runs, and which release's files the checks ran with. Starting a release
leaves traces in the state, as a migration would: ``homeassistant/schema`` moves forward to the
release, and ``homeassistant/started-<release>`` appears.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cave_ops.context import Context
from cave_ops.envfile import load_env
from cave_ops.paths import Paths
from cave_ops.runner import CommandError, Result

FIRST = "v2026.11.14.1"
SERVICES = ("homeassistant", "mosquitto")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Release Bot",
    "GIT_AUTHOR_EMAIL": "release@example.invalid",
    "GIT_COMMITTER_NAME": "Release Bot",
    "GIT_COMMITTER_EMAIL": "release@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


class SimulatedKill(BaseException):
    """Stands for SIGKILL or a power cut: no handler in ``cave`` catches it, nothing cleans up."""


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


# -------------------------------------------------------------------------------- Docker stack
@dataclass
class FakeStack:
    """``docker compose`` for the home's project (``cave``) and the update's checks.

    ``running`` is the release whose containers run, or ``None``. Make things fail with
    ``unhealthy`` (releases that start but never become healthy), ``fail`` (operation to exit
    status: ``down``, ``up``, ``pull``, ``check_config``, ``validate``, and ``config`` for the
    check of a release's Compose files), ``fail_times`` (operation to a number of failures
    before it works), ``fail_up`` (releases whose ``up`` fails), ``starting`` (releases that
    report "starting" for a number of polls), and run code at an operation with
    ``before``/``after`` (for example a ``SimulatedKill``). ``ops`` lists
    ``(operation, release)`` in order.
    """

    data: Path
    ha: Any
    running: str | None = None
    unhealthy: set[str] = field(default_factory=set)
    fail: dict[str, int] = field(default_factory=dict)
    fail_up: set[str] = field(default_factory=set)
    fail_times: dict[str, int] = field(default_factory=dict)
    """Operation -> how many of its next calls fail (then it works again)."""
    starting: dict[str, int] = field(default_factory=dict)
    """Release -> for how many ``ps`` polls after its start it reports "starting", as Home
    Assistant does during its start period."""
    _polls_left: int = 0
    before: dict[str, Callable[[str | None], None]] = field(default_factory=dict)
    after: dict[str, Callable[[str | None], None]] = field(default_factory=dict)
    validator_errors: str = (
        "heating.yaml is not valid:\n"
        "Room 'Bad room': the field 'thermostats' must list at least one thermostat.\n"
    )
    ops: list[tuple[str, str | None]] = field(default_factory=list)
    configured: list[str] = field(default_factory=list)
    """The ``COMPOSE_FILE`` of every ``config`` check of a release."""

    def start(self, release: str) -> None:
        self.running = release
        self._polls_left = self.starting.get(release, 0)
        self.ha.down = False
        ha = self.data / "homeassistant"
        (ha / f"started-{release}").write_text("started\n")
        schema = ha / "schema"
        if not schema.exists() or schema.read_text().strip() < release:
            schema.write_text(release + "\n")

    def names(self) -> list[str]:
        return [name for name, _ in self.ops]

    def handle(self, args: tuple[str, ...], cwd: Path | None, env: Mapping[str, str]) -> Result:
        rest = list(args[2:])
        while rest and rest[0] == "-f":
            del rest[:2]
        project = env.get("COMPOSE_PROJECT_NAME", "cave")
        if project == "cave":
            assert cwd is not None
            release_file = cwd / "RELEASE"
        else:
            release_file = Path(env["COMPOSE_FILE"].split(os.pathsep)[0]).parent / "RELEASE"
        release = release_file.read_text().strip() if release_file.is_file() else None
        command = rest[0]

        if command == "config" and project != "cave":
            # The update's check of the release's Compose files (not recorded in ``ops``).
            self.configured.append(env["COMPOSE_FILE"])
            if self.fail.get("config"):
                return Result(args, self.fail["config"], "", "service zigbee2mqtt: invalid\n")
            return Result(args, 0, "")
        if command == "config":
            return Result(args, 0, "\n".join(SERVICES) + "\n")
        if command == "ps":
            if self.running is None:
                return Result(args, 0, "")
            health = "unhealthy" if self.running in self.unhealthy else "healthy"
            if self._polls_left > 0:
                self._polls_left -= 1
                health = "starting"
            lines = [
                json.dumps({"Service": s, "State": "running", "Health": health}) for s in SERVICES
            ]
            return Result(args, 0, "\n".join(lines) + "\n")

        if command == "run":
            if "check_config" in rest:
                op = "check_config"
            elif "custom_components.cave_heating.validate" in rest:
                op = "validate"
            else:
                raise AssertionError(f"unexpected one-off command: {args}")
        else:
            op = command
        if project != "cave":
            assert project == "cave-update", project
            assert op in ("pull", "check_config", "validate"), op
        else:
            assert op in ("down", "up"), op

        if op in self.before:
            self.before[op](release)
        self.ops.append((op, release))
        status = self.fail.get(op, 0)
        if self.fail_times.get(op, 0) > 0:
            self.fail_times[op] -= 1
            status = 1
        if op == "up" and release in self.fail_up:
            status = 1
        if status:
            err = self.validator_errors if op == "validate" else f"{op} failed in the fake\n"
            return Result(args, status, "", err)
        if op == "down":
            self.running = None
            self.ha.down = True
        elif op == "up":
            assert release is not None
            self.start(release)
        if op in self.after:
            self.after[op](release)
        return Result(args, 0, "")


@dataclass
class FakeImages:
    """Docker's image store, for ``docker image ls`` and ``docker image rm``.

    ``store`` maps each reference (``repository:tag``, or ``repository@digest`` for an image
    without a tag) to its digest. ``in_use`` lists references a container uses: removing them is
    refused, as Docker refuses without ``--force``. ``removed`` lists what was removed, in order.
    """

    store: dict[str, str] = field(default_factory=dict)
    in_use: set[str] = field(default_factory=set)
    removed: list[str] = field(default_factory=list)

    def add(self, *references: str) -> None:
        for reference in references:
            self.store[reference] = f"sha256:{abs(hash(reference)):064x}"[:71]

    def handle(self, args: tuple[str, ...]) -> Result:
        command = args[2]
        if command == "ls":
            assert "--format" in args, args
            lines = []
            for reference, digest in sorted(self.store.items()):
                if "@" in reference:
                    name, tag = reference.split("@", 1)[0], "<none>"
                else:
                    name, tag = reference.rsplit(":", 1)
                lines.append(f"{name}\t{tag}\t{digest}")
            return Result(args, 0, "".join(line + "\n" for line in lines))
        if command == "rm":
            assert "--force" not in args, "never forced"
            assert "-f" not in args, "never forced"
            reference = args[-1]
            if reference in self.in_use:
                return Result(args, 1, "", f"conflict: unable to remove {reference}: in use\n")
            if reference not in self.store:
                return Result(args, 1, "", f"No such image: {reference}\n")
            del self.store[reference]
            self.removed.append(reference)
            return Result(args, 0, f"Untagged: {reference}\n")
        raise AssertionError(f"unexpected image command: {args}")


class ServerRunner:
    """Git for real, ``docker compose`` through the ``FakeStack``, ``docker image`` through
    ``FakeImages``, the rest to ``fake_runner``."""

    def __init__(self, fake_runner: Any, stack: FakeStack) -> None:
        self.fake = fake_runner
        self.stack = stack
        self.images = FakeImages()
        self.git_fail: dict[str, list[tuple[int, str]]] = {}
        """Git subcommand -> answers ``(exit status, standard error)`` given instead of running
        it, one per call, in order; once used up, Git runs for real."""
        self.git_before: dict[str, Callable[[tuple[str, ...]], None]] = {}
        """Git subcommand -> code run before it, with its arguments."""

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
        argv = tuple(args)
        if argv[:1] == ("git",) or argv[:2] in (("docker", "compose"), ("docker", "image")):
            self.fake.calls.append(
                RecordedCall(argv, cwd, capture, check, input_text, env, timeout)
            )
            if argv[0] == "git":
                result = self._git(argv, cwd)
            elif argv[1] == "image":
                result = self.images.handle(argv)
            else:
                result = self.stack.handle(argv, cwd, env or {})
            if check and not result.ok:
                raise CommandError(result)
            return result
        return self.fake.run(
            argv,
            cwd=cwd,
            env=env,
            capture=capture,
            check=check,
            input_text=input_text,
            timeout=timeout,
        )

    def _git(self, argv: tuple[str, ...], cwd: Path | None) -> Result:
        sub = argv[1] if len(argv) > 1 else ""
        if sub in self.git_before:
            self.git_before[sub](argv)
        if self.git_fail.get(sub):
            status, err = self.git_fail[sub].pop(0)
            return Result(argv, status, "", err)
        done = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **GIT_ENV},
            capture_output=True,
            text=True,
            check=False,
        )
        return Result(argv, done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class RecordedCall:
    """The fields of ``Call`` in ``tests/ops/conftest.py``, which cannot be imported here."""

    args: tuple[str, ...]
    cwd: Path | None
    capture: bool
    check: bool
    input_text: str | None
    env: Mapping[str, str] | None
    timeout: float | None


# ------------------------------------------------------------------------------------- server
@dataclass
class Server:
    origin: Path
    root: Path
    data: Path
    stack: FakeStack
    runner: ServerRunner

    def release(self, tag: str, files: Mapping[str, str] | None = None) -> str:
        """Publish ``tag`` on ``origin``: a commit whose files say it is ``tag``."""
        (self.origin / "RELEASE").write_text(tag + "\n")
        for name, text in (files or {}).items():
            path = self.origin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git("add", "-A", cwd=self.origin)
        git("commit", "-q", "-m", f"release {tag}", cwd=self.origin)
        git("tag", tag, cwd=self.origin)
        return git("rev-parse", "HEAD", cwd=self.origin)

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.root)

    def commit_of(self, tag: str) -> str:
        return git("rev-parse", f"{tag}^{{commit}}", cwd=self.origin)

    def checked_out(self) -> str:
        return (self.root / "RELEASE").read_text().strip()

    def record(self) -> dict[str, Any]:
        path = self.data / ".cave" / "ops" / "update.json"
        result: dict[str, Any] = json.loads(path.read_text())
        return result

    def schema(self) -> str:
        return (self.data / "homeassistant" / "schema").read_text().strip()


def _state(data: Path, token: str) -> None:
    """A state directory as a running server has it (concept section 4.2)."""
    files = {
        "homeassistant/configuration.yaml": "",
        "homeassistant/secrets.yaml": "# empty\n",
        "homeassistant/automations.yaml": "[]\n",
        "homeassistant/cave/heating.yaml": "version: 1\nrooms: []\n",
        "homeassistant/cave/.applied/heating.yaml": "version: 1\nrooms: []\n",
        "homeassistant/dashboards/heating.yaml": "title: Heizung\n",
        "homeassistant/.storage/core.config_entries": '{"data": {"entries": []}}\n',
        "homeassistant/home-assistant_v2.db": "not really a database\n",
        "homeassistant/schema": FIRST + "\n",
        "homeassistant/.cave/token": token + "\n",
        "zigbee2mqtt/configuration.yaml": "homeassistant: true\n",
        "zigbee2mqtt/database.db": '{"id": 1}\n',
        "mosquitto/passwd": "cave:$7$hash\n",
        ".cave/mqtt-credentials": "0123abcd\n",
    }
    for name, text in files.items():
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (data / "homeassistant/.cave/token").chmod(0o600)
    (data / "homeassistant/.storage/core.config_entries").chmod(0o600)


@pytest.fixture
def server(tmp_path: Path, fake_runner: Any, fake_ha: Any) -> Server:
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "compose.yaml").write_text("name: cave\nservices: {}\n")
    (origin / ".gitignore").write_text(".env\n")
    root = tmp_path / "checkout"
    data = tmp_path / "state"
    stack = FakeStack(data=data, ha=fake_ha)
    server = Server(origin, root, data, stack, ServerRunner(fake_runner, stack))
    server.release(FIRST)
    git("clone", "-q", str(origin), str(root), cwd=tmp_path)
    env_file = root / ".env"
    env_file.write_text(f"CAVE_DATA={data}\nCAVE_AUTO_UPDATE=1\n")
    env_file.chmod(0o600)
    _state(data, fake_ha.token)
    fake_ha.services["script"] = ["cave_notify"]
    stack.start(FIRST)
    return server


@pytest.fixture
def ctx(server: Server, fake_ha: Any, clock: Any, journal: list[str]) -> Context:
    return Context(
        paths=Paths(root=server.root, data=server.data),
        env=load_env(server.root / ".env"),
        runner=server.runner,
        out=io.StringIO(),
        err=io.StringIO(),
        transport=fake_ha,
        journal=journal.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.fixture
def notifications(fake_ha: Any) -> Callable[[], list[str]]:
    """The messages that reached Home Assistant through ``script.cave_notify``."""

    def read() -> list[str]:
        return [
            data["message"]
            for domain, service, data in fake_ha.service_calls
            if (domain, service) == ("script", "cave_notify")
        ]

    return read


def tree(path: Path) -> dict[str, bytes]:
    """Every file below ``path`` with its content, to compare states."""
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def state_tree() -> Callable[[Path], dict[str, bytes]]:
    """``state_tree(data)``: the state entries' files, without the commands' own records: the
    lock, ``.cave/ops``, the record of a stack stopped on purpose and the watch's memory."""

    def read(data: Path) -> dict[str, bytes]:
        records = (".cave/ops/", ".cave/lock", ".cave/stopped.json", ".cave/watch.json")
        return {
            name: content for name, content in tree(data).items() if not name.startswith(records)
        }

    return read


@pytest.fixture
def killed() -> type[BaseException]:
    """``SimulatedKill``: raise it from a hook to stop ``cave`` the way SIGKILL or a power cut
    would, without any of its handlers running."""
    return SimulatedKill
