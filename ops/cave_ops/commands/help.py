"""``cave help``: list the commands, or show one command's options."""

from __future__ import annotations

import argparse

from ..cli import build_parser, discover
from ..context import Context

HELP = "List the commands, or show the options of one"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("topic", nargs="?", metavar="command", help="the command to describe")


def run(ctx: Context, args: argparse.Namespace) -> int:
    commands = discover()
    if args.topic is None:
        ctx.say("usage: cave <command> [options]\n\ncommands:")
        width = max(len(name) for name in commands)
        for command in commands.values():
            ctx.say(f"  {command.name:<{width}}  {command.help}")
        ctx.say("\nRun `cave help <command>` for the options of one command.")
        return 0
    if args.topic not in commands:
        ctx.warn(f"cave: unknown command {args.topic!r}; `cave help` lists them")
        return 2
    build_parser(commands).parse_args([args.topic, "--help"])  # prints and exits
    return 0
