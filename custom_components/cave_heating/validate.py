"""Validate a heating.yaml without a running Home Assistant (concept section 4.5).

Usage, with Home Assistant's configuration directory as the working directory::

    python -m custom_components.cave_heating.validate cave/heating.yaml

Exit status 0 means the file is valid. 1 means it is not, or cannot be read; every problem is
written to standard error, one line each, naming the room and the field. 2 means the command was
called wrongly. The rules are the ones the integration applies when it loads the applied copy.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from .schema import ConfigError, load_file

EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

USAGE = "usage: python -m custom_components.cave_heating.validate <heating.yaml>\n"


def main(argv: Sequence[str]) -> int:
    """Validate the file named in ``argv`` and return the exit status."""
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    path = Path(argv[0])
    try:
        config = load_file(path)
    except ConfigError as err:
        sys.stderr.write(f"{path} is not valid:\n")
        for error in err.errors:
            sys.stderr.write(f"  {error}\n")
        return EXIT_INVALID
    thermostats = sum(len(room.trvs) for room in config.rooms)
    sys.stdout.write(
        f"{path} is valid: version {config.version}, {len(config.rooms)} rooms, "
        f"{thermostats} thermostats.\n"
    )
    return EXIT_VALID


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
