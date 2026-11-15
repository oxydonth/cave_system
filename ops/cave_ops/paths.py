"""Where everything is: the checkout (code) and ``CAVE_DATA`` (state).

The layout is the contract of concept sections 4.2 and 4.5. ``/config`` in the Home Assistant
container is ``CAVE_DATA/homeassistant``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SERVER_DATA_DIR = Path("/var/lib/cave")

# Mount points inside the Home Assistant container (concept section 4.5).
CONTAINER_CONFIG = "/config"
CONTAINER_TOOLS = "/cave/tools"


def default_data_dir(root: Path, environ: Mapping[str, str]) -> Path:
    """Where ``CAVE_DATA`` points when ``.env`` does not say yet.

    The environment variable ``CAVE_DATA`` wins. Otherwise ``/var/lib/cave`` when it exists and
    can be written (a host prepared by ``scripts/provision-host.sh``), else ``data`` inside the
    checkout (development). The result is always absolute, because Compose resolves relative
    bind-mount paths against the project directory and that is easy to get wrong.
    """
    explicit = environ.get("CAVE_DATA")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if SERVER_DATA_DIR.is_dir() and os.access(SERVER_DATA_DIR, os.W_OK | os.X_OK):
        return SERVER_DATA_DIR
    return (root / "data").resolve()


@dataclass(frozen=True)
class Paths:
    """Every path the commands use, derived from the checkout and the state directory."""

    root: Path
    data: Path

    # --- code: the checkout, mounted read-only into the containers
    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def compose_file(self) -> Path:
        return self.root / "compose.yaml"

    @property
    def heating_example(self) -> Path:
        return self.root / "homeassistant" / "heating.example.yaml"

    @property
    def z2m_template(self) -> Path:
        return self.root / "zigbee2mqtt" / "configuration.template.yaml"

    # --- state: Home Assistant's /config
    @property
    def ha_config(self) -> Path:
        """The host side of ``/config``."""
        return self.data / "homeassistant"

    @property
    def heating(self) -> Path:
        """``cave/heating.yaml``: the description of the home, as edited."""
        return self.ha_config / "cave" / "heating.yaml"

    @property
    def heating_applied(self) -> Path:
        """``cave/.applied/heating.yaml``: the last version that passed validation."""
        return self.ha_config / "cave" / ".applied" / "heating.yaml"

    @property
    def dashboard(self) -> Path:
        """``dashboards/heating.yaml``: generated from the applied copy."""
        return self.ha_config / "dashboards" / "heating.yaml"

    @property
    def token(self) -> Path:
        """The script token (concept section 4.5)."""
        return self.ha_config / ".cave" / "token"

    # --- state of the other services
    @property
    def z2m_data(self) -> Path:
        return self.data / "zigbee2mqtt"

    @property
    def z2m_config(self) -> Path:
        return self.z2m_data / "configuration.yaml"

    @property
    def mosquitto_data(self) -> Path:
        return self.data / "mosquitto"

    @property
    def mosquitto_passwd(self) -> Path:
        """The broker's password file, inside its data directory (see compose.yaml)."""
        return self.mosquitto_data / "passwd"

    @property
    def mqtt_stamp(self) -> Path:
        """Records which broker login ``passwd`` was written for (see ``init``)."""
        return self.data / ".cave" / "mqtt-credentials"

    @property
    def lock_file(self) -> Path:
        return self.data / ".cave" / "lock"

    def container_path(self, host_path: Path) -> str | None:
        """The path of a file inside the Home Assistant container, if it is under ``/config``."""
        try:
            relative = host_path.resolve().relative_to(self.ha_config.resolve())
        except ValueError:
            return None
        return f"{CONTAINER_CONFIG}/{relative.as_posix()}"
