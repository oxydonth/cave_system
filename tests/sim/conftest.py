"""Fixtures for the simulator tests.

Some tests here run Home Assistant (the ``hass`` fixture). As in tests/cave_heating/conftest.py,
this directory imports ``custom_components.cave_heating`` at import time and enables the
repository's custom integrations, so that Home Assistant's loader finds this repository's
``custom_components`` rather than the test library's. Unlike there, the fixture is enabled only
for tests that use ``hass``: most simulator tests run without Home Assistant and should not pay
for starting it.

``simulated_home`` is the fixture from ``cave_sim.testing.fixtures``.
"""

import pytest

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)
from cave_sim.testing.fixtures import simulated_home  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Let every test that uses Home Assistant load the integrations in this repository."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
