"""Message fixtures: what a device of each model publishes, stored as JSON.

At run time the Zigbee2MQTT stand-in does not compute discovery messages: it reads them from
``fixtures/<model>.json`` and fills in the device's name and addresses. The shipped fixtures are
generated from the model descriptions by :func:`generate` (``python -m cave_sim.zigbee.fixtures``);
a recording of a real device (``python -m cave_sim.zigbee.record``) has the same layout and simply
replaces the file, after which the simulator publishes exactly what the real device published.

Layout of a fixture file::

    {
      "model": "TRVZB",
      "source": "generated: ..." | "recorded: ...",
      "discovery": {"<component>/@IEEE@/<object id>/config": {<payload>}, ...},
      "state": {<a state message as Zigbee2MQTT publishes it>}
    }

Inside a fixture the device's friendly name, the object ID derived from it, its IEEE address and the
coordinator's IEEE address are replaced by the placeholders below.
"""

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cave_sim.zigbee.discovery import DeviceInfo, Settings, discover_device, serialize
from cave_sim.zigbee.models import MODELS, ZigbeeModel

FIXTURE_DIR: Final = Path(__file__).parent / "fixtures"

FRIENDLY_NAME: Final = "@FRIENDLY_NAME@"
OBJECT_ID: Final = "@OBJECT_ID@"
IEEE: Final = "@IEEE@"
COORDINATOR_IEEE: Final = "@COORDINATOR_IEEE@"

# The friendly name generated fixtures are built with. It contains a space and capitals, so the
# friendly name and the object ID derived from it never coincide and can both be found again.
CANONICAL_NAME: Final = "Fixture Device"
CANONICAL_IEEE: Final = "0x00158d0000000000"

# Keys of a state message that change all the time and are always written by the simulator.
DYNAMIC_STATE_KEYS: Final = frozenset({"last_seen", "linkquality", "update"})


def object_id_from_name(friendly_name: str) -> str:
    """Zigbee2MQTT's object ID for a friendly name: lower case, whitespace to underscores."""
    return re.sub(r"\s+", "_", friendly_name).lower()


@dataclass(frozen=True)
class Identity:
    """The values that stand behind the placeholders."""

    friendly_name: str
    ieee: str
    coordinator_ieee: str

    @property
    def object_id(self) -> str:
        """The object ID derived from the friendly name."""
        return object_id_from_name(self.friendly_name)

    def check(self) -> None:
        """Refuse a name whose object ID could not be told apart from the name itself."""
        if self.object_id == self.friendly_name:
            raise ValueError(
                f"friendly name {self.friendly_name!r} must contain a capital letter or a space"
            )


def _replace_strings(value: Any, replace: Any) -> Any:
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, dict):
        return {replace(k): _replace_strings(v, replace) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_strings(v, replace) for v in value]
    return value


def to_placeholders(value: Any, identity: Identity) -> Any:
    """Replace the identity's values in every string of ``value`` by placeholders."""
    identity.check()

    def replace(text: str) -> str:
        text = text.replace(identity.coordinator_ieee, COORDINATOR_IEEE)
        text = text.replace(identity.ieee, IEEE)
        text = text.replace(identity.friendly_name, FRIENDLY_NAME)
        return text.replace(identity.object_id, OBJECT_ID)

    return _replace_strings(value, replace)


def from_placeholders(value: Any, identity: Identity) -> Any:
    """Put the identity's values back in place of the placeholders."""

    def replace(text: str) -> str:
        return (
            text.replace(COORDINATOR_IEEE, identity.coordinator_ieee)
            .replace(IEEE, identity.ieee)
            .replace(FRIENDLY_NAME, identity.friendly_name)
            .replace(OBJECT_ID, identity.object_id)
        )

    return _replace_strings(value, replace)


@dataclass(frozen=True)
class Fixture:
    """A model's messages, with placeholders."""

    model: str
    source: str
    discovery: Mapping[str, dict[str, Any]]
    state: Mapping[str, Any]

    def render_discovery(self, identity: Identity) -> dict[str, dict[str, Any]]:
        """Discovery messages for one device, keyed by topic below the discovery prefix."""
        rendered: dict[str, dict[str, Any]] = from_placeholders(dict(self.discovery), identity)
        return rendered

    def initial_state(self) -> dict[str, Any]:
        """The state a new device starts with, without the keys the simulator always writes."""
        return {k: v for k, v in self.state.items() if k not in DYNAMIC_STATE_KEYS}

    def to_json(self) -> str:
        """The file content: indented, keys sorted, ending with a newline."""
        document = {
            "model": self.model,
            "source": self.source,
            "discovery": self.discovery,
            "state": self.state,
        }
        return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def path_for(model: str) -> Path:
    """The fixture file of ``model``."""
    return FIXTURE_DIR / f"{model}.json"


def load(model: str, directory: Path = FIXTURE_DIR) -> Fixture:
    """Read the fixture of ``model``."""
    document = json.loads((directory / f"{model}.json").read_text(encoding="utf-8"))
    return Fixture(
        model=document["model"],
        source=document["source"],
        discovery=document["discovery"],
        state=document["state"],
    )


def generate(model: ZigbeeModel, settings: Settings | None = None) -> Fixture:
    """Build the hand-made fixture of ``model`` from its description."""
    settings = settings or Settings()
    identity = Identity(CANONICAL_NAME, CANONICAL_IEEE, settings.coordinator_ieee)
    discovery = discover_device(DeviceInfo(CANONICAL_NAME, CANONICAL_IEEE, model), settings)
    # Round-trip through Zigbee2MQTT's serialisation, so the fixture holds what goes on the wire.
    wire = {topic: json.loads(serialize(payload)) for topic, payload in discovery.items()}
    return Fixture(
        model=model.model,
        source=(
            "generated: cave_sim.zigbee.models (zigbee-herdsman-converters v26.125.0) through "
            "cave_sim.zigbee.discovery (Zigbee2MQTT 2.14.3 lib/extension/homeassistant.ts)"
        ),
        discovery=to_placeholders(wire, identity),
        state=json.loads(serialize(dict(model.initial_state))),
    )


def main(argv: list[str]) -> int:
    """Write (default) or check (``--check``) the generated fixtures of every model."""
    check = "--check" in argv
    stale: list[str] = []
    for model in MODELS.values():
        path = path_for(model.model)
        if path.exists() and not load(model.model).source.startswith("generated"):
            continue  # a recording; never overwritten
        content = generate(model).to_json()
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(model.model)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        sys.stderr.write(f"stale fixtures: {', '.join(stale)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
