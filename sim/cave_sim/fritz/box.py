"""A fake FRITZ!Box with FRITZ!Smart Thermo 302 thermostats.

What it implements, and where it comes from:

* The session login of ``login_sid.lua`` with the PBKDF2 challenge-response of FRITZ!OS 7.24 and
  later, and the MD5 fallback (FRITZ! "HTTP_Session-ID_EN.pdf", 2021, as linked from
  fritz.com/en/pages/interfaces). A session is valid for 20 minutes and is extended by every use.
* The commands of ``webservices/homeautoswitch.lua`` that ``pyfritzhome`` 0.6.21 and Home
  Assistant 2026.10.4's ``fritzbox`` integration send, with the XML of ``getdevicelistinfos`` for a
  radiator thermostat (FRITZ! "AHA-HTTP-Interface.pdf", 05.08.25, sections 3 and 3.2; HTTP status
  codes from its section 4).

The box knows the thermostats; each thermostat regulates its radiator on its own sensor. A new
setpoint is reported either at once or after ``report_delay`` seconds, because which of the two a
real box does is not confirmed (concept section 12). While the box's summer or holiday mode or the
key lock "via UI/API" is active, the box reports ``lock`` as set (the AHA document: the lock "wird
automatisch bei summeractive==1 oder holidayactive==1 aktiviert") and refuses ``sethkrtsoll``.
"""

import hashlib
import hmac
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Final
from xml.sax.saxutils import escape

from cave_sim.clock import Clock, Timer
from cave_sim.home import DeviceSpec
from cave_sim.room import PROPORTIONAL_BAND, RadiatorBias, Room

_LOGGER = logging.getLogger(__name__)

INVALID_SID: Final = "0000000000000000"
SESSION_LIFETIME: Final = 20 * 60.0
PBKDF2_ITERATIONS: Final = (10_000, 2_000)  # the example in HTTP_Session-ID_EN.pdf
# AHA "hkr" encoding: half degrees; 253 is OFF and 254 is ON.
HKR_OFF: Final = 253
HKR_ON: Final = 254
HKR_MIN: Final = 16
HKR_MAX: Final = 56
# Assumption: "frost protection" of a thermostat set to OFF.
FROST_PROTECTION: Final = 5.0


@dataclass(frozen=True)
class Response:
    """An HTTP response of the box."""

    status: int
    body: str
    content_type: str = "text/plain"


def pbkdf2_response(challenge: str, password: str) -> str:
    """The response to a ``2$<iter1>$<salt1>$<iter2>$<salt2>`` challenge.

    As in HTTP_Session-ID_EN.pdf.
    """
    _, iter1, salt1, iter2, salt2 = challenge.split("$")
    hash1 = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt1), int(iter1))
    hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iter2))
    return f"{salt2}${hash2.hex()}"


def md5_response(challenge: str, password: str) -> str:
    """The legacy response: MD5 of UTF-16LE ``<challenge>-<password>``, non-Latin-1 as ``.``."""
    text = "".join(ch if ord(ch) <= 255 else "." for ch in f"{challenge}-{password}")
    digest = hashlib.md5(text.encode("utf-16-le"), usedforsecurity=False).hexdigest()
    return f"{challenge}-{digest}"


def temperature_to_hkr(value: float) -> int:
    """A temperature in °C as the AHA half-degree value, clamped to 16..56."""
    return min(max(round(value * 2), HKR_MIN), HKR_MAX)


@dataclass
class FritzThermostat:
    """One FRITZ!Smart Thermo 302 as the box sees it."""

    spec: DeviceSpec
    room: Room
    internal_id: int
    tsoll: int = 40  # what the box reports
    device_tsoll: int = 40  # what the thermostat regulates to
    komfort: int = 42
    absenk: int = 32
    present: bool = True
    battery: int = 80
    battery_low: bool = False
    lock: bool = False
    device_lock: bool = False
    window_open_end: int = 0
    boost_end: int = 0
    opening: float = 0.0
    radiator: RadiatorBias = field(default_factory=lambda: RadiatorBias(0.0))
    fw_version: str = "05.16"
    product_name: str = "FRITZ!Smart Thermo 302"
    pending: list[Timer] = field(default_factory=list)

    @property
    def ain(self) -> str:
        """The actor identification number."""
        return self.spec.ain_or_default

    @property
    def device_id(self) -> str:
        """The control-API ID."""
        return self.spec.device_id

    def measured(self) -> float:
        """What the thermostat's own sensor reads, next to the radiator."""
        return self.room.temperature + self.radiator.bias

    def regulate(self, now: float) -> None:
        """Set the valve from the device's setpoint; the own sensor regulates (as the real one)."""
        self.radiator.update(now, self.opening)
        temperature = self.measured()
        if self.boost_end > now or self.device_tsoll == HKR_ON:
            self.opening = 1.0
        elif self.window_open_end > now or self.device_tsoll == HKR_OFF:
            self.opening = 1.0 if temperature < FROST_PROTECTION else 0.0
        else:
            target = self.device_tsoll / 2
            self.opening = min(max((target - temperature) / PROPORTIONAL_BAND, 0.0), 1.0)


@dataclass
class Session:
    """A logged-in session."""

    user: str
    last_used: float


class FakeFritzBox:
    """The box: login, sessions, thermostats and the AHA commands."""

    def __init__(
        self,
        clock: Clock,
        username: str,
        password: str,
        *,
        report_delay: float = 0.0,
    ) -> None:
        self.clock = clock
        self.username = username
        self.password = password
        self.report_delay = report_delay
        self.summer_active = False
        self.holiday_active = False
        # Assumption: the box marks a thermostat busy while a setpoint waits for the device. Off by
        # default, because pyfritzhome then polls with real sleeps (wait_device_txbusy).
        self.txbusy_while_pending = False
        self.thermostats: dict[str, FritzThermostat] = {}
        self.sessions: dict[str, Session] = {}
        # The most recent requests, for tests and the control API.
        self.requests: deque[tuple[str, dict[str, str]]] = deque(maxlen=1000)
        self._salt1 = secrets.token_hex(4)
        self._challenge = self._new_challenge()

    # --- devices ----------------------------------------------------------------------------

    def add_thermostat(self, spec: DeviceSpec, room: Room) -> FritzThermostat:
        """Register a thermostat in ``room``."""
        thermostat = FritzThermostat(spec, room, internal_id=16 + len(self.thermostats))
        thermostat.radiator = RadiatorBias(self.clock.now())
        self.thermostats[thermostat.ain] = thermostat
        room.valve_openings.append(lambda: thermostat.opening)
        return thermostat

    def tick(self) -> None:
        """Let every thermostat regulate."""
        now = self.clock.now()
        for thermostat in self.thermostats.values():
            thermostat.regulate(now)

    def locked(self, thermostat: FritzThermostat) -> bool:
        """The lock as reported: set by hand or by an active summer or holiday mode."""
        return thermostat.lock or self.summer_active or self.holiday_active

    def set_target(self, thermostat: FritzThermostat, value: int) -> None:
        """Deliver a setpoint, at once or after the reporting delay."""

        def deliver() -> None:
            thermostat.device_tsoll = value
            thermostat.tsoll = value
            thermostat.pending = [timer for timer in thermostat.pending if timer.running]
            thermostat.regulate(self.clock.now())

        if self.report_delay <= 0:
            deliver()
            return
        timer = Timer(self.clock, deliver)
        timer.start(self.report_delay)
        thermostat.pending.append(timer)

    def dial(self, thermostat: FritzThermostat, value: int) -> None:
        """A change on the device itself; the box learns it with the device's next report."""
        thermostat.device_tsoll = value
        thermostat.tsoll = value
        thermostat.regulate(self.clock.now())

    # --- login ------------------------------------------------------------------------------

    def _new_challenge(self) -> str:
        iter1, iter2 = PBKDF2_ITERATIONS
        return f"2${iter1}${self._salt1}${iter2}${secrets.token_hex(4)}"

    def _valid_session(self, sid: str | None) -> Session | None:
        if not sid or sid == INVALID_SID:
            return None
        session = self.sessions.get(sid)
        now = self.clock.now()
        if session is None or now - session.last_used > SESSION_LIFETIME:
            self.sessions.pop(sid, None)
            return None
        session.last_used = now
        return session

    def _check_response(self, response: str, version2: bool) -> bool:
        if version2 and self._challenge.startswith("2$"):
            expected = pbkdf2_response(self._challenge, self.password)
        else:
            challenge = self._challenge.split("$")[-1]
            expected = md5_response(challenge, self.password)
        return hmac.compare_digest(response, expected)

    def login(self, params: dict[str, str]) -> Response:
        """``login_sid.lua``: log in, check a session, or log out."""
        self.requests.append(("login_sid.lua", dict(params)))
        version2 = params.get("version") == "2"
        sid = params.get("sid")
        if "logout" in params or "security:command/logout" in params:
            self.sessions.pop(sid or "", None)
            return self._session_info(INVALID_SID, version2)
        if "response" in params:
            user = params.get("username", "")
            ok = user == self.username and self._check_response(params["response"], version2)
            self._challenge = self._new_challenge()
            if not ok:
                _LOGGER.warning("Login failed for user %r", user)
                return self._session_info(INVALID_SID, version2)
            new_sid = secrets.token_hex(8)
            self.sessions[new_sid] = Session(user, self.clock.now())
            return self._session_info(new_sid, version2)
        session = self._valid_session(sid)
        return self._session_info(sid if session and sid else INVALID_SID, version2)

    def _session_info(self, sid: str, version2: bool) -> Response:
        challenge = self._challenge if version2 else self._challenge.split("$")[-1]
        rights = (
            "<Rights><Name>HomeAuto</Name><Access>2</Access></Rights>"
            if sid != INVALID_SID
            else "<Rights/>"
        )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f"<SessionInfo><SID>{sid}</SID><Challenge>{challenge}</Challenge>"
            f'<BlockTime>0</BlockTime><Users><User last="1">{escape(self.username)}</User></Users>'
            f"{rights}</SessionInfo>"
        )
        return Response(200, body, "text/xml")

    # --- home automation commands -----------------------------------------------------------

    def homeautoswitch(self, params: dict[str, str]) -> Response:
        """``webservices/homeautoswitch.lua``."""
        self.requests.append(("homeautoswitch.lua", dict(params)))
        if self._valid_session(params.get("sid")) is None:
            return Response(403, "403 Forbidden")
        command = params.get("switchcmd", "")
        ain = params.get("ain")
        if command == "getdevicelistinfos":
            return self._xml(self.device_list_xml())
        if command == "gettemplatelistinfos":
            return self._xml('<templatelist version="1"></templatelist>')
        if command == "gettriggerlistinfos":
            return self._xml('<triggerlist version="1"></triggerlist>')
        if command == "getswitchlist":
            return Response(200, "\n")
        thermostat = self.thermostats.get(ain or "")
        if thermostat is None:
            return Response(400, "400 Bad Request")
        return self._device_command(command, thermostat, params)

    def _device_command(
        self, command: str, thermostat: FritzThermostat, params: dict[str, str]
    ) -> Response:
        present = thermostat.present
        if command == "getdeviceinfos":
            return self._xml(self.device_xml(thermostat))
        if command == "getswitchpresent":
            return Response(200, "1\n" if present else "0\n")
        if command == "getswitchname":
            return Response(200, f"{thermostat.spec.name}\n")
        if command == "gettemperature":
            celsius = round(thermostat.measured() * 10)
            return Response(200, f"{celsius}\n" if present else "inval\n")
        if command == "gethkrtsoll":
            return Response(200, f"{thermostat.tsoll}\n" if present else "inval\n")
        if command == "gethkrkomfort":
            return Response(200, f"{thermostat.komfort}\n")
        if command == "gethkrabsenk":
            return Response(200, f"{thermostat.absenk}\n")
        if command == "sethkrtsoll":
            return self._sethkrtsoll(thermostat, params.get("param", ""))
        if command in ("sethkrboost", "sethkrwindowopen"):
            return self._set_endtime(command, thermostat, params.get("endtimestamp", ""))
        return Response(400, "400 Bad Request")

    def _sethkrtsoll(self, thermostat: FritzThermostat, param: str) -> Response:
        try:
            value = int(param)
        except ValueError:
            return Response(400, "400 Bad Request")
        if not (HKR_MIN <= value <= HKR_MAX or value in (HKR_OFF, HKR_ON)):
            return Response(400, "400 Bad Request")
        if self.locked(thermostat):
            # Assumption: the box answers a locked thermostat like an invalid request.
            _LOGGER.info("sethkrtsoll refused for %s: locked", thermostat.ain)
            return Response(400, "400 Bad Request")
        self.set_target(thermostat, value)
        return Response(200, "\n")

    def _set_endtime(self, command: str, thermostat: FritzThermostat, raw: str) -> Response:
        try:
            end = int(raw)
        except ValueError:
            return Response(400, "400 Bad Request")
        now = self.clock.now()
        if end != 0 and not now < end <= now + 24 * 3600:
            return Response(400, "400 Bad Request")
        if command == "sethkrboost":
            thermostat.boost_end = end
        else:
            thermostat.window_open_end = end
        thermostat.regulate(now)
        return Response(200, f"{end}\n")

    @staticmethod
    def _xml(body: str) -> Response:
        return Response(200, '<?xml version="1.0" encoding="utf-8"?>' + body, "text/xml")

    # --- XML --------------------------------------------------------------------------------

    def device_list_xml(self) -> str:
        """``getdevicelistinfos``."""
        devices = "".join(self.device_xml(t) for t in self.thermostats.values())
        return f'<devicelist version="1" fwversion="8.10">{devices}</devicelist>'

    def device_xml(self, thermostat: FritzThermostat) -> str:
        """One ``<device>`` element as described in AHA-HTTP-Interface.pdf section 3.2."""
        now = self.clock.now()
        present = thermostat.present
        pending = any(timer.running for timer in thermostat.pending)
        txbusy = int(pending and self.txbusy_while_pending)

        def value(number: int) -> str:
            return str(number) if present else ""

        temperature = thermostat.measured()
        window_active = thermostat.window_open_end > now
        boost_active = thermostat.boost_end > now
        hkr = (
            "<hkr>"
            f"<tist>{value(round(temperature * 2))}</tist>"
            f"<tsoll>{value(thermostat.tsoll)}</tsoll>"
            f"<absenk>{thermostat.absenk}</absenk>"
            f"<komfort>{thermostat.komfort}</komfort>"
            f"<lock>{int(self.locked(thermostat))}</lock>"
            f"<devicelock>{int(thermostat.device_lock)}</devicelock>"
            "<errorcode>0</errorcode>"
            f"<windowopenactiv>{int(window_active)}</windowopenactiv>"
            f"<windowopenactiveendtime>{thermostat.window_open_end if window_active else 0}"
            "</windowopenactiveendtime>"
            f"<boostactive>{int(boost_active)}</boostactive>"
            f"<boostactiveendtime>{thermostat.boost_end if boost_active else 0}"
            "</boostactiveendtime>"
            f"<batterylow>{int(thermostat.battery_low)}</batterylow>"
            f"<battery>{thermostat.battery}</battery>"
            "<nextchange><endperiod>0</endperiod><tchange>255</tchange></nextchange>"
            f"<summeractive>{int(self.summer_active)}</summeractive>"
            f"<holidayactive>{int(self.holiday_active)}</holidayactive>"
            "<adaptiveHeatingActive>0</adaptiveHeatingActive>"
            "<adaptiveHeatingRunning>0</adaptiveHeatingRunning>"
            "</hkr>"
        )
        return (
            f'<device identifier="{escape(thermostat.ain)}" id="{thermostat.internal_id}" '
            f'functionbitmask="320" fwversion="{thermostat.fw_version}" manufacturer="AVM" '
            f'productname="{escape(thermostat.product_name)}">'
            f"<present>{int(present)}</present><txbusy>{txbusy}</txbusy>"
            f"<name>{escape(thermostat.spec.name)}</name>"
            f"<battery>{thermostat.battery}</battery>"
            f"<batterylow>{int(thermostat.battery_low)}</batterylow>"
            f"<temperature><celsius>{value(round(temperature * 10))}</celsius>"
            "<offset>0</offset></temperature>"
            f"{hkr}</device>"
        )
