"""Fixtures that let Home Assistant's test library load the package of this repository.

The two things below are the same as in tests/cave_heating/conftest.py, and any test directory
that uses the ``hass`` fixture needs them (see CLAUDE.md): the import of the integration package
at import time, and the autouse ``enable_custom_integrations`` fixture.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)
from tests.cave_heating.fakes import FakeHome


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test in this directory load the integrations in this repository."""


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Allow the one-minute timer of the notification sensors to outlive the test.

    The template integration keeps the timer of a trigger-based entity until the process ends; it
    does not cancel it when Home Assistant stops, and the test library otherwise fails the test.
    """
    return True


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """A fresh Home Assistant configuration directory per test, for the applied heating.yaml."""
    return str(tmp_path)


@pytest.fixture
async def home(hass: HomeAssistant) -> FakeHome:
    """Fake thermostats and sensors, and helpers to start the real cave_heating on top of them."""
    fake = FakeHome(hass)
    await fake.async_setup()
    return fake


@pytest.fixture(autouse=True)
def no_logged_errors(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """A test run of the package logs no error, except the one failure it exists to test.

    A template that raises, an action that fails: Home Assistant logs it and carries on, so
    without this the package could be broken in a way no assertion notices. The one expected
    error is a notify action that does not exist or fails, logged by the automation
    "Cave: send a notification copy", because tests of a wrong name in the helper provoke it.
    """
    yield
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and "send a notification copy" not in record.getMessage()
    ]
    assert errors == []
