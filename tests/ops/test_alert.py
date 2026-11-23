"""The alert channel without Home Assistant (``alert.py``), against a local HTTP receiver.

What goes out, in what format, and that the URL with its token goes nowhere else: not into the
output, not into the journal, not through a proxy, not after a redirect. A receiver that refuses
or hangs costs no more than the timeout, and ``send`` never raises.
"""

from __future__ import annotations

import base64
import socket
import threading
import time

import pytest

from cave_ops import alert
from cave_ops.health import Health

HEALTHY = Health(True, (), 2)
STOPPED = Health(False, ("homeassistant: no container",), 0)
# A made-up token, in the shape ntfy's access tokens have.
TOKEN = "tk_fakefakefakefakefakefakefake"


def _join_abandoned_sends() -> None:
    """A send that ran out of time is abandoned, not stopped: let it end before the test does."""
    for thread in threading.enumerate():
        if thread.name == "cave-alert":
            thread.join(10)


def _everything_printed(ctx, journal) -> str:
    return ctx.out.getvalue() + ctx.err.getvalue() + "\n".join(journal)


def test_unset_nothing_leaves_the_server(ctx, alert_endpoint, journal, monkeypatch):
    def no_send(*args):
        raise AssertionError("a send was tried")

    monkeypatch.setattr(alert, "_post", no_send)

    for value in (None, "", "   "):
        if value is not None:
            ctx.env["CAVE_ALERT_URL"] = value
        assert alert.send(ctx, "t", "b") == alert.Sent(False, None)
        assert alert.warn_owner(ctx, "update", "x", "y", STOPPED) == alert.Sent(False, None)

    assert alert_endpoint.received == []
    assert journal == []
    assert not ctx.paths.watch_record.exists()


def test_an_alert_is_a_plain_utf8_post_with_the_title_in_a_header(ctx, alert_endpoint, journal):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    sent = alert.warn_owner(
        ctx, "update", "update rolled back", "A new release failed: zurückgerollt.", HEALTHY
    )

    assert sent == alert.Sent(True)
    (got,) = alert_endpoint.received
    assert got.path == "/cave-alerts"
    assert got.headers["Title"] == f"{socket.gethostname()}: update rolled back"
    assert got.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert got.body.startswith("A new release failed: zurückgerollt.")
    assert f"Server: {socket.gethostname()}" in got.body
    assert "Command: cave update" in got.body
    assert "Stack: running and healthy" in got.body
    assert any("cave alert sent" in line for line in journal)


@pytest.mark.parametrize(
    ("health", "said"),
    [
        (HEALTHY, "running and healthy"),
        (STOPPED, "stopped: nothing runs"),
        (Health(False, ("homeassistant: health check unhealthy",), 2), "running, but NOT healthy"),
        (Health(False, ("docker compose ps failed",), None), "unknown (Docker did not answer)"),
    ],
)
def test_the_stack_is_described_in_fixed_words_only(ctx, alert_endpoint, health, said):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    alert.warn_owner(ctx, "backup", "backup failed", "The backup failed.", health)

    body = alert_endpoint.received[0].body
    assert f"Stack: {said}\n" in body
    for problem in health.problems:
        assert problem not in body, "the problems may name more than the state"


@pytest.mark.parametrize("user", ["cave", ""])
def test_a_token_in_the_url_goes_to_the_receiver_only(ctx, alert_endpoint, journal, user):
    """As a password (ntfy takes an access token with an empty user name) and in the query."""
    host = alert_endpoint.base.removeprefix("http://")
    ctx.env["CAVE_ALERT_URL"] = f"http://{user}:{TOKEN}@{host}/cave-alerts?auth={TOKEN}"

    sent = alert.warn_owner(ctx, "update", "update rolled back", "Rolled back.", HEALTHY)

    assert sent.delivered
    (got,) = alert_endpoint.received
    assert (
        got.headers["Authorization"]
        == "Basic " + base64.b64encode(f"{user}:{TOKEN}".encode()).decode()
    )
    assert got.path == f"/cave-alerts?auth={TOKEN}"
    assert TOKEN not in got.body
    assert TOKEN not in _everything_printed(ctx, journal)


@pytest.mark.parametrize("mode", ["refuse", "error", "redirect", "not a url", "ftp", "bad port"])
def test_a_failed_send_says_why_without_the_url(ctx, alert_endpoint, journal, mode):
    secret_url = {
        "refuse": alert_endpoint.refusing_url + f"?auth={TOKEN}",
        "not a url": f"ntfy.fritz.box/{TOKEN}",
        "ftp": f"ftp://ntfy.fritz.box/{TOKEN}",
        "bad port": f"http://ntfy.fritz.box:{TOKEN}/cave",
    }.get(mode, alert_endpoint.url + f"?auth={TOKEN}")
    alert_endpoint.mode = mode
    ctx.env["CAVE_ALERT_URL"] = secret_url

    sent = alert.warn_owner(ctx, "update", "update rolled back", "Rolled back.", HEALTHY)

    assert not sent.delivered
    assert sent.error
    expected = {
        "refuse": "could not be reached",
        "error": "answered 500",
        "redirect": "answered 302",
        "not a url": "not a usable http or https URL",
        "ftp": "not a usable http or https URL",
        "bad port": "not a usable http or https URL",
    }[mode]
    assert expected in sent.error
    printed = _everything_printed(ctx, journal)
    assert TOKEN not in printed
    assert "ntfy.fritz.box" not in printed
    assert "could not be sent" in ctx.err.getvalue()
    # A redirect is not followed: nothing arrives at the place it points to.
    assert all(item.path != "/elsewhere" for item in alert_endpoint.received)


def test_a_hanging_receiver_costs_no_more_than_the_timeout(ctx, alert_endpoint, monkeypatch):
    monkeypatch.setattr(alert, "TIMEOUT", 0.5)
    alert_endpoint.mode = "hang"
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    started = time.monotonic()
    sent = alert.send(ctx, "title", "body")
    took = time.monotonic() - started

    assert not sent.delivered
    assert "no answer within 0.5 s" in (sent.error or "")
    assert took < 2.0
    alert_endpoint.release.set()
    _join_abandoned_sends()


def test_a_name_that_never_resolves_costs_no_more_than_the_timeout(ctx, monkeypatch):
    """The socket timeout does not cover the name lookup: the thread does."""
    monkeypatch.setattr(alert, "TIMEOUT", 0.5)
    answered = threading.Event()

    def slow_lookup(*args, **kwargs):
        answered.wait(10)
        raise OSError("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    ctx.env["CAVE_ALERT_URL"] = "http://ntfy.example.invalid/cave"

    started = time.monotonic()
    sent = alert.send(ctx, "title", "body")

    assert not sent.delivered
    assert time.monotonic() - started < 2.0
    answered.set()
    _join_abandoned_sends()


def test_a_proxy_in_the_environment_is_not_used(ctx, alert_endpoint, monkeypatch):
    refusing = alert_endpoint.refusing_url.rsplit("/", 1)[0]
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy"):
        monkeypatch.setenv(name, refusing)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    assert alert.send(ctx, "title", "body").delivered
    assert len(alert_endpoint.received) == 1


def test_a_warning_delivered_while_the_stack_is_down_counts_as_the_incident_s_alert(
    ctx, alert_endpoint, clock
):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url
    ctx.wall = lambda: 1_800_000_000.0 + clock.now

    alert.warn_owner(
        ctx, "update", "update failed, act now", "Nothing runs.", STOPPED, about_the_stack=True
    )

    memory = alert.load_memory(ctx.paths)
    assert memory.alerted == 1_800_000_000.0
    assert memory.what == "cave update: update failed, act now"


def test_a_warning_while_the_stack_is_healthy_is_no_incident_alert(ctx, alert_endpoint):
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    alert.warn_owner(ctx, "backup", "backup failed", "The backup failed.", HEALTHY)

    assert alert.load_memory(ctx.paths).alerted is None


def test_an_undelivered_warning_is_no_incident_alert(ctx, alert_endpoint):
    alert_endpoint.mode = "error"
    ctx.env["CAVE_ALERT_URL"] = alert_endpoint.url

    alert.warn_owner(
        ctx, "update", "update failed, act now", "Nothing runs.", STOPPED, about_the_stack=True
    )

    assert alert.load_memory(ctx.paths).alerted is None
