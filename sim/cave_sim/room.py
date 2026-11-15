"""A simple thermal model of a room.

The room temperature ``T`` follows

    dT/dt = heating * sum(openings) - (loss + window_loss * open_windows) * (T - T_out)

with rates per hour. Between two steps the valve openings and windows are held constant, which makes
the equation linear, so each step is integrated exactly rather than with Euler steps: the result
does not depend on the step length. Humidity is not modelled; it stays where it was set.

This is deliberately crude. It exists so that temperatures move in the right direction and at a
plausible speed when a valve opens or a window is opened, not to predict a real flat.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

# Assumption: a thermostat's own sensor sits next to the radiator and, once the radiator is warm,
# reads this much above the room per unit of valve opening; it gets there with this time constant.
RADIATOR_BIAS: Final = 2.0
RADIATOR_TIME_CONSTANT: Final = 600.0
# Assumption: a proportional valve that is fully open 1 K below the setpoint.
PROPORTIONAL_BAND: Final = 1.0


@dataclass
class RoomParameters:
    """Rates per hour; the defaults give a room that settles at T_out + 20 K with one open valve."""

    heating_per_hour: float = 2.0
    loss_per_hour: float = 0.1
    window_loss_per_hour: float = 2.0


@dataclass
class Room:
    """One room: its air temperature and humidity, its valves and its windows."""

    room_id: str
    name: str
    temperature: float
    humidity: float
    parameters: RoomParameters = field(default_factory=RoomParameters)
    valve_openings: list[Callable[[], float]] = field(default_factory=list)
    window_states: list[Callable[[], bool]] = field(default_factory=list)

    def open_windows(self) -> int:
        """How many of the room's windows are open."""
        return sum(1 for is_open in self.window_states if is_open())

    def heating_input(self) -> float:
        """The sum of the valve openings, each between 0 and 1."""
        return sum(min(max(opening(), 0.0), 1.0) for opening in self.valve_openings)

    def step(self, seconds: float, outdoor: float) -> None:
        """Advance the temperature by ``seconds`` with the current valves and windows."""
        hours = seconds / 3600.0
        params = self.parameters
        heating = params.heating_per_hour * self.heating_input()
        loss = params.loss_per_hour + params.window_loss_per_hour * self.open_windows()
        if loss <= 0:
            self.temperature += heating * hours
            return
        equilibrium = outdoor + heating / loss
        self.temperature = equilibrium + (self.temperature - equilibrium) * math.exp(-loss * hours)


class RadiatorBias:
    """How much a sensor next to the radiator reads above the room; follows the valve with a lag."""

    def __init__(self, now: float) -> None:
        self.bias = 0.0
        self._updated = now

    def update(self, now: float, opening: float) -> None:
        """Move towards ``RADIATOR_BIAS * opening`` for the time since the last update."""
        elapsed = max(now - self._updated, 0.0)
        self._updated = now
        target = RADIATOR_BIAS * opening
        self.bias += (target - self.bias) * (1 - math.exp(-elapsed / RADIATOR_TIME_CONSTANT))
