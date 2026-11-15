# cave_system

Local-first home automation on Home Assistant, heating first. Read [docs/concept.md](docs/concept.md)
(what and why) and [docs/work-packages.md](docs/work-packages.md) (how it is split and delivered)
before changing anything. Do not edit either to make your work fit; describe a mismatch in the pull request.
The exception is a contract change (the names in concept sections 4.1, 4.2, 4.4, 4.5, 5.1 and 5.2), which
updates the concept in the same pull request.

## Commands

| Command | Does |
|---|---|
| `make setup` | Install Python 3.14 and all dependencies from `uv.lock` (uv only; no Homebrew, pip or system packages) |
| `make check` | Everything that can run locally: `lint`, `typecheck`, `test`. Run it before every push |
| `make lint` | Ruff check and format check, yamllint, ShellCheck, actionlint |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy `--strict` on `custom_components`, `sim` and `tools` (the ones that exist) |
| `make test` | pytest with coverage |

hassfest needs Docker and runs only in the pipeline. Run any tool by hand with `uv run <tool>`.

## Delivery rules

- One branch `wp/<number>-<slug>` from the current `main` per work package, one pull request, squash merge.
  Nobody merges their own work. No direct pushes to `main`.
- Branch protection is not available on this private repository (the GitHub API refuses it without a paid
  plan). These rules, and the `ci-ok` check being green, are conventions the agents follow; GitHub does not enforce them.
- The pull request description states what was built, how it was verified, and every deviation.
  A change to a contract (concept sections 4.1, 4.2, 4.4, 4.5, 5.1 and 5.2) updates the concept in the same pull request.
- Done means: acceptance criteria demonstrated, review verdict `VERDICT: APPROVE`, and the branch rebased on the
  current `main` with the pipeline (`ci-ok`) green on its latest commit.
- Tests assert behaviour; a test that cannot fail does not count.
- No secrets, no commented-out code, no `TODO` without saying what is missing and why.
- Every new command, variable or configuration key is documented where a user would look for it.
- Touch only the work package's own paths plus its named shared files.

## Conventions and traps

- Dependencies live in the `dev` group of `pyproject.toml`. After editing it run `uv lock`; after a rebase
  regenerate `uv.lock` instead of merging it by hand.
- `.github/workflows/ci.yml` only lists jobs. Larger checks go in their own reusable workflow and are called
  from `ci.yml`; add each new job to `needs` of `ci-ok`, which checks every job listed there and needs nothing else.
- Pin GitHub Actions to a full commit hash with the version in a comment; look the hash up, never guess.
- Do not create `custom_components/__init__.py`: Home Assistant's loader needs a namespace package.
- The root `tests/conftest.py` must stay free of Home Assistant, so that tests which do not need it (and the
  Python 3.11 tests of the planned `ops/` package) neither load nor pay for it. A test directory that uses the
  `hass` fixture has a `conftest.py` like `tests/cave_heating/conftest.py`, with its import of
  `custom_components.cave_heating` and the autouse `enable_custom_integrations` fixture. Keep both, or the loader
  reports "Integration not found".
- The Home Assistant version appears in `uv.lock` (through `pytest-homeassistant-custom-component`), in the hassfest
  image tag in `ci.yml` and later in the image tag in `compose.yaml`. They are bumped together by hand, in one pull
  request; Dependabot ignores them. See the comment at the hassfest step.
- Linters skip `.venv`, `.claude` and the root `data/`, anchored to the repository root so that nested directories
  named `data` are still linted. Keep the virtualenv out of every linter's path: it is about 650 MB.
- Shared lint settings, including per-directory Ruff ignores for `tests/`, `sim/`, `ops/` and `tools/`, are in `pyproject.toml`.
