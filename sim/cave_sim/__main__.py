"""``python -m cave_sim``: run the simulated home against a real MQTT broker.

Configured through the environment (see ``sim/README.md``):

=========================  ===========================================  ==========================
Variable                   Meaning                                      Default
=========================  ===========================================  ==========================
``CAVE_SIM_HOME``          The home description                         ``sim/demo/home.yaml``
``CAVE_SIM_MQTT_HOST``     MQTT broker host                             ``mosquitto``
``CAVE_SIM_MQTT_PORT``     MQTT broker port                             ``1883``
``CAVE_MQTT_USER``         Broker login (concept section 4.2)           none
``CAVE_MQTT_PASSWORD``     Broker password                              none
``CAVE_FRITZ_USER``        Login the fake FRITZ!Box accepts             ``cave``
``CAVE_FRITZ_PASSWORD``    Its password (required)                      —
``CAVE_SIM_BIND``          Address the HTTP servers listen on           ``0.0.0.0``
``CAVE_SIM_FRITZ_PORT``    Fake FRITZ!Box port                          ``8480``
``CAVE_SIM_CONTROL_PORT``  Control API port                             ``8481``
``CAVE_SIM_LOG_LEVEL``     Python log level                             ``INFO``
=========================  ===========================================  ==========================

Nothing here imports Home Assistant or pytest; ``cave_sim.testing`` is not used at run time.
"""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from cave_sim import control_api
from cave_sim.clock import AsyncioClock
from cave_sim.fritz import server as fritz_server
from cave_sim.home import load_home
from cave_sim.mqtt import PahoTransport
from cave_sim.runtime import SimulatedHome
from cave_sim.zigbee.discovery import Settings

_LOGGER = logging.getLogger("cave_sim")

DEFAULT_HOME = Path(__file__).resolve().parent.parent / "demo" / "home.yaml"


class ConfigError(Exception):
    """The environment does not configure the simulator correctly."""


@dataclass(frozen=True)
class Config:
    """The simulator's configuration."""

    home: Path
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str | None
    mqtt_password: str | None
    fritz_user: str
    fritz_password: str
    bind: str
    fritz_port: int
    control_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Read the configuration; raise :class:`ConfigError` naming the variable at fault."""

        def port(name: str, default: int) -> int:
            raw = env.get(name, str(default))
            if not raw.isdigit() or not 0 < int(raw) < 65536:
                raise ConfigError(f"{name} must be a port number, got {raw!r}")
            return int(raw)

        fritz_password = env.get("CAVE_FRITZ_PASSWORD", "")
        if not fritz_password:
            raise ConfigError("CAVE_FRITZ_PASSWORD must be set: the fake FRITZ!Box needs a login")
        return cls(
            home=Path(env.get("CAVE_SIM_HOME", str(DEFAULT_HOME))),
            mqtt_host=env.get("CAVE_SIM_MQTT_HOST", "mosquitto"),
            mqtt_port=port("CAVE_SIM_MQTT_PORT", 1883),
            mqtt_user=env.get("CAVE_MQTT_USER") or None,
            mqtt_password=env.get("CAVE_MQTT_PASSWORD") or None,
            fritz_user=env.get("CAVE_FRITZ_USER") or "cave",
            fritz_password=fritz_password,
            bind=env.get("CAVE_SIM_BIND", "0.0.0.0"),
            fritz_port=port("CAVE_SIM_FRITZ_PORT", 8480),
            control_port=port("CAVE_SIM_CONTROL_PORT", 8481),
            log_level=env.get("CAVE_SIM_LOG_LEVEL", "INFO").upper(),
        )


async def run(config: Config) -> None:
    """Run until SIGTERM or SIGINT."""
    loop = asyncio.get_running_loop()
    spec = load_home(config.home)
    settings = Settings()
    started = asyncio.Event()

    def to_loop(callback: Callable[[], None]) -> None:
        """Move a callback from paho's network thread onto the event loop."""
        loop.call_soon_threadsafe(callback)

    def on_connected() -> None:
        if not started.is_set():
            started.set()
        else:
            _LOGGER.info("Reconnected; republishing bridge state and device states")
            home.zigbee.republish_states()

    transport = PahoTransport(
        config.mqtt_host,
        config.mqtt_port,
        dispatch=to_loop,
        username=config.mqtt_user,
        password=config.mqtt_password,
        will=(f"{settings.base_topic}/bridge/state", '{"state":"offline"}'),
        on_connected=on_connected,
    )
    home = SimulatedHome(
        spec,
        AsyncioClock(loop),
        transport,
        fritz_username=config.fritz_user,
        fritz_password=config.fritz_password,
        settings=settings,
    )

    runners = []
    for app, port in (
        (fritz_server.create_app(home.fritzbox), config.fritz_port),
        (control_api.create_app(home), config.control_port),
    ):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.bind, port).start()
        runners.append(runner)
    _LOGGER.info(
        "Fake FRITZ!Box on %s:%s, control API on %s:%s",
        config.bind,
        config.fritz_port,
        config.bind,
        config.control_port,
    )

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    transport.start()
    _LOGGER.info("Connecting to MQTT broker %s:%s", config.mqtt_host, config.mqtt_port)
    started_or_stopped = asyncio.ensure_future(started.wait())
    stopping = asyncio.ensure_future(stop.wait())
    await asyncio.wait({started_or_stopped, stopping}, return_when=asyncio.FIRST_COMPLETED)
    if started.is_set():
        home.start()
        _LOGGER.info(
            "Simulating %d devices in %d rooms from %s",
            len(home.devices),
            len(home.rooms),
            config.home,
        )
        await stopping
    started_or_stopped.cancel()

    home.stop()
    await asyncio.to_thread(
        transport.stop, (f"{settings.base_topic}/bridge/state", '{"state":"offline"}')
    )
    for runner in runners:
        await runner.cleanup()


def main() -> int:
    """Entry point."""
    try:
        config = Config.from_env(os.environ)
    except ConfigError as error:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error("%s", error)
        return 2
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
