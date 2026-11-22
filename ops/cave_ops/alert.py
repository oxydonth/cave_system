"""Alerts that do not depend on Home Assistant (concept section 4.5).

When ``CAVE_ALERT_URL`` in ``.env`` names a URL, ``send`` posts a short text to it with an HTTP
POST: a plain UTF-8 body and the title in a ``Title`` header, which is ntfy's format and what most
webhook receivers accept. When it is unset or empty, nothing leaves the server.

What goes out is fixed, never a command's own message: the server's name, the command, what
happened in fixed words, and the stack's state in fixed words (``Health.state``). No room, no
device and no reading (concept section 10).

The URL may carry a token, in its query or as ``user:password@`` (sent as HTTP basic
authentication). It is never printed or logged, and it is sent nowhere but to itself: no proxy
from the environment, and no redirect is followed.

A send takes at most ``TIMEOUT`` seconds in all, whatever the receiver or the name lookup does
(it runs in a thread that is abandoned after that), never raises, and never changes a command's
exit status.

``cave watch`` remembers in ``CAVE_DATA/.cave/watch.json`` (``Memory``) what it has alerted. A
command's warning that was delivered while the stack was not healthy counts as that incident's
alert, so the watch does not alert it again and sends the all-clear once the stack is healthy.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .health import Health, check_health
from .paths import Paths
from .records import write_atomic

if TYPE_CHECKING:
    from .context import Context

VARIABLE = "CAVE_ALERT_URL"
TIMEOUT = 10.0
"""Seconds one send may take in all."""


@dataclass(frozen=True)
class Sent:
    """Whether the receiver took the alert; ``error`` says why not (``None``: not configured)."""

    delivered: bool
    error: str | None = None


def configured(ctx: Context) -> bool:
    return bool(ctx.env.get(VARIABLE, "").strip())


def server_name() -> str:
    return socket.gethostname() or "the server"


def compose(command: str, headline: str, happened: str, stack: str) -> tuple[str, str]:
    """The title and body of an alert. Only these fixed parts go out."""
    host = server_name()
    title = f"{host}: {headline}"
    body = (
        f"{happened}\n\n"
        f"Server: {host}\n"
        f"Command: {command}\n"
        f"Stack: {stack}\n\n"
        "On the server, `./cave status` shows more; docs/operations.md says what to do.\n"
    )
    return title, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the alert, and the token in its URL, go to that URL only."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _request(url: str, title: str, body: str) -> urllib.request.Request:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("not an http or https URL")
    headers = {
        # Header values are sent as Latin-1: keep the title plain ASCII.
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Content-Type": "text/plain; charset=utf-8",
    }
    netloc = parts.netloc
    if parts.username is not None or parts.password is not None:
        credentials = f"{unquote(parts.username or '')}:{unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        host = parts.hostname
        netloc = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            netloc += f":{parts.port}"
    target = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    return urllib.request.Request(target, data=body.encode(), headers=headers, method="POST")


def _why(error: BaseException) -> str:
    """What went wrong, in words that cannot contain the URL."""
    if isinstance(error, urllib.error.HTTPError):
        return f"the receiver answered {error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, TimeoutError):
            return f"no answer within {TIMEOUT:g} s"
        if isinstance(reason, OSError):
            detail = f": {reason.strerror}" if reason.strerror else ""
            return f"the receiver could not be reached ({type(reason).__name__}{detail})"
        return "the receiver could not be reached"
    if isinstance(error, TimeoutError):
        return f"no answer within {TIMEOUT:g} s"
    if isinstance(error, ValueError | http.client.InvalidURL):
        return f"{VARIABLE} is not a usable http or https URL"
    if isinstance(error, OSError):
        detail = f": {error.strerror}" if error.strerror else ""
        return f"the receiver could not be reached ({type(error).__name__}{detail})"
    if isinstance(error, http.client.HTTPException):
        return f"the receiver's answer was not HTTP ({type(error).__name__})"
    return f"unexpected {type(error).__name__}"


def _post(url: str, title: str, body: str, outcome: list[str | None]) -> None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(_request(url, title, body), timeout=TIMEOUT) as response:
            response.read(4096)
        outcome.append(None)
    except BaseException as error:  # the contract: whatever happens, never raise
        outcome.append(_why(error))


def send(ctx: Context, title: str, body: str) -> Sent:
    """Post an alert to ``CAVE_ALERT_URL``; nothing when it is unset. Never raises, and takes at
    most ``TIMEOUT`` seconds. The journal gets a line saying whether it went out."""
    url = ctx.env.get(VARIABLE, "").strip()
    if not url:
        return Sent(False)
    outcome: list[str | None] = []
    worker = threading.Thread(
        target=_post, args=(url, title, body, outcome), name="cave-alert", daemon=True
    )
    worker.start()
    worker.join(TIMEOUT)
    if worker.is_alive() or not outcome:
        error: str | None = f"no answer within {TIMEOUT:g} s"
    else:
        error = outcome[0]
    if error is None:
        ctx.journal(f"cave alert sent: {title}")
        return Sent(True)
    ctx.journal(f"cave alert NOT sent ({error}): {title}")
    return Sent(False, error)


def warn_owner(
    ctx: Context, command: str, headline: str, happened: str, health: Health | None = None
) -> Sent:
    """The alert for a command's warning (concept section 4.5), in addition to its notification.

    ``health`` is the stack's state if the caller knows it; else it is asked. A delivered alert
    while the stack is not healthy is that incident's alert for ``cave watch`` too.
    """
    if not configured(ctx):
        return Sent(False)
    if health is None:
        try:
            health = check_health(ctx)
        except Exception:  # the stack's state is reported as unknown, the alert still goes out
            health = Health(False, ("Docker could not be asked",), None)
    title, body = compose(f"cave {command}", headline, happened, health.state())
    sent = send(ctx, title, body)
    if sent.delivered and not health.healthy:
        try:
            mark_alerted(ctx, f"cave {command}: {headline}")
        except OSError as error:
            ctx.warn(f"What was alerted could not be recorded: {error}")
    elif not sent.delivered:
        ctx.warn(f"The alert to {VARIABLE} could not be sent: {sent.error}.")
    return sent


# ------------------------------------------------------------------------- what was alerted
@dataclass
class Memory:
    """What ``cave watch`` has seen and said, in seconds since the epoch (``ctx.wall``)."""

    since: float | None = None
    """When the watch first saw the current incident, with no command at work."""
    alerted: float | None = None
    """When the current incident's alert was delivered; ``None`` while none was."""
    last: float | None = None
    """The watch's last run."""
    what: str | None = None
    """What the delivered alert said, in its headline."""


def load_memory(paths: Paths) -> Memory:
    try:
        raw = json.loads(paths.watch_record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Memory()
    if not isinstance(raw, dict):
        return Memory()

    def number(name: str) -> float | None:
        value = raw.get(name)
        return float(value) if isinstance(value, int | float) else None

    what = raw.get("what")
    return Memory(number("since"), number("alerted"), number("last"), what if what else None)


def save_memory(paths: Paths, memory: Memory) -> None:
    write_atomic(paths.watch_record, (json.dumps(asdict(memory)) + "\n").encode(), 0o644)


def mark_alerted(ctx: Context, what: str) -> None:
    """A command's alert went out while the stack was not healthy: the watch treats that
    incident as alerted."""
    memory = load_memory(ctx.paths)
    now = ctx.wall()
    if memory.alerted is None:
        memory.since = memory.since if memory.since is not None else now
        memory.alerted = now
        memory.what = what
        save_memory(ctx.paths, memory)
