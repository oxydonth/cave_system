# cave_system

Local-first home automation on Home Assistant, heating first. Read [docs/concept.md](docs/concept.md)
(what and why) and [docs/work-packages.md](docs/work-packages.md) (how it is split and delivered)
before changing anything. Do not edit either to make your work fit; describe a mismatch in the pull request.

## Commands

| Command | Does |
|---|---|
| `make setup` | Install Python 3.14 and all dependencies from `uv.lock` (uv only; no Homebrew, pip or system packages) |
| `make check` | Everything the pipeline runs: `lint`, `typecheck`, `test`. Run it before every push |
| `make lint` | Ruff check and format check, yamllint, ShellCheck, actionlint |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy `--strict` on `custom_components`, `sim` and `tools` (the ones that exist) |
| `make test` | pytest with coverage |

hassfest needs Docker and runs only in the pipeline. Run any tool by hand with `uv run <tool>`.

## Delivery rules

- One branch `wp/<number>-<slug>` from the current `main` per work package, one pull request, squash merge.
  Nobody merges their own work. No direct pushes to `main`.
- The pull request description states what was built, how it was verified, and every deviation.
  A change to a contract (names in concept sections 4.1, 4.4, 5.1, 5.2) updates the concept in the same pull request.
- Done means: acceptance criteria demonstrated, pipeline green (`ci-ok`), review verdict `VERDICT: APPROVE`.
- Tests assert behaviour; a test that cannot fail does not count.
- No secrets, no commented-out code, no `TODO` without saying what is missing and why.
- Every new command, variable or configuration key is documented where a user would look for it.
- Touch only the work package's own paths plus its named shared files.

## Conventions and traps

- Dependencies live in the `dev` group of `pyproject.toml`. After editing it run `uv lock`; after a rebase
  regenerate `uv.lock` instead of merging it by hand.
- `.github/workflows/ci.yml` only lists jobs. Larger checks go in their own reusable workflow and are called
  from `ci.yml`; add each new job to `needs` and the `env` block of `ci-ok`.
- Pin GitHub Actions to a full commit hash with the version in a comment; look the hash up, never guess.
- Do not create `custom_components/__init__.py`: Home Assistant's loader needs a namespace package.
- `tests/conftest.py` imports `custom_components.cave_heating` and enables custom integrations for every test.
  Keep both, or the loader reports "Integration not found".
- Linters skip `.venv`, `.claude` and `data`. Keep it that way: the virtualenv is about 650 MB.
