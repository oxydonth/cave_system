# Developer entry points. `make check` runs everything that can run locally: lint, type check
# and tests. hassfest needs Docker and runs only in the pipeline.

UV ?= uv
# --locked fails instead of silently updating uv.lock; run `uv lock` after editing pyproject.toml.
RUN := $(UV) run --locked

# Never descend into the virtualenv, agent worktrees, runtime state or Git's own directory.
PRUNE := -path ./.git -prune -o -path ./.venv -prune -o -path ./.claude -prune -o -path ./data -prune -o

# mypy strict: only directories that exist. sim/ and tools/ arrive in later work packages.
# Planned: ops/ (the `cave` tool, Python 3.11+, standard library only) gets its own mypy run
# with `--python-version 3.11` next to this one; see the note in pyproject.toml.
TYPED_DIRS := custom_components $(wildcard sim tools)
COV_ARGS := $(addprefix --cov=,$(TYPED_DIRS))
YAML_FILES = $(shell find . $(PRUNE) -type f \( -name '*.yml' -o -name '*.yaml' \) -print)
# Shell scripts: every *.sh, plus the `cave` entry point only if it is a shell script.
SHELL_FILES = $(strip $(shell find . $(PRUNE) -type f -name '*.sh' -print) $(shell [ -f ./cave ] && head -n 1 ./cave | grep -qE '^\#!.*sh' && echo ./cave))

.DEFAULT_GOAL := help

.PHONY: help setup lint format typecheck test check

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

typecheck: ## mypy --strict on custom_components, sim and tools
	$(RUN) mypy $(TYPED_DIRS)

test: ## pytest with coverage
	$(RUN) pytest $(COV_ARGS) --cov-report=term-missing

check: lint typecheck test ## Everything that can run locally; hassfest runs only in the pipeline
