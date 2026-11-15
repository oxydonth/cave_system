# cave_system

Local-first home automation for a flat in Germany, heating first.

Home Assistant runs everything on a small box at home. This repository holds the deployment, the heating logic, the dashboards, simulated devices for testing, and the pipeline that ships tested changes to the box.

- [Concept](docs/concept.md): what is being built, which hardware it is for, and why.
- [Work packages](docs/work-packages.md): how the build is split up, implemented and reviewed.

Status: concept stage. Nothing here is deployable yet.

## Development

Everything runs through [uv](https://docs.astral.sh/uv/) and `make`. The only prerequisites are `uv` and `make`; uv installs Python 3.14 and every tool, including ShellCheck and actionlint, from `uv.lock`. Docker is needed only for the stack and demo work packages, never for `make check`.

```sh
make setup   # install Python 3.14 and all dependencies
make check   # lint, typecheck and test: what the pipeline runs, apart from hassfest
```

| Command | Does |
|---|---|
| `make setup` | `uv sync --locked`: create `.venv` from `uv.lock` |
| `make lint` | Ruff (lint and format check), yamllint, ShellCheck, actionlint |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy `--strict` on `custom_components`, `sim` and `tools` |
| `make test` | pytest with coverage |
| `make check` | `lint`, `typecheck` and `test` |

Configuration lives in `pyproject.toml` (dependencies, pytest, Ruff, mypy) and `.yamllint`. The pipeline is `.github/workflows/ci.yml`; its single required check is `ci-ok`. hassfest, Home Assistant's validator for integrations, runs only there because it needs Docker. After editing dependencies in `pyproject.toml`, run `uv lock`.

Contributors and coding agents: see [CLAUDE.md](CLAUDE.md) for the delivery rules, and the [concept](docs/concept.md) and [work packages](docs/work-packages.md) for what is being built and how.
