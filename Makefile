# Developer entry points. `make check` is exactly what the pipeline's lint and test jobs run
# (hassfest needs Docker and only runs in the pipeline).

UV ?= uv
# --locked fails instead of silently updating uv.lock; run `uv lock` after editing pyproject.toml.
RUN := $(UV) run --locked

# Never descend into the virtualenv, agent worktrees, runtime state or Git's own directory.
PRUNE := -path ./.git -prune -o -path ./.venv -prune -o -path ./.claude -prune -o -path ./data -prune -o

# mypy strict: only directories that exist. sim/ and tools/ arrive in later work packages.
TYPED_DIRS := custom_components $(wildcard sim tools)
COV_ARGS := $(addprefix --cov=,$(TYPED_DIRS))
YAML_FILES = $(shell find . $(PRUNE) -type f \( -name '*.yml' -o -name '*.yaml' \) -print)
SHELL_FILES = $(shell find . $(PRUNE) -type f \( -name '*.sh' -o -path './cave' -o -path './scripts/cave.d/*' \) -print)

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
	$(RUN) actionlint

format: ## Apply Ruff fixes and formatting
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

typecheck: ## mypy --strict on custom_components, sim and tools
	$(RUN) mypy $(TYPED_DIRS)

test: ## pytest with coverage
	$(RUN) pytest $(COV_ARGS) --cov-report=term-missing

check: lint typecheck test ## Everything the pipeline runs (except hassfest)
