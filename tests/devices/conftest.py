"""Fixtures for the device integration tests (work package 7).

Every test here runs Home Assistant with its real MQTT and FRITZ!SmartHome integrations, fed by the
simulators of ``sim/``, and ``cave_heating`` on top. As in ``tests/cave_heating/conftest.py``, two
things are load-bearing: ``custom_components.cave_heating`` is imported at import time, and
``enable_custom_integrations`` is active for every test that uses ``hass``, or Home Assistant's
loader reports "Integration not found". The matrix script's own tests need neither, so the fixture
is enabled only where ``hass`` is used, as in ``tests/sim/conftest.py``.

``rig`` is the simulated home with the heating on top (``tests/devices/rig.py``).
"""

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)
from cave_sim.testing.fixtures import simulated_home  # noqa: F401  (fixture)
from tests.devices.rig import Rig


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Let every test that uses Home Assistant load the integrations in this repository."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """A fresh configuration directory per test, so no applied heating.yaml leaks between tests."""
    return str(tmp_path)


@pytest.fixture
def rig(hass: HomeAssistant, freezer: Any, request: pytest.FixtureRequest) -> Rig:
    """A simulated home with cave_heating on top; ``await rig.start(...)`` builds it."""
    return Rig(hass, freezer, request.getfixturevalue("simulated_home"))
