"""Fixtures that let Home Assistant's test library find this repository's integration.

Two things here are load-bearing, and any test directory that uses the ``hass`` fixture needs
the same two:

* ``custom_components.cave_heating`` must be imported at import time, before any ``hass``
  fixture runs, even in test modules that never import it themselves. Otherwise Home
  Assistant's loader resolves ``custom_components`` to the test library's own directory and
  reports "Integration not found".
* ``enable_custom_integrations`` must be active for every test, so the loader looks in this
  repository's ``custom_components`` directory.

Each test also gets its own configuration directory, so the applied ``heating.yaml`` a test
writes never leaks into another test.
"""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)
from tests.cave_heating.fakes import FakeHome


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test in this directory load the integrations in this repository."""


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """A fresh Home Assistant configuration directory per test."""
    return str(tmp_path)


@pytest.fixture
async def home(hass: HomeAssistant) -> FakeHome:
    """Fake thermostats, sensors and services, and helpers to set up cave_heating."""
    fake = FakeHome(hass)
    await fake.async_setup()
    return fake
