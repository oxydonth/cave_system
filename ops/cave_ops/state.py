"""What a state directory holds before Home Assistant first starts (concept section 4.2).

Shared by ``init`` (the home's state) and ``demo`` (the demo's), so the two cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from .paths import Paths

SECRETS_YAML = "# Home Assistant secrets, for `!secret` in the configuration. Empty on purpose.\n"
PLACEHOLDER_DASHBOARD = """\
# Placeholder until `./cave apply` generates the heating dashboard from cave/heating.yaml.
title: Heizung
views:
  - title: Heizung
    cards:
      - type: markdown
        content: >-
          No rooms yet. Describe them in `cave/heating.yaml` and run `./cave apply`.
"""

# Files Home Assistant includes but does not create (concept section 4.2).
HA_FILES = {
    "secrets.yaml": SECRETS_YAML,
    "automations.yaml": "[]\n",
    "scripts.yaml": "{}\n",
    "scenes.yaml": "[]\n",
}

# Mount points of the read-only code mounts (compose.yaml). Created by `cave` so that they belong
# to the user who runs it rather than to root, which Docker would otherwise create them as.
MOUNT_DIRS = ("packages", "custom_components/cave_heating")


def home_assistant_directories(paths: Paths) -> tuple[Path, ...]:
    """Home Assistant's directories in a state directory, in the order they are created."""
    return (
        paths.ha_config,
        paths.ha_config / "cave",
        paths.ha_config / "cave" / ".applied",
        paths.ha_config / "dashboards",
    )
