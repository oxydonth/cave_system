"""The control API over HTTP, on the demo home."""

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp import ClientSession, web

from cave_sim import control_api
from cave_sim.clock import ManualClock
from cave_sim.home import load_home
from cave_sim.mqtt import InMemoryBroker
from cave_sim.runtime import SimulatedHome
from cave_sim.testing import DEMO_HOME


class Api:
    def __init__(self, home: SimulatedHome, broker: InMemoryBroker, url: str) -> None:
        self.home = home
        self.broker = broker
        self.url = url
        self.session = ClientSession()

    async def get(self, path: str) -> tuple[int, Any]:
        async with self.session.get(self.url + path) as response:
            return response.status, await response.json()

    async def post(self, path: str, body: Any) -> tuple[int, Any]:
        async with self.session.post(self.url + path, data=json.dumps(body)) as response:
            return response.status, await response.json()


@pytest.fixture
async def api(socket_enabled: None) -> AsyncGenerator[Api]:
    broker = InMemoryBroker()
    home = SimulatedHome(
        load_home(DEMO_HOME),
        ManualClock(),
        broker.client("sim"),
        fritz_username="u",
        fritz_password="p",
    )
    home.start()
    runner = web.AppRunner(control_api.create_app(home))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    client = Api(home, broker, f"http://127.0.0.1:{runner.addresses[0][1]}")
    yield client
    await client.session.close()
    await runner.cleanup()


async def test_health_and_state(api: Api) -> None:
    assert await api.get("/api/health") == (200, {"status": "ok"})
    status, state = await api.get("/api/state")
    assert status == 200
    assert set(state["rooms"]) == {"living_room", "bedroom", "bathroom"}
    assert state["devices"]["bad_heizung"]["family"] == "fritz"
    assert state["devices"]["wohnzimmer_heizung_fenster"]["model"] == "TRV-ZBT"


async def test_open_and_close_a_window(api: Api) -> None:
    status, device = await api.post("/api/devices/wohnzimmer_fenster/window", {"open": True})
    assert status == 200
    assert device["state"]["contact"] is False
    assert api.home.rooms["living_room"].open_windows() == 1
    _, device = await api.post("/api/devices/wohnzimmer_fenster/window", {"open": False})
    assert device["state"]["contact"] is True


async def test_set_room_and_outdoor_temperature_and_humidity(api: Api) -> None:
    status, room = await api.post("/api/rooms/bedroom/temperature", {"temperature": 21.5})
    assert (status, room["temperature"]) == (200, 21.5)
    _, room = await api.post("/api/rooms/bathroom/humidity", {"humidity": 80})
    assert room["humidity"] == 80
    assert await api.post("/api/outdoor", {"temperature": -3}) == (
        200,
        {"outdoor_temperature": -3.0},
    )


async def test_take_a_device_offline_and_back(api: Api) -> None:
    _, device = await api.post("/api/devices/schlafzimmer_heizung/availability", {"online": False})
    assert device["available"] is False
    retained = api.broker.retained["zigbee2mqtt/Schlafzimmer Heizung/availability"]
    assert json.loads(retained.payload) == {"state": "offline"}
    _, device = await api.post("/api/devices/bad_heizung/availability", {"online": False})
    assert device["available"] is False


async def test_a_fritz_thermostat_shows_the_set_commands_it_received(api: Api) -> None:
    status, device = await api.get("/api/devices/bad_heizung")
    assert (status, device["set_commands"]) == (200, 0)
    # Two set commands, as the FRITZ!Box receives them from Home Assistant's client.
    box = api.home.fritzbox
    thermostat = box.thermostats["13979 0000001"]
    box._sethkrtsoll(thermostat, "44")
    box._sethkrtsoll(thermostat, "44")
    # A dial turned on the device is not a command the box received.
    await api.post("/api/devices/bad_heizung/dial", {"target": 19.0})

    _, device = await api.get("/api/devices/bad_heizung")
    assert device["set_commands"] == 2
    _, state = await api.get("/api/state")
    assert state["devices"]["bad_heizung"]["set_commands"] == 2


async def test_turn_a_dial(api: Api) -> None:
    _, device = await api.post("/api/devices/wohnzimmer_heizung_tuer/dial", {"target": 23.2})
    assert device["state"]["occupied_heating_setpoint"] == 23.0
    _, device = await api.post("/api/devices/bad_heizung/dial", {"target": "off"})
    assert device["state"]["target_temperature"] == "off"
    status, error = await api.post("/api/devices/wohnzimmer_heizung_tuer/dial", {"target": "off"})
    assert status == 400
    assert "dial position" in error["error"]


async def test_change_the_fritz_behaviour(api: Api) -> None:
    status, box = await api.post("/api/fritzbox", {"report_delay": 180, "summer": True})
    assert status == 200
    assert box["report_delay"] == 180
    assert box["summer"] is True
    _, device = await api.post("/api/devices/bad_heizung/fritz-lock", {"lock": False})
    assert device["state"]["lock"] is True  # summer mode sets the lock
    _, box = await api.post("/api/fritzbox", {"summer": False})
    _, device = await api.post("/api/devices/bad_heizung/fritz-lock", {"device_lock": True})
    assert device["state"]["lock"] is False
    assert device["state"]["device_lock"] is True


async def test_battery_and_sonoff_behaviour(api: Api) -> None:
    _, device = await api.post("/api/devices/wohnzimmer_fenster/battery", {"level": 9, "low": True})
    assert device["state"]["battery"] == 9
    assert device["state"]["battery_low"] is True
    _, device = await api.post(
        "/api/devices/wohnzimmer_heizung_fenster/sonoff", {"returns_on_feed": False}
    )
    assert device["returns_on_feed"] is False


async def test_read_device_state(api: Api) -> None:
    status, device = await api.get("/api/devices/schlafzimmer_heizung")
    assert status == 200
    assert device["model"] == "BTH-RA"
    assert device["state"]["operating_mode"] == "manual"
    assert "valve_opening" in device


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/devices/nothing/window", {"open": True}, 404),
        ("/api/rooms/nowhere/temperature", {"temperature": 20}, 404),
        ("/api/devices/bad_heizung/window", {"open": True}, 400),
        ("/api/devices/wohnzimmer_fenster/window", {"open": "yes"}, 400),
        ("/api/devices/wohnzimmer_fenster/window", {}, 400),
        ("/api/devices/wohnzimmer_fenster/battery", {"level": 101}, 400),
        ("/api/rooms/bedroom/humidity", {"humidity": -1}, 400),
        ("/api/fritzbox", {"report_delay": -1}, 400),
        ("/api/fritzbox", {"colour": "red"}, 400),
        ("/api/devices/bad_heizung/sonoff", {"returns_on_feed": True}, 400),
    ],
)
async def test_invalid_requests(api: Api, path: str, body: Any, status: int) -> None:
    actual, response = await api.post(path, body)
    assert actual == status
    assert "error" in response


async def test_a_body_that_is_not_json(api: Api) -> None:
    async with api.session.post(api.url + "/api/outdoor", data="warm") as response:
        assert response.status == 400


async def test_a_device_falls_silent_and_answers_again(api: Api) -> None:
    status, device = await api.post("/api/devices/wohnzimmer/silent", {"silent": True})
    assert status == 200
    assert (device["answering"], device["available"]) == (False, True)
    _, device = await api.post("/api/devices/wohnzimmer/silent", {"silent": False})
    assert device["answering"] is True
    status, _ = await api.post("/api/devices/bad_heizung/silent", {"silent": True})
    assert status == 400


async def test_the_passive_timeout(api: Api) -> None:
    status, settings = await api.post("/api/zigbee2mqtt", {"passive_timeout_minutes": 5})
    assert (status, settings) == (200, {"passive_timeout_minutes": 5})
    assert (await api.post("/api/zigbee2mqtt", {"passive_timeout_minutes": 0}))[0] == 400


async def test_change_the_mode_on_the_device(api: Api) -> None:
    _, device = await api.post("/api/devices/schlafzimmer_heizung/mode", {"mode": "pause"})
    assert device["state"]["operating_mode"] == "pause"
    _, device = await api.post("/api/devices/wohnzimmer_heizung_tuer/mode", {"mode": "off"})
    assert device["state"]["system_mode"] == "off"
    status, error = await api.post("/api/devices/wohnzimmer_heizung_tuer/mode", {"mode": "pause"})
    assert status == 400
    assert "system_mode" in error["error"]


async def test_trigger_the_fritz_window_detection(api: Api) -> None:
    _, box = await api.post("/api/fritzbox", {"window_target": "off", "locked_command": "ignore"})
    assert (box["window_target"], box["locked_command"]) == ("off", "ignore")
    _, device = await api.post("/api/devices/bad_heizung/window-detection", {"minutes": 10})
    assert device["state"]["window_open_until"] == int(api.home.clock.now()) + 600
    assert (await api.post("/api/fritzbox", {"window_target": "closed"}))[0] == 400
    assert (await api.post("/api/devices/wohnzimmer/window-detection", {"minutes": 10}))[0] == 400


async def test_health_needs_a_started_home_and_a_broker_connection(socket_enabled: None) -> None:
    connected = False
    home = SimulatedHome(
        load_home(DEMO_HOME),
        ManualClock(),
        InMemoryBroker().client("sim"),
        fritz_username="u",
        fritz_password="p",
        connected=lambda: connected,
    )
    runner = web.AppRunner(control_api.create_app(home))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/api/health"
    try:
        async with ClientSession() as session:
            async with session.get(url) as response:
                assert response.status == 503
            home.start()
            async with session.get(url) as response:
                assert response.status == 503
                assert "disconnected" in (await response.json())["reason"]
            connected = True
            async with session.get(url) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()
