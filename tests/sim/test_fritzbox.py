"""The fake FRITZ!Box, driven by the real pyfritzhome client and by Home Assistant's integration."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pyfritzhome import Fritzhome, LoginError
from requests.exceptions import HTTPError

from cave_sim.clock import ManualClock
from cave_sim.fritz import server
from cave_sim.fritz.box import FakeFritzBox, pbkdf2_response
from cave_sim.home import DeviceSpec, parse_home
from cave_sim.room import Room
from cave_sim.testing import async_advance

USER = "cave-test"
PASSWORD = "not-a-real-password"  # an obviously fake login for the fake box
AIN = "13979 0000001"


def test_pbkdf2_response_matches_the_fritz_example() -> None:
    """The worked example of FRITZ!'s HTTP_Session-ID_EN.pdf."""
    response = pbkdf2_response("2$10000$5A1711$2000$5A1722", "1example!")
    assert response == "5A1722$1798a1672bca7c6463d6b245f82b53703b0f50813401b03e4045a5861e689adb"


class Box:
    """A fake box on 127.0.0.1 with one thermostat, and a client for it."""

    def __init__(self, box: FakeFritzBox, clock: ManualClock, url: str) -> None:
        self.box = box
        self.clock = clock
        self.url = url
        self.thermostat = box.thermostats[AIN]

    def client(self, password: str = PASSWORD) -> Fritzhome:
        return Fritzhome(host=self.url, user=USER, password=password, timeout=5)

    async def run(self, function: Callable[..., Any], *args: Any) -> Any:
        """pyfritzhome is synchronous; run it in a thread while the server runs on the loop."""
        return await asyncio.to_thread(function, *args)


@pytest.fixture
async def fritz(socket_enabled: None) -> AsyncGenerator[Box]:
    clock = ManualClock()
    box = FakeFritzBox(clock, USER, PASSWORD)
    room = Room("bathroom", "Bad", 20.0, 60.0)
    box.add_thermostat(
        DeviceSpec(model="FRITZ!Smart Thermo 302", name="Bad Heizung", ain=AIN), room
    )
    box.tick()
    runner = web.AppRunner(server.create_app(box))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    yield Box(box, clock, f"http://127.0.0.1:{runner.addresses[0][1]}")
    await runner.cleanup()


async def test_log_in_and_list_devices(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)
    devices = await fritz.run(client.get_devices)

    assert [device.ain for device in devices] == [AIN]
    device = devices[0]
    assert device.name == "Bad Heizung"
    assert device.productname == "FRITZ!Smart Thermo 302"
    assert device.has_thermostat
    assert device.has_temperature_sensor
    assert device.present
    assert device.target_temperature == 20.0
    assert device.temperature == 20.0
    assert device.battery_level == 80
    assert not device.summer_active
    assert not device.holiday_active
    assert not device.lock
    assert await fritz.run(client.has_templates)


async def test_a_wrong_password_is_refused(fritz: Box) -> None:
    with pytest.raises(LoginError):
        await fritz.run(fritz.client("wrong").login)


async def test_requests_without_a_session_are_forbidden(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)
    await fritz.run(client.logout)
    client._sid = "0123456789abcdef"  # a session the box never issued
    with pytest.raises(HTTPError) as error:
        await fritz.run(client.get_device_elements)
    assert error.value.response.status_code == 403


async def test_a_session_expires_after_twenty_idle_minutes(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)
    fritz.clock.advance(19 * 60)
    await fritz.run(client.get_device_elements)  # still valid, and extended
    fritz.clock.advance(19 * 60)
    await fritz.run(client.get_device_elements)
    fritz.clock.advance(21 * 60)
    with pytest.raises(HTTPError):
        await fritz.run(client.get_device_elements)


async def test_set_and_read_back_a_target_reported_at_once(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)

    await fritz.run(client.set_target_temperature, AIN, 22.5)

    assert await fritz.run(client.get_target_temperature, AIN) == 22.5
    await fritz.run(client.update_devices)
    assert client.get_device_by_ain(AIN).target_temperature == 22.5
    assert fritz.thermostat.device_tsoll == 45


async def test_a_delayed_box_reports_the_new_target_only_after_the_delay(fritz: Box) -> None:
    fritz.box.report_delay = 180
    client = fritz.client()
    await fritz.run(client.login)

    await fritz.run(client.set_target_temperature, AIN, 23.0)
    assert await fritz.run(client.get_target_temperature, AIN) == 20.0
    fritz.clock.advance(179)
    assert await fritz.run(client.get_target_temperature, AIN) == 20.0
    fritz.clock.advance(1)
    assert await fritz.run(client.get_target_temperature, AIN) == 23.0


async def test_the_box_counts_the_set_commands_each_thermostat_receives(fritz: Box) -> None:
    """Every sethkrtsoll counts, delayed, repeated and refused ones alike; reads do not."""
    fritz.box.report_delay = 60
    client = fritz.client()
    await fritz.run(client.login)
    assert fritz.thermostat.set_commands == 0

    await fritz.run(client.set_target_temperature, AIN, 23.0)
    await fritz.run(client.get_target_temperature, AIN)
    await fritz.run(client.update_devices)
    assert fritz.thermostat.set_commands == 1
    await fritz.run(client.set_target_temperature, AIN, 23.0)  # the same value again
    fritz.thermostat.lock = True
    with pytest.raises(HTTPError):
        await fritz.run(client.set_target_temperature, AIN, 22.0)
    assert fritz.thermostat.set_commands == 3


async def test_off_and_on_are_the_special_values(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)
    await fritz.run(client.set_target_temperature, AIN, 0)  # below 8 °C means off
    await fritz.run(client.update_devices)
    assert client.get_device_by_ain(AIN).get_hkr_state() == "off"
    await fritz.run(client.set_target_temperature, AIN, 30)  # above 28 °C means on
    await fritz.run(client.update_devices)
    assert client.get_device_by_ain(AIN).get_hkr_state() == "on"


@pytest.mark.parametrize("mode", ["summer", "holiday", "lock"])
async def test_a_command_is_refused_while_summer_holiday_or_lock_is_active(
    fritz: Box, mode: str
) -> None:
    if mode == "lock":
        fritz.thermostat.lock = True
    else:
        setattr(fritz.box, f"{mode}_active", True)
    client = fritz.client()
    await fritz.run(client.login)

    with pytest.raises(HTTPError) as error:
        await fritz.run(client.set_target_temperature, AIN, 22.0)

    assert error.value.response.status_code == 400
    assert fritz.thermostat.device_tsoll == 40
    await fritz.run(client.update_devices)
    device = client.get_device_by_ain(AIN)
    assert device.lock  # the box reports the lock as set in all three cases
    assert device.summer_active == (mode == "summer")
    assert device.holiday_active == (mode == "holiday")


async def test_an_absent_thermostat_reports_no_temperatures(fritz: Box) -> None:
    fritz.thermostat.present = False
    client = fritz.client()
    await fritz.run(client.login)
    device = (await fritz.run(client.get_devices))[0]
    assert device.present is False
    assert device.target_temperature is None


DEMO_BATHROOM = parse_home(
    {
        "rooms": [
            {
                "id": "bathroom",
                "name": "Bad",
                "temperature": 20.0,
                "devices": [{"model": "FRITZ!Smart Thermo 302", "name": "Bad Heizung", "ain": AIN}],
            }
        ]
    }
)


async def test_home_assistant_sets_a_target(hass: HomeAssistant, simulated_home: Any) -> None:
    """Home Assistant's fritzbox integration logs in, finds the thermostat and drives it."""
    sim = await simulated_home(DEMO_BATHROOM)
    state = hass.states.get("climate.bad_heizung")
    assert state is not None
    assert state.state == "heat"
    assert state.attributes["temperature"] == 20.0

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.bad_heizung", "temperature": 21.5},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert sim.home.fritzbox.thermostats[AIN].device_tsoll == 43
    assert hass.states.get("climate.bad_heizung").attributes["temperature"] == 21.5


async def test_home_assistant_sees_a_delayed_report_late(
    hass: HomeAssistant, simulated_home: Any, freezer: Any
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.configure_fritzbox(report_delay=120)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.bad_heizung", "temperature": 22.0},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("climate.bad_heizung").attributes["temperature"] == 20.0

    # Home Assistant polls every 30 s; after the delay the next poll brings the new target.
    await async_advance(hass, freezer, 150, step=30)
    assert hass.states.get("climate.bad_heizung").attributes["temperature"] == 22.0


@pytest.mark.parametrize("mode", ["summer", "holiday"])
async def test_home_assistant_refuses_during_summer_or_holiday(
    hass: HomeAssistant, simulated_home: Any, mode: str
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.configure_fritzbox(**{mode: True})
    await sim.async_refresh_fritzbox()
    assert hass.states.get("climate.bad_heizung").attributes["preset_mode"] == mode

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.bad_heizung", "temperature": 23.0},
            blocking=True,
        )
    assert sim.home.fritzbox.thermostats[AIN].device_tsoll == 40


async def test_home_assistant_refuses_while_the_key_lock_is_on(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.set_fritz_lock("bad_heizung", lock=True)
    await sim.async_refresh_fritzbox()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.bad_heizung", "temperature": 23.0},
            blocking=True,
        )


async def test_a_dial_turned_off_reads_as_mode_off(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.turn_dial("bad_heizung", "off")
    await sim.async_refresh_fritzbox()
    assert hass.states.get("climate.bad_heizung").state == "off"

    sim.home.turn_dial("bad_heizung", 19.5)
    await sim.async_refresh_fritzbox()
    state = hass.states.get("climate.bad_heizung")
    assert state.state == "heat"
    assert state.attributes["temperature"] == 19.5


async def test_an_absent_thermostat_is_unavailable(
    hass: HomeAssistant, simulated_home: Any
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.set_online("bad_heizung", False)
    await sim.async_refresh_fritzbox()
    assert hass.states.get("climate.bad_heizung").state == "unavailable"
    sim.home.set_online("bad_heizung", True)
    await sim.async_refresh_fritzbox()
    assert hass.states.get("climate.bad_heizung").state == "heat"


async def test_the_thermostat_s_own_window_detection(fritz: Box) -> None:
    client = fritz.client()
    await fritz.run(client.login)
    fritz.box.detect_open_window(fritz.thermostat, 10)
    device = (await fritz.run(client.get_devices))[0]
    assert device.window_open is True
    end = int(fritz.clock.now()) + 600
    assert (
        f"<windowopenactiveendtime>{end}</windowopenactiveendtime>" in fritz.box.device_list_xml()
    )
    assert device.target_temperature == 20.0  # "unchanged" is the default
    assert fritz.thermostat.opening == 0.0

    fritz.box.window_target = "off"
    await fritz.run(client.update_devices)
    assert client.get_device_by_ain(AIN).get_hkr_state() == "off"
    fritz.clock.advance(601)
    await fritz.run(client.update_devices)
    device = client.get_device_by_ain(AIN)
    assert device.window_open is False
    assert device.target_temperature == 20.0


async def test_a_locked_box_can_accept_a_command_without_effect(fritz: Box) -> None:
    fritz.box.locked_command = "ignore"
    fritz.thermostat.lock = True
    client = fritz.client()
    await fritz.run(client.login)
    await fritz.run(client.set_target_temperature, AIN, 22.0)  # no error
    assert fritz.thermostat.device_tsoll == 40
    assert await fritz.run(client.get_target_temperature, AIN) == 20.0


@pytest.mark.parametrize("window_target", ["unchanged", "off"])
async def test_home_assistant_during_the_thermostat_s_window_detection(
    hass: HomeAssistant, simulated_home: Any, window_target: str
) -> None:
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.configure_fritzbox(window_target=window_target)
    sim.home.detect_open_window("bad_heizung", 15)
    await sim.async_refresh_fritzbox()

    assert hass.states.get("binary_sensor.bad_heizung_open_window_detected").state == "on"
    state = hass.states.get("climate.bad_heizung")
    if window_target == "off":
        assert state.state == "off"
        assert state.attributes["temperature"] is None
    else:
        assert state.state == "heat"
        assert state.attributes["temperature"] == 20.0


@pytest.mark.parametrize("locked_command", ["refuse", "ignore"])
async def test_a_lock_set_after_the_last_poll(
    hass: HomeAssistant, simulated_home: Any, locked_command: str
) -> None:
    """Home Assistant checks the lock it last polled; in the gap the command reaches the box.

    Refused, it reaches the caller as requests' HTTPError, not as a HomeAssistantError. Ignored,
    the call succeeds and nothing changes, so only the missing confirmation shows it.
    """
    sim = await simulated_home(DEMO_BATHROOM)
    sim.home.configure_fritzbox(locked_command=locked_command)
    sim.home.set_fritz_lock("bad_heizung", lock=True)  # no refresh: Home Assistant does not know
    call = hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.bad_heizung", "temperature": 23.0},
        blocking=True,
    )
    if locked_command == "refuse":
        with pytest.raises(HTTPError) as error:
            await call
        assert not isinstance(error.value, HomeAssistantError)
    else:
        await call
    await hass.async_block_till_done()
    assert sim.home.fritzbox.thermostats[AIN].device_tsoll == 40
