# `ops/`: the `cave` command

The command-line tool that operates the stack (concept section 4.4). Python, **standard library
only**, and it must run on Python 3.11 or newer, because it runs on the server's own Python. The
entry point is `./cave` in the repository root; it stops with a clear message on an older Python.

Ruff and mypy check `ops/` and `cave` against Python 3.11 (`ops/ruff.toml`, `make typecheck`), and
the tests in `tests/ops/` run on 3.11 (`make test-py311`) and on 3.14 with the rest of the suite.
Nothing in `ops/` or in `tests/ops/` may import Home Assistant.

## Modules

Everything a command needs from the outside reaches it through the `Context`, so tests can replace
each part (`tests/ops/conftest.py`).

| Module | Provides |
|---|---|
| `cli.py` | Command discovery (`discover()`), the argument parser and `main(argv, root=, ctx=)`. A `CaveError` becomes `cave: <message>` and exit status 1. |
| `context.py` | `Context`: `paths`, `env` (`.env` overlaid by the process environment), `runner`, `out`/`err`, `ha()`, `lock(name)`, `docker(...)`, `compose(...)`, `git(...)`, `say()`/`warn()`, `journal`, `sleep`/`monotonic`, `compose_files` (the demo adds `compose.sim.yaml`). `Context.create(root)` builds the real one. |
| `paths.py` | `Paths(root, data)`: every path of concept sections 4.2 and 4.5 (`heating`, `heating_applied`, `dashboard`, `token`, `lock_file`, ...), and `container_path(host_path)` for the path inside the Home Assistant container. |
| `envfile.py` | The variable table of concept section 4.2 (`VARIABLES`), `.env` parsing and rendering. `.env.example` is rendered from the table: `PYTHONPATH=ops python3 -m cave_ops.envfile > .env.example`. |
| `runner.py` | `Runner` protocol and `SubprocessRunner`: `run(args, cwd=, env=, capture=, check=, input_text=, timeout=) -> Result(returncode, stdout, stderr)`. A missing program is status 127, not an exception; `check=True` raises `CommandError`. |
| `ha.py` | `read_token(paths)` and `HAClient`: `config()`, `get_state()`, `services()`, `has_service()`, `call_service()`. Failures are `HAError` (with `status`) and `HAUnreachable`. The transport is replaceable. |
| `health.py` | `check_health(ctx)` and `wait_healthy(ctx, timeout=, interval=)`: "Healthy" as defined in concept section 4.5, returning `Health(healthy, problems)`. |
| `notify.py` | `notify(ctx, title, message)`: the notification contract of concept section 4.5 (`script.cave_notify` called with the fields `title` and `message`, else `persistent_notification.create`, always also the journal). Never raises. |
| `container.py` | `run_in_homeassistant(ctx, command, entrypoint=, workdir=, capture=)`: a one-off command in the pinned Home Assistant image with the service's mounts (`/config`, `/cave/tools`); for everything that needs Home Assistant's own Python packages. |
| `heating.py` | `validate_and_store(ctx, source=None) -> ApplyResult`: runs the validator in a one-off container, and only if it passes stores the applied copy. Also `validate()` and `store_applied()`. |
| `lock.py` | `Lock(path, command)` and `ctx.lock(command)`: the one lock of concept section 4.5, `with ctx.lock("update"): ...`. Raises `LockBusy` naming the holder. |
| `mosquitto.py` | `password_line(user, password)`: a line of the broker's password file. |
| `versioncheck.py` | The version check behind `make versions`. |
| `errors.py` | `CaveError`: raise it for failures the user can act on. |

## How a command is added

A command is one module in `ops/cave_ops/commands/`. Nothing else is registered, so adding one
touches no other file. The module name is the command name with underscores as hyphens
(`device_check.py` is `cave device-check`); modules starting with `_` are helpers
(`_common.py` has `require_initialised(ctx)`).

```python
"""``cave example``: what it does."""

from __future__ import annotations

import argparse

from ..context import Context

HELP = "One line for `cave help`"


def add_arguments(parser: argparse.ArgumentParser) -> None:  # optional
    parser.add_argument("--flag", action="store_true")


def run(ctx: Context, args: argparse.Namespace) -> int:
    ctx.say("hello")
    return 0
```

Rules for commands:

* Run programs only through `ctx.compose(...)`, `ctx.docker(...)`, `ctx.git(...)` or `ctx.runner`,
  talk to Home Assistant only through `ctx.ha()`, write output with `ctx.say()`/`ctx.warn()`, and
  wait with `ctx.sleep()`; then the fixtures replace all of them.
* A command that uses Compose starts with `require_initialised(ctx)`.
* `update`, `backup` and `restore` hold `ctx.lock(...)`.
* Report every outcome of a script (`update`, `backup`) through `notify()`.
* Whatever needs Home Assistant's own Python packages runs in a one-off container, as
  `heating.validate()` does, never by importing Home Assistant here.
* Document the command in `docs/` where a user would look.

## Recipes

```python
# Validate a heating.yaml and store it as the applied copy (apply, demo). An invalid file
# changes nothing; the messages name the room and field and can be printed as they are.
result = validate_and_store(ctx, source)  # source=None: the edited cave/heating.yaml
if not result.ok:
    for line in result.messages:
        ctx.warn(line)
    return 1
# result.changed, result.applied_path (ctx.paths.heating_applied)

# Home Assistant's API; None means "no script token yet" (before `cave bootstrap`).
client = ctx.ha()
if client is None:
    ctx.say("Home Assistant has to be restarted instead.")
else:
    client.call_service("cave_heating", "reload")  # HAError / HAUnreachable are CaveErrors

# A one-off command in the Home Assistant image, with /config and /cave/tools mounted.
result = run_in_homeassistant(
    ctx, ["/cave/tools/render_dashboard.py", "/config/cave/.applied/heating.yaml"]
)

# Tell the owner, and the journal, without ever raising.
notify(ctx, "cave update", "Installed v2026.11.15.1")

# Hold the lock for the whole operation.
with ctx.lock("update"):
    health = wait_healthy(ctx, timeout=300)
```

## Testing a command

`tests/ops/conftest.py` documents the fixtures at its top. A typical test:

```python
def test_example(run_cave, fake_runner, fake_ha, write_token):
    fake_runner.on(["docker", "compose", "ps"], out="...")  # what the program prints
    write_token()  # the script token exists
    fake_ha.services["script"] = ["cave_notify"]  # Home Assistant has this service

    result = run_cave("example", "--flag")  # runs cave_ops.cli.main with ctx

    assert result.code == 0
    assert fake_runner.ran("docker", "compose", "ps")
    assert fake_ha.service_calls == [...]
```

An unexpected command fails the test, so a test states every program the command may run. Tests
must also pass on Python 3.11 (`make test-py311`): no syntax or library newer than 3.11.
