"""The control API over HTTP: a thin JSON layer over :class:`cave_sim.runtime.SimulatedHome`.

Every endpoint is listed in ``sim/README.md``. Requests and responses are JSON. A request for a room
or device that does not exist is answered with 404, an invalid request with 400 and an
``{"error": ...}`` body; a successful change answers with the new state of what was changed.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web

from cave_sim.runtime import ControlError, SimulatedHome, UnknownTarget

HOME_KEY: Final = web.AppKey("home", SimulatedHome)

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class BadRequest(ControlError):
    """The request body is not what the endpoint expects."""


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError, UnicodeDecodeError:
        raise BadRequest("the body must be a JSON object") from None
    if not isinstance(data, dict):
        raise BadRequest("the body must be a JSON object")
    return data


def _field(
    data: dict[str, Any], key: str, kind: type | tuple[type, ...], *, required: bool = True
) -> Any:
    if key not in data:
        if required:
            raise BadRequest(f"missing field {key!r}")
        return None
    value = data[key]
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:
        raise BadRequest(f"field {key!r} has the wrong type")
    if not isinstance(value, kinds):
        raise BadRequest(f"field {key!r} has the wrong type")
    return value


def _number(data: dict[str, Any], key: str) -> float:
    return float(_field(data, key, (int, float)))


def _optional_number(data: dict[str, Any], key: str) -> float | None:
    value = _field(data, key, (int, float), required=False)
    return None if value is None else float(value)


def _flag(data: dict[str, Any], key: str) -> bool:
    return bool(_field(data, key, bool))


def _optional_flag(data: dict[str, Any], key: str) -> bool | None:
    value = _field(data, key, bool, required=False)
    return None if value is None else bool(value)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda obj: json.dumps(obj, sort_keys=True))


@web.middleware
async def _errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except UnknownTarget as error:
        return _json({"error": str(error)}, 404)
    except ControlError as error:
        return _json({"error": str(error)}, 400)


def create_app(home: SimulatedHome) -> web.Application:
    """The aiohttp application of the control API."""
    routes = web.RouteTableDef()

    def device_response(device_id: str) -> web.Response:
        return _json(home.device_state(device_id))

    def room_response(room_id: str) -> web.Response:
        return _json(home.state()["rooms"][room_id])

    @routes.get("/api/health")
    async def health(_request: web.Request) -> web.Response:
        healthy, reason = home.healthy()
        if healthy:
            return _json({"status": "ok"})
        return _json({"status": "unavailable", "reason": reason}, 503)

    @routes.get("/api/state")
    async def state(_request: web.Request) -> web.Response:
        return _json(home.state())

    @routes.get("/api/devices/{device_id}")
    async def device(request: web.Request) -> web.Response:
        return device_response(request.match_info["device_id"])

    @routes.post("/api/devices/{device_id}/window")
    async def window(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.set_window(device_id, _flag(await _body(request), "open"))
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/availability")
    async def availability(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.set_online(device_id, _flag(await _body(request), "online"))
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/silent")
    async def silent(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.set_silent(device_id, _flag(await _body(request), "silent"))
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/mode")
    async def mode(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.set_device_mode(device_id, _field(await _body(request), "mode", str))
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/window-detection")
    async def window_detection(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.detect_open_window(device_id, _number(await _body(request), "minutes"))
        return device_response(device_id)

    @routes.post("/api/zigbee2mqtt")
    async def zigbee2mqtt(request: web.Request) -> web.Response:
        data = await _body(request)
        unknown = set(data) - {"passive_timeout_minutes"}
        if unknown:
            raise BadRequest(f"unknown fields {sorted(unknown)}")
        home.configure_zigbee2mqtt(
            passive_timeout_minutes=_optional_number(data, "passive_timeout_minutes")
        )
        return _json(home.state()["zigbee2mqtt"])

    @routes.post("/api/devices/{device_id}/dial")
    async def dial(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        target = _field(await _body(request), "target", (int, float, str))
        if isinstance(target, str) and target not in ("on", "off"):
            raise BadRequest("target must be a number, 'on' or 'off'")
        home.turn_dial(device_id, target)
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/battery")
    async def battery(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        data = await _body(request)
        level = _field(data, "level", int)
        if not 0 <= level <= 100:
            raise BadRequest("level must be between 0 and 100")
        home.set_battery(device_id, level, _optional_flag(data, "low"))
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/fritz-lock")
    async def fritz_lock(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        data = await _body(request)
        home.set_fritz_lock(
            device_id,
            lock=_optional_flag(data, "lock"),
            device_lock=_optional_flag(data, "device_lock"),
        )
        return device_response(device_id)

    @routes.post("/api/devices/{device_id}/sonoff")
    async def sonoff(request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        home.set_returns_on_feed(device_id, _flag(await _body(request), "returns_on_feed"))
        return device_response(device_id)

    @routes.post("/api/rooms/{room_id}/temperature")
    async def room_temperature(request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        value = _number(await _body(request), "temperature")
        home.set_room_temperature(room_id, value)
        return room_response(room_id)

    @routes.post("/api/rooms/{room_id}/humidity")
    async def room_humidity(request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        value = _number(await _body(request), "humidity")
        if not 0 <= value <= 100:
            raise BadRequest("humidity must be between 0 and 100")
        home.set_room_humidity(room_id, value)
        return room_response(room_id)

    @routes.post("/api/outdoor")
    async def outdoor(request: web.Request) -> web.Response:
        value = _number(await _body(request), "temperature")
        home.set_outdoor_temperature(value)
        return _json({"outdoor_temperature": home.outdoor_temperature})

    @routes.post("/api/fritzbox")
    async def fritzbox(request: web.Request) -> web.Response:
        data = await _body(request)
        unknown = set(data) - {
            "report_delay",
            "summer",
            "holiday",
            "txbusy_while_pending",
            "window_target",
            "locked_command",
        }
        if unknown:
            raise BadRequest(f"unknown fields {sorted(unknown)}")
        home.configure_fritzbox(
            report_delay=_optional_number(data, "report_delay"),
            summer=_optional_flag(data, "summer"),
            holiday=_optional_flag(data, "holiday"),
            txbusy_while_pending=_optional_flag(data, "txbusy_while_pending"),
            window_target=_field(data, "window_target", str, required=False),
            locked_command=_field(data, "locked_command", str, required=False),
        )
        return _json(home.state()["fritzbox"])

    app = web.Application(middlewares=[_errors])
    app[HOME_KEY] = home
    app.add_routes(routes)
    return app
