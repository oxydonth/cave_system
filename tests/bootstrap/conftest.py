"""A real Home Assistant, in-process and reachable over HTTP, for ``tools/bootstrap.py``.

The tool talks to Home Assistant only through its HTTP and WebSocket APIs. These fixtures start
the parts of Home Assistant 2026.10.4 that it uses (onboarding, auth, the config API, the schedule
helper) behind a real aiohttp server on 127.0.0.1, so the tool runs exactly as it does in the
container, against the code that implements those endpoints. Only what would leave the machine
is replaced: the MQTT broker check, the FRITZ!Box and the integrations' set-up of their entries.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import sys
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import homeassistant.components.mqtt.config_flow  # noqa: F401 - imported for patch() below
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from tools.bootstrap import Output, Settings, bootstrap

OWNER_PASSWORD = "owner-pass-for-tests"
MQTT_PASSWORD = "mqtt-pass-for-tests"
FRITZ_PASSWORD = "fritz-pass-for-tests"

HEATING = """\
version: 1
rooms:
  - id: living_room
    name: Wohnzimmer
    trvs:
      - entity: climate.wohnzimmer_heizung
    schedule: schedule.living_room_heating
  - id: bathroom
    name: Bad
    trvs:
      - entity: climate.bad_heizung
    schedule: schedule.bad_heizplan
  - id: hall
    name: Flur
    trvs:
      - entity: climate.flur_heizung
"""


@dataclass
class Run:
    """The outcome of one run of the tool."""

    ok: bool
    output: str
    error: str = ""


@dataclass
class Home:
    """Home Assistant and everything a test needs to run the tool against it."""

    hass: HomeAssistant
    url: str
    settings: Settings
    broker_logins: list[dict[str, Any]] = field(default_factory=list)
    """Every broker configuration the MQTT flow tested, in order."""
    broker_accepts: bool = True

    async def run(self, **changes: Any) -> Run:
        """Run the tool; ``changes`` replace fields of the settings for this run only."""
        settings = replace(self.settings, **changes)
        stream = io.StringIO()
        out = Output(settings.secrets, stream)
        try:
            await bootstrap(settings, out)
        except Exception as error:
            out.say(f"bootstrap failed: {error}")
            return Run(False, stream.getvalue(), str(error))
        return Run(True, stream.getvalue())

    @property
    def token_file(self) -> Path:
        return self.settings.token_file


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Each schedule helper keeps a timer for its next change, which outlives the test."""
    return True


@pytest.fixture
async def home(
    hass: HomeAssistant,
    local_auth: Any,
    aiohttp_client: Any,
    socket_enabled: None,
    tmp_path: Path,
) -> AsyncIterator[Home]:
    # Everything that registers HTTP views must be set up before the server starts: MQTT's
    # dependency file_upload, analytics (onboarding's core_config step needs it), and network
    # for FRITZ!Box Tools. In the container Home Assistant sets them all up at start.
    for domain in (
        "analytics",
        "http",
        "api",
        "config",
        "onboarding",
        "websocket_api",
        "schedule",
        "input_boolean",
        "person",
        "file_upload",
        "network",
    ):
        assert await async_setup_component(hass, domain, {})
    for domain in ONBOARDING_ENTRIES:  # stand-ins, so that their entries can be removed
        mock_integration(hass, MockModule(domain))
    client = await aiohttp_client(hass.http.app)
    url = str(client.make_url("/")).rstrip("/")

    token_dir = tmp_path / "config" / ".cave"
    token_dir.mkdir(parents=True, mode=0o700)
    heating = tmp_path / "config" / "cave" / ".applied" / "heating.yaml"
    heating.parent.mkdir(parents=True)
    heating.write_text(HEATING, encoding="utf-8")

    settings = Settings.from_json(
        {
            "owner_name": "Max",
            "owner_user": "max",
            "owner_password": OWNER_PASSWORD,
            "time_zone": "Europe/Berlin",
            "mqtt_host": "127.0.0.1",
            "mqtt_port": 1883,
            "mqtt_user": "cave",
            "mqtt_password": MQTT_PASSWORD,
            "url": url,
            "token_file": str(token_dir / "token"),
            "heating_file": str(heating),
            "config_entries_file": str(tmp_path / "config" / ".storage" / "core.config_entries"),
            "timeout": 10,
            "onboarding_wait": 5,
        }
    )
    result = Home(hass, url, settings)

    def try_connection(config: dict[str, Any]) -> bool:
        result.broker_logins.append(dict(config))
        return result.broker_accepts

    with (
        patch("homeassistant.components.mqtt.config_flow.try_connection", try_connection),
        patch("homeassistant.components.mqtt.async_setup_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.mqtt.async_unload_entry", AsyncMock(return_value=True)),
        patch.object(hass.config_entries.flow, "async_init", _without_onboarding_flows(hass)),
    ):
        yield result
        # Unloaded while the replaced set-up functions are still in place.
        for entry in hass.config_entries.async_entries():
            await hass.config_entries.async_unload(entry.entry_id)


def _without_onboarding_flows(hass: HomeAssistant) -> Any:
    """Onboarding's core_config step starts flows for integrations that go to the internet
    (radio browser, Google Translate) and for the shopping list; their client libraries are not
    installed here. The entries those flows create are added directly instead, with the source
    `onboarding`, as their flows would (`met` declines without a home location)."""
    original = hass.config_entries.flow.async_init

    async def async_init(
        handler: str, *, context: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if (context or {}).get("source") == "onboarding":
            if handler in ONBOARDING_ENTRIES:
                MockConfigEntry(
                    domain=handler, title=ONBOARDING_ENTRIES[handler], source="onboarding"
                ).add_to_hass(hass)
            return {"type": "abort", "reason": "not_in_tests"}
        return await original(handler, context=context, **kwargs)

    return async_init


ONBOARDING_ENTRIES = {
    "radio_browser": "Radio Browser",
    "google_translate": "Google Translate text-to-speech",
    "shopping_list": "Shopping list",
}


FRITZ_LIBRARIES = (
    "fritzconnection",
    "fritzconnection.core",
    "fritzconnection.core.exceptions",
    "fritzconnection.lib",
    "fritzconnection.lib.fritzcall",
    "fritzconnection.lib.fritzhosts",
    "fritzconnection.lib.fritzstatus",
    "fritzconnection.lib.fritzwlan",
    "pyfritzhome",
    "pyfritzhome.devicetypes",
    "pyfritzhome.devicetypes.fritzhomeentitybase",
    "pyfritzhome.fritzhomedevice",
    "xmltodict",
)


class _StubLibrary(types.ModuleType):
    """Stands in for a client library that is not installed: every name is a fresh class, which
    works both as a base class and in ``except``. The config flows' calls are patched anyway."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        stub = type(name, (Exception,), {})
        setattr(self, name, stub)
        return stub


@pytest.fixture
async def fritz_box(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MagicMock]:
    """A FRITZ!Box for both FRITZ! config flows, and their entries set up without one.

    The test library does not install the FRITZ! client libraries (``pyfritzhome`` arrives with
    the simulators); where they are missing they are replaced by stubs for this test only.
    """
    before = set(sys.modules)
    missing = {
        top
        for top in {name.split(".")[0] for name in FRITZ_LIBRARIES}
        if top not in sys.modules and importlib.util.find_spec(top) is None
    }
    for name in FRITZ_LIBRARIES:
        if name.split(".")[0] in missing:
            monkeypatch.setitem(sys.modules, name, _StubLibrary(name))
    importlib.import_module("homeassistant.components.fritz.config_flow")
    importlib.import_module("homeassistant.components.fritzbox.config_flow")
    smarthome = MagicMock()
    tools = MagicMock()
    tools.return_value.call_action.side_effect = lambda service, action: {
        "NewModelName": "FRITZ!Box 7590",
        "NewEnable": True,
    }
    tools.return_value.services = {}
    with (
        patch("homeassistant.components.fritzbox.config_flow.Fritzhome", smarthome),
        patch("homeassistant.components.fritz.config_flow.FritzConnection", tools),
        patch("homeassistant.components.fritzbox.async_setup_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.fritz.async_setup_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.fritzbox.async_unload_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.fritz.async_unload_entry", AsyncMock(return_value=True)),
    ):
        yield MagicMock(smarthome=smarthome, tools=tools)
        for domain in ("fritz", "fritzbox"):
            for entry in hass.config_entries.async_entries(domain):
                await hass.config_entries.async_unload(entry.entry_id)
    # Integration modules imported against the stubs must not outlive this test.
    for name in set(sys.modules) - before:
        if name.startswith(("homeassistant.components.fritz", *missing)):
            del sys.modules[name]
