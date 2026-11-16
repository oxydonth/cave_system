#!/usr/bin/env python3
"""First-run setup of Home Assistant: what ``cave bootstrap`` runs (concept sections 4.5 and 8).

It runs inside the Home Assistant container, on its Python, against the Home Assistant that is
running, and talks to it only through the HTTP and WebSocket APIs its frontend uses. The settings,
including the passwords, arrive as one JSON object on standard input, so they never appear in a
process listing; ``ops/cave_ops/commands/bootstrap.py`` builds it from ``.env``.

Every step first looks at what exists and changes only what is missing, so a second run changes
nothing and a run that was interrupted is completed by the next one:

1. Wait until Home Assistant answers.
2. Get a session: the stored script token if it is still the valid ``cave-scripts`` token of an
   administrator and onboarding is complete; otherwise create the owner (onboarding's first step)
   or log in as the owner with the password from ``.env``. A login session is revoked at the end.
3. Set the time zone, metric units, country and currency, once: only while Home Assistant has no
   country yet, which is how it looks before anyone has set it up. Later changes in the UI stay.
4. Finish the onboarding steps that are still open, so that the web UI opens normally.
5. The script token: keep a valid one; otherwise delete any ``cave-scripts`` token left in Home
   Assistant (its file was lost) and create a new one. The file is ``/config/.cave/token``, mode
   0600, owned by the owner of its directory, which ``cave init`` created as the user who runs
   ``./cave``; so that user, and only that user, can read it on the host.
6. MQTT, and with FRITZ!Box credentials FRITZ!SmartHome and FRITZ!Box Tools, through their config
   flows, answering a menu where a flow opens with one. An MQTT entry whose broker login differs
   from ``.env`` is reconfigured.
7. A schedule helper with a default timetable for every room of the applied ``heating.yaml`` that
   names one, with exactly the entity ID the room names.

Secrets are never printed: every line goes through ``Output``, which masks them, and errors from
requests that carried a secret never include the response body.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import aiohttp
import yaml

TOKEN_NAME = "cave-scripts"  # noqa: S105 - the token's name (concept section 4.5), not a secret
TOKEN_LIFESPAN_DAYS = 3650
LONG_LIVED = "long_lived_access_token"
ONBOARDING_STEPS = ("user", "core_config", "analytics", "integration")
# The language of the default areas that onboarding creates; Home Assistant's own default.
ONBOARDING_LANGUAGE = "en"
# What onboarding's core_config step sets up by itself, besides `met` (which declines without a
# home location): radio_browser and google_translate reach the internet when used.
ONBOARDING_INTEGRATIONS = ("radio_browser", "google_translate", "shopping_list")
# Home Assistant's own settings that configuration.yaml leaves to the UI (concept section 4.2).
UNIT_SYSTEM = "metric"
COUNTRY = "DE"
CURRENCY = "EUR"

# The default timetable of a room's schedule helper: on means comfort, off means eco.
WORKDAY = [{"from": "06:00:00", "to": "08:00:00"}, {"from": "17:00:00", "to": "22:00:00"}]
WEEKEND = [{"from": "08:00:00", "to": "22:00:00"}]
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_WEEK: dict[str, list[dict[str, str]]] = {
    day: WORKDAY if day not in ("saturday", "sunday") else WEEKEND for day in DAYS
}
SCHEDULE_ICON = "mdi:calendar-clock"
SWITCH_ICON = "mdi:home-account"

POLL_INTERVAL = 2.0  # seconds between two looks at a condition that is being waited for
REQUEST_TIMEOUT = 60.0  # seconds for one request; a config flow may contact a device
ENTRY_TIMEOUT = 90.0  # seconds for a new config entry to be set up


class BootstrapError(Exception):
    """A failure the owner can act on. The message never contains a secret."""


# ------------------------------------------------------------------------------------ settings
@dataclass(frozen=True)
class Login:
    username: str
    password: str


@dataclass(frozen=True)
class Settings:
    """What ``cave bootstrap`` passes on standard input."""

    owner_name: str
    owner: Login
    time_zone: str
    mqtt_host: str
    mqtt_port: int
    mqtt: Login
    fritz_host: str | None = None
    fritz: Login | None = None
    url: str = "http://127.0.0.1:8123"
    token_file: Path = Path("/config/.cave/token")
    heating_file: Path = Path("/config/cave/.applied/heating.yaml")
    config_entries_file: Path = Path("/config/.storage/core.config_entries")
    timeout: float = 300.0
    fritz_tools: bool = True
    """Set up FRITZ!Box Tools with a FRITZ!Box. The demo's simulated box has no TR-064."""
    switches: tuple[tuple[str, str], ...] = ()
    """``(entity ID, name)`` of ``input_boolean`` helpers to create, switched on once created;
    the demo's stand-in for presence."""
    onboarding_wait: float = 60.0
    """How long to wait for the integrations onboarding sets up by itself to appear."""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Settings:
        def text(key: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise BootstrapError(f"setting {key!r} is missing or empty")
            return value

        fritz_host = data.get("fritz_host") or None
        fritz: Login | None = None
        if fritz_host is not None:
            fritz = Login(text("fritz_user"), text("fritz_password"))
        optional: dict[str, Any] = {}
        for key in ("token_file", "heating_file", "config_entries_file"):
            if data.get(key):
                optional[key] = Path(str(data[key]))
        if data.get("url"):
            optional["url"] = str(data["url"]).rstrip("/")
        if data.get("timeout") is not None:
            optional["timeout"] = float(data["timeout"])
        if data.get("onboarding_wait") is not None:
            optional["onboarding_wait"] = float(data["onboarding_wait"])
        if "fritz_tools" in data:
            optional["fritz_tools"] = bool(data["fritz_tools"])
        switches = data.get("switches") or []
        if not isinstance(switches, list) or not all(
            isinstance(item, dict)
            and str(item.get("entity_id", "")).startswith("input_boolean.")
            and _is_slug(str(item["entity_id"]).partition(".")[2])
            and item.get("name")
            for item in switches
        ):
            raise BootstrapError("setting 'switches' must list input_boolean entity IDs with names")
        optional["switches"] = tuple((str(s["entity_id"]), str(s["name"])) for s in switches)
        return cls(
            owner_name=text("owner_name"),
            owner=Login(text("owner_user"), text("owner_password")),
            time_zone=text("time_zone"),
            mqtt_host=text("mqtt_host"),
            mqtt_port=int(data.get("mqtt_port", 1883)),
            mqtt=Login(text("mqtt_user"), text("mqtt_password")),
            fritz_host=str(fritz_host) if fritz_host else None,
            fritz=fritz,
            **optional,
        )

    @property
    def secrets(self) -> list[str]:
        logins = [self.owner, self.mqtt, *([self.fritz] if self.fritz else [])]
        return [login.password for login in logins]

    @property
    def fritz_url(self) -> str:
        """FRITZ!SmartHome's address: ``CAVE_FRITZ_HOST``, with ``http://`` if it has none."""
        host = self.fritz_host or ""
        return host if "://" in host else f"http://{host}"

    @property
    def fritz_tools_host(self) -> str:
        """FRITZ!Box Tools wants the host name alone; it finds the TR-064 port itself."""
        return urlsplit(self.fritz_url).hostname or (self.fritz_host or "")


# -------------------------------------------------------------------------------------- output
class Output:
    """Prints progress, masking every secret, and records what was changed."""

    def __init__(self, secrets: Sequence[str], stream: TextIO | None = None) -> None:
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)
        self._stream = stream if stream is not None else sys.stdout
        self.changes: list[str] = []

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "********")
        return text

    def say(self, message: str) -> None:
        print(self.mask(message), file=self._stream, flush=True)

    def changed(self, message: str) -> None:
        self.changes.append(message)
        self.say(f"  changed: {message}")

    def unchanged(self, message: str) -> None:
        self.say(f"  ok:      {message}")


# ------------------------------------------------------------------------ Home Assistant's APIs
@dataclass
class Reply:
    status: int
    body: Any

    def message(self) -> str:
        if isinstance(self.body, dict) and "message" in self.body:
            return str(self.body["message"])
        return str(self.body)[:200]


class Api:
    """Home Assistant's HTTP API. Returns status and decoded body; the caller judges them."""

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self.session = session
        self.url = url

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: Any = None,
        form: Mapping[str, str] | None = None,
    ) -> Reply:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self.session.request(
                method,
                self.url + path,
                json=payload,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                raw = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BootstrapError(
                f"cannot reach Home Assistant at {self.url}: {type(error).__name__}"
            ) from error
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        return Reply(status, body)

    async def expect(
        self,
        method: str,
        path: str,
        what: str,
        *,
        token: str | None = None,
        payload: Any = None,
        form: Mapping[str, str] | None = None,
        secret: bool = False,
    ) -> Any:
        """``request``, failing unless it is 200; ``secret`` keeps the body out of errors."""
        reply = await self.request(method, path, token=token, payload=payload, form=form)
        if reply.status != 200:
            detail = "" if secret else f": {reply.message()}"
            raise BootstrapError(f"{what}: Home Assistant answered {reply.status}{detail}")
        return reply.body


class TokenRejected(BootstrapError):
    """The WebSocket API did not accept the token."""


class WebSocket:
    """Home Assistant's WebSocket API, authenticated with an access token."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.replace("http", "ws", 1) + "/api/websocket"
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0

    async def __aenter__(self) -> WebSocket:
        try:
            self._ws = await self._session.ws_connect(self._url)
            hello = await self._ws.receive_json(timeout=REQUEST_TIMEOUT)
            if hello.get("type") != "auth_required":
                raise BootstrapError(f"unexpected WebSocket greeting: {hello.get('type')}")
            await self._ws.send_json({"type": "auth", "access_token": self._token})
            answer = await self._ws.receive_json(timeout=REQUEST_TIMEOUT)
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as error:
            await self.close()
            raise BootstrapError(
                f"Home Assistant's WebSocket API failed: {type(error).__name__}"
            ) from error
        if answer.get("type") != "auth_ok":
            await self.close()
            raise TokenRejected("Home Assistant did not accept the access token")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def call(self, kind: str, **fields: Any) -> Any:
        """Send one command and return its result; a failure raises ``BootstrapError``."""
        if self._ws is None:
            raise BootstrapError(f"{kind}: the WebSocket is not open")
        self._next_id += 1
        message_id = self._next_id
        try:
            await self._ws.send_json({"id": message_id, "type": kind, **fields})
            while True:
                answer = await self._ws.receive_json(timeout=REQUEST_TIMEOUT)
                if answer.get("id") == message_id and answer.get("type") == "result":
                    break
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as error:
            raise BootstrapError(f"{kind}: no answer from Home Assistant") from error
        if not answer.get("success"):
            failure = answer.get("error") or {}
            raise BootstrapError(f"{kind} failed: {failure.get('code')}: {failure.get('message')}")
        return answer.get("result")


async def wait_for(
    condition: Callable[[], Awaitable[Any]],
    what: str,
    limit: float,
    interval: float = POLL_INTERVAL,
) -> Any:
    """Await ``condition()`` until it returns something truthy; fail after ``limit`` seconds.

    ``condition`` may raise ``BootstrapError`` while the thing is not there yet; the last such
    error is part of the timeout's message, which says what was being waited for.
    """
    deadline = time.monotonic() + limit
    last = ""
    while True:
        try:
            result = await condition()
            if result:
                return result
        except BootstrapError as error:
            last = f" (last: {error})"
        if time.monotonic() >= deadline:
            raise BootstrapError(f"gave up after {limit:.0f} s waiting for {what}{last}")
        await asyncio.sleep(interval)


# ----------------------------------------------------------------------------------- the steps
@dataclass
class Session:
    """An access token, and the refresh token to revoke afterwards if this run logged in."""

    access_token: str
    refresh_token: str | None = None


@dataclass
class StoredToken:
    """What the token file holds and what Home Assistant says about it."""

    value: str | None
    valid: bool = False
    usable: bool = False
    """A valid ``cave-scripts`` long-lived token of an administrator: kept as it is."""
    notes: list[str] = field(default_factory=list)


class Bootstrap:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession, out: Output) -> None:
        self.settings = settings
        self.api = Api(session, settings.url)
        self.session = session
        self.out = out
        self.client_id = f"{settings.url}/"
        self.redirect_uri = f"{settings.url}/?auth_callback=1"

    # --- 1. Home Assistant answers
    async def onboarding_status(self) -> dict[str, bool]:
        """Which onboarding steps are done, once Home Assistant answers.

        A Home Assistant that starts with onboarding complete does not register the onboarding
        views at all (``components/onboarding/__init__.py``), so 404 means every step is done.
        The HTTP server starts listening once the frontend is set up (``components/http``), and
        the frontend depends on onboarding, so the 404 never comes from an earlier moment.
        """

        async def answered() -> dict[str, bool] | None:
            reply = await self.api.request("GET", "/api/onboarding")
            if reply.status == 404:
                return dict.fromkeys(ONBOARDING_STEPS, True)
            if reply.status != 200 or not isinstance(reply.body, list):
                raise BootstrapError(f"/api/onboarding answered {reply.status}")
            return {item["step"]: bool(item["done"]) for item in reply.body}

        done: dict[str, bool] = await wait_for(
            answered,
            f"Home Assistant to answer at {self.settings.url}/api/onboarding",
            self.settings.timeout,
        )
        return done

    # --- 2. a session
    def read_token_file(self) -> str | None:
        try:
            value = self.settings.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError, UnicodeDecodeError:
            return None
        return value or None

    async def inspect_token(self, value: str | None) -> StoredToken:
        stored = StoredToken(value)
        if value is None:
            stored.notes.append("there is no script token yet")
            return stored
        if not (value.isascii() and value.isprintable()) or " " in value:
            stored.notes.append("the script token file does not hold a token")
            return stored
        try:
            async with WebSocket(self.session, self.settings.url, value) as ws:
                stored.valid = True
                user = await ws.call("auth/current_user")
                tokens = await ws.call("auth/refresh_tokens")
        except TokenRejected:
            stored.notes.append("Home Assistant no longer accepts the stored script token")
            return stored
        current = next((t for t in tokens if t.get("is_current")), None)
        if current is None or current.get("client_name") != TOKEN_NAME:
            stored.notes.append(f"the stored token is not the {TOKEN_NAME} token")
        elif current.get("type") != LONG_LIVED:
            stored.notes.append("the stored token is not a long-lived token")
        elif not user.get("is_admin"):
            stored.notes.append("the stored token belongs to a user who is not an administrator")
        else:
            stored.usable = True
        return stored

    async def create_owner(self) -> str | None:
        """Onboarding's first step: the owner account. Returns an authorisation code."""
        owner = self.settings.owner
        reply = await self.api.request(
            "POST",
            "/api/onboarding/users",
            payload={
                "client_id": self.client_id,
                "name": self.settings.owner_name,
                "username": owner.username,
                "password": owner.password,
                "language": ONBOARDING_LANGUAGE,
            },
        )
        if reply.status == 200:
            self.out.changed(f"created the owner account {owner.username!r}")
            return str(reply.body["auth_code"])
        if reply.status == 403:  # someone completed this step in the meantime
            return None
        raise BootstrapError(
            f"Home Assistant refused to create the owner {owner.username!r} ({reply.status}): "
            f"{reply.message()}"
        )

    async def log_in(self) -> str:
        """Log in as the owner with the password from ``.env``; return an authorisation code."""
        owner = self.settings.owner
        flow = await self.api.expect(
            "POST",
            "/auth/login_flow",
            "starting a login",
            payload={
                "client_id": self.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": self.redirect_uri,
            },
        )
        result = await self.api.expect(
            "POST",
            f"/auth/login_flow/{flow['flow_id']}",
            "logging in",
            payload={
                "client_id": self.client_id,
                "username": owner.username,
                "password": owner.password,
            },
            secret=True,
        )
        if result.get("type") == "create_entry":
            return str(result["result"])
        errors = result.get("errors") or {}
        if errors.get("base") == "invalid_auth":
            raise BootstrapError(
                f"Home Assistant refused the login {owner.username!r} with the password from "
                ".env (CAVE_HA_OWNER_USER, CAVE_HA_OWNER_PASSWORD). The owner exists already, so "
                "the bootstrap needs that login to create the script token. If the password was "
                "changed in Home Assistant, put the same password into .env; the bootstrap never "
                "changes the owner's password."
            )
        raise BootstrapError(
            f"the login as {owner.username!r} stopped at step {result.get('step_id')!r} "
            f"({result.get('type')}, errors {errors}); the bootstrap supports a plain password "
            "login only"
        )

    async def open_session(self, user_step_done: bool) -> Session:
        code = None if user_step_done else await self.create_owner()
        if code is None:
            code = await self.log_in()
        tokens = await self.api.expect(
            "POST",
            "/auth/token",
            "exchanging the authorisation code for a token",
            form={"grant_type": "authorization_code", "code": code, "client_id": self.client_id},
            secret=True,
        )
        return Session(str(tokens["access_token"]), str(tokens["refresh_token"]))

    async def close_session(self, session: Session) -> None:
        """Revoke this run's login, so that it leaves no session behind in Home Assistant."""
        if session.refresh_token is None:
            return
        try:
            await self.api.request("POST", "/auth/revoke", form={"token": session.refresh_token})
        except BootstrapError as error:
            self.out.say(f"warning: could not end the login session: {error}")

    # --- 3. Home Assistant's own settings
    async def core_settings(self, ws: WebSocket, token: str) -> None:
        config = await self.api.expect(
            "GET", "/api/config", "reading the configuration", token=token
        )
        wanted = {
            "time_zone": self.settings.time_zone,
            "unit_system": UNIT_SYSTEM,
            "country": COUNTRY,
            "currency": CURRENCY,
        }
        if config.get("country") is None:
            await ws.call("config/core/update", **wanted)
            self.out.changed(
                f"set the time zone {self.settings.time_zone}, {UNIT_SYSTEM} units, "
                f"country {COUNTRY} and currency {CURRENCY}"
            )
            return
        self.out.unchanged(
            f"time zone {config.get('time_zone')}, country {config.get('country')}, "
            f"currency {config.get('currency')} (set before; changes in the UI are kept)"
        )
        if config.get("time_zone") != self.settings.time_zone:
            self.out.say(
                f"  note:    Home Assistant's time zone {config.get('time_zone')} differs from "
                f"TZ={self.settings.time_zone} in .env; change one of them so that they agree"
            )

    # --- 4. onboarding
    async def finish_onboarding(self, done: Mapping[str, bool], token: str) -> bool:
        """Complete the open steps; return whether this run completed ``core_config``."""
        open_steps = [step for step in ONBOARDING_STEPS[1:] if not done.get(step)]
        if not open_steps and done.get("user"):
            self.out.unchanged("onboarding is complete")
            return False
        completed_core_config = False
        for step in open_steps:
            payload: dict[str, str] = {}
            if step == "integration":
                payload = {"client_id": self.client_id, "redirect_uri": self.redirect_uri}
            reply = await self.api.request(
                "POST", f"/api/onboarding/{step}", token=token, payload=payload
            )
            if reply.status == 403 and "already done" in reply.message():
                continue
            if reply.status != 200:
                raise BootstrapError(
                    f"onboarding step {step}: Home Assistant answered {reply.status}: "
                    f"{reply.message()}"
                )
            self.out.changed(f"completed the onboarding step {step}")
            completed_core_config = completed_core_config or step == "core_config"
        return completed_core_config

    # --- integrations that onboarding adds by itself
    async def remove_onboarding_integrations(self, token: str, wait: bool) -> None:
        """Remove what onboarding's ``core_config`` step sets up without being asked.

        Home Assistant starts their flows in the background when that step completes
        (``components/onboarding/views.py``). A local-only system does not carry integrations
        that reach the internet when used. Only entries onboarding created (source
        ``onboarding``) are removed, so the same integration added later by hand stays. In the
        run that completed the step, the entries are awaited first, so that a later run finds
        nothing left to remove.
        """

        async def onboarding_entries() -> list[dict[str, Any]]:
            found = []
            for domain in ONBOARDING_INTEGRATIONS:
                found += [
                    entry
                    for entry in await self.entries(token, domain)
                    if entry.get("source") == "onboarding"
                ]
            return found

        if wait:
            deadline = time.monotonic() + self.settings.onboarding_wait
            while True:
                domains = {entry["domain"] for entry in await onboarding_entries()}
                if domains >= set(ONBOARDING_INTEGRATIONS):
                    break
                if time.monotonic() >= deadline:
                    missing = sorted(set(ONBOARDING_INTEGRATIONS) - domains)
                    self.out.say(
                        f"  note:    onboarding did not set up {', '.join(missing)} within "
                        f"{self.settings.onboarding_wait:.0f} s; nothing to remove there"
                    )
                    break
                await asyncio.sleep(POLL_INTERVAL)
        entries = await onboarding_entries()
        if not entries:
            self.out.unchanged(
                f"none of {', '.join(ONBOARDING_INTEGRATIONS)} (set up by onboarding) is left"
            )
            return
        for entry in entries:
            await self.api.expect(
                "DELETE",
                f"/api/config/config_entries/entry/{entry['entry_id']}",
                f"removing {entry['domain']}",
                token=token,
            )
            self.out.changed(
                f"removed {entry['domain']} ({entry.get('title')}), which onboarding set up and "
                "which reaches the internet when used"
            )

    # --- 5. the script token
    async def ensure_token(self, ws: WebSocket, stored: StoredToken) -> None:
        path = self.settings.token_file
        if stored.usable:
            self.out.unchanged(f"script token {TOKEN_NAME} in {path}")
            self.fix_token_file(path)
            return
        for note in stored.notes:
            self.out.say(f"  note:    {note}")
        tokens = await ws.call("auth/refresh_tokens")
        for old in tokens:
            if old.get("client_name") == TOKEN_NAME and old.get("type") == LONG_LIVED:
                await ws.call("auth/delete_refresh_token", refresh_token_id=old["id"])
                self.out.changed(f"deleted the old {TOKEN_NAME} token, whose file was lost")
        value = await ws.call(
            "auth/long_lived_access_token", client_name=TOKEN_NAME, lifespan=TOKEN_LIFESPAN_DAYS
        )
        write_token_file(path, str(value))
        self.out.changed(f"created the script token {TOKEN_NAME} in {path}")

    def fix_token_file(self, path: Path) -> None:
        """Mode 0600 and the owner of the directory; changed only where it differs."""
        info, directory = path.stat(), path.parent.stat()
        if info.st_mode & 0o777 != 0o600:
            path.chmod(0o600)
            self.out.changed(f"set the mode of {path} to 0600")
        if (info.st_uid, info.st_gid) != (directory.st_uid, directory.st_gid):
            os.chown(path, directory.st_uid, directory.st_gid)
            self.out.changed(f"gave {path} to the owner of its directory")

    # --- 6. integrations
    async def entries(self, token: str, domain: str) -> list[dict[str, Any]]:
        body = await self.api.expect(
            "GET",
            f"/api/config/config_entries/entry?domain={domain}",
            f"listing the {domain} entries",
            token=token,
        )
        return list(body)

    async def run_flow(
        self,
        token: str,
        handler: str,
        forms: Mapping[str, Mapping[str, Any]],
        menus: Mapping[str, str] | None = None,
        *,
        entry_id: str | None = None,
        success: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Drive a config flow: answer each form by its step, choose in each menu.

        A form that comes back with errors, an abort not listed in ``success`` and any other
        result stop the flow (it is removed again) and raise an error naming the step and the
        error keys, never the values, because Home Assistant may echo a password there.
        """
        start: dict[str, Any] = {"handler": handler, "show_advanced_options": False}
        if entry_id is not None:
            start["entry_id"] = entry_id
        result = await self.api.expect(
            "POST",
            "/api/config/config_entries/flow",
            f"starting {handler}",
            token=token,
            payload=start,
        )
        flow_id = result.get("flow_id")
        try:
            for _ in range(10):
                kind, step = result.get("type"), result.get("step_id")
                if kind == "create_entry":
                    return dict(result)
                if kind == "abort":
                    if result.get("reason") in success:
                        return dict(result)
                    flow_id = None  # an aborted flow is gone already
                    raise BootstrapError(
                        f"{handler}: Home Assistant aborted: {result.get('reason')}"
                    )
                if kind == "menu":
                    choice = (menus or {}).get(str(step))
                    if choice is None or choice not in result.get("menu_options", []):
                        raise BootstrapError(
                            f"{handler}: unexpected menu {step!r} with the options "
                            f"{list(result.get('menu_options', []))}"
                        )
                    answer: Mapping[str, Any] = {"next_step_id": choice}
                elif kind == "form":
                    if result.get("errors"):
                        raise BootstrapError(f"{handler}: step {step!r} failed: {result['errors']}")
                    if step not in forms:
                        raise BootstrapError(f"{handler}: unexpected step {step!r}")
                    answer = forms[str(step)]
                else:
                    raise BootstrapError(f"{handler}: unexpected {kind!r} at step {step!r}")
                result = await self.api.expect(
                    "POST",
                    f"/api/config/config_entries/flow/{flow_id}",
                    f"{handler} at step {step!r}",
                    token=token,
                    payload=dict(answer),
                    secret=True,
                )
            raise BootstrapError(f"{handler}: the flow did not finish")
        except BootstrapError:
            if flow_id is not None:
                await self.api.request(
                    "DELETE", f"/api/config/config_entries/flow/{flow_id}", token=token
                )
            raise

    async def wait_loaded(self, token: str, domain: str) -> None:
        async def loaded() -> bool:
            states = [entry.get("state") for entry in await self.entries(token, domain)]
            if states and all(state == "loaded" for state in states):
                return True
            raise BootstrapError(f"{domain} is {', '.join(map(str, states)) or 'missing'}")

        await wait_for(loaded, f"Home Assistant to set up {domain}", ENTRY_TIMEOUT)

    def stored_entry_data(self, entry_id: str) -> dict[str, Any] | None:
        """An entry's data from Home Assistant's storage (no API has it); ``None`` if unknown."""
        try:
            stored = json.loads(self.settings.config_entries_file.read_text(encoding="utf-8"))
            entries = stored["data"]["entries"]
        except OSError, ValueError, KeyError, TypeError:
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
                data = entry.get("data")
                return dict(data) if isinstance(data, dict) else None
        return None

    async def ensure_mqtt(self, token: str) -> None:
        settings = self.settings
        broker = {
            "broker": settings.mqtt_host,
            "port": settings.mqtt_port,
            "username": settings.mqtt.username,
            "password": settings.mqtt.password,
            "other_settings": {"set_client_cert": False, "set_ca_cert": "off", "transport": "tcp"},
        }
        target = f"{settings.mqtt_host}:{settings.mqtt_port} as {settings.mqtt.username!r}"
        existing = await self.entries(token, "mqtt")
        if not existing:
            await self.run_flow(token, "mqtt", {"broker": broker}, {"user": "broker"})
            self.out.changed(f"set up MQTT with the broker {target}")
            await self.wait_loaded(token, "mqtt")
            return
        entry = existing[0]
        data = self.stored_entry_data(str(entry["entry_id"]))
        wanted = {key: broker[key] for key in ("broker", "port", "username", "password")}
        if data is not None and any(data.get(key) != value for key, value in wanted.items()):
            await self.run_flow(
                token,
                "mqtt",
                {"broker": broker},
                entry_id=str(entry["entry_id"]),
                success=("reconfigure_successful",),
            )
            self.out.changed(f"changed the MQTT broker login to the one in .env ({target})")
            await self.wait_loaded(token, "mqtt")
            return
        self.out.unchanged(f"MQTT is set up ({entry.get('state')})")

    async def ensure_fritz(self, token: str) -> None:
        settings = self.settings
        if settings.fritz is None:
            self.out.unchanged(
                "no FRITZ!Box in .env (CAVE_FRITZ_HOST): FRITZ!SmartHome and FRITZ!Box Tools "
                "are not set up"
            )
            return
        login = settings.fritz
        integrations = (
            (
                "fritzbox",
                "FRITZ!SmartHome",
                {
                    "url": settings.fritz_url,
                    "username": login.username,
                    "password": login.password,
                },
            ),
            (
                "fritz",
                "FRITZ!Box Tools",
                {
                    "host": settings.fritz_tools_host,
                    "username": login.username,
                    "password": login.password,
                },
            ),
        )
        for domain, title, form in integrations:
            if domain == "fritz" and not settings.fritz_tools:
                self.out.unchanged(f"{title} is left out by request (it needs TR-064)")
                continue
            existing = await self.entries(token, domain)
            if existing:
                self.out.unchanged(f"{title} is set up ({existing[0].get('state')})")
                continue
            await self.run_flow(token, domain, {"user": form}, success=("already_configured",))
            self.out.changed(f"set up {title} for {settings.fritz_url}")
            await self.wait_loaded(token, domain)

    # --- 7. schedule helpers
    def rooms_with_schedules(self) -> list[tuple[str, str, str]]:
        """``(room name, object ID, entity ID)`` of each applied room that names a schedule."""
        path = self.settings.heating_file
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.out.say(f"  note:    {path} does not exist, so there are no rooms yet")
            return []
        except (OSError, yaml.YAMLError) as error:
            raise BootstrapError(f"cannot read {path}: {error}") from error
        rooms = (loaded or {}).get("rooms") or [] if isinstance(loaded, dict) else []
        found = []
        for room in rooms:
            if not isinstance(room, dict) or not room.get("schedule"):
                continue
            entity_id = str(room["schedule"])
            domain, _, object_id = entity_id.partition(".")
            if domain != "schedule" or not object_id or not _is_slug(object_id):
                raise BootstrapError(
                    f"room {room.get('id')!r}: schedule {entity_id!r} is not a schedule entity ID"
                )
            found.append((str(room.get("name") or room.get("id")), object_id, entity_id))
        return found

    async def ensure_schedules(self, ws: WebSocket, token: str) -> None:
        wanted = self.rooms_with_schedules()
        if not wanted:
            self.out.unchanged("no room in the applied heating.yaml names a schedule")
            return
        items = {item["id"]: item for item in await ws.call("schedule/list")}
        registry = await ws.call("config/entity_registry/list")
        by_entity = {entry["entity_id"]: entry for entry in registry}
        for room_name, object_id, entity_id in wanted:
            title = f"{room_name} heating schedule"
            entry = by_entity.get(entity_id)
            if entry is None:
                state = await self.api.request("GET", f"/api/states/{entity_id}", token=token)
                if state.status == 200:  # an entity without a registry entry, e.g. from YAML
                    self.out.unchanged(f"schedule {entity_id} exists (defined outside the UI)")
                    continue
            else:
                item = (
                    items.get(entry.get("unique_id"))
                    if entry.get("platform") == "schedule"
                    else None
                )
                if item is not None and item.get("name") == object_id:
                    # An earlier run stopped between creating the helper and naming it.
                    await self.rename_schedule(ws, item, title)
                    self.out.changed(f"named the schedule {entity_id} {title!r}")
                else:
                    self.out.unchanged(f"schedule {entity_id} exists (its timetable is left alone)")
                continue
            # Created under its object ID as name, so that Home Assistant derives exactly the
            # entity ID the room names, and a run that stops before the readable name is set is
            # recognised by the next one. If Home Assistant still chose another ID, the registry
            # entry is renamed to the one the room names.
            item = await ws.call(
                "schedule/create", name=object_id, icon=SCHEDULE_ICON, **DEFAULT_WEEK
            )
            created = await self.registry_entry_for(ws, str(item["id"]))
            if created["entity_id"] != entity_id:
                await ws.call(
                    "config/entity_registry/update",
                    entity_id=created["entity_id"],
                    new_entity_id=entity_id,
                )
            await self.rename_schedule(ws, item, title)
            self.out.changed(
                f"created the schedule {entity_id} ({title}) with the default timetable"
            )

    async def registry_entry_for(
        self, ws: WebSocket, unique_id: str, platform: str = "schedule"
    ) -> dict[str, Any]:
        async def registered() -> dict[str, Any] | None:
            for entry in await ws.call("config/entity_registry/list"):
                if entry.get("platform") == platform and entry.get("unique_id") == unique_id:
                    return dict(entry)
            return None

        found: dict[str, Any] = await wait_for(
            registered, f"the entity of the new {platform} {unique_id}", 30.0, interval=0.5
        )
        return found

    async def ensure_switches(self, ws: WebSocket, token: str) -> None:
        """The ``input_boolean`` helpers of the settings (the demo's presence), switched on.

        Made like the schedules: created under the object ID as name so the entity ID comes out
        as asked, switched on, then named; an existing entity is left alone.
        """
        if not self.settings.switches:
            return
        items = {item["id"]: item for item in await ws.call("input_boolean/list")}
        by_entity = {e["entity_id"]: e for e in await ws.call("config/entity_registry/list")}
        for entity_id, name in self.settings.switches:
            object_id = entity_id.partition(".")[2]
            entry = by_entity.get(entity_id)
            if entry is None:
                state = await self.api.request("GET", f"/api/states/{entity_id}", token=token)
                if state.status == 200:
                    self.out.unchanged(f"{entity_id} exists (defined outside the UI)")
                    continue
            else:
                item = (
                    items.get(entry.get("unique_id"))
                    if entry["platform"] == "input_boolean"
                    else None
                )
                if item is not None and item.get("name") == object_id:
                    await self.finish_switch(ws, token, entity_id, item, name)
                    self.out.changed(f"switched on and named {entity_id} {name!r}")
                else:
                    self.out.unchanged(f"{entity_id} exists (its state is left alone)")
                continue
            item = await ws.call("input_boolean/create", name=object_id, icon=SWITCH_ICON)
            created = await self.registry_entry_for(ws, str(item["id"]), "input_boolean")
            if created["entity_id"] != entity_id:
                await ws.call(
                    "config/entity_registry/update",
                    entity_id=created["entity_id"],
                    new_entity_id=entity_id,
                )
            await self.finish_switch(ws, token, entity_id, item, name)
            self.out.changed(f"created {entity_id} ({name}), switched on")

    async def finish_switch(
        self, ws: WebSocket, token: str, entity_id: str, item: Mapping[str, Any], name: str
    ) -> None:
        await self.api.expect(
            "POST",
            "/api/services/input_boolean/turn_on",
            f"switching on {entity_id}",
            token=token,
            payload={"entity_id": entity_id},
        )
        await ws.call(
            "input_boolean/update",
            input_boolean_id=item["id"],
            name=name,
            icon=item.get("icon") or SWITCH_ICON,
        )

    async def rename_schedule(self, ws: WebSocket, item: Mapping[str, Any], name: str) -> None:
        days = {day: item.get(day, []) for day in DAYS}
        await ws.call(
            "schedule/update",
            schedule_id=item["id"],
            name=name,
            icon=item.get("icon") or SCHEDULE_ICON,
            **days,
        )

    # --- everything
    async def run(self) -> None:
        out = self.out
        out.say(f"Home Assistant at {self.settings.url}")
        done = await self.onboarding_status()
        stored = await self.inspect_token(self.read_token_file())
        complete = all(done.get(step) for step in ONBOARDING_STEPS)
        session = (
            Session(str(stored.value))
            if complete and stored.usable
            else await self.open_session(user_step_done=bool(done.get("user")))
        )
        try:
            if done.get("user"):
                out.unchanged(f"the owner account exists (login {self.settings.owner.username!r})")
            async with WebSocket(self.session, self.settings.url, session.access_token) as ws:
                await self.core_settings(ws, session.access_token)
                onboarded_now = await self.finish_onboarding(done, session.access_token)
                await self.ensure_token(ws, stored)
                await self.ensure_mqtt(session.access_token)
                await self.ensure_fritz(session.access_token)
                await self.ensure_schedules(ws, session.access_token)
                await self.ensure_switches(ws, session.access_token)
                await self.remove_onboarding_integrations(session.access_token, onboarded_now)
        finally:
            await self.close_session(session)


def _is_slug(value: str) -> bool:
    return all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in value)


def write_token_file(path: Path, token: str) -> None:
    """Replace the token file atomically: mode 0600, owned by the owner of its directory.

    ``cave init`` creates the directory as the user who runs ``./cave``. Inside the container this
    runs as root, which may give the file away; the directory's owner is the right one with
    rootful and rootless Docker alike, because both see the directory through the same mapping.
    """
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = directory.stat()
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".token-")
    try:
        os.fchmod(fd, 0o600)
        if (os.fstat(fd).st_uid, os.fstat(fd).st_gid) != (owner.st_uid, owner.st_gid):
            os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


async def bootstrap(settings: Settings, out: Output) -> None:
    async with aiohttp.ClientSession() as session:
        await Bootstrap(settings, session, out).run()


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    try:
        raw = json.loads(stdin.read())
        if not isinstance(raw, dict):
            raise BootstrapError("the settings on standard input are not a JSON object")
        settings = Settings.from_json(raw)
    except (ValueError, BootstrapError) as error:
        print(f"bootstrap: cannot read the settings: {error}", file=stdout or sys.stdout)
        return 2
    out = Output(settings.secrets, stdout)
    try:
        asyncio.run(bootstrap(settings, out))
    except BootstrapError as error:
        out.say(f"bootstrap failed: {error}")
        return 1
    if out.changes:
        out.say(f"bootstrap: done, {len(out.changes)} change(s)")
    else:
        out.say("bootstrap: done, nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
