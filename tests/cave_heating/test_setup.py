"""The skeleton integration is found by Home Assistant and sets up with any configuration."""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from custom_components.cave_heating.const import DOMAIN


async def test_integration_is_discovered(hass: HomeAssistant) -> None:
    """The loader finds this repository's integration and reads its manifest."""
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.domain == "nope"
    assert integration.file_path.parts[-2:] == ("custom_components", "cave_heating")


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {DOMAIN: None},
        {DOMAIN: {}},
        {DOMAIN: {"rooms": [{"id": "living_room", "name": "Wohnzimmer"}]}},
        {DOMAIN: {"unknown_key": [1, 2, 3]}},
    ],
    ids=["no_section", "empty_section", "empty_mapping", "one_room", "unvalidated_content"],
)
async def test_sets_up_with_any_configuration(
    hass: HomeAssistant, configuration: dict[str, Any]
) -> None:
    """Home Assistant sets the integration up and does not validate its configuration yet."""
    assert await async_setup_component(hass, DOMAIN, configuration)
    await hass.async_block_till_done()

    assert DOMAIN in hass.config.components
    assert hass.data[DOMAIN] == configuration.get(DOMAIN)
