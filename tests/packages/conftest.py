"""Fixtures that let Home Assistant's test library load the package of this repository.

The two things below are the same as in tests/cave_heating/conftest.py, and any test directory
that uses the ``hass`` fixture needs them (see CLAUDE.md): the import of the integration package
at import time, and the autouse ``enable_custom_integrations`` fixture.
"""

import pytest

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)


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
