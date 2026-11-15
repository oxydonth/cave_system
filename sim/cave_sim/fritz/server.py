"""The fake FRITZ!Box's HTTP side: ``/login_sid.lua`` and ``/webservices/homeautoswitch.lua``.

Both accept their parameters in the query string (as ``pyfritzhome`` sends them) and in a form body
(as the FRITZ! documentation shows for the login).
"""

from aiohttp import web

from cave_sim.fritz.box import FakeFritzBox, Response


async def _params(request: web.Request) -> dict[str, str]:
    params = dict(request.query)
    if request.method == "POST":
        params.update({key: str(value) for key, value in (await request.post()).items()})
    return params


def _to_http(response: Response) -> web.Response:
    return web.Response(
        status=response.status,
        text=response.body,
        content_type=response.content_type,
        charset="utf-8",
    )


def create_app(box: FakeFritzBox) -> web.Application:
    """The aiohttp application serving ``box``."""

    async def login(request: web.Request) -> web.Response:
        return _to_http(box.login(await _params(request)))

    async def homeautoswitch(request: web.Request) -> web.Response:
        return _to_http(box.homeautoswitch(await _params(request)))

    app = web.Application()
    app.router.add_route("*", "/login_sid.lua", login)
    app.router.add_route("*", "/webservices/homeautoswitch.lua", homeautoswitch)
    return app
