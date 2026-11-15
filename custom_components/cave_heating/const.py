"""Constants for the cave_heating integration.

Durations that steer the control logic are not here: they come from the ``timing`` block and the
room settings of ``heating.yaml`` (see ``schema.py``).
"""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "cave_heating"

# The applied copy of the home description, relative to Home Assistant's configuration
# directory (concept section 4.5). The integration never reads the edited file next to it.
APPLIED_FILE_PARTS: Final = ("cave", ".applied", "heating.yaml")

# Persistent room and home state (concept section 5.5, "Start-up").
STORAGE_KEY: Final = f"{DOMAIN}.state"
STORAGE_VERSION: Final = 1
# How long changes are collected before the state file is written. Home Assistant writes
# pending changes when it stops, so nothing is lost on a clean shutdown.
STORAGE_SAVE_DELAY_SECONDS: Final = 5

# Room thermostat range (concept section 5.2).
MIN_TEMPERATURE: Final = 5.0
MAX_TEMPERATURE: Final = 30.0
TEMPERATURE_STEP: Final = 0.5

# cave_heating.hold: the duration when none is given (concept section 5.5) and the longest one
# accepted, so that a hold can never keep a thermostat outside the controller for long.
HOLD_DEFAULT_DURATION: Final = timedelta(minutes=15)
HOLD_MAX_DURATION: Final = timedelta(hours=4)

# Integrations whose thermostats are slow unless the file says otherwise (concept section 5.1).
SLOW_PLATFORMS: Final = frozenset({"fritzbox"})

# Device identifiers.
HOME_DEVICE_ID: Final = "home"
ROOM_DEVICE_PREFIX: Final = "room_"

# Services (concept section 5.2).
SERVICE_BOOST: Final = "boost"
SERVICE_RESUME_SCHEDULE: Final = "resume_schedule"
SERVICE_RELOAD: Final = "reload"
SERVICE_HOLD: Final = "hold"
SERVICE_RELEASE: Final = "release"

ATTR_DURATION: Final = "duration"
ATTR_REASON: Final = "reason"
ATTR_OVERRIDE_UNTIL: Final = "override_until"
ATTR_BOOST_UNTIL: Final = "boost_until"
ATTR_THERMOSTATS: Final = "thermostats"
ATTR_MESSAGES: Final = "messages"
