"""Command discovery and dispatch.

Every module in ``cave_ops/commands/`` whose name does not start with an underscore is a
command. Its name is the module name with underscores turned into hyphens (``device_check.py``
is ``cave device-check``). A command module defines:

* ``HELP``: one line for ``cave help``;
* ``add_arguments(parser)`` (optional): declares its options on an ``argparse`` parser;
* ``run(ctx, args) -> int``: does the work and returns the exit status.

Nothing else is registered anywhere, so a work package adds a command by adding a module.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import commands as commands_package
from .context import Context
from .errors import CaveError

RunFunction = Callable[[Context, argparse.Namespace], int]


@dataclass(frozen=True)
class Command:
    """A discovered command."""

    name: str
    help: str
    run: RunFunction
    add_arguments: Callable[[argparse.ArgumentParser], None] | None


def discover() -> dict[str, Command]:
    """Import every module of ``cave_ops.commands`` and return the commands by name, sorted."""
    found: dict[str, Command] = {}
    for info in pkgutil.iter_modules(commands_package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{commands_package.__name__}.{info.name}")
        for required in ("HELP", "run"):
            if not hasattr(module, required):
                raise CaveError(f"command module {info.name} has no {required}")
        name = info.name.replace("_", "-")
        found[name] = Command(name, module.HELP, module.run, getattr(module, "add_arguments", None))
    return dict(sorted(found.items()))


def build_parser(commands: dict[str, Command]) -> argparse.ArgumentParser:
    """The parser for ``cave <command> ...``."""
    parser = argparse.ArgumentParser(
        prog="cave",
        description="Operate the cave_system stack. `cave help` lists the commands.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for command in commands.values():
        sub: Any = subparsers.add_parser(command.name, help=command.help, description=command.help)
        if command.add_arguments is not None:
            command.add_arguments(sub)
    return parser


def main(
    argv: Sequence[str] | None = None, *, root: Path | None = None, ctx: Context | None = None
) -> int:
    """Run ``cave`` and return its exit status.

    ``root`` is the checkout; ``ctx`` replaces the real context (tests).
    """
    commands = discover()
    parser = build_parser(commands)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    if not args.command:
        parser.print_help(file=sys.stderr)
        return 2
    if ctx is None:
        ctx = Context.create(root or Path.cwd())
    try:
        return commands[args.command].run(ctx, args)
    except CaveError as error:
        ctx.warn(f"cave: {error}")
        return 1
    except SystemExit as stop:  # `cave help <command>` prints argparse's help and exits
        return stop.code if isinstance(stop.code, int) else 1
    except KeyboardInterrupt:
        ctx.warn("cave: interrupted")
        return 130
