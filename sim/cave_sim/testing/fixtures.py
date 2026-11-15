"""The pytest fixture ``simulated_home``.

Import it into the ``conftest.py`` of a test directory that uses Home Assistant::

    from cave_sim.testing.fixtures import simulated_home  # noqa: F401

``simulated_home`` is a factory: ``await simulated_home(home)`` starts a simulated home (a
:class:`~cave_sim.home.HomeSpec` or the path of a ``home.yaml``; default the demo home) and
returns a :class:`~cave_sim.testing.SimulatedHomeHandle`. What it started is stopped after the
test.
It enables sockets (connections stay limited to ``127.0.0.1``) because the fake FRITZ!Box listens on
the loopback address, and uses the test library's ``mock_hass_config`` (an empty configuration.yaml,
which the MQTT integration reads), as its own ``mqtt_mock`` fixture does.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from cave_sim.home import HomeSpec
from cave_sim.testing import DEMO_HOME, SimulatedHomeHandle, async_start_simulated_home

type SimulatedHomeFactory = Callable[..., Awaitable[SimulatedHomeHandle]]


@pytest.fixture
async def simulated_home(
    hass: HomeAssistant,
    mock_hass_config: None,
    mqtt_client_mock: Any,
    mqtt_mock_entry: Any,
    socket_enabled: None,
) -> AsyncGenerator[SimulatedHomeFactory]:
    """Start simulated homes for the test and stop them afterwards."""
    handles: list[SimulatedHomeHandle] = []

    async def start(
        home: HomeSpec | Path | str = DEMO_HOME, *, setup_fritzbox: bool = True
    ) -> SimulatedHomeHandle:
        if handles:
            raise RuntimeError("one simulated home per test: Home Assistant has one MQTT entry")
        handle = await async_start_simulated_home(
            hass,
            home,
            mqtt_client_mock=mqtt_client_mock,
            mqtt_mock_entry=mqtt_mock_entry,
            setup_fritzbox=setup_fritzbox,
        )
        handles.append(handle)
        return handle

    yield start
    for handle in handles:
        await handle.async_stop()
