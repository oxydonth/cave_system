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
| `make check` | Everything that can run locally: `lint`, `typecheck`, `test`, `test-py311`, `versions`. Run it before every push |
| `make lint` | Ruff check and format check, yamllint, ShellCheck, actionlint |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy `--strict` on `custom_components`, `sim` and `tools` (the ones that exist); `ops/` and `cave` against Python 3.11 |
| `make test` | pytest with coverage, in parallel: one process per CPU, or `PYTEST_WORKERS=<n>` (`0`: none) |
| `make test-py311` | `tests/ops` on Python 3.11 without Home Assistant, in parallel the same way (needs network once, for uv to fetch Python 3.11) |
| `make versions` | The Home Assistant version in `uv.lock`, `compose.yaml` and `ci.yml` agree; `paho-mqtt` and `pyfritzhome` match Home Assistant |

The pipeline's `lint` and `test` jobs run what `make check` runs, plus the provision-host dry run, hassfest and the
device matrix; these, the tests that need Docker and the job `e2e` (the stack, the simulator image, the end-to-end tests) run only in the pipeline. The `cave` command (`ops/`, see `ops/README.md`) is standard library only and runs on Python 3.11; run it here with `uv run python cave ...`. Run any tool by hand with `uv run <tool>`.

## Delivery rules

- One branch `wp/<number>-<slug>` from the current `main` per work package, one pull request, squash merge.
  Nobody merges their own work. No direct pushes to `main`.
- The repository is public since 23 November 2026. Branch protection and rulesets are available since then, but
  none is set up: until the owner sets one up, these rules, and the `ci-ok` check being green, are conventions the
  agents follow; GitHub does not enforce them.
- The pull request description states what was built, how it was verified, and every deviation.
  A change to a contract (concept sections 4.1, 4.2, 4.4, 4.5, 5.1 and 5.2) updates the concept in the same pull request.
- Done means: acceptance criteria demonstrated, review verdict `VERDICT: APPROVE`, and the branch rebased on the
  current `main` with the pipeline (`ci-ok`) green on its latest commit.
- The server rehearsal (`.github/workflows/server-rehearsal.yml`, `docs/operations.md`, "The server rehearsal") is
  not part of `ci-ok`. It runs on a pull request that changes `ops/`, `cave`, `scripts/`, `compose.yaml`,
  `homeassistant/`, `mosquitto/`, `tools/` or the rehearsal, and when it ran, it has to be green on the latest commit
  before the merge. It also runs weekly; GitHub turns off scheduled
  workflows in a public repository after 60 days without activity, so after a quiet spell check that it still runs
  (`gh workflow enable server-rehearsal.yml`).
- Tests assert behaviour; a test that cannot fail does not count.
- No secrets, no commented-out code, no `TODO` without saying what is missing and why.
- Every new command, variable or configuration key is documented where a user would look for it.
- Touch only the work package's own paths plus its named shared files.

## Conventions and traps

- Dependencies live in the `dev` group of `pyproject.toml`. After editing it run `uv lock`; after a rebase
  regenerate `uv.lock` instead of merging it by hand.
- The pipeline is `.github/workflows/ci.yml`; its jobs are listed at the top of that file and in "Shared files" of
  `docs/work-packages.md`. GitHub bills every job rounded up to the minute, so a new check is a step in the job that
  already has what it needs (`lint` for static checks, `test` for tests and hassfest, `e2e` for checks with
  containers: `.github/actions/stack/`, `.github/actions/sim/` or `e2e.yml` itself), with
  `if: ${{ !cancelled() }}` when it is not the first check of its job, except in `e2e`, whose parts depend on the
  one before (each needs the runner the previous part left clean) and keep the default. A new job is the last resort and goes into
  `needs` of `ci-ok`. `ci-ok` is the one check to look at: it fails when a job failed, was cancelled, or was skipped
  when it should have run. `stack.yml` and `sim.yml` run one check alone on demand (`gh workflow run`).
- A pull request that changes only Markdown files runs `lint` (with the document check, `tests/docs`) and skips the
  other jobs. So no test outside `tests/docs` may read a Markdown file. A push to `main` runs everything.
- Pin GitHub Actions to a full commit hash with the version in a comment; look the hash up, never guess.
- Do not create `custom_components/__init__.py`: Home Assistant's loader needs a namespace package.
- The root `tests/conftest.py` must stay free of Home Assistant, so that tests which do not need it (and the
  Python 3.11 tests of the planned `ops/` package) neither load nor pay for it. A test directory that uses the
  `hass` fixture has a `conftest.py` like `tests/cave_heating/conftest.py`, with its import of
  `custom_components.cave_heating` and the autouse `enable_custom_integrations` fixture. Keep both, or the loader
  reports "Integration not found".
- The Home Assistant version appears in `uv.lock` (through `pytest-homeassistant-custom-component`), in the hassfest
  image tag in `ci.yml` and in the image tag in `compose.yaml`. They are bumped together by hand, in one pull
  request; Dependabot ignores them and `make versions` enforces it. See the comment at `HASSFEST_IMAGE` in `ci.yml`.
- Linters skip `.venv`, `.claude` and the root `data/`, anchored to the repository root so that nested directories
  named `data` are still linted. Keep the virtualenv out of every linter's path: it is about 650 MB.
- Shared lint settings, including per-directory Ruff ignores for `tests/`, `sim/`, `ops/` and `tools/`, are in `pyproject.toml`.
