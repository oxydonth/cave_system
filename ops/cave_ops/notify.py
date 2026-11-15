"""Notifications from scripts (concept section 4.5).

A call to ``script.cave_notify`` over Home Assistant's REST API; if that script does not exist
yet, ``persistent_notification.create``. Always also a line in the system journal.

``script.cave_notify`` is called with the fields ``title`` and ``message``; the script of the
notifications package must accept exactly these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import CaveError

if TYPE_CHECKING:
    from .context import Context

SCRIPT_DOMAIN = "script"
SCRIPT_SERVICE = "cave_notify"


@dataclass(frozen=True)
class Notification:
    """Where a notification went. ``via`` is ``script``, ``persistent_notification`` or ``None``."""

    via: str | None
    error: str | None = None

    @property
    def delivered(self) -> bool:
        return self.via is not None


def notify(ctx: Context, title: str, message: str) -> Notification:
    """Tell the owner. Never raises: a Home Assistant that is down must not break the caller.

    The journal line is written first and always. Without a script token, or when Home
    Assistant cannot be reached, only the journal gets it and ``error`` says why.
    """
    ctx.journal(f"{title}: {message}")
    try:
        client = ctx.ha()
        if client is None:
            return Notification(None, "no script token yet; only the journal has the message")
        data = {"title": title, "message": message}
        if client.has_service(SCRIPT_DOMAIN, SCRIPT_SERVICE):
            client.call_service(SCRIPT_DOMAIN, SCRIPT_SERVICE, data)
            return Notification("script")
        client.call_service("persistent_notification", "create", data)
        return Notification("persistent_notification")
    except CaveError as error:
        return Notification(None, str(error))
    except Exception as error:  # the contract: whatever goes wrong, never raise
        return Notification(None, f"unexpected {type(error).__name__}: {error}")
