"""Running external programs (``docker``, ``git``) through one replaceable seam.

Commands never call ``subprocess`` themselves: they call ``ctx.runner`` (a ``Runner``), so tests
swap in the ``fake_runner`` fixture of ``tests/ops/conftest.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import CaveError


@dataclass(frozen=True)
class Result:
    """What a finished program left behind."""

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Standard output followed by standard error, for messages."""
        return self.stdout + self.stderr


class CommandError(CaveError):
    """A program exited with a failure and the caller asked for ``check=True``."""

    def __init__(self, result: Result) -> None:
        self.result = result
        tail = result.output.strip().splitlines()[-5:]
        detail = ("\n  " + "\n  ".join(tail)) if tail else ""
        super().__init__(
            f"`{' '.join(result.args)}` failed with status {result.returncode}{detail}"
        )


class Runner(Protocol):
    """Runs a program and reports what happened.

    ``capture=True`` collects the output in the ``Result``; ``capture=False`` lets the program
    write to the terminal (for ``logs`` and long-running ``up``), and the ``Result`` then has
    empty output. A program that cannot be started yields status 127 and a message in
    ``stderr`` rather than an exception. ``check=True`` raises ``CommandError`` on any failure.
    """

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
    ) -> Result: ...


class SubprocessRunner:
    """The real runner."""

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
        full_env = {**os.environ, **env} if env else None
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=full_env,
                input=input_text,
                capture_output=capture,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = Result(argv, done.returncode, done.stdout or "", done.stderr or "")
        except FileNotFoundError:
            result = Result(argv, 127, "", f"{argv[0]}: command not found\n")
        except subprocess.TimeoutExpired:
            result = Result(argv, 124, "", f"{argv[0]}: timed out after {timeout} seconds\n")
        if check and not result.ok:
            raise CommandError(result)
        return result
