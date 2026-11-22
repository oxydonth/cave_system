# cave_system

Local-first home automation for a flat in Germany, heating first. Every radiator is controlled per
room: on a schedule, paused while a window is open, set back while nobody is home, with a boost
button. Home Assistant provides the device support, the web UI and the phone apps; this repository
provides the deployment, the heating logic, the heating dashboard, simulated devices for testing,
and the pipeline that ships tested changes to the server.

Everything runs on a small server at home. Every control path works with the internet unplugged: the
internet is used only to fetch updates and, if you want them, push notifications to a phone away
from home. No vendor cloud is involved. The reasons for each decision are in the
[concept](docs/concept.md).

## What it needs

| | |
|---|---|
| A server | A mini PC (Intel N100 or N150, 8 GB or more, SSD) or a Raspberry Pi 4 or 5 with 4 GB or more, running Debian 13 or Raspberry Pi OS 64-bit. |
| Radiator thermostats | Sonoff TRV Gen2 or TRVZB, Bosch Thermostat II [+M], or FRITZ!Smart Thermo 302, for hot-water radiators. |
| Room sensors and window contacts | Sonoff SNZB-02P and SNZB-04P. |
| A Zigbee coordinator | SMLIGHT SLZB-06MG24 over Ethernet. Not needed for FRITZ! thermostats alone. |
| A FRITZ!Box | For FRITZ! thermostats, for knowing which phones are at home, and for the VPN to reach the system from outside. |
| A GitHub account | With access to this private repository: the server pulls tested releases from it. |

What to buy, how many, in what order and for roughly how much is in [hardware.md](docs/hardware.md).

## From nothing to a heated flat

Do these in order, once. Each guide is written to be read from its top and acted on.

| Step | Guide | Result |
|---|---|---|
| 1. Try it without hardware | [The demo](docs/demo.md) | The whole system with simulated devices on any machine with Docker, in a few minutes. |
| 2. Buy | [What to buy](docs/hardware.md) | The base first, then a trial room. |
| 3. Install the server | [Installing the stack](docs/install.md) | A running, set-up Home Assistant with its broker, a nightly update and a nightly backup. |
| 4. Pair and describe the rooms | [Pairing devices and describing rooms](docs/pairing.md) | Thermostats and sensors paired, rooms applied, each thermostat checked. |
| 5. Phone and access from outside | [Phone, presence and remote access](docs/phone-and-remote.md) | The app, notifications, heating set back when nobody is home, the FRITZ!Box VPN. |
| 6. Check what only hardware can show | [Hardware acceptance](docs/hardware-acceptance.md) | A checklist, worked through once. |

Afterwards: [updates, backups and what to do when one fails](docs/operations.md), [the heating dashboard
and the notifications](docs/dashboard-and-notifications.md), and [what is tested on which
device, and how to check a real one](docs/devices.md).

## The demo

`./cave demo` starts the whole system with simulated devices (three rooms with Zigbee and FRITZ!
thermostats, room sensors and window contacts, timings in seconds), and says where to open it. It needs only Docker and a checkout of this repository, not
a server and not hardware. [docs/demo.md](docs/demo.md) says what to look at and how to open a
window, turn a dial or take a thermostat offline from the command line.

## Documentation

| Document | For |
|---|---|
| [Concept](docs/concept.md) | What is built and why; the contracts between the parts |
| [Work packages](docs/work-packages.md) | How the build was split up, implemented and reviewed |
| [`cave_heating`](custom_components/cave_heating/README.md) | The heating integration: the file format, the entities, the services |
| [`cave` command](ops/README.md) | The command-line tool and how a command is added |
| [Simulated devices](sim/README.md) | Every simulated topic and endpoint, and the differences from the real devices |

## Development

Everything runs through [uv](https://docs.astral.sh/uv/) and `make`. The only prerequisites are `uv` and `make`; uv installs Python 3.14 and every tool, including ShellCheck and actionlint, from `uv.lock`. Docker is needed for the stack and the demo, never for `make check`.

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
| `make test` | pytest with coverage: the unit tests, the device tests, and the check that the documentation matches the repository (below) |
| `make test-py311` | The tests of the `cave` command (`tests/ops`) on Python 3.11, without Home Assistant |
| `make versions` | Home Assistant's version in `uv.lock`, `compose.yaml` and `ci.yml`, and its client libraries, agree |
| `make check` | `lint`, `typecheck`, `test`, `test-py311` and `versions`: everything that can run locally |

Configuration lives in `pyproject.toml` (dependencies, pytest, Ruff, mypy) and `.yamllint`. The pipeline is `.github/workflows/ci.yml`; the one check to wait for is `ci-ok`. The stack itself (`./cave init`, `./cave up`, [docs/install.md](docs/install.md)) and the `cave` command ([ops/README.md](ops/README.md)) need Docker; hassfest, Home Assistant's validator for integrations, the `stack` job and the `e2e` job (the demo and its scenarios, and the heating dashboard opened in a real browser, whose screenshots are an artifact of the run) run only in the pipeline, so a green `make check` does not replace a green pipeline. After editing dependencies in `pyproject.toml`, run `uv lock`.

Contributors and coding agents: see [CLAUDE.md](CLAUDE.md) for the delivery rules; the concept and work packages linked above say what is being built and how.

**The documentation is checked.** `tests/docs/test_documentation.py` runs with `make test`: every relative link in a Markdown file must resolve (including the heading it names), every `cave` command and option written in code must exist, and every path of the repository written in code must exist. Rename a file, remove a command or mistype one in a guide and `make check` says where.

**The browser check.** `tests/e2e/dashboard_browser.py` opens the heating dashboard of a running demo in headless Chromium, at a desktop and a phone width, and fails if a card shows an error. To run it against your own demo, install the browser once and point it at the demo: `uv run playwright install chromium`, then `uv run python tests/e2e/dashboard_browser.py --user owner --password <the demo's CAVE_HA_OWNER_PASSWORD from its demo.env> --thermostat climate.cave_living_room --out screenshots` ([docs/demo.md](docs/demo.md#the-browser-check) has the rest).
