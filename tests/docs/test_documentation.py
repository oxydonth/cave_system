"""The documentation matches the repository (work package 9).

Four checks over every Markdown file of the repository:

* every relative link resolves to a file or directory, and a link to a heading of a Markdown file
  names a heading that exists (with GitHub's way of turning a heading into an anchor);
* every `cave` command written in code (`./cave apply`, `cave device-check`, a line of a code
  block) is a command of the `cave` tool, as `cave help` lists them;
* every `--option` written after such a command is one of that command's options;
* every path of the repository written in code (`scripts/provision-host.sh`, `sim/demo/`) exists.

`make check` runs this with the other tests, so a renamed file, a removed command or a typing
error in a guide fails before the push. The checkers are tested at the end of this file on
documents that are wrong.
"""

from __future__ import annotations

import argparse
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from cave_ops.cli import build_parser, discover

ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRECTORIES = {".git", ".venv", ".claude", "data", "node_modules", "__pycache__"}

# Directories and files at the root of the repository: a path in code that starts with one of
# them is checked. (A path such as `cave/heating.yaml` is state on the server, not in the
# repository, and is left alone.)
REPOSITORY_ROOTS = (
    "ops",
    "sim",
    "scripts",
    "docs",
    "tools",
    "tests",
    "custom_components",
    "homeassistant",
    "zigbee2mqtt",
    "mosquitto",
    ".github",
)
REPOSITORY_FILES = {
    "compose.yaml",
    "compose.sim.yaml",
    "Makefile",
    "CLAUDE.md",
    "README.md",
    ".env.example",
    "pyproject.toml",
    "uv.lock",
}

# Named in the documents as something that must not exist, or as an MQTT topic.
NOT_IN_THE_REPOSITORY = {"custom_components/__init__.py"}

FENCE = re.compile(r"^\s*(```|~~~)\s*(\w*)")
# Code in which a `cave` command is checked: inline code and shell blocks. A block of another
# kind (a directory tree, YAML, Python) has words that merely follow the word "cave".
SHELL = {"inline", "sh", "bash", "shell", "console", "zsh"}
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
LINK_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SCHEME = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")
SEPARATORS = {"|", "||", "&&", ";", ">", ">>", "<", "&"}


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not SKIPPED_DIRECTORIES & set(path.relative_to(ROOT).parts)
    )


def split_document(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str, str]]]:
    """The prose lines and the code of a document, each with its line number.

    Code is the content of fenced blocks and of inline code spans, with the language of the block
    (`inline` for a span); the spans are cut out of the prose lines, so that a link or a heading
    in an example is not taken for one.
    """
    prose: list[tuple[int, str]] = []
    code: list[tuple[int, str, str]] = []
    fence: str | None = None
    language = ""
    for number, line in enumerate(text.splitlines(), start=1):
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence, language = marker[1], marker[2]
            elif fence == marker[1]:
                fence = None
            continue
        if fence:
            code.append((number, line, language))
            continue
        code.extend((number, match[2], "inline") for match in INLINE_CODE.finditer(line))
        prose.append((number, INLINE_CODE.sub("", line)))
    return prose, code


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link keeps its text
    text = re.sub(r"[`*]|</?[a-z]+>", "", text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.strip().replace(" ", "-")


def anchors(text: str) -> set[str]:
    """The anchors of a Markdown document, with GitHub's -1, -2 for repeated headings."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else HEADING.match(line)
        if match:
            base = slug(match[2])
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def broken_links(path: Path, text: str, root: Path = ROOT) -> list[str]:
    """What is wrong with the relative links of one document, one message per problem."""
    problems: list[str] = []
    prose, _ = split_document(text)
    own_anchors = anchors(text)
    for number, line in prose:
        targets = [match[1] for match in LINK.finditer(line)]
        definition = LINK_DEFINITION.match(line)
        if definition:
            targets.append(definition[1])
        for target in targets:
            if SCHEME.match(target):
                continue
            where = f"{path.relative_to(root)}:{number}: ({target})"
            file_part, _, fragment = target.partition("#")
            if not file_part:
                if fragment and fragment not in own_anchors:
                    problems.append(f"{where} there is no heading with that anchor in this file")
                continue
            destination = (path.parent / file_part.split("?")[0]).resolve()
            if not destination.exists():
                problems.append(f"{where} {destination.relative_to(root.resolve())} does not exist")
            elif (
                fragment
                and destination.suffix == ".md"
                and fragment not in anchors(destination.read_text(encoding="utf-8"))
            ):
                problems.append(f"{where} {destination.name} has no heading with that anchor")
    return problems


def cave_invocations(code: str) -> Iterator[tuple[str, list[str]]]:
    """The ``cave`` commands written in one line of code, each with the words that follow it.

    A word counts as the tool when it is ``./cave``, ``/opt/cave/cave``, or ``cave`` at the start
    or after ``sudo`` or a shell separator (not the user name in ``-u cave``, not the directory in
    ``git -C /opt/cave``, not the word in a sentence). The command is the
    word after it, when that is a plain lower-case word; the words that follow, up to the end of
    the line or the next ``|``, ``&&`` or ``;``, are its arguments.
    """
    try:
        words = shlex.split(code, comments=True, posix=True)
    except ValueError:
        words = code.split()
    for index, word in enumerate(words):
        before = words[index - 1] if index else ""
        if word == "cave":
            # The tool as a bare word follows nothing, `sudo` or a shell separator; after anything
            # else it is a user name (`-u cave`), or the word "cave" in a sentence.
            if before and before not in {"sudo", "$", *SEPARATORS}:
                continue
        elif word != "./cave" and not word.endswith("/cave/cave"):
            continue
        following = words[index + 1 :]
        if not following or not re.fullmatch(r"[a-z][a-z-]*", following[0]):
            continue
        arguments: list[str] = []
        for argument in following[1:]:
            if argument in SEPARATORS:
                break
            arguments.append(argument)
        yield following[0], arguments


def command_options() -> dict[str, set[str]]:
    """Every command of the tool with the options its parser knows."""
    found = discover()
    parser = build_parser(found)
    options: dict[str, set[str]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                options[name] = {flag for a in sub._actions for flag in a.option_strings}
    return options


def wrong_commands(
    path: Path, text: str, options: dict[str, set[str]], root: Path = ROOT
) -> list[str]:
    """The commands and options of the tool that a document names but the tool does not have."""
    problems: list[str] = []
    _, code = split_document(text)
    for number, line, language in code:
        if language not in SHELL:
            continue
        for command, arguments in cave_invocations(line):
            where = f"{path.relative_to(root)}:{number}: `cave {command}`"
            if command not in options:
                problems.append(f"{where} is not a command; `cave help` lists {sorted(options)}")
                continue
            for argument in arguments:
                if argument.startswith("--"):
                    flag = argument.split("=")[0]
                    if flag not in options[command]:
                        problems.append(
                            f"{where} has no option {flag}; it has {sorted(options[command])}"
                        )
    return problems


def repository_paths(code: str) -> Iterator[str]:
    """The paths of the repository written in one piece of code."""
    for word in re.split(r"[\s'\"=(),;]+", code):
        word = word.removeprefix("./").rstrip(".:,")
        if not word or re.search(r"[<>*{}$|\\]", word):
            continue
        word = re.sub(r"(#.*|:\d+(-\d+)?)$", "", word)
        first = word.split("/")[0]
        is_file = word.endswith("/") or "." in word.rsplit("/", 1)[-1]
        if (
            ("/" in word and first in REPOSITORY_ROOTS and is_file) or word in REPOSITORY_FILES
        ) and (word not in NOT_IN_THE_REPOSITORY):
            yield word


def missing_paths(path: Path, text: str, root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    _, code = split_document(text)
    for number, line, _language in code:
        for candidate in repository_paths(line):
            if not (root / candidate).exists():
                problems.append(f"{path.relative_to(root)}:{number}: `{candidate}` does not exist")
    return problems


FILES = markdown_files()
IDS = [str(path.relative_to(ROOT)) for path in FILES]


def test_there_are_documents_to_check() -> None:
    assert {"README.md", "docs/install.md", "docs/concept.md"} <= set(IDS)


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_relative_links_resolve(path: Path) -> None:
    problems = broken_links(path, path.read_text(encoding="utf-8"))
    assert not problems, "\n" + "\n".join(problems)


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_cave_commands_and_options_exist(path: Path) -> None:
    problems = wrong_commands(path, path.read_text(encoding="utf-8"), command_options())
    assert not problems, "\n" + "\n".join(problems)


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_paths_of_the_repository_exist(path: Path) -> None:
    problems = missing_paths(path, path.read_text(encoding="utf-8"))
    assert not problems, "\n" + "\n".join(problems)


# --- the checkers, on documents that are wrong

DOCUMENT = """\
# Guide

See [the demo](demo.md), [a heading](#a-heading--with-punctuation), [there](other.md#second-part),
[gone](gone.md), [bad anchor](#nope), [outside](https://example.com/x), [mail](mailto:a@b.c)
and `[not a link](nowhere.md)`.

## A heading — with punctuation

```sh
sudo -u cave ./cave frobnicate --now
./cave demo down --timeout 5 --bogus
cave init | tee log
cat scripts/provision-host.sh docs/gone.md
```

Run `cave bootstrap`, then `/opt/cave/cave status`, but `cave` alone is the tool,
`sudo -u cave editor /opt/cave/.env` has a user name, `git -C /opt/cave fsck` a directory,
and `a state that cave does not know` a sentence.
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "provision-host.sh").write_text("#!/bin/sh\n")
    (tmp_path / "docs" / "demo.md").write_text("# Demo\n")
    (tmp_path / "docs" / "other.md").write_text("# Other\n\n## Second part\n\n## Second part\n")
    (tmp_path / "docs" / "guide.md").write_text(DOCUMENT)
    return tmp_path


def test_checker_finds_broken_links(tree: Path) -> None:
    path = tree / "docs" / "guide.md"
    problems = broken_links(path, path.read_text(), tree)
    assert len(problems) == 2, problems
    assert any("gone.md" in problem and "does not exist" in problem for problem in problems)
    assert any("(#nope)" in problem for problem in problems)


def test_checker_finds_commands_and_options_that_do_not_exist(tree: Path) -> None:
    path = tree / "docs" / "guide.md"
    options = {"demo": {"--timeout", "--data"}, "init": set(), "bootstrap": set(), "status": set()}
    problems = wrong_commands(path, path.read_text(), options, tree)
    assert len(problems) == 2, problems
    assert any("`cave frobnicate` is not a command" in problem for problem in problems)
    assert any("`cave demo` has no option --bogus" in problem for problem in problems)


def test_checker_finds_paths_that_do_not_exist(tree: Path) -> None:
    path = tree / "docs" / "guide.md"
    problems = missing_paths(path, path.read_text(), tree)
    assert problems == [f"docs/guide.md:{13}: `docs/gone.md` does not exist"]


def test_the_real_tool_has_the_commands_the_guides_use() -> None:
    options = command_options()
    assert {"init", "up", "apply", "bootstrap", "demo", "help"} <= set(options)
    assert "--timeout" in options["demo"]


def test_anchors_follow_githubs_rules() -> None:
    text = "# A — b\n\n## Same\n\n## Same\n\n```\n# not a heading\n```\n\n### `code` and *more*\n"
    assert anchors(text) == {"a--b", "same", "same-1", "code-and-more"}
