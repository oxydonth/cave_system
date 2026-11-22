"""Fixtures that let Home Assistant's test library load the package of this repository.

The two things below are the same as in tests/cave_heating/conftest.py, and any test directory
that uses the ``hass`` fixture needs them (see CLAUDE.md): the import of the integration package
at import time, and the autouse ``enable_custom_integrations`` fixture.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

import custom_components.cave_heating  # noqa: F401  (import-time side effect, see above)
from tests.cave_heating.fakes import FakeHome
from tests.packages.helpers import START, logged_errors, unexpected_errors


@pytest.fixture(autouse=True)
def clock(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Freeze the clock at ``START`` for every test, before Home Assistant is set up.

    The package's sensors are evaluated by a time pattern (a trigger of the automation "Cave: notify
    about what needs attention"): second 0 of every minute plus an offset of 50 to 500 ms that
    Home Assistant draws at random for each tracker. ``advance`` moves whole minutes,
    so the clock keeps its second within the minute for the whole test, and that second used to
    be wherever the real clock was when the test started. Two of them made tests fail now and
    then (see ``check_clock`` in helpers.py); second 30, half a minute from both, makes every
    test the same on every run, whatever the offset and however fast the machine.
    """
    freezer.move_to(START)
    return freezer


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    clock: FrozenDateTimeFactory, enable_custom_integrations: None
) -> None:
    """Let every test in this directory load the integrations in this repository.

    It asks for ``clock`` first, so that Home Assistant starts on the frozen clock.
    """


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Allow the one-minute timer of the package's automation to outlive the test.

    Stopping Home Assistant does not detach an automation's triggers, so the timer of its time
    pattern is still scheduled when the test ends, and the test library otherwise fails the test.
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


@pytest.fixture
def expected_errors() -> list[str]:
    """The errors the test provokes on purpose, as regular expressions for the whole message.

    Empty unless the test adds to it, so a test that provokes nothing allows nothing. A test of
    a failing notify action adds what that failure logs (``failed_copy`` in helpers.py).
    """
    return []


@pytest.fixture(autouse=True)
def no_logged_errors(
    caplog: pytest.LogCaptureFixture, expected_errors: list[str]
) -> Iterator[None]:
    """A test run of the package logs no error, except the ones the test provokes on purpose.

    A template that raises, an action that fails: Home Assistant logs it and carries on, so
    without this the package could be broken in a way no assertion notices. It looks at what was
    logged while the fixtures were set up and while the test ran (``logged_errors``).
    """
    yield
    assert unexpected_errors(logged_errors(caplog), expected_errors) == []
