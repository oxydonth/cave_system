"""The heating dashboard in a real browser (concept section 8, level "Browser").

Opens Home Assistant in headless Chromium, logs in through the login form, opens the heating
dashboard at ``/cave-heating`` (rooms view, then maintenance view), waits until it has rendered
and fails if any card shows an error or a configuration problem. Each view is checked at a
desktop width and at a phone width, and each is photographed.

What counts as a failed card was read from the frontend this project pins
(``home-assistant-frontend`` 20260930.5, the one Home Assistant 2026.10.4 requires):

* ``hui-error-card``: a card whose type is unknown, whose configuration is rejected (``setConfig``
  threw) or that could not be created. It shows "Configuration error" or the reason.
* ``hui-warning``: a card or row that names an entity Home Assistant does not have ("Entity not
  available"), which is how the tile, entities and thermostat cards report a missing entity.
* ``hui-warning-element``: the same for a picture-elements element.
* ``hui-error-badge``, ``hui-error-heading-badge``, ``hui-error-section``: the same for a badge,
  a badge of a heading card and a whole section.
* ``ha-alert`` of the type ``error``, which the markdown card shows when its template fails.

The elements live in shadow roots, so they are found by walking the shadow DOM. Searching the page
for the word "error" would find nothing (the texts are translated, and the page is mostly shadow
DOM) and would pass on a page that has not finished loading. So the check waits first, for what
only a rendered dashboard has: every card the dashboard configuration lists, and in each room
the thermostat card showing the live state of its room thermostat. Only then does it look for
errors, and again before it takes the screenshot.

Run by hand against a running demo (``CAVE_E2E_DEMO_DATA`` as in docs/demo.md)::

    uv run python tests/e2e/dashboard_browser.py --url http://127.0.0.1:8123 \\
        --user owner --password "$CAVE_HA_OWNER_PASSWORD" --out screenshots
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

DASHBOARD = "cave-heating"
VIEWS = ("rooms", "maintenance")

#: The two widths: a desktop window and a phone. The heights are only the first guess: the
#: window is made as tall as the dashboard before the screenshot, so that every room is on it.
VIEWPORTS: dict[str, dict[str, Any]] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True},
}

#: Elements the frontend uses to say that something on the dashboard failed (module docstring).
ERROR_TAGS = (
    "hui-error-card",
    "hui-warning",
    "hui-warning-element",
    "hui-error-badge",
    "hui-error-heading-badge",
    "hui-error-section",
)

#: The element that renders a card of a type: `hui-<type>-card`, and the heading card, whose
#: element is `hui-heading-card`. A type that has no entry here is not counted.
CARD_ELEMENTS = {
    "thermostat": "hui-thermostat-card",
    "tile": "hui-tile-card",
    "entities": "hui-entities-card",
    "history-graph": "hui-history-graph-card",
    "markdown": "hui-markdown-card",
    "heading": "hui-heading-card",
}

#: How long to wait for the page to render. The demo is idle when this runs, so a dashboard
#: that takes longer than this is a problem, not a slow runner.
RENDER_TIMEOUT = 60.0
#: How long the screenshots may be, in bytes, for the copy printed into the job log.
LOG_COPY_LIMIT = 100_000

# Deep search of the shadow DOM, from `root` (a document or a shadow root).
JS_DEEP = """
const deep = (root, visit) => {
  for (const el of root.querySelectorAll('*')) {
    visit(el);
    if (el.shadowRoot) deep(el.shadowRoot, visit);
  }
};
"""

JS_ERRORS = (
    "(tags) => {"
    + JS_DEEP
    + """
  const found = [];
  const text = (el) => {
    const parts = [];
    deep(el.shadowRoot || el, (n) => {
      if (n.shadowRoot === null && n.children.length === 0 && n.textContent) {
        parts.push(n.textContent.trim());
      }
    });
    return (parts.join(' ') || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
  };
  const root = document.querySelector('home-assistant');
  if (!root) return found;
  const visit = (el) => {
    const tag = el.localName;
    if (tags.includes(tag)) {
      found.push({tag, text: text(el), config: JSON.stringify(el._config || null).slice(0, 300)});
    } else if (tag === 'ha-alert' && (el.alertType === 'error' ||
               el.getAttribute('alert-type') === 'error')) {
      found.push({tag: 'ha-alert (error)', text: el.textContent.trim().slice(0, 200), config: ''});
    }
  };
  if (root.shadowRoot) deep(root.shadowRoot, visit);
  return found;
}"""
)

# The cards on the page: for each, its element name, its configuration and its state.
JS_CARDS = (
    "(tags) => {"
    + JS_DEEP
    + """
  const out = [];
  const root = document.querySelector('home-assistant');
  if (!root || !root.shadowRoot) return out;
  deep(root.shadowRoot, (el) => {
    if (!tags.includes(el.localName)) return;
    const config = el._config || {};
    const state = config.entity && el.hass ? el.hass.states[config.entity] : null;
    const rect = el.getBoundingClientRect();
    out.push({
      tag: el.localName,
      entity: config.entity || null,
      state: state ? state.state : null,
      current: state && state.attributes ? state.attributes.current_temperature : null,
      text: (function () {
        const parts = [];
        deep(el.shadowRoot || el, (n) => {
          if (n.children.length === 0 && n.shadowRoot === null && n.textContent) {
            parts.push(n.textContent.trim());
          }
        });
        return parts.join(' ').replace(/\\s+/g, ' ').trim();
      })(),
      visible: rect.width > 0 && rect.height > 0,
    });
  });
  return out;
}"""
)

JS_DASHBOARD_CONFIG = """async (path) => {
  const hass = document.querySelector('home-assistant').hass;
  return await hass.callWS({type: 'lovelace/config', url_path: path});
}"""

# The tallest scrolling area of the dashboard: what the window must hold to show every card.
JS_CONTENT_HEIGHT = (
    "() => {"
    + JS_DEEP
    + """
  let tallest = 0;
  const root = document.querySelector('home-assistant');
  if (!root || !root.shadowRoot) return 0;
  deep(root.shadowRoot, (el) => {
    if (el.scrollHeight > el.clientHeight + 4 && el.clientHeight > 100) {
      tallest = Math.max(tallest, el.scrollHeight + el.getBoundingClientRect().top);
    }
  });
  return Math.ceil(tallest);
}"""
)


class DashboardProblem(AssertionError):
    """The dashboard did not render, or shows an error."""


@dataclass
class Report:
    """What the check found, per view and width; ``problems`` is empty when everything passed."""

    problems: list[str] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    log_copies: list[Path] = field(default_factory=list)

    def add(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")


def _wait(condition: Callable[[], Any], what: str, timeout: float = RENDER_TIMEOUT) -> Any:
    """Poll ``condition`` until it returns something truthy; raise naming ``what`` otherwise."""
    deadline = time.monotonic() + timeout
    last: object = None
    while True:
        try:
            result = condition()
            if result:
                return result
            last = result
        except PlaywrightError as error:  # a page that is navigating: try again
            last = error
        if time.monotonic() >= deadline:
            raise DashboardProblem(
                f"gave up after {timeout:.0f} s waiting for {what} (last: {last!r})"
            )
        time.sleep(0.4)


def login(browser: Browser, base_url: str, user: str, password: str) -> dict[str, Any]:
    """Log in through Home Assistant's login form; return the browser's storage for reuse.

    Home Assistant keeps its tokens in the browser's local storage, so another window with this
    storage is logged in as well. The form is used once, here.
    """
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(f"{base_url}/{DASHBOARD}/rooms", wait_until="domcontentloaded")
        # A visitor without tokens is sent to /auth/authorize; its form is in shadow DOM.
        username = page.locator("input[name=username]")
        username.wait_for(state="visible", timeout=RENDER_TIMEOUT * 1000)
        username.fill(user)
        page.locator("input[name=password]").fill(password)
        page.get_by_role("button", name=re.compile("^(log in|anmelden)$", re.I)).click()
        # Logged in: the dashboard's own page, with Home Assistant's connection up.
        page.wait_for_function(
            "() => { const h = document.querySelector('home-assistant');"
            " return !!(h && h.hass && h.hass.connected && h.hass.states"
            " && Object.keys(h.hass.states).length > 0); }",
            timeout=RENDER_TIMEOUT * 1000,
        )
        return context.storage_state()  # type: ignore[return-value]
    finally:
        context.close()


def dashboard_cards(config: dict[str, Any], view_path: str) -> list[dict[str, Any]]:
    """Every card of a view in the dashboard configuration, sections and nested stacks included."""
    cards: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in CARD_ELEMENTS:
                cards.append(node)
            for key in ("cards", "sections"):
                for child in node.get(key) or []:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for view in config.get("views", []):
        if view.get("path") == view_path:
            walk(view)
    return cards


def _expected_counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in cards:
        element = CARD_ELEMENTS[card["type"]]
        counts[element] = counts.get(element, 0) + 1
    return counts


def _rendered(page: Page) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = page.evaluate(JS_CARDS, list(CARD_ELEMENTS.values()))
    return result


def wait_until_rendered(page: Page, cards: list[dict[str, Any]], view: str) -> None:
    """Wait for every card of the view, and for the room thermostats to show live values."""
    expected = _expected_counts(cards)
    thermostats = [card["entity"] for card in cards if card["type"] == "thermostat"]
    markdown = [card for card in cards if card["type"] == "markdown"]

    def all_cards_present() -> bool:
        shown: dict[str, int] = {}
        for card in _rendered(page):
            if card["visible"]:
                shown[card["tag"]] = shown.get(card["tag"], 0) + 1
        return all(shown.get(tag, 0) >= count for tag, count in expected.items())

    _wait(all_cards_present, f"all {sum(expected.values())} cards of the {view} view {expected}")

    if thermostats:

        def thermostats_live() -> bool:
            rendered = {
                c["entity"]: c for c in _rendered(page) if c["tag"] == "hui-thermostat-card"
            }
            for entity in thermostats:
                card = rendered.get(entity)
                if card is None or card["state"] in (None, "unavailable", "unknown"):
                    return False
                if not isinstance(card["current"], int | float):
                    return False
                # The card shows it: a temperature with its unit, not a placeholder.
                if not re.search(r"\d\s*°", card["text"]):
                    return False
            return True

        _wait(thermostats_live, f"the thermostat cards of {thermostats} to show live values")

    if markdown:

        def markdown_rendered() -> bool:
            texts = [c["text"] for c in _rendered(page) if c["tag"] == "hui-markdown-card"]
            return len(texts) >= len(markdown) and all(len(text) > 3 for text in texts)

        _wait(markdown_rendered, "the markdown cards to show their templates' results")


def find_errors(page: Page) -> list[dict[str, str]]:
    """The elements on the page that mean a card or badge failed (see the module docstring)."""
    result: list[dict[str, str]] = page.evaluate(JS_ERRORS, list(ERROR_TAGS))
    return result


def _describe(errors: list[dict[str, str]]) -> str:
    return "; ".join(
        f"<{e['tag']}> {e['text']!r}"
        + (f" for {e['config']}" if e["config"] not in ("", "null") else "")
        for e in errors
    )


def _screenshots(page: Page, name: str, out: Path, report: Report) -> None:
    """The window as tall as the dashboard, as PNG, and a small JPEG for the job log."""
    height = page.evaluate(JS_CONTENT_HEIGHT)
    width = page.viewport_size["width"] if page.viewport_size else 1280
    if height:
        page.set_viewport_size({"width": width, "height": min(max(height + 24, 600), 6000)})
        page.wait_for_timeout(1500)  # the layout and the history graphs settle
    png = out / f"{name}.png"
    page.screenshot(path=png)
    report.screenshots.append(png)
    small = out / "log" / f"{name}.jpg"
    small.parent.mkdir(parents=True, exist_ok=True)
    for quality in (50, 35, 25, 18, 12, 6):
        page.screenshot(path=small, type="jpeg", quality=quality)
        if small.stat().st_size <= LOG_COPY_LIMIT:
            break
    report.log_copies.append(small)


def check_view(
    context: BrowserContext,
    base_url: str,
    width_name: str,
    view: str,
    out: Path,
    report: Report,
    required_thermostats: tuple[str, ...] = (),
) -> None:
    where = f"{width_name} {view}"
    page = context.new_page()
    try:
        page.goto(f"{base_url}/{DASHBOARD}/{view}", wait_until="domcontentloaded")
        try:
            _wait(
                lambda: page.evaluate(
                    "() => !!document.querySelector('home-assistant')?.hass?.states"
                ),
                "Home Assistant's frontend to connect",
            )
            config = page.evaluate(JS_DASHBOARD_CONFIG, DASHBOARD)
            cards = dashboard_cards(config, view)
            if not cards:
                raise DashboardProblem(f"the dashboard configuration has no cards in view '{view}'")
            if view == "rooms":
                listed = {card.get("entity") for card in cards if card["type"] == "thermostat"}
                for entity in required_thermostats:
                    if entity not in listed:
                        report.add(where, f"the dashboard has no thermostat card for {entity}")
            wait_until_rendered(page, cards, view)
            page.wait_for_timeout(1000)  # a late failure of a card must still be caught
        except DashboardProblem as problem:
            report.add(where, f"did not finish rendering: {problem}")
        except PlaywrightError as error:
            report.add(where, f"browser error: {error}")
        errors = find_errors(page)
        if errors:
            report.add(where, f"{len(errors)} card(s) show an error: {_describe(errors)}")
        _screenshots(page, f"{width_name}-{view}", out, report)
        errors_after = find_errors(page)
        if len(errors_after) > len(errors):
            report.add(where, f"more errors after the layout settled: {_describe(errors_after)}")
    finally:
        page.close()


def run(
    base_url: str,
    user: str,
    password: str,
    out: Path,
    browser_channel: str | None = None,
    required_thermostats: tuple[str, ...] = (),
) -> Report:
    """Check both views at both widths; return what was found and the screenshots taken."""
    out.mkdir(parents=True, exist_ok=True)
    report = Report()
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(channel=browser_channel)
            if browser_channel
            else (playwright.chromium.launch())
        )
        try:
            storage = login(browser, base_url, user, password)
            for width_name, options in VIEWPORTS.items():
                context = browser.new_context(storage_state=storage, **options)
                try:
                    for view in VIEWS:
                        check_view(
                            context, base_url, width_name, view, out, report, required_thermostats
                        )
                finally:
                    context.close()
        finally:
            browser.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--url", default="http://127.0.0.1:8123")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    parser.add_argument("--channel", help="a browser installed on the machine, such as 'chrome'")
    parser.add_argument(
        "--thermostat",
        action="append",
        default=[],
        help="a room thermostat (climate.cave_<room>) the rooms view must show; repeatable",
    )
    args = parser.parse_args(argv)
    report = run(args.url, args.user, args.password, args.out, args.channel, tuple(args.thermostat))
    for path in report.screenshots:
        print(f"screenshot {path} ({path.stat().st_size} bytes)")  # noqa: T201
    for problem in report.problems:
        print(f"PROBLEM {problem}")  # noqa: T201
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
