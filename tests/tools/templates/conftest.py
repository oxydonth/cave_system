"""Fixtures that let the dashboard's templates run in a real Home Assistant.

The two things below are the same as in tests/cave_heating/conftest.py, and any test directory
that uses the ``hass`` fixture needs them (see CLAUDE.md): the import of the integration package
at import time, and the autouse ``enable_custom_integrations`` fixture. The generator's own tests
in the parent directory do not use Home Assistant and have no such conftest.
"""

import pytest

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test in this directory load the integrations in this repository."""
