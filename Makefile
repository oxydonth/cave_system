# Developer entry points. `make check` runs everything that can run locally: lint, type check
# and tests. hassfest needs Docker and runs only in the pipeline.

UV ?= uv
# --locked fails instead of silently updating uv.lock; run `uv lock` after editing pyproject.toml.
RUN := $(UV) run --locked

# Never descend into the virtualenv, agent worktrees, runtime state or Git's own directory.
PRUNE := -path ./.git -prune -o -path ./.venv -prune -o -path ./.claude -prune -o -path ./data -prune -o

# mypy strict: only directories that exist. sim/ and tools/ arrive in later work packages.
# ops/ (the `cave` tool, Python 3.11+, standard library only) has its own mypy run with
# `--python-version 3.11`; see the note in pyproject.toml.
# A directory counts once it holds Python files: `./cave init` creates an empty tools/ (the
# mount source for Home Assistant), and mypy refuses a directory without any.
TYPED_DIRS := custom_components $(foreach dir,sim tools,$(if $(shell find $(dir) -name '*.py' -print -quit 2>/dev/null),$(dir)))
COV_ARGS := $(addprefix --cov=,$(TYPED_DIRS)) --cov=cave_ops
YAML_FILES = $(shell find . $(PRUNE) -type f \( -name '*.yml' -o -name '*.yaml' \) -print)
# A literal # cannot be written inside a function call in a way that make 3.81 (macOS) and 4.3
# (Linux) agree on, so it comes from a variable.
HASH := \#
# Shell scripts: every *.sh, plus the `cave` entry point only if it is a shell script.
SHELL_FILES = $(strip $(shell find . $(PRUNE) -type f -name '*.sh' -print) $(shell [ -f ./cave ] && head -n 1 ./cave | grep -qE '^[$(HASH)]!.*sh' && echo ./cave))

.DEFAULT_GOAL := help

.PHONY: help setup lint format typecheck test test-py311 versions check

help: ## List the targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/^([a-z]+):.*## (.*)/  make \1\t\2/'

setup: ## Install Python 3.14 and every dependency from uv.lock
	$(UV) sync --locked

lint: ## Ruff (lint and format check), yamllint, ShellCheck, actionlint
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) yamllint --strict $(YAML_FILES)
	@if [ -n "$(SHELL_FILES)" ]; then $(RUN) shellcheck $(SHELL_FILES); else echo "shellcheck: no shell scripts yet"; fi
	@# actionlint-py downloads its binary when it is installed. Locally a missing binary only skips
	@# this step; in the pipeline (CI=true) it is a failure.
	@if $(RUN) actionlint -version >/dev/null 2>&1; then $(RUN) actionlint; \
	elif [ -z "$$CI" ]; then echo "actionlint: binary unavailable, skipped (the pipeline runs it)"; \
	else echo "actionlint: binary unavailable"; exit 1; fi

format: ## Apply Ruff fixes and formatting
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

typecheck: ## mypy --strict on custom_components, sim and tools; ops and cave against Python 3.11
	$(RUN) mypy $(TYPED_DIRS)
	MYPYPATH=ops $(RUN) mypy --python-version 3.11 -p cave_ops
	MYPYPATH=ops $(RUN) mypy --python-version 3.11 cave

test: ## pytest with coverage
	$(RUN) pytest $(COV_ARGS) --cov-report=term-missing

# The tests of the `cave` command must also pass on the oldest Python it supports. They run in an
# isolated environment with only pytest (and PyYAML for the checks of the YAML files), so that
# nothing from Home Assistant can creep in. The configuration file is not named pytest.ini; see
# the note in pyproject.toml. uv downloads Python 3.11 when it is missing.
test-py311: ## The tests of tests/ops on Python 3.11, without Home Assistant
	$(UV) run --isolated --no-project --python 3.11 --with pytest --with pyyaml \
		pytest -c tests/ops/py311.ini --confcutdir=tests/ops tests/ops

versions: ## Home Assistant's version in uv.lock, compose.yaml and ci.yml, and its client libraries, agree
	PYTHONPATH=ops $(RUN) python -m cave_ops.versioncheck

check: lint typecheck test test-py311 versions ## Everything that can run locally; hassfest and the stack run only in the pipeline
