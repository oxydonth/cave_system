"""A small client for Home Assistant's REST API, and the script token it authenticates with.

The transport is a plain function so that tests can replace it (``fake_ha`` in
``tests/ops/conftest.py``); the default uses ``urllib`` with the standard library only.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .errors import CaveError
from .paths import Paths

DEFAULT_URL = "http://127.0.0.1:8123"

# (method, url, headers, body, timeout seconds) -> (status, response body)
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]


class HAError(CaveError):
    """Home Assistant answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"Home Assistant answered {status}: {message}")


class HAUnreachable(CaveError):
    """Home Assistant could not be reached at all."""


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    """The real transport. HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _error_message(raw: bytes) -> str:
    """What Home Assistant said: its JSON ``message`` when it has one, else the body as text.

    A service that fails validation answers 400 with ``{"message": "..."}``; the message is what
    a person needs, for example the problems of an invalid ``heating.yaml``.
    """
    text = raw.decode(errors="replace").strip()
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("message"), str):
        text = decoded["message"].strip()
    return text[:2000]


def read_token(paths: Paths) -> str | None:
    """The script token (``CAVE_DATA/homeassistant/.cave/token``), or ``None`` if there is none yet.

    ``cave bootstrap`` creates it. An empty file counts as missing.
    """
    try:
        token = paths.token.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except PermissionError as error:
        raise CaveError(
            f"cannot read the script token {paths.token}: {error.strerror}. "
            "It must be readable by the user that runs ./cave."
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise CaveError(f"cannot read the script token {paths.token}: {error}") from error
    if token and not (
        token.isascii() and token.isprintable() and not any(c.isspace() for c in token)
    ):
        raise CaveError(
            f"the script token {paths.token} is not a single line of printable characters. "
            "Remove it and run `./cave bootstrap` again to create a new one."
        )
    return token or None


class HAClient:
    """Calls Home Assistant's REST API with a bearer token. Failures are ``CaveError``s."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_URL,
        transport: Transport = urllib_transport,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        """Send a request and return the decoded JSON body (``None`` for an empty body)."""
        headers = {"Authorization": f"Bearer {self._token}"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        try:
            status, raw = self._transport(method, self._base + path, headers, body, self._timeout)
        except (OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
            raise HAUnreachable(f"cannot reach Home Assistant at {self._base}: {error}") from error
        if status >= 400:
            raise HAError(status, _error_message(raw))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as error:
            raise HAError(status, f"not JSON: {raw[:80]!r}") from error

    def config(self) -> dict[str, Any]:
        """``GET /api/config``; its ``components`` list says what is loaded."""
        result = self.request("GET", "/api/config")
        if not isinstance(result, dict):
            raise HAError(200, "/api/config answered in an unexpected shape")
        return result

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """``GET /api/states/<entity>``; ``None`` if the entity does not exist."""
        try:
            result: dict[str, Any] = self.request("GET", f"/api/states/{entity_id}")
        except HAError as error:
            if error.status == 404:
                return None
            raise
        return result

    def services(self) -> dict[str, set[str]]:
        """``GET /api/services``, as ``{domain: {service, ...}}``."""
        listing = self.request("GET", "/api/services")
        try:
            return {entry["domain"]: set(entry.get("services", {})) for entry in listing}
        except (KeyError, TypeError, AttributeError) as error:
            raise HAError(200, "/api/services answered in an unexpected shape") from error

    def has_service(self, domain: str, service: str) -> bool:
        return service in self.services().get(domain, set())

    def call_service(self, domain: str, service: str, data: Mapping[str, Any] | None = None) -> Any:
        """``POST /api/services/<domain>/<service>``."""
        return self.request("POST", f"/api/services/{domain}/{service}", dict(data or {}))
