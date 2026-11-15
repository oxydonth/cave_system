# cave_system

Local-first home automation for a flat in Germany, heating first.

Home Assistant runs everything on a small box at home. This repository holds the deployment, the heating logic, the dashboards, simulated devices for testing, and the pipeline that ships tested changes to the box.

- [Concept](docs/concept.md): what is being built, which hardware it is for, and why.
- [Work packages](docs/work-packages.md): how the build is split up, implemented and reviewed.
- [What to buy](docs/hardware.md): the shopping list, in the order to buy it.
- [Phone, presence and remote access](docs/phone-and-remote.md): the apps, notifications, presence and the FRITZ!Box VPN.

Status: the runtime stack can be installed ([docs/install.md](docs/install.md)); the heating logic and the rest are still being built (parts 2 and 3 of the phone guide apply once the system is installed).

## Development

Everything runs through [uv](https://docs.astral.sh/uv/) and `make`. The only prerequisites are `uv` and `make`; uv installs Python 3.14 and every tool, including ShellCheck and actionlint, from `uv.lock`. Docker is needed only for the stack and demo work packages, never for `make check`.

```sh
make setup   # install Python 3.14 and all dependencies
make check   # lint, type check and tests: everything that can run locally
```

| Command | Does |
|---|---|
| `make setup` | `uv sync --locked`: create `.venv` from `uv.lock` |
| `make lint` | Ruff (lint and format check), yamllint, ShellCheck, actionlint |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy `--strict` on `custom_components`, `sim` and `tools`; `ops` and `cave` against Python 3.11 |
| `make test` | pytest with coverage |
| `make test-py311` | The tests of the `cave` command (`tests/ops`) on Python 3.11, without Home Assistant |
| `make versions` | Home Assistant's version in `uv.lock`, `compose.yaml` and `ci.yml`, and its client libraries, agree |
| `make check` | `lint`, `typecheck`, `test`, `test-py311` and `versions`: everything that can run locally |

Configuration lives in `pyproject.toml` (dependencies, pytest, Ruff, mypy) and `.yamllint`. The pipeline is `.github/workflows/ci.yml`; the one check to wait for is `ci-ok`. The stack itself (`./cave init`, `./cave up`, [docs/install.md](docs/install.md)) and the `cave` command ([ops/README.md](ops/README.md)) need Docker; hassfest, Home Assistant's validator for integrations, and the `stack` job run only in the pipeline, so a green `make check` does not replace a green pipeline. After editing dependencies in `pyproject.toml`, run `uv lock`.

Contributors and coding agents: see [CLAUDE.md](CLAUDE.md) for the delivery rules; the concept and work packages linked above say what is being built and how.
