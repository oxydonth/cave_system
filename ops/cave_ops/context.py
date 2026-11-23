"""The ``Context`` every command receives: paths, settings and every seam to the outside world.

Commands reach the outside only through the context (the runner, the Home Assistant client, the
journal, the clock), so tests can replace each of them. ``Context.create`` builds the real one.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .envfile import NAMES, load_env
from .ha import DEFAULT_URL, HAClient, Transport, read_token, urllib_transport
from .lock import Lock
from .paths import Paths, default_data_dir
from .runner import Result, Runner, SubprocessRunner


def syslog_journal(message: str) -> None:
    """Write a line to the system journal (through syslog); silently nothing where there is none."""
    try:
        import syslog  # not available everywhere

        syslog.openlog("cave")
        syslog.syslog(syslog.LOG_INFO, message)
        syslog.closelog()
    except (ImportError, OSError):
        pass


@dataclass
class Context:
    """Everything a command needs. Fields are replaceable; the methods are conveniences."""

    paths: Paths
    env: dict[str, str]
    """``.env`` overlaid by the process environment, for the names in ``envfile.VARIABLES``."""
    runner: Runner = field(default_factory=SubprocessRunner)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    transport: Transport = urllib_transport
    ha_url: str = DEFAULT_URL
    journal: Callable[[str], None] = syslog_journal
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    """Seconds since the epoch: for what has to be compared across runs (``cave watch``)."""
    compose_files: tuple[str, ...] = ()
    """Overlay files given to Compose after ``compose.yaml`` (the demo adds ``compose.sim.yaml``).

    Empty means Compose's own default: ``compose.yaml`` and, if present, ``compose.override.yaml``.
    Non-empty: ``compose.yaml``, then ``compose.override.yaml`` if present, then these files.
    """
    compose_env: dict[str, str] = field(default_factory=dict)
    """Environment added to every Compose call, over the process environment and ``.env``
    (Compose lets the environment win). The demo sets ``CAVE_DATA`` here for a state directory
    of its own."""

    @classmethod
    def create(cls, root: Path, environ: Mapping[str, str] | None = None) -> Context:
        """The real context for the checkout at ``root``, reading ``.env`` if it exists."""
        environ = os.environ if environ is None else environ
        env = load_env(root / ".env")
        env.update({name: environ[name] for name in NAMES if name in environ})
        data = env.get("CAVE_DATA") or str(default_data_dir(root, environ))
        return cls(paths=Paths(root=root, data=Path(data)), env=env)

    # --- output
    def say(self, message: str = "") -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)

    # --- external programs, always from the checkout so Compose finds compose.yaml and .env
    def docker(
        self,
        *args: str,
        capture: bool = True,
        check: bool = False,
        input_text: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        """``docker <args>``; the keywords are those of ``Runner.run``."""
        return self.runner.run(
            ["docker", *args],
            cwd=self.paths.root,
            env=env,
            capture=capture,
            check=check,
            input_text=input_text,
            timeout=timeout,
        )

    def compose_prefix(self) -> list[str]:
        """The ``-f`` options of ``compose_files``; empty when Compose may choose its files."""
        if not self.compose_files:
            return []
        files = ["compose.yaml"]
        if (self.paths.root / "compose.override.yaml").is_file():
            files.append("compose.override.yaml")
        return [part for name in (*files, *self.compose_files) for part in ("-f", name)]

    def compose(
        self,
        *args: str,
        capture: bool = True,
        check: bool = False,
        input_text: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        """``docker compose <args>`` for the active profiles, ``compose_files`` and ``compose_env``.

        ``env`` is added to ``compose_env`` for this call.
        """
        merged = {**self.compose_env, **(env or {})}
        return self.docker(
            "compose",
            *self.compose_prefix(),
            *args,
            capture=capture,
            check=check,
            input_text=input_text,
            timeout=timeout,
            env=merged or None,
        )

    def git(
        self, *args: str, capture: bool = True, check: bool = False, timeout: float | None = 120.0
    ) -> Result:
        """``git <args>`` in the checkout. Bounded by ``timeout`` (``None``: unbounded)."""
        return self.runner.run(
            ["git", *args], cwd=self.paths.root, capture=capture, check=check, timeout=timeout
        )

    # --- Home Assistant
    def ha(self) -> HAClient | None:
        """A client with the script token, or ``None`` before ``cave bootstrap`` has made one."""
        token = read_token(self.paths)
        if token is None:
            return None
        return HAClient(token, base_url=self.ha_url, transport=self.transport)

    # --- the lock of concept section 4.5
    def lock(self, command: str) -> Lock:
        """``with ctx.lock("update"):`` raises ``LockBusy`` if another command holds it."""
        return Lock(self.paths.lock_file, command, sleep=self.sleep, monotonic=self.monotonic)
