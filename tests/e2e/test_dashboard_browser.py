"""The heating dashboard in a real browser, on the demo (concept section 8, level "Browser").

* ``test_dashboard_in_a_browser`` (``CAVE_E2E=demo``) is the check itself; see
  ``dashboard_browser.py`` for what it asserts. It writes the screenshots to the directory
  ``CAVE_E2E_SCREENSHOTS`` (default ``screenshots/`` in the repository), where the ``e2e`` job
  picks them up.
* ``test_detector_*`` run without a stack: they give the detector pages that a frontend with
  a broken dashboard would produce, and one that it would not, and show that it tells them apart.
  They need a browser (``uv run playwright install chromium``) and are skipped without one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from tests.e2e import dashboard_browser as check

REPO = Path(__file__).resolve().parents[2]


def test_dashboard_in_a_browser(stack, e2e_stack: str) -> None:  # type: ignore[no-untyped-def]
    if e2e_stack != "demo":
        pytest.skip("the dashboard of the demo home is checked: CAVE_E2E=demo")
    env = stack.env
    rooms = yaml.safe_load((REPO / "sim" / "demo" / "heating.yaml").read_text())["rooms"]
    out = Path(os.environ.get("CAVE_E2E_SCREENSHOTS", REPO / "screenshots"))
    report = check.run(
        os.environ.get("CAVE_E2E_URL", "http://127.0.0.1:8123"),
        env["CAVE_HA_OWNER_USER"],
        env["CAVE_HA_OWNER_PASSWORD"],
        out,
        os.environ.get("CAVE_E2E_BROWSER_CHANNEL") or None,
        tuple(f"climate.cave_{room['id']}" for room in rooms),
    )
    assert not report.problems, "the heating dashboard has problems:\n" + "\n".join(report.problems)
    names = sorted(path.name for path in report.screenshots)
    assert names == [
        "desktop-maintenance.png",
        "desktop-rooms.png",
        "phone-maintenance.png",
        "phone-rooms.png",
    ]
    assert all(path.stat().st_size > 10_000 for path in report.screenshots)
    assert all(path.stat().st_size <= check.LOG_COPY_LIMIT for path in report.log_copies)


# A page like the frontend's: the dashboard's elements are in shadow roots.
PAGE = """
<home-assistant></home-assistant>
<script>
  const make = (parent, html) => {
    const host = document.createElement('div');
    parent.appendChild(host);
    host.attachShadow({mode: 'open'}).innerHTML = html;
    return host;
  };
  const root = document.querySelector('home-assistant');
  root.attachShadow({mode: 'open'});
  const view = make(root.shadowRoot, '');
  %s
</script>
"""


def _errors(body: str) -> list[dict[str, str]]:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(PAGE % body)
                return check.find_errors(page)
            finally:
                browser.close()
    except PlaywrightError as error:
        if "Executable doesn't exist" in str(error):
            pytest.skip("no browser installed: uv run playwright install chromium")
        raise


@pytest.mark.parametrize(
    ("snippet", "tag"),
    [
        ("view.shadowRoot.innerHTML = '<hui-error-card></hui-error-card>';", "hui-error-card"),
        (
            "view.shadowRoot.innerHTML = '<hui-warning>Entity not available</hui-warning>';",
            "hui-warning",
        ),
        (
            "const card = make(view.shadowRoot, '<div>nested</div>');"
            "card.shadowRoot.innerHTML = '<hui-error-badge></hui-error-badge>';",
            "hui-error-badge",
        ),
        (
            "view.shadowRoot.innerHTML = "
            "'<ha-alert alert-type=\"error\">Template error</ha-alert>';",
            "ha-alert (error)",
        ),
        (
            "const markdown = document.createElement('hui-markdown-card');"
            "view.shadowRoot.appendChild(markdown);"
            "markdown.attachShadow({mode: 'open'}).innerHTML = "
            "'<ha-alert alert-type=\"warning\">Template warning</ha-alert>';",
            "ha-alert (warning) in a markdown card",
        ),
    ],
)
def test_detector_finds_what_the_frontend_shows_for_a_failed_card(snippet: str, tag: str) -> None:
    errors = _errors(snippet)
    assert [error["tag"] for error in errors] == [tag]


def test_detector_leaves_a_healthy_dashboard_alone() -> None:
    snippet = (
        "view.shadowRoot.innerHTML = '<hui-thermostat-card></hui-thermostat-card>"
        '<ha-alert alert-type="info">Nothing to show</ha-alert>'
        '<ha-alert alert-type="warning">A warning outside a markdown card</ha-alert>'
        "<p>An error is something the page says, not an element</p>';"
    )
    assert _errors(snippet) == []
