"""The version check of concept section 4.5, run by ``make versions`` (part of ``make check``).

Home Assistant's version appears in three places that must agree: ``homeassistant`` in
``uv.lock``, the image tag of the ``homeassistant`` service in ``compose.yaml`` and the hassfest
image tag in ``.github/workflows/ci.yml``. And the client libraries of Home Assistant's MQTT and
FRITZ! integrations, ``paho-mqtt`` and ``pyfritzhome``, must be locked at exactly the versions
those integrations ask for (the test library does not install them, so the simulator work
package pins them by hand), whenever they are locked at all.

Run it with ``PYTHONPATH=ops python -m cave_ops.versioncheck``. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

# package -> the integration whose manifest says which version it needs
CLIENT_LIBRARIES = {"paho-mqtt": "mqtt", "pyfritzhome": "fritzbox"}

_HA_IMAGE = re.compile(r"image:\s*ghcr\.io/home-assistant/home-assistant:([^\s@]+)")
_HASSFEST_IMAGE = re.compile(r"ghcr\.io/home-assistant/hassfest:([^\s@\"']+)")


def locked_versions(lock_text: str) -> dict[str, str]:
    """``{package name: version}`` from the text of a ``uv.lock``."""
    packages = tomllib.loads(lock_text).get("package", [])
    return {item["name"]: item["version"] for item in packages if "version" in item}


def _find_all(pattern: re.Pattern[str], text: str) -> list[str]:
    """Every tag the pattern finds: a second, stale occurrence must not hide behind the first."""
    return [match.group(1) for match in pattern.finditer(text)]


def installed_manifest_requirement(integration: str, library: str) -> str | None:
    """The version of ``library`` that the installed Home Assistant's ``integration`` requires."""
    try:
        manifest = metadata.distribution("homeassistant").locate_file(
            f"homeassistant/components/{integration}/manifest.json"
        )
    except metadata.PackageNotFoundError:
        return None
    path = Path(str(manifest))
    if not path.is_file():
        return None
    requirements: list[str] = json.loads(path.read_text(encoding="utf-8")).get("requirements", [])
    for requirement in requirements:
        name, _, version = requirement.partition("==")
        if name.strip().lower() == library:
            return version.strip()
    return None


def check(
    lock_text: str,
    compose_text: str,
    ci_text: str,
    requirement: Callable[[str, str], str | None] = installed_manifest_requirement,
) -> list[str]:
    """Return what is inconsistent, one line each; empty means all places agree."""
    problems: list[str] = []
    locked = locked_versions(lock_text)

    ha = locked.get("homeassistant")
    if ha is None:
        problems.append("uv.lock: homeassistant is not locked")
    else:
        for place, pattern, text in (
            ("compose.yaml image tag", _HA_IMAGE, compose_text),
            ("ci.yml hassfest image tag", _HASSFEST_IMAGE, ci_text),
        ):
            tags = _find_all(pattern, text)
            if not tags:
                problems.append(f"{place}: not found")
            problems.extend(
                f"{place} is {tag}, but uv.lock locks homeassistant {ha}"
                for tag in tags
                if tag != ha
            )

    for library, integration in CLIENT_LIBRARIES.items():
        if library not in locked:
            continue
        wanted = requirement(integration, library)
        if wanted is None:
            problems.append(
                f"{library} is locked at {locked[library]}, but the version Home Assistant's "
                f"{integration} integration asks for cannot be read (is homeassistant installed?)"
            )
        elif wanted != locked[library]:
            problems.append(
                f"{library} is locked at {locked[library]}, but Home Assistant's {integration} "
                f"integration asks for exactly {wanted}"
            )
    return problems


def check_repository(root: Path) -> list[str]:
    """``check`` applied to the files of the checkout at ``root``."""
    return check(
        (root / "uv.lock").read_text(encoding="utf-8"),
        (root / "compose.yaml").read_text(encoding="utf-8"),
        (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
    )


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    problems = check_repository(root)
    for problem in problems:
        print(f"version check: {problem}", file=sys.stderr)
    if not problems:
        print("version check: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
