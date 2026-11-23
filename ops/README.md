# `ops/`: the `cave` command

The command-line tool that operates the stack (concept section 4.4). Python, **standard library
only**, and it must run on Python 3.11 or newer, because it runs on the server's own Python. The
entry point is `./cave` in the repository root; it stops with a clear message on an older Python.
Run as root (`update`, `backup`, `restore`), it writes no bytecode, so that no root-owned
`__pycache__` appears in the service user's checkout.

Ruff and mypy check `ops/` and `cave` against Python 3.11 (`ops/ruff.toml`, `make typecheck`), and
the tests in `tests/ops/` run on 3.11 (`make test-py311`) and on 3.14 with the rest of the suite.
Nothing in `ops/` or in `tests/ops/` may import Home Assistant.

## Modules

Everything a command needs from the outside reaches it through the `Context`, so tests can replace
each part (`tests/ops/conftest.py`).

| Module | Provides |
|---|---|
| `cli.py` | Command discovery (`discover()`), the argument parser and `main(argv, root=, ctx=)`. A `CaveError` becomes `cave: <message>` and exit status 1. |
| `context.py` | `Context`: `paths`, `env` (`.env` overlaid by the process environment), `runner`, `out`/`err`, `ha()`, `lock(name)`, `docker(...)`, `compose(...)`, `git(...)`, `say()`/`warn()`, `journal`, `sleep`/`monotonic`, `wall` (seconds since the epoch, for what is compared across runs), `compose_files`, `compose_env`. `Context.create(root)` builds the real one. See "Compose with overlays and environment" below. |
| `paths.py` | `Paths(root, data)`: every path of concept sections 4.2 and 4.5 (`heating`, `heating_applied`, `dashboard`, `token`, `lock_file`, `stopped_record`, `watch_record`, ...), and `container_path(host_path)` for the path inside the Home Assistant container. Also the container's paths of the dashboard generator and its files (`GENERATOR`, `APPLIED_IN_CONTAINER`, `DASHBOARD_IN_CONTAINER`), which `apply` and `demo` both use. |
| `envfile.py` | The variable table of concept section 4.2 (`VARIABLES`), `.env` parsing (`parse_env`: quotes, inline comments; no `$` interpolation, so use single quotes for values with `$`), `missing_names()` / `apply_updates()` to complete a `.env` in place, and rendering. `.env.example` is rendered from the table: `PYTHONPATH=ops python3 -m cave_ops.envfile > .env.example`. |
| `runner.py` | `Runner` protocol and `SubprocessRunner`: `run(args, cwd=, env=, capture=, check=, input_text=, timeout=) -> Result(returncode, stdout, stderr)`. A missing program is status 127 and a timeout is status 124, not exceptions; `check=True` raises `CommandError`. Nothing in `cave` runs unbounded by accident: `ctx.git()` defaults to 120 s, the health check asks Docker with 60 s, `run_in_homeassistant` defaults to 600 s, which also has to cover the first pull of the image (about 2 GB): pull first with `ctx.compose("pull", timeout=None)` where a slow link is likely. |
| `ha.py` | `read_token(paths)` and `HAClient`: `config()`, `get_state()`, `states()`, `services()`, `has_service()`, `call_service()`. Failures are `HAError` (with `status`) and `HAUnreachable`. The transport is replaceable. |
| `health.py` | `check_health(ctx)` and `wait_healthy(ctx, timeout=, interval=)`: "Healthy" as defined in concept section 4.5, returning `Health(healthy, problems, running)`; `running` counts the services whose container runs (`None`: Docker could not be asked), `stopped` is `running == 0`, and `state()` says it in the fixed words an alert may carry. Without the script token it proves: every container of the active profiles runs and passes its health check, which for Home Assistant includes that it logged "Home Assistant initialized". It does not prove that Home Assistant is out of recovery mode. With the token it also requires `/api/config` to report `state` RUNNING, no `recovery_mode`/`safe_mode`, and `cave_heating` set up. |
| `notify.py` | `notify(ctx, title, message)`: the notification contract of concept section 4.5 (`script.cave_notify` called with the fields `title` and `message`, else `persistent_notification.create`, always also the journal). Never raises. |
| `alert.py` | Alerts without Home Assistant (concept section 4.5), only when `CAVE_ALERT_URL` is set: `send(ctx, title, body)` posts ntfy's format with `urllib`, no proxy, no redirect, at most `TIMEOUT` (10) seconds in a thread it abandons after that, never raises, never prints the URL. `warn_owner(ctx, command, headline, happened, health=None, about_the_stack=False)` is what a command calls for a warning: fixed words only, never its message; `about_the_stack` marks an alert that tells of the stack itself, which then also counts as the watch's alert for the incident. `Memory`, `load_memory`, `save_memory`, `mark_alerted`: what `cave watch` has sent (`CAVE_DATA/.cave/watch.json`). |
| `records.py` | `write_atomic(path, data, mode, writer=)` (`writer` gives each of several writers its own temporary file), and the record of a stack stopped on purpose (`CAVE_DATA/.cave/stopped.json`): `note_stopped(paths, what, at)`, `stopped(paths)`, `clear_stopped(paths, at)`, and `read_stopped` / `restore_stopped` for a command that must put it back after a failure. |
| `container.py` | `run_in_homeassistant(ctx, command, entrypoint=, workdir=, capture=)`: a one-off command in the pinned Home Assistant image with the service's mounts (`/config`, `/cave/tools`); for everything that needs Home Assistant's own Python packages. |
| `heating.py` | `validate_and_store(ctx, source=None) -> ApplyResult`: reads the file **once**, validates a staged copy of exactly those bytes in a one-off container, and only if it passes stores those same bytes as the applied copy. Also `validate()`, `validation_result()` (the validator's `Result` itself, to tell a rejection from a validator that could not run) and `store_applied(ctx, path_or_bytes)`. |
| `lock.py` | `Lock(path, command, sleep=, monotonic=)` and `ctx.lock(command)`: the one lock of concept section 4.5, `with ctx.lock("update"): ...`. Raises `LockBusy` naming the holder after trying for `PATIENCE` (2) seconds; the holder is named only while its process lives. Root and the service user take it in either order: it needs no write access to the file (a read-only holder only cannot write its name), and root gives `.cave` and the lock file to the owner of `CAVE_DATA` (a linked `.cave` is refused). `held_by(path)` says who holds it without taking it (for `status` and `cave watch`): an instant's shared lock, which the patience covers. |
| `mosquitto.py` | `password_line(user, password)`: a line of the broker's password file; `accepts(text, user, password)`: whether a password file lets that login in. |
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
* `apply`, `bootstrap`, `update`, `backup` and `restore` hold `ctx.lock(...)`; `apply`, `bootstrap`
  and `backup` also refuse while `_update_state.maintenance_pending(ctx)` names an interrupted
  update or a restore that stopped half-way. `demo` (its own state), `device-check` (no state
  of its own) and `watch` (it only reads `held_by`) take no lock.
* Report every outcome of a script (`update`, `backup`) through `notify()`, and every outcome whose
  message is a warning also through `alert.warn_owner()`, with fixed words.
* A command that starts the home's stack calls `records.clear_stopped()` once it has started it or
  changed the state; one that stops it on purpose calls `records.note_stopped()`. A command that
  refuses or fails before it changes anything leaves the record as it was.
* Whatever needs Home Assistant's own Python packages runs in a one-off container, as
  `heating.validate()` does, never by importing Home Assistant here.
* Document the command in `docs/` where a user would look.

## Compose with overlays and environment

`ctx.compose(...)` runs `docker compose` from the checkout. By default Compose chooses its own
files (`compose.yaml` and, if it exists, `compose.override.yaml`). Set `ctx.compose_files = ("compose.sim.yaml",)` to add overlays: the context then names `compose.yaml`, the local
`compose.override.yaml` if present, and the overlays, in that order, because any `-f` stops
Compose from reading its default files. Set `ctx.compose_env = {"CAVE_DATA": "/srv/demo"}` (or pass
`env={...}` to a call) to give Compose environment variables; Compose lets the environment win over
`.env`, which is how the demo gets a state directory of its own. Pass a copy of the context, not
the original, when only one command should use them (`dataclasses.replace(ctx, compose_files=...)`).

`CAVE_DATA` in `compose_env` alone does not make a separate demo. The demo also needs its own
Compose project name (`COMPOSE_PROJECT_NAME` in `compose_env`; the project is `cave` otherwise, so
`up` would replace the home's containers) and its own `ctx.paths` (`Paths(root, demo_data)`), because
the token, the applied copy and `validate_and_store` follow `ctx.paths`, which still points at the
home's state.

## Hand-over to the work packages that build on this

* Files that containers create in the state directory (the script token, Home Assistant's
  `.storage`, Mosquitto's data) are owned by root or by the container's user, mode 0600 or
  similar. **Bootstrap** must give the script token to the user that runs `./cave`. **Backup** and
  **restore** cannot read `.storage` or Mosquitto's data as that user: they need `docker compose
  exec`/`run` as root inside the containers, or root.
* `homeassistant/configuration.yaml` sets no core setting (name, time zone, units, country,
  currency, location): any of them in YAML would lock them all in the UI. **Bootstrap** has to
  set the time zone from `TZ`, metric units, country and currency through Home Assistant, or
  Home Assistant stays on UTC; the owner sets the home's location in the UI or in onboarding.
* `init` records which broker login it wrote into `passwd` in `CAVE_DATA/.cave/mqtt-credentials`, because after the first start the broker's container owns `passwd` and `init` cannot read it. The record can only mislead if the password file is changed outside `init` while it is unreadable; the broker login is changed only through `.env` and `init`.
* `script.cave_notify` is called with the fields `title` and `message`.
* `init` stores the starter as the applied copy without the validator. Once the validator has
  merged, a later work package may switch `init` to `validate_and_store`.

## Update, backup and restore

`update`, `status`, `backup`, `restore` and `watch` share helpers that only they use, in
`commands/_server.py` (which user may run them, Git as the owner of the checkout,
`check_out_exactly` after an interrupted checkout, which returns what it left and what it removed,
copying files with their owners, `state_entries` (never the commands' own records), and
`extract`, which checks an archive as a whole and never writes outside its target; use it for
every archive that root unpacks), `commands/_update_state.py` (the update record
and the snapshots), `commands/_archives.py` (backup archives, SQLite's online backup) and
`commands/_releases.py` (release tags; also run by the pipeline's release job) and
`commands/_images.py` (removing the images of releases no longer needed, at the end of an update
run). Their working files live in `CAVE_DATA/.cave/ops/`, which neither a snapshot nor a backup
contains, and neither contains the lock, `stopped.json` nor `watch.json`. Every other entry of
`CAVE_DATA`, including the rest of `CAVE_DATA/.cave/`, is in both. docs/operations.md
explains them to the owner; their tests are in `tests/ops/maintenance/`.

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

An unexpected command fails the test, so a test states every program the command may run. The
tests of `tests/ops/maintenance/` fake Docker's image store too (`server.runner.images`), and
`alert_endpoint` is a local HTTP receiver for `CAVE_ALERT_URL`. Tests
must also pass on Python 3.11 (`make test-py311`): no syntax or library newer than 3.11.
