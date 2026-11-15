"""Shared fixtures for the whole test suite.

Two things here are load-bearing for Home Assistant's test library:

* ``custom_components.cave_heating`` must be imported at import time, before any ``hass``
  fixture runs, even in test modules that never import it themselves. Otherwise Home
  Assistant's loader resolves ``custom_components`` to the test library's own directory and
  reports "Integration not found".
* ``enable_custom_integrations`` must be active for every test, so the loader looks in this
  repository's ``custom_components`` directory.
"""

import pytest

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test load the integrations in this repository."""
