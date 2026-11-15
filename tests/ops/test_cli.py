"""Command discovery, dispatch and the entry point."""

from __future__ import annotations

import runpy
import subprocess
import sys
import textwrap

import pytest

from cave_ops import cli
from cave_ops import commands as commands_package
from cave_ops.errors import CaveError

EXPECTED = ["check", "down", "help", "init", "logs", "ps", "restart", "up", "version"]


def test_the_commands_of_work_package_2_are_discovered():
    names = list(cli.discover())

    assert all(name in names for name in EXPECTED)
    assert names == sorted(names)


def test_help_lists_every_command_with_its_description(run_cave):
    result = run_cave("help")

    assert result.code == 0
    for name, command in cli.discover().items():
        assert name in result.out
        assert command.help in result.out


def test_help_for_one_command_shows_its_options(run_cave, capsys):
    result = run_cave("help", "up")

    assert result.code == 0
    assert "--no-wait" in capsys.readouterr().out


def test_unknown_command_is_refused(run_cave):
    result = run_cave("help", "no-such-command")

    assert result.code == 2
    assert "unknown command" in result.err


def test_no_command_prints_usage_and_fails(run_cave):
    assert run_cave().code == 2


def test_a_new_command_is_added_by_adding_a_module(run_cave, tmp_path, monkeypatch):
    extra = tmp_path / "extra_commands"
    extra.mkdir()
    (extra / "say_hello.py").write_text(
        textwrap.dedent(
            """
            HELP = "Say hello"

            def add_arguments(parser):
                parser.add_argument("who")

            def run(ctx, args):
                ctx.say("hello " + args.who)
                return 7
            """
        )
    )
    (extra / "_private.py").write_text("raise RuntimeError('must not be imported')\n")
    monkeypatch.setattr(commands_package, "__path__", [*commands_package.__path__, str(extra)])

    result = run_cave("say-hello", "world")

    assert (result.code, result.out) == (7, "hello world\n")
    assert "say-hello" in run_cave("help").out


def test_a_command_module_without_run_is_rejected(tmp_path, monkeypatch):
    extra = tmp_path / "broken_commands"
    extra.mkdir()
    (extra / "broken.py").write_text('HELP = "no run function"\n')
    monkeypatch.setattr(commands_package, "__path__", [*commands_package.__path__, str(extra)])

    with pytest.raises(CaveError, match="no run"):
        cli.discover()


def test_expected_failures_print_a_message_and_exit_1(run_cave):
    # No .env yet: every Compose command stops with advice instead of a traceback.
    result = run_cave("up")

    assert result.code == 1
    assert result.err.startswith("cave: ")
    assert "./cave init" in result.err
    assert "Traceback" not in result.err


def test_entry_point_runs_from_any_directory(repo_root, tmp_path):
    done = subprocess.run(
        [sys.executable, str(repo_root / "cave"), "help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "init" in done.stdout


def test_entry_point_stops_clearly_on_an_old_python(repo_root, monkeypatch, capsys):
    namespace = runpy.run_path(str(repo_root / "cave"), run_name="cave_under_test")
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))

    code = namespace["_main"]()

    assert code == 1
    message = capsys.readouterr().err
    assert "needs Python 3.11 or newer" in message
    assert "this is Python 3.9" in message
