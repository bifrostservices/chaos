#!/usr/bin/env python3
# Run with:
#   uv run python chaos.py

# CHAOS: CHarging Automatically On Solar
# Copyright (c) 2026 Erik Jacobsen

# MIT License
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import asyncio
import collections
import contextlib
import errno
import itertools
import json
import logging
import pathlib
import re
import signal
import socket
import sys
import threading
import time
import httpx
from urllib.parse import urlparse, parse_qs, urlencode
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any, Sequence

import pypowerwall
from pypowerwall.local.exceptions import LoginError, PowerwallConnectionError
from lucidmotors import LucidAPI, ChargeState  # type: ignore[attr-defined]
from lucidmotors.exceptions import APIError
import fastapi
import grpc
import pygal
import teslapy
import uvicorn
from starlette.middleware.trustedhost import TrustedHostMiddleware

__version__ = "0.8.0"

# Directory for runtime-generated files (chaos.log, long-history JSON) so a single
# Docker bind mount persists all of them across container rebuilds.
_DATA_DIR = pathlib.Path("data")
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Tesla OAuth token cache (teslapy). Holds bearer tokens with full account access —
# lives in the data dir so the existing Docker bind mount persists it.
_TESLA_CACHE_FILE = _DATA_DIR / "tesla_token_cache.json"
_TESLA_POLL_INTERVAL_S_DEFAULT = 300
_TESLA_CHARGING_POLL_INTERVAL_S = 60   # faster cadence while a Tesla is charging (car is awake — no sleep risk)
_TESLA_POLL_JITTER_S = 5               # due-time tolerance so a cycle on the nominal boundary never skips the poll
_TESLA_CHARGING_GRACE_POLLS = 3        # transient non-online reports tolerated while charging (cars don't sleep mid-charge)
_TESLA_ATTR_WINDOW_S = 150             # max Tesla-data age vs site poll for attribution (tolerates one glitched poll)
_TESLA_ATTR_HOME_MARGIN_KW = 0.5       # home load must exceed Tesla charging by at least this baseline
_TESLA_ATTR_SURPLUS_BAND_KW = 0.5      # |surplus| at most this — the charge-on-solar signature
_MILES_TO_KM = 1.60934

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_DATA_DIR / "chaos.log"),
    ],
)
# The pypowerwall logger is set to DEBUG below so its swallowed failure reasons
# can be captured; keep those DEBUG records out of stdout and chaos.log.
for _handler in logging.getLogger().handlers:
    _handler.setLevel(logging.INFO)
log = logging.getLogger(__name__)


class _PypowerwallErrorCapture(logging.Handler):
    """Collects pypowerwall's failure messages for the worker thread currently
    reading a Powerwall. pypowerwall logs network failures (timeouts, connection
    errors, rate-limit cooldown) only at DEBUG and then returns None, so without
    this the reason never reaches our log. Records are keyed by thread so
    concurrent site polls don't mix their messages."""

    _INTERESTING = re.compile(r"error|fail|timeout|unable|rate limit|expired|denied", re.IGNORECASE)

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.active: dict[int, list[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        messages = self.active.get(record.thread)
        if messages is None:
            return
        message = record.getMessage()
        if record.levelno >= logging.WARNING or self._INTERESTING.search(message):
            messages.append(message)

    def runCapturing(self, fn):
        """Call fn in the current thread; return (fn's result, last captured message or None)."""
        threadId = threading.get_ident()
        messages: list[str] = []
        self.active[threadId] = messages
        try:
            return fn(), (messages[-1] if messages else None)
        finally:
            self.active.pop(threadId, None)


_pypowerwallCapture = _PypowerwallErrorCapture()
logging.getLogger("pypowerwall").setLevel(logging.DEBUG)
logging.getLogger("pypowerwall").addHandler(_pypowerwallCapture)


class ChargingMode(str, Enum):
    ENABLED = "enabled"    # normal automatic control
    MANUAL = "manual"      # CHAOS monitors but never issues charging commands
    DISABLED = "disabled"  # forces STOP, blocks new sessions


# Cable connected but not actively charging — safe to call start_charging()
CHARGEABLE_STATES = {
    ChargeState.CHARGE_STATE_CABLE_CONNECTED,
    ChargeState.CHARGE_STATE_AUTHORIZED,
    ChargeState.CHARGE_STATE_CHARGING_STOPPED,
    ChargeState.CHARGE_STATE_CHARGING_PAUSED,
    ChargeState.CHARGE_STATE_CHARGING_END_OK,
}

# Actively charging or in the process of starting
CHARGING_STATES = {
    ChargeState.CHARGE_STATE_CHARGING,
    ChargeState.CHARGE_STATE_ESTABLISHING_SESSION,
    ChargeState.CHARGE_STATE_CHARGER_PREPARATION,
    ChargeState.CHARGE_STATE_AUTHORIZING_PNC,
    ChargeState.CHARGE_STATE_AUTHORIZING_EXTERNAL,
}
# Display-name forms of CHARGING_STATES, for checks against _dashboard.chargeStateName
_CHARGING_STATE_NAMES = {ChargeState.Name(s) for s in CHARGING_STATES}

def _isUnplugged(chargeState: int) -> bool:
    """True when the EV cable is not connected at all (not in any plugged-in state)."""
    return chargeState not in CHARGEABLE_STATES and chargeState not in CHARGING_STATES


def _isTransientLucidError(exc: Exception) -> bool:
    """True for transient gRPC errors (UNAVAILABLE, INTERNAL, RESOURCE_EXHAUSTED) that are safe to retry."""
    return isinstance(exc, APIError) and exc.code in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    )


# Poll-interval multipliers (applied as a factor on pollingIntervalSeconds)
_INTERVAL_DARK = 10           # No solar production — check infrequently
_INTERVAL_PW_LOW = 5          # Powerwall SoC too low — wait for recharge
_INTERVAL_EV_DONE = 60        # EV at target — rarely changes while idle
_INTERVAL_AFTER_COMMAND = 3   # Let vehicle settle after a start/stop command

# Lucid API retry settings for transient errors
_LUCID_RETRY_ATTEMPTS = 3
_LUCID_RETRY_DELAY_SECS = 3        # delay between retries for UNAVAILABLE/INTERNAL
_LUCID_RATE_LIMIT_DELAY_SECS = 60  # longer backoff for RESOURCE_EXHAUSTED (rate limit)
_CONNECT_TIMEOUT = 15.0       # Seconds before a Powerwall connection attempt times out
# Solar=0 with a healthy rolling average signals a transient TEDAPI read error (e.g. timeout)
_SOLAR_ZERO_SUSPECT_W = 200   # Minimum rolling avg (W) that makes a 0W solar reading suspect
_PW_POLL_RETRY_ATTEMPTS = 2   # Retries after a 0W solar transient before giving up
_PW_POLL_RETRY_DELAY_S = 2.0  # Seconds between retries
_SITE_UNREACHABLE_NOTIFY_CYCLES = 5  # Consecutive failed polls (exceptions or all-zero readings) before firing a webhook
_SITE_RECONNECT_INTERVAL_S = 300  # Min seconds between reconnect attempts for a site that failed to connect at startup
_PROBE_TIMEOUT_S = 5.0  # Per-layer timeout for the TCP/HTTPS reachability probe run after a failed site poll
_BOTH_FULL_RESET_MARGIN_PCT = 5   # EV or PW must drop this far below its threshold to re-arm the both-full webhook
_PW_POLLING_INTERVAL_S = 60   # Powerwall poll cadence (seconds); dark → ×_INTERVAL_DARK
# After a STOP command, the Lucid API may lag a few cycles before reflecting the stopped state.
# This window suppresses external-session detection and command re-issue during that lag period.
# It is intentionally much shorter than commandCooldownMinutes so the EV card is not hidden for
# the entire decision cooldown if the vehicle keeps/resumes charging.
_STOP_LAG_SECS = 120          # Max seconds of API lag expected after a STOP command
_WEBHOOK_RETRY_DELAY_SECS = 5  # seconds to wait before retrying a webhook on 429
# Auto site-switch detection: when the EV is confirmed charging but the active site's
# home load doesn't reflect the expected EV draw, CHAOS suspects the EV is on a different
# site. Detection requires the mismatch to persist for _AUTO_SWITCH_CYCLES_REQUIRED
# consecutive cycles to avoid reacting to transient load spikes or one-cycle ADJUST lag.
_AUTO_SWITCH_CYCLES_REQUIRED = 3    # consecutive mismatch cycles before switching
_AUTO_SWITCH_LOAD_RATIO = 0.5       # fraction of expectedEvPower used as load threshold

_HISTORY_MAXLEN = 120  # number of recent cycles to keep in history (for display and debugging)
_LONG_HISTORY_INTERVAL_SECS = 300  # record one long-history entry per 5 minutes
_LONG_HISTORY_MAXLEN = 288         # 24 hours at 5-minute resolution

async def _lucidCallWithRetry(coro_fn, prefix: str = "") -> Any:
    """Call an async Lucid API function with retry on transient errors."""
    pfx = f"{prefix} " if prefix else ""
    for attempt in range(_LUCID_RETRY_ATTEMPTS):
        try:
            return await coro_fn()
        except APIError as e:
            if _isTransientLucidError(e) and attempt < _LUCID_RETRY_ATTEMPTS - 1:
                delay = (
                    _LUCID_RATE_LIMIT_DELAY_SECS
                    if e.code == grpc.StatusCode.RESOURCE_EXHAUSTED
                    else _LUCID_RETRY_DELAY_SECS
                )
                log.warning(
                    f"[Lucid] {pfx}Transient API error ({e}), "
                    f"retrying in {delay}s "
                    f"(attempt {attempt + 1}/{_LUCID_RETRY_ATTEMPTS})..."
                )
                await asyncio.sleep(delay)
            else:
                raise


async def _lucidLoginWithRetry(lucid, lucidConfig: dict) -> None:
    """Log in to the Lucid API with retries for transient server errors."""
    await _lucidCallWithRetry(lambda: lucid.login(lucidConfig["username"], lucidConfig["password"]))


def loadConfig(path="config.json"):
    with open(path) as f:
        return json.load(f)


def _requireStringList(value, message: str) -> None:
    """Raise RuntimeError(message) unless value is a list of non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value):
        raise RuntimeError(message)


def _validateConfig(config: dict) -> None:
    """Validate required config keys and fail fast with a clear error message."""
    def require(obj, keys, context):
        for k in keys:
            if k not in obj:
                raise RuntimeError(f"Missing required config key '{k}' in {context}")

    require(config, ["activePowerwall", "powerwalls", "lucid", "charging", "pollingIntervalSeconds"], "config root")

    activeName = config["activePowerwall"]
    pwEntry = next((pw for pw in config["powerwalls"] if pw["name"] == activeName), None)
    if pwEntry is None:
        raise RuntimeError(f"No Powerwall named '{activeName}' found in config['powerwalls']")

    # Validate ALL Powerwall entries so misconfigured non-active sites fail at startup
    # rather than crashing when the user tries to switch to them.
    for pw in config["powerwalls"]:
        entryName = pw.get("name", "<unnamed>")
        require(pw, ["name", "host", "password", "email", "timezone", "authType"], f"powerwalls['{entryName}']")
        authType = pw["authType"]
        if authType not in ("local", "TEDAPI"):
            raise RuntimeError(
                f"Invalid authType '{authType}' in powerwalls['{entryName}']: must be 'local' or 'TEDAPI'"
            )

    allowedHosts = config.get("dashboard", {}).get("allowedHosts")
    if allowedHosts is not None:
        _requireStringList(allowedHosts, "config['dashboard']['allowedHosts'] must be a list of non-empty host strings")

    teslaCfg = config.get("tesla")
    if teslaCfg is not None and teslaCfg.get("enabled", True):
        require(teslaCfg, ["email"], "config['tesla']")
        vehicleNames = teslaCfg.get("vehicleNames")
        if vehicleNames is not None:
            _requireStringList(vehicleNames, "config['tesla']['vehicleNames'] must be a list of non-empty vehicle names")
        pollInterval = teslaCfg.get("pollIntervalSeconds")
        if pollInterval is not None and (isinstance(pollInterval, bool) or not isinstance(pollInterval, (int, float)) or pollInterval <= 0):
            raise RuntimeError("config['tesla']['pollIntervalSeconds'] must be a positive number")

    require(config["lucid"], ["username", "password"], "config['lucid']")
    require(
        config["charging"],
        ["minPowerwallSocPercent", "targetEvChargePercent", "chargerVoltage",
         "minChargingAmps", "maxChargingAmps", "commandCooldownMinutes"],
        "config['charging']",
    )


def _teslaEnabled(config: dict) -> bool:
    """True when the optional tesla block is present, enabled, and has an email."""
    teslaCfg = config.get("tesla")
    return bool(teslaCfg) and teslaCfg.get("enabled", True) and bool(teslaCfg.get("email"))


def _teslaAuthResponseUrl(pasted: str, redirectUri: str, expectedState: str) -> str | None:
    """Normalize whatever the user pasted — a full redirect URL
    (tesla://auth/callback?code=...), a bare query string (code=...&state=...),
    or the code value itself (e.g. from DevTools) — into an authorization
    response URL. fetch_token must receive authorization_response (not code=):
    teslapy restarts the whole handshake, regenerating the PKCE verifier the
    pasted code is bound to, whenever that kwarg is absent."""
    params = parse_qs(urlparse(pasted).query)
    if "code" not in params and "://" not in pasted:
        params = parse_qs(pasted)
    code = (params.get("code") or [None])[0]
    if code is None and "://" not in pasted and "=" not in pasted and " " not in pasted and len(pasted) >= 10:
        code = pasted
    if code is None:
        return None
    state = (params.get("state") or [expectedState])[0]
    return f"{redirectUri}?{urlencode({'code': code, 'state': state})}"


def _ensureTeslaCacheFile() -> None:
    """Pre-create an empty token cache so teslapy doesn't log a scary traceback
    on first use (its cache loader warns with exc_info when the file is absent)."""
    if not _TESLA_CACHE_FILE.exists():
        _TESLA_CACHE_FILE.write_text("{}")
        _TESLA_CACHE_FILE.chmod(0o600)


def powerwallCacheFile(activeName: str, host: str) -> str:
    # Build a stable, filesystem-safe cache filename per gateway.
    raw = f"{activeName}-{host}"
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in raw)
    return f".powerwall-{safe}"


class PowerwallState:
    """Holds the latest Powerwall reading and maintains rolling averages for solar
    and home load.

    The constructor mirrors the old dataclass signature so existing call-sites
    and tests that build a one-shot instance require no changes.  A persistent
    instance updated via ``update()`` accumulates history across poll cycles so
    that ``surplusKw`` and ``isDark`` automatically reflect the smoothed value.
    """

    _HISTORY_SIZE = 5

    def __init__(
        self,
        solarWatts: float = 0,
        homeWatts: float = 0,
        powerwallSocPercent: float = 0,
    ) -> None:
        self._solarHistory: collections.deque = collections.deque(maxlen=self._HISTORY_SIZE)
        self._homeHistory:  collections.deque = collections.deque(maxlen=self._HISTORY_SIZE)
        self.solarWatts: float = max(0, solarWatts or 0)
        self.homeWatts: float = homeWatts or 0
        self.powerwallSocPercent: float = powerwallSocPercent or 0
        # Seed history only when non-zero values are provided (i.e. one-shot test/mock
        # instances).  A persistent instance created with no args starts with an empty
        # history; the first update() call will populate it.
        if self.solarWatts > 0:
            self._solarHistory.append(self.solarWatts)
        if self.homeWatts > 0:
            self._homeHistory.append(self.homeWatts)

    def update(self, solarWatts: float, homeWatts: float, powerwallSocPercent: float) -> None:
        """Record a new Powerwall reading and advance the rolling averages."""
        self.solarWatts = max(0, solarWatts or 0)
        self.homeWatts = homeWatts or 0
        self.powerwallSocPercent = powerwallSocPercent or 0
        self._solarHistory.append(self.solarWatts)
        self._homeHistory.append(self.homeWatts)
        if abs(self.avgSolarWatts - self.solarWatts) > 50:
            log.info(
                f"Solar rolling avg: {self.avgSolarWatts:.0f}W  "
                f"(raw: {self.solarWatts:.0f}W  over {len(self._solarHistory)} sample(s))"
            )
        if abs(self.avgHomeWatts - self.homeWatts) > 1000:
            log.info(
                f"Home rolling avg: {self.avgHomeWatts:.0f}W  "
                f"(raw: {self.homeWatts:.0f}W  over {len(self._homeHistory)} sample(s))"
            )

    @property
    def avgSolarWatts(self) -> float:
        """Rolling mean of the last ``_HISTORY_SIZE`` solar readings.

        Falls back to the current raw reading when history has not yet been
        populated (e.g. a one-shot test instance that was never ``update()``d).
        """
        if not self._solarHistory:
            return self.solarWatts
        return sum(self._solarHistory) / len(self._solarHistory)

    @property
    def avgHomeWatts(self) -> float:
        """Rolling mean of the last ``_HISTORY_SIZE`` home-load readings.

        Falls back to the current raw reading when history has not yet been
        populated (e.g. a one-shot test instance that was never ``update()``d).
        """
        if not self._homeHistory:
            return self.homeWatts
        return sum(self._homeHistory) / len(self._homeHistory)

    @property
    def surplusKw(self) -> float:
        """Smoothed surplus in kW: rolling-avg solar minus rolling-avg home load.

        Using averaged home prevents a single transient home-load spike from
        immediately triggering a STOP command.
        """
        return (self.avgSolarWatts - self.avgHomeWatts) / 1000

    def updateSoc(self, powerwallSocPercent: float) -> None:
        """Update only the Powerwall SoC — used during solar transients where
        `solarWatts` is unreliable but SoC is still valid."""
        self.powerwallSocPercent = powerwallSocPercent or 0

    def resetHomeHistory(self) -> None:
        """Discard stale home-load history after an EV charge-state transition.

        Home load changes significantly when the EV starts or stops charging (by the
        EV's full charging draw).  Old readings from the previous regime would skew
        ``avgHomeWatts`` — and therefore ``effectiveSurplusKw`` — for 2–3 cycles,
        potentially causing an over-allocation immediately after a START.
        """
        self._homeHistory.clear()

    @property
    def isDark(self) -> bool:
        """True when smoothed solar production is negligible (< 50W)."""
        return self.avgSolarWatts < 50

    def __repr__(self) -> str:
        return (
            f"PowerwallState(solar={self.solarWatts:.0f}W"
            f" avg={self.avgSolarWatts:.0f}W"
            f" home={self.homeWatts:.0f}W"
            f" avgHome={self.avgHomeWatts:.0f}W"
            f" surplus={self.surplusKw:+.2f}kW"
            f" soc={self.powerwallSocPercent:.1f}%)"
        )


@dataclass
class ChargingThresholds:
    minPowerwallSocPercent: float  # Powerwall must stay above this to charge the EV
    targetEvChargePercent: float      # stop charging EV once it reaches this level
    chargerVoltage: float             # AC voltage at EVSE (e.g. 240V in North America)
    minChargingAmps: int              # minimum amps to sustain; below this, stop charging
    maxChargingAmps: int              # cap imposed by EVSE or vehicle onboard charger
    commandCooldownMinutes: float     # after a start/stop command, hold for this many minutes before stopping (protects HV contacts)
    stopConfirmCycles: int = 4        # consecutive low-surplus cycles required before issuing STOP (1 = stop on first low cycle)
    allowExternalChargeInterference: bool = True  # if True, take over externally-started sessions; if False, observe only

    @property
    def minSurplusKw(self) -> float:
        """Minimum solar surplus to start/continue charging, derived from minChargingAmps and chargerVoltage."""
        return self.minChargingAmps * self.chargerVoltage / 1000


@dataclass
class VehicleState:
    evSocPercent: float  # EV battery state of charge (0–100)
    chargeState: int     # protobuf int — use ChargeState.Name() to get string
    evChargingAmps: int = 0  # last commanded AC current limit (0 when not charging)
    lastCommandTime: datetime | None = None   # when the last start/stop was issued
    lastCommandWasStart: bool = False         # True if last command was start; False if stop
    lastActionDesc: str = ""                  # human-readable description of last decision for dashboard
    lowSurplusCycles: int = 0                 # consecutive cycles where surplus was too low while charging (stop-confirm guard)


@dataclass
class DashboardState:
    lastUpdated: datetime | None = None
    evSocPercent: float | None = None
    chargeStateName: str = "unknown"
    evChargingAmps: int = 0
    evChargingKw: float = 0.0
    lastError: str | None = None
    lucidError: str | None = None  # last Lucid poll failure; None after a successful poll
    vehicleName: str = ""
    vehicleModel: str = ""
    vehicleRangeKm: float | None = None   # remaining_range from Lucid proto (km)
    vehicleOdometerKm: float | None = None
    vehicleLastPolled: datetime | None = None  # last time pollVehicleAndDecide ran
    vehicleSoftwareVersion: str = ""
    ratedRangeMiles: float | None = None  # from config; enables range card gradient fill
    targetEvChargePercent: float | None = None  # from config; draws target marker on EV card
    units: str = "imperial"               # "imperial" (mi) or "metric" (km)
    version: str = ""                     # set once at startup from __version__
    nextPollAt: datetime | None = None    # when the next backend poll cycle is expected to run
    activePowerwall: str = ""            # name of currently active Powerwall
    powerwallNames: list[str] = field(default_factory=list)  # all configured Powerwall names
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=_HISTORY_MAXLEN))
    siteReadings: list = field(default_factory=list)  # per-site Powerwall data for dashboard; see _buildSiteReadings
    chargingMode: ChargingMode = ChargingMode.ENABLED  # user-controlled: enabled/manual/disabled
    teslaConfigured: bool = False  # tesla config block present and enabled → UI shows Tesla section
    teslaAvailable: bool = True    # False on account-level Tesla auth/API failure
    teslaError: str | None = None
    # One dict per Tesla vehicle: vin, name, socPercent, chargeStateName, chargingKw,
    # rangeKm, chargeLimitPercent, asleep, lastPolled (ISO string — JSON-safe in a list).
    teslaVehicles: list = field(default_factory=list)


_dashboard = DashboardState()
_allSites: dict = {}  # dict[str, SiteInfo]; set in runChaos; read by chart endpoints
_autoSwitchConsecutiveCount: dict[str, int] = {}  # consecutive mismatch cycles per candidate site
_NON_ACTIVE_SOLAR_COLOR = "rgba(255, 240, 100, 0.85)"  # light yellow — deemphasized variant of active Solar (#fbbf24)
_NON_ACTIVE_HOME_COLOR  = "rgba(56, 189, 248, 0.45)"   # dimmed sky   — matches active Home Load (#38bdf8)
_NON_ACTIVE_PW_COLOR    = "rgba(255, 240, 100, 0.85)"  # light yellow — matches non-active Solar color for visual consistency
_DASHED_PREFIX          = "╌╌ "                        # legend label prefix indicating a dashed line series
_DASHED_STYLE           = {"stroke_style": {"dasharray": "8 3", "width": 2.5}}
_PYGAL_DEFAULT_COLORS   = ("#fbbf24", "#38bdf8", "#4ade80", "#a78bfa")
_TESLA_CHART_COLORS     = ("#f87171", "#fb923c", "#e879f9", "#facc15")  # one per Tesla vehicle, cycled
_CHART_NO_DATA = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200">'
    b'<text x="50%" y="50%" fill="#64748b" text-anchor="middle" font-family="system-ui">'
    b'No data yet</text></svg>'
)
# Inter-task signals: set by FastAPI POST handlers, cleared and consumed by the main poll loop.
_forcePoll = asyncio.Event()       # /api/poll — trigger an immediate poll cycle
_switchPowerwall = asyncio.Event() # /api/powerwall — switch the active Powerwall site
_pendingPowerwallName: str | None = None
_webApiKey: str = ""  # optional; if non-empty, POST endpoints require X-API-Key header
_chargingMode: ChargingMode = ChargingMode.ENABLED  # user-controlled via POST /api/charging
_webhookUrl: str = ""  # set once in runChaos; read by _pollOneSite for unreachable/recovered notifications


@dataclass
class SiteInfo:
    """Bundles all per-site Powerwall information into one object."""
    name: str
    pw: pypowerwall.Powerwall | None  # None if startup connection failed
    pwConfig: dict | None = None      # this site's config['powerwalls'] entry, for reconnecting when pw is None
    lastReconnectAttempt: datetime | None = None  # throttles reconnects while pw is None
    pwState: PowerwallState = field(default_factory=PowerwallState)
    error: str | None = None
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=_HISTORY_MAXLEN))
    longHistory: collections.deque = field(default_factory=lambda: collections.deque(maxlen=_LONG_HISTORY_MAXLEN))
    lastLongHistoryTime: datetime | None = None
    model: str | None = None            # gateway model from DIN part number (static; read at connect)
    firmwareVersion: str | None = None  # gateway firmware version (static; read at connect)
    lifetimeChargedWh: float | None = None     # battery meter energy_imported (lifetime, Wh)
    lifetimeDischargedWh: float | None = None  # battery meter energy_exported (lifetime, Wh)
    consecutiveFailures: int = 0     # consecutive failed polls (exceptions or all-zero readings, not solar transients)
    firstFailureTime: datetime | None = None  # when the current run of failed polls began; None while healthy
    unreachableNotified: bool = False  # True once the unreachable webhook has fired, until recovery
    bothFullNotified: bool = False  # True once the EV-at-target + PW-full webhook has fired, until either drops past the reset margin


def _rawSurplusKw(solarWatts: float, homeWatts: float) -> float:
    """Raw solar surplus in kW (not rolling-averaged). Used for display and history."""
    return round((solarWatts - homeWatts) / 1000, 2)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CHAOS Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23fbbf24' d='M13 2L4.5 13.5H11L10 22L19.5 10.5H13L13 2Z'/%3E%3C/svg%3E">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
  h1{font-size:1.6em;font-weight:700;color:#f8fafc;margin-bottom:2px}
  .subtitle{color:#94a3b8;font-size:.8em;margin-bottom:20px}
  .cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}
  .card{background:#1e293b;border-radius:10px;padding:14px 18px;min-width:130px;border:1px solid #334155}
  .card-label{font-size:.7em;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em}
  .card-value{font-size:1.5em;font-weight:600;margin-top:4px;color:#f1f5f9}
  .card-group-label{font-size:.65em;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin:18px 0 6px}
  .card-group-label .glmeta{text-transform:none;letter-spacing:0;font-weight:400;color:#64748b;margin-left:8px}
  .card-group-label .glerr{color:#f87171}
  .card-group-label .glname{color:#f1f5f9;font-weight:600}
  .card-group-label .glerr .glname{color:inherit}
  .devices{display:grid;grid-template-columns:1fr;column-gap:24px;align-items:start}
  @media(min-width:1350px){
    .devices{grid-template-columns:repeat(2,minmax(0,1fr))}
    .devices .device:only-child{grid-column:1/-1}
  }
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.8em;font-weight:600}
  .g{background:#166534;color:#bbf7d0}.s{background:#374151;color:#9ca3af}
  .error-bar{background:#7f1d1d;color:#fca5a5;padding:10px 16px;border-radius:8px;margin-bottom:16px;font-size:.85em}
  h2{color:#94a3b8;font-size:.75em;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
  table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden;font-size:.82em}
  th{background:#334155;padding:8px 10px;text-align:left;font-size:.72em;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;white-space:nowrap}
  td{padding:7px 10px;border-bottom:1px solid #0f172a}
  tr:last-child td{border-bottom:none}
  .pos{color:#4ade80}.neg{color:#f87171}.muted{color:#94a3b8}
  .footer{margin-top:24px;color:#334155;font-size:.72em;text-align:right}
  .hi{color:#fbbf24}
  .poll-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:6px;padding:4px 12px;font-size:.8em;cursor:pointer;margin-left:10px;vertical-align:middle}
  .poll-btn:hover{background:#334155;color:#e2e8f0}
  .poll-btn:disabled{opacity:.5;cursor:default}
  .charging-toggle{display:inline-flex;gap:4px;margin-left:10px;vertical-align:middle}
  .toggle-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:6px;padding:4px 12px;font-size:.8em;cursor:pointer}
  .toggle-btn:hover:not(.toggle-active){background:#334155;color:#e2e8f0}
  .toggle-btn.toggle-enable.toggle-active{background:#14532d;color:#86efac;border-color:#166534;cursor:default}
  .toggle-btn.toggle-manual.toggle-active{background:#78350f;color:#fbbf24;border-color:#92400e;cursor:default}
  .toggle-btn.toggle-disable.toggle-active{background:#450a0a;color:#fca5a5;border-color:#7f1d1d;cursor:default}
  .chart-section{margin-bottom:24px}
  .chart-section h2{margin-bottom:8px}
  .chart-wrap{background:#1e293b;border-radius:10px;padding:4px;border:1px solid #334155;margin-bottom:10px;max-width:1400px}
  .chart-wrap svg{width:100%;height:auto;display:block}
  .title-row{display:flex;align-items:center;justify-content:space-between}
  .site-row-header{display:flex;align-items:center;gap:10px;margin:16px 0 6px}
  .activate-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:16px;padding:3px 14px;font-size:.75em;cursor:pointer}
  .activate-btn:hover:not(:disabled){background:#334155;color:#e2e8f0}
  .activate-btn.active{background:#166534;color:#bbf7d0;border-color:#166534;cursor:default}
  .site-row-error{color:#fca5a5;font-size:.78em;margin-bottom:6px}
</style>
</head><body>
<div class="title-row">
<h1>⚡ <abbr title="CHarging Automatically On Solar">CHAOS</abbr></h1>
<div>
<div class="charging-toggle">
  <button class="toggle-btn toggle-enable" id="btnEnable" onclick="setChargingMode('enabled')" title="Controls CHAOS's Lucid charging only — Tesla vehicles are read-only">Enable Lucid Charging</button>
  <button class="toggle-btn toggle-manual" id="btnManual" onclick="setChargingMode('manual')" title="CHAOS keeps monitoring but sends no charging commands">Manual (Monitor Only)</button>
  <button class="toggle-btn toggle-disable" id="btnDisable" onclick="setChargingMode('disabled')" title="Controls CHAOS's Lucid charging only — Tesla vehicles are read-only">Disable Lucid Charging</button>
</div>
<button class="poll-btn" id="pollBtn" onclick="forcePoll()">Update now</button>
</div>
</div>
<div class="tagline"><span class="hi">CH</span>arging <span class="hi">A</span>utomatically <span class="hi">O</span>n <span class="hi">S</span>olar</div>
<div class="subtitle" id="sub">Loading…</div>
<div id="err" class="error-bar" style="display:none"></div>
<div id="siteRows" class="devices"></div>
<div id="vehiclesSection" class="devices">
<div class="device">
<div class="card-group-label" id="evLabel">Lucid</div>
<div class="cards">
  <div class="card" id="card-ev"><div class="card-label">Battery</div><div class="card-value" id="ev">—</div></div>
  <div class="card" id="card-range"><div class="card-label">Range</div><div class="card-value" id="range">—</div></div>
  <div class="card"><div class="card-label">Charge State</div><div class="card-value" id="cs">—</div></div>
  <div class="card"><div class="card-label">Charging</div><div class="card-value" id="chargingKw">—</div></div>
</div>
</div>
</div>
<h2>History</h2>
<div class="chart-section">
  <div class="chart-wrap" id="chartPowerHour"></div>
  <div class="chart-wrap" id="chartPower"></div>
  <div class="chart-wrap" id="chartSoc"></div>
</div>
<h2>Recent Cycles</h2>
<table><thead><tr>
  <th>Time</th><th>Solar</th><th>Home</th><th>Surplus</th><th>PW%</th><th>EV%</th><th>State</th><th>Amps</th><th>Action</th>
</tr></thead><tbody id="hist"></tbody></table>
<script>
const MI=1.60934;
function badge(s){
  if(!s||s==='unknown')return '<span class="badge s">—</span>';
  const n=escHtml(s.replace('CHARGE_STATE_','').replace(/_/g,' '));
  const cls=(!s.includes('STOPPED')&&!s.includes('END')&&!s.includes('PAUSED')&&s.includes('CHARG'))?'g':'s';
  return '<span class="badge '+cls+'">'+n+'</span>';
}
function kwFmt(w){
  if(w==null)return'—';
  return (w/1000).toFixed(2)+' kW';
}
function surpFmt(v){
  if(v==null)return'—';
  const cls=v>=0?'pos':'neg';
  return'<span class="'+cls+'">'+(v>=0?'+':'')+v.toFixed(2)+'kW</span>';
}
function fillBg(id,pct,color,markerPct){
  const el=document.getElementById(id);
  if(!el)return;
  const p=Math.max(0,Math.min(100,pct||0));
  const fill='linear-gradient(to right,'+color+' '+p+'%,transparent '+p+'%)';
  if(markerPct==null){el.style.background=fill;return;}
  const m=Math.max(0,Math.min(100,markerPct));
  el.style.background='linear-gradient(to right,transparent calc('+m+'% - 1px),rgba(74,222,128,0.9) calc('+m+'% - 1px),rgba(74,222,128,0.9) calc('+m+'% + 1px),transparent calc('+m+'% + 1px)),'+fill;
}
function fillTdBg(pct,color){
  const p=Math.max(0,Math.min(100,pct||0));
  return'style="background:linear-gradient(to right,'+color+' '+p+'%,transparent '+p+'%)"';
}
function escHtml(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}
function ageFmt(iso){
  if(!iso)return null;
  const secs=Math.round((Date.now()-new Date(iso).getTime())/1000);
  if(secs<5)return'just now';
  if(secs<90)return secs+'s ago';
  const mins=Math.round(secs/60);
  if(mins<90)return mins+' min ago';
  const hrs=Math.round(mins/60);
  return hrs+' hr ago';
}
function refresh(){
  fetch('/api/state').then(r=>r.json()).then(d=>{
    const imperial=d.units!=='metric';
    const ts=d.lastUpdated?new Date(d.lastUpdated).toLocaleString():'Never';
    if(d.version)document.getElementById('ver').textContent='CHAOS v'+d.version;
    const mode=d.chargingMode||'enabled';
    document.getElementById('btnEnable').classList.toggle('toggle-active',mode==='enabled');
    document.getElementById('btnManual').classList.toggle('toggle-active',mode==='manual');
    document.getElementById('btnDisable').classList.toggle('toggle-active',mode==='disabled');

    // Live countdown to next poll — update every second
    if(window._countdownTimer)clearInterval(window._countdownTimer);
    window._nextPollAt=d.nextPollAt?new Date(d.nextPollAt):null;
    window._lastUpdatedStr=ts;
    function updateSub(){
      const sub=document.getElementById('sub');
      if(!window._nextPollAt){sub.textContent='Last updated: '+window._lastUpdatedStr;return;}
      const secs=Math.max(0,Math.round((window._nextPollAt-Date.now())/1000));
      const nextStr=secs<=0?'any moment…':secs<60?'in '+secs+'s':'in '+Math.round(secs/60)+'min';
      sub.textContent='Last updated: '+window._lastUpdatedStr+' · next update '+nextStr;
    }
    updateSub();
    window._countdownTimer=setInterval(updateSub,1000);

    // Schedule next UI fetch to coincide with the next backend poll (+2s buffer)
    const delay=window._nextPollAt?Math.max(1000,window._nextPollAt-Date.now()+2000):65000;
    setTimeout(refresh,delay);
    // Group-header label: base (pre-escaped HTML) turns red with a tooltip on
    // fetch failure; meta is a muted, non-uppercased suffix.
    function glabel(base,err,meta){
      let h=err?'<span class="glerr" title="'+escHtml(err).replace(/"/g,'&quot;')+'">'+base+'</span>':base;
      if(meta)h+='<span class="glmeta">· '+meta+'</span>';
      return h;
    }
    // Device header meta: identity (model · odometer · firmware) + optional extra + freshness
    const odoFmt=km=>imperial?Math.round(km/MI).toLocaleString()+' mi':Math.round(km).toLocaleString()+' km';
    const mwhFmt=wh=>wh<1e6?Math.round(wh/1e3)+' kWh':(wh/1e6).toFixed(1)+' MWh';
    function vmeta(model,odoKm,fw,fresh,extra){
      return [model?escHtml(model):null,odoKm!=null?odoFmt(odoKm):null,fw?'fw '+escHtml(fw):null,extra,fresh].filter(Boolean).join(' · ');
    }
    // Vehicle header label: bright vehicle name first, then the manufacturer
    function vlabel(name,make){return (name?'<span class="glname">'+escHtml(name)+'</span> · ':'')+make;}

    // Build one row per Powerwall site
    const siteRows=document.getElementById('siteRows');
    siteRows.innerHTML='';
    const multiSite=d.siteReadings&&d.siteReadings.length>1;
    (d.siteReadings||[]).forEach(site=>{
      const isActive=site.isActive;
      const btnHtml=multiSite
        ?'<button class="activate-btn'+(isActive?' active':'')+'" data-name="'+escHtml(site.name)+'"'+(isActive?' disabled':'')+' onclick="switchPowerwall(this.dataset.name)">'+(isActive?'Active ✓':'Use for charging')+'</button>'
        :'';
      const life=[site.lifetimeChargedWh!=null?mwhFmt(site.lifetimeChargedWh)+' in':null,
                  site.lifetimeDischargedWh!=null?mwhFmt(site.lifetimeDischargedWh)+' out':null].filter(Boolean).join(' · ');
      const hdr='<div class="site-row-header">'
        +'<span class="card-group-label" style="margin:0">'+glabel('<span class="glname">'+escHtml(site.name)+'</span>',site.error,vmeta(site.model,null,site.firmwareVersion,site.lastPolled?'polled '+ageFmt(site.lastPolled):null,life||null))+'</span>'
        +btnHtml+'</div>';
      const errHtml=site.error?'<div class="site-row-error">⚠ '+escHtml(site.error)+'</div>':'';
      const cardId='card-pw-'+site.name.replace(/\\s+/g,'_');
      // Home load minus whichever EV draw is attributed to this site: Lucid on the
      // active site, else backend-attributed Tesla charging (mutually exclusive).
      const tKw=site.teslaChargingKw||0;
      let homeW=site.homeWatts;
      if(isActive&&d.evChargingKw>0){if(d.evChargingKw*1000<homeW)homeW-=d.evChargingKw*1000;}
      else if(tKw>0&&tKw*1000<homeW)homeW-=tKw*1000;
      const cards='<div class="cards">'
        +'<div class="card" id="'+cardId+'"><div class="card-label">Powerwall</div><div class="card-value">'+(site.powerwallSocPercent!=null?site.powerwallSocPercent.toFixed(1)+'%':'—')+'</div></div>'
        +'<div class="card"><div class="card-label">Solar</div><div class="card-value">'+kwFmt(site.solarWatts)+'</div></div>'
        +'<div class="card"><div class="card-label">Home Load</div><div class="card-value">'+kwFmt(homeW)+'</div></div>'
        +'<div class="card"><div class="card-label">Surplus</div><div class="card-value">'+surpFmt(site.surplusKw)+'</div></div>'
        +(isActive&&d.evChargingKw>0?'<div class="card"><div class="card-label">Lucid Charging</div><div class="card-value">'+kwFmt(d.evChargingKw*1000)+'</div></div>':'')
        +(tKw>0?'<div class="card"><div class="card-label">Tesla Charging</div><div class="card-value">'+kwFmt(tKw*1000)+'</div></div>':'')
        +'</div>';
      siteRows.insertAdjacentHTML('beforeend','<div class="device">'+hdr+errHtml+cards+'</div>');
      fillBg(cardId,site.powerwallSocPercent,'rgba(56,189,248,0.22)');
    });

    const lage=ageFmt(d.vehicleLastPolled);
    document.getElementById('evLabel').innerHTML=glabel(vlabel(d.vehicleName,'Lucid'),d.lucidError,
      vmeta(d.vehicleModel,d.vehicleOdometerKm,d.vehicleSoftwareVersion,lage?'data: '+lage:null));
    document.getElementById('ev').textContent=d.evSocPercent!=null?d.evSocPercent+'%':'—';
    const rangeFmt=d.vehicleRangeKm!=null?(imperial?Math.round(d.vehicleRangeKm/MI)+' mi':Math.round(d.vehicleRangeKm)+' km'):'—';
    document.getElementById('range').textContent=rangeFmt;
    document.getElementById('cs').innerHTML=badge(d.chargeStateName);
    document.getElementById('chargingKw').textContent=d.evChargingKw>0?d.evChargingKw.toFixed(1)+' kW':'—';
    fillBg('card-ev',d.evSocPercent,'rgba(74,222,128,0.22)',d.targetEvChargePercent);
    if(d.ratedRangeMiles!=null&&d.vehicleRangeKm!=null)
      fillBg('card-range',d.vehicleRangeKm/(d.ratedRangeMiles*MI)*100,'rgba(251,191,36,0.22)');

    // Tesla vehicles (read-only) — one card group per vehicle, built dynamically and
    // appended alongside the static Lucid tile so both can share a grid row when they fit
    const tsec=document.getElementById('vehiclesSection');
    tsec.querySelectorAll('.tesla-device').forEach(el=>el.remove());
    if(d.teslaConfigured){
      let th='';
      (d.teslaVehicles||[]).forEach((v,i)=>{
        const age=ageFmt(v.lastPolled);
        const fresh=v.asleep?'💤 asleep'+(age?' · '+age:''):(age?'data: '+age:null);
        th+='<div class="device tesla-device">'
          +'<div class="card-group-label">'+glabel(vlabel(v.name,'Tesla'),d.teslaAvailable?null:(d.teslaError||'Tesla unavailable'),vmeta(v.model,v.odometerKm,v.softwareVersion,fresh))+'</div>'
          +'<div class="cards">'
          +'<div class="card" id="card-tesla-'+i+'"><div class="card-label">Battery</div><div class="card-value">'+(v.socPercent!=null?v.socPercent+'%':'—')+'</div></div>'
          +'<div class="card" id="card-tesla-range-'+i+'"><div class="card-label">Range</div><div class="card-value">'+(v.rangeKm!=null?(imperial?Math.round(v.rangeKm/MI)+' mi':Math.round(v.rangeKm)+' km'):'—')+'</div></div>'
          +'<div class="card"><div class="card-label">Charge State</div><div class="card-value">'+badge((v.chargeStateName||'').toUpperCase())+'</div></div>'
          +'<div class="card"><div class="card-label">Charging</div><div class="card-value">'+(v.chargingKw>0?v.chargingKw.toFixed(1)+' kW':'—')+'</div></div>'
          +'</div></div>';
      });
      // Unavailable before any vehicle was ever seen: no per-vehicle headers exist
      // to carry the red state, so show one section-level header instead.
      if(!th&&!d.teslaAvailable)th='<div class="device tesla-device"><div class="card-group-label">'+glabel('Tesla',d.teslaError||'Tesla unavailable',null)+'</div></div>';
      tsec.insertAdjacentHTML('beforeend',th);
      (d.teslaVehicles||[]).forEach((v,i)=>{
        fillBg('card-tesla-'+i,v.socPercent,'rgba(248,113,113,0.22)',v.chargeLimitPercent);
        // Range as a fraction of a full charge equals SOC (range/rangeAt100%) — same
        // amber treatment as the Lucid range card.
        if(v.socPercent!=null)fillBg('card-tesla-range-'+i,v.socPercent,'rgba(251,191,36,0.22)');
      });
    }
    const eb=document.getElementById('err');
    if(d.lastError){eb.style.display='';eb.textContent='⚠ '+d.lastError;}
    else eb.style.display='none';
    function _injectSvg(id,s){const el=document.getElementById(id);el.innerHTML='';el.appendChild(document.createRange().createContextualFragment(s));}
    const t=Date.now();
    fetch('/api/charts/power-hour.svg?t='+t).then(r=>r.text()).then(s=>_injectSvg('chartPowerHour',s));
    fetch('/api/charts/power.svg?t='+t).then(r=>r.text()).then(s=>_injectSvg('chartPower',s));
    fetch('/api/charts/soc.svg?t='+t).then(r=>r.text()).then(s=>_injectSvg('chartSoc',s));
    const tb=document.getElementById('hist');
    tb.innerHTML='';
    (d.history||[]).slice().reverse().forEach(h=>{
      const t=new Date(h.timestamp).toLocaleTimeString();
      const tr=document.createElement('tr');
      const pwFill=fillTdBg(h.powerwallSocPercent,'rgba(56,189,248,0.18)');
      const evFill=h.evSocPercent!=null?fillTdBg(h.evSocPercent,'rgba(74,222,128,0.18)'):'';
      tr.innerHTML='<td>'+t+'</td>'
        +'<td>'+kwFmt(h.solarWatts)+'</td>'
        +'<td>'+kwFmt((h.evChargingKw||0)*1000<h.homeWatts?h.homeWatts-(h.evChargingKw||0)*1000:h.homeWatts)+'</td>'
        +'<td>'+surpFmt(h.surplusKw)+'</td>'
        +'<td '+pwFill+'>'+(h.powerwallSocPercent!=null?h.powerwallSocPercent.toFixed(1)+'%':'—')+'</td>'
        +'<td '+evFill+'>'+(h.evSocPercent!=null?h.evSocPercent+'%':'<span class="muted">—</span>')+'</td>'
        +'<td>'+badge(h.chargeStateName)+'</td>'
        +'<td>'+(h.evChargingAmps>0?h.evChargingAmps+'A':'<span class="muted">—</span>')+'</td>'
        +'<td class="muted">'+(h.action||'')+'</td>';
      tb.appendChild(tr);
    });
  }).catch(()=>{ setTimeout(refresh,65000); });
}
function setChargingMode(mode){
  fetch('/api/charging',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':'__CHAOS_API_KEY__'},body:JSON.stringify({mode:mode})})
    .then(()=>{setTimeout(refresh,500);setTimeout(refresh,5000);});
}
function forcePoll(){
  const btn=document.getElementById('pollBtn');
  btn.disabled=true;btn.textContent='Polling…';
  fetch('/api/poll',{method:'POST',headers:{'X-API-Key':'__CHAOS_API_KEY__'}}).finally(()=>{
    setTimeout(()=>{btn.disabled=false;btn.textContent='Poll now';refresh();},4000);
  });
}
function switchPowerwall(name){
  fetch('/api/powerwall',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':'__CHAOS_API_KEY__'},body:JSON.stringify({name:name})})
    .then(()=>{setTimeout(refresh,500);setTimeout(refresh,5000);});
}
refresh();
</script>
<div class="footer"><span id="ver"></span> &nbsp;·&nbsp; <a href="/api/log" target="_blank" style="color:#64748b;text-decoration:none;">view log</a></div>
</body></html>"""

_webApp = fastapi.FastAPI(docs_url=None, redoc_url=None)

def _checkApiKey(x_api_key: str = fastapi.Header(default="")):
    """FastAPI dependency: reject request if an API key is configured and the header doesn't match."""
    if _webApiKey and x_api_key != _webApiKey:
        raise fastapi.HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

def _pygalStyle(colors=_PYGAL_DEFAULT_COLORS):
    return pygal.style.Style(  # type: ignore[attr-defined]
        background="#0f172a",
        plot_background="#1e293b",
        foreground="#94a3b8",
        foreground_strong="#e2e8f0",
        foreground_subtle="#475569",
        guide_stroke_color="#334155",
        major_guide_stroke_color="#334155",
        colors=colors,
        font_family="system-ui,sans-serif",
        label_font_size=11,
        legend_font_size=11,
        title_font_size=12,
    )

def _pygalConfig(title: str, y_title: str, y_range=None) -> pygal.Config:
    cfg: Any = pygal.Config()
    cfg.title = title
    cfg.height = 200
    cfg.show_dots = False
    cfg.interpolate = "cubic"
    cfg.legend_at_bottom = True
    cfg.legend_at_bottom_columns = 4
    cfg.disable_xml_declaration = True
    cfg.js = ["/pygal-tooltips.min.js"]
    cfg.y_title = y_title
    cfg.x_label_rotation = 30
    cfg.truncate_label = 8
    cfg.y_labels_major_count = 5
    cfg.show_minor_y_labels = False
    cfg.x_labels_major_count = 12
    cfg.show_minor_x_labels = False
    if y_range:
        cfg.range = y_range
    return cfg

def _buildChartSvg(chart_type, title: str, y_title: str, datasets: Sequence[tuple[str, list, dict] | tuple[str, list]], labels: list[str], y_range=None, colors=None) -> bytes:
    # Build a per-series color list for pygal's Style. Datasets may include an explicit
    # "color" key in their kwargs dict; remaining slots are filled from the base palette.
    # pygal does not support per-series color via chart.add() — it must go in the Style.
    base_colors = list(colors) if colors else list(_PYGAL_DEFAULT_COLORS)
    color_cycle = itertools.cycle(base_colors)
    all_colors = []
    for entry in datasets:
        kw = entry[2] if len(entry) > 2 else {}
        all_colors.append(kw["color"] if "color" in kw else next(color_cycle))

    cfg = _pygalConfig(title, y_title, y_range)
    chart = chart_type(cfg, style=_pygalStyle(tuple(all_colors)))
    chart.x_labels = labels
    for entry in datasets:
        name, data = entry[0], entry[1]
        kwargs = dict(entry[2]) if len(entry) > 2 else {}
        kwargs.pop("color", None)  # color is encoded in the Style, not chart.add()
        chart.add(name, data, **kwargs)
    return chart.render()

def _chartActiveHistory() -> "tuple[list, list] | None":
    """Return (history_list, labels) for the active site, or None if fewer than 2 points."""
    active = _dashboard.activePowerwall
    hist = list(_allSites[active].history) if active in _allSites else []
    if len(hist) < 2:
        return None
    labels = [datetime.fromisoformat(h["timestamp"]).astimezone().strftime("%H:%M") for h in hist]
    return hist, labels

def _chartLongHistory() -> "tuple[list, list] | None":
    """Return (history_list, labels) for the active site's long history, or None if fewer than 2 points."""
    active = _dashboard.activePowerwall
    hist = list(_allSites[active].longHistory) if active in _allSites else []
    if len(hist) < 2:
        return None
    labels = [datetime.fromisoformat(h["timestamp"]).astimezone().strftime("%H:%M") for h in hist]
    return hist, labels

def _paddedSiteHistory(hist_deque: "collections.deque", n: int) -> list:
    """Return the history deque padded with None on the left to length n."""
    hist = list(hist_deque)[-n:]
    return [None] * (n - len(hist)) + hist

@_webApp.get("/", response_class=fastapi.responses.HTMLResponse)
async def _webIndex():
    # repr() produces a JS-safe quoted string (e.g. 'secret' or ''); the replace
    # target includes the surrounding single quotes already in the template so the
    # substitution yields syntactically valid JS regardless of key value.
    return _HTML_TEMPLATE.replace("'__CHAOS_API_KEY__'", repr(_webApiKey))

@_webApp.get("/api/state")
async def _webApiState():
    d = asdict(_dashboard)
    # datetime objects are not JSON-serialisable by default; convert to ISO strings
    for key in ("lastUpdated", "vehicleLastPolled", "nextPollAt"):
        if d[key] is not None:
            d[key] = d[key].isoformat()
    # asdict() doesn't recurse into deques — convert explicitly for JSON serialisation
    d["history"] = list(d["history"])
    return d

_LOG_TAIL_LINES = 2000  # max lines served by /api/log to bound memory use on large log files

@_webApp.get("/api/log", response_class=fastapi.responses.Response)
async def _webApiLog():
    log_path = _DATA_DIR / "chaos.log"
    if not log_path.exists():
        return fastapi.Response(content="(log file not found)", media_type="text/plain; charset=utf-8")
    lines = log_path.read_text(errors="replace").splitlines()
    content = "\n".join(lines[-_LOG_TAIL_LINES:])
    return fastapi.Response(content=content, media_type="text/plain; charset=utf-8")

@_webApp.get("/pygal-tooltips.min.js", response_class=fastapi.responses.Response)
async def _webPygalJs():
    js = (pathlib.Path(__file__).parent / "pygal-tooltips.min.js").read_bytes()
    return fastapi.Response(content=js, media_type="application/javascript")

@_webApp.post("/api/poll")
async def _webApiPoll(_: None = fastapi.Depends(_checkApiKey)):
    _forcePoll.set()
    return {"status": "ok"}

_CHARGING_MODE_DESC = {
    ChargingMode.ENABLED:  "charging enabled",
    ChargingMode.MANUAL:   "charging set to manual (monitor only)",
    ChargingMode.DISABLED: "charging disabled",
}

@_webApp.post("/api/charging")
async def _webApiSetCharging(body: dict, _: None = fastapi.Depends(_checkApiKey)):
    global _chargingMode
    try:
        mode = ChargingMode(body.get("mode"))
    except ValueError:
        raise fastapi.HTTPException(status_code=400, detail="'mode' must be one of: enabled, manual, disabled")
    if mode != _chargingMode:
        _chargingMode = mode
        _dashboard.chargingMode = mode
        desc = _CHARGING_MODE_DESC[mode]
        log.info("Charging mode set to %s by user", mode.value)
        if _dashboard.history:
            prev = _dashboard.history[-1].get("action", "")
            _dashboard.history[-1]["action"] = desc + (" • " + prev if prev else "")
        await asyncio.to_thread(_writeChargingModeToConfig, mode)
        _forcePoll.set()  # apply immediately
    return {"ok": True, "chargingMode": _chargingMode}

@_webApp.post("/api/powerwall")
async def _webApiSwitchPowerwall(body: dict, _: None = fastapi.Depends(_checkApiKey)):
    global _pendingPowerwallName
    name = body.get("name")
    if not name:
        raise fastapi.HTTPException(status_code=400, detail="Missing 'name'")
    if name == _dashboard.activePowerwall:
        return {"ok": True}  # already active — no switch needed
    _pendingPowerwallName = name
    _switchPowerwall.set()
    _forcePoll.set()  # wake the sleep loop so the switch happens immediately
    return {"ok": True}

@_webApp.get("/api/charts/power.svg")
async def _webChartPower():
    result = _chartLongHistory()
    if result is None:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    active_hist, labels = result
    active = _dashboard.activePowerwall
    n = len(active_hist)
    datasets: list[tuple[str, list, dict] | tuple[str, list]] = [
        ("Solar",       [round(h["solarWatts"] / 1000, 2) for h in active_hist]),
        ("Home Load",   [round(max(0.0, h["homeWatts"] - (h.get("evChargingKw") or 0) * 1000) / 1000, 2) for h in active_hist]),
        ("Lucid Charging", [h.get("evChargingKw", 0) or 0     for h in active_hist]),
    ]
    for siteName, site in ((sn, s) for sn, s in _allSites.items() if sn != active):
        pad = _paddedSiteHistory(site.longHistory, n)
        datasets.append((f"{_DASHED_PREFIX}{siteName} Solar",     [round(h["solarWatts"] / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_SOLAR_COLOR}))
        datasets.append((f"{_DASHED_PREFIX}{siteName} Home Load", [round(h["homeWatts"]  / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_HOME_COLOR}))
    svg = _buildChartSvg(pygal.Line, "Power (24h)", "kW", datasets, labels)
    return fastapi.Response(content=svg, media_type="image/svg+xml")

@_webApp.get("/api/charts/power-hour.svg")
async def _webChartPowerHour():
    result = _chartActiveHistory()
    if result is None:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    all_hist, all_labels = result
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    pairs = [(h, l) for h, l in zip(all_hist, all_labels)
             if datetime.fromisoformat(h["timestamp"]) >= cutoff]
    if len(pairs) < 2:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    active_hist, labels = zip(*pairs)
    active_hist, labels = list(active_hist), list(labels)
    active = _dashboard.activePowerwall
    n = len(active_hist)
    datasets: list[tuple[str, list, dict] | tuple[str, list]] = [
        ("Solar",       [round(h["solarWatts"] / 1000, 2) for h in active_hist]),
        ("Home Load",   [round(max(0.0, h["homeWatts"] - (h.get("evChargingKw") or 0) * 1000) / 1000, 2) for h in active_hist]),
        ("Lucid Charging", [h.get("evChargingKw", 0) or 0     for h in active_hist]),
    ]
    for siteName, site in ((sn, s) for sn, s in _allSites.items() if sn != active):
        pad = _paddedSiteHistory(site.history, n)
        datasets.append((f"{_DASHED_PREFIX}{siteName} Solar",     [round(h["solarWatts"] / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_SOLAR_COLOR}))
        datasets.append((f"{_DASHED_PREFIX}{siteName} Home Load", [round(h["homeWatts"]  / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_HOME_COLOR}))
    svg = _buildChartSvg(pygal.Line, "Power (Last Hour)", "kW", datasets, labels)
    return fastapi.Response(content=svg, media_type="image/svg+xml")

@_webApp.get("/api/charts/soc.svg")
async def _webChartSoc():
    result = _chartLongHistory()
    if result is None:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    active_hist, labels = result
    active = _dashboard.activePowerwall
    n = len(active_hist)
    datasets: list[tuple[str, list, dict] | tuple[str, list]] = [
        ("Powerwall", [h["powerwallSocPercent"] for h in active_hist]),
        ("Lucid", [h.get("evSocPercent") for h in active_hist]),
    ]
    colors = ["#38bdf8", "#4ade80"]  # sky=Powerwall, green=EV (matches power chart)
    # One series per Tesla vehicle seen in history (ordered by first appearance).
    # Keys are VINs (stable across renames); label with the current name, falling
    # back to the key itself so legacy name-keyed entries and removed vehicles
    # still render.
    teslaKeys = list(dict.fromkeys(key for h in active_hist for key in (h.get("teslaSoc") or ())))
    teslaNames = {v["vin"]: v["name"] for v in _dashboard.teslaVehicles}
    for key, color in zip(teslaKeys, itertools.cycle(_TESLA_CHART_COLORS)):
        datasets.append((teslaNames.get(key, key), [soc.get(key) if (soc := h.get("teslaSoc")) else None for h in active_hist]))
        colors.append(color)
    for siteName, site in ((sn, s) for sn, s in _allSites.items() if sn != active):
        pad = _paddedSiteHistory(site.longHistory, n)
        datasets.append((f"{_DASHED_PREFIX}{siteName} PW", [h["powerwallSocPercent"] if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_PW_COLOR}))
    svg = _buildChartSvg(
        pygal.Line, "State of Charge (24h)", "%",
        datasets,
        labels,
        y_range=(0, 100),
        colors=tuple(colors),
    )
    return fastapi.Response(content=svg, media_type="image/svg+xml")


async def sendWebhook(url, payload):
    if not url:
        return
    # Google Chat webhooks only accept fields defined in their Message proto.
    # Strip all keys except "text" when targeting a Google Chat webhook URL.
    # Fall back to a JSON dump if the payload has no "text" key.
    # Use parsed hostname (not substring match) to avoid false positives on URLs
    # where "chat.googleapis.com" appears in the path or query string.
    host = (urlparse(url).hostname or "").lower()
    if host == "chat.googleapis.com" or host.endswith(".chat.googleapis.com"):
        text = payload.get("text") or json.dumps(payload)
        payload = {"text": text}
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.post(url, json=payload, timeout=10)
            if response.status_code == 429:
                await asyncio.sleep(_WEBHOOK_RETRY_DELAY_SECS)
                response = await client.post(url, json=payload, timeout=10)
            if not response.is_success:
                log.warning(f"[Webhook] Returned {response.status_code}: {response.text[:200]}")
    except Exception as e:
        log.warning(f"[Webhook] Delivery failed: {e}")


def fireWebhook(url, payload):
    """Dispatch a webhook notification without blocking the poll cycle."""
    if url:
        asyncio.create_task(sendWebhook(url, payload))


class ChargingAction(Enum):
    NONE = auto()        # no change needed
    START = auto()       # start charging at targetAmps
    STOP = auto()        # stop charging
    ADJUST = auto()      # update current limit to targetAmps
    HOLD_MIN = auto()    # surplus low but in cooldown after start — hold at minChargingAmps
    HOLD_STOP = auto()   # surplus low but confirming before stop — counting consecutive cycles
    SKIP_START = auto()  # surplus ok but in cooldown after stop — defer start
    OBSERVE = auto()     # externally-started charge; allowExternalChargeInterference=False


@dataclass
class ChargingDecision:
    action: ChargingAction
    targetAmps: int
    reason: str


# Maps each ChargingAction to a dashboard Action-column label.
# ADJUST is excluded — it uses decision.reason directly (e.g. "10A → 12A").
_ACTION_DESC: dict[ChargingAction, str] = {
    ChargingAction.NONE:       "",
    ChargingAction.START:      "start",
    ChargingAction.STOP:       "stop",
    ChargingAction.HOLD_MIN:   "hold min",
    ChargingAction.SKIP_START: "skip start",
    ChargingAction.OBSERVE:    "observing",
    # ADJUST and HOLD_STOP use decision.reason directly for per-cycle detail messages
}


def _decideChargingAction(
    isCharging: bool,
    canCharge: bool,
    targetAmps: int,
    prevEvChargingAmps: int,
    thresholds: ChargingThresholds,
    inCooldown: bool,
    lastCommandWasStart: bool,
    cooldownRemainingDesc: str,   # human-readable remaining time string for log messages
    pwState: PowerwallState,
    effectiveSurplusKw: float,
    evSocPercent: float,
    externalSession: bool = False,  # True when car was found charging without CHAOS having started it
    chargingMode: ChargingMode = ChargingMode.ENABLED,  # DISABLED blocks/stops; MANUAL monitors only
    lowSurplusCycles: int = 0,      # consecutive cycles of insufficient surplus while charging (stop-confirm guard)
) -> ChargingDecision:
    """Pure function: given current state, return the charging action to take.
    Contains no I/O — fully unit-testable without mocking Lucid or Powerwall."""
    if chargingMode == ChargingMode.DISABLED:
        if isCharging:
            return ChargingDecision(ChargingAction.STOP, 0, "charging disabled by user")
        return ChargingDecision(ChargingAction.NONE, prevEvChargingAmps, "charging disabled")

    if chargingMode == ChargingMode.MANUAL:
        return ChargingDecision(ChargingAction.NONE, prevEvChargingAmps, "manual mode — observing only")

    if isCharging:
        if externalSession and not thresholds.allowExternalChargeInterference:
            return ChargingDecision(
                ChargingAction.OBSERVE,
                prevEvChargingAmps,
                "externally-started charge — not interfering",
            )
        if externalSession and inCooldown and not lastCommandWasStart:
            return ChargingDecision(
                ChargingAction.OBSERVE,
                prevEvChargingAmps,
                f"externally-started charge — deferring takeover, within stop cooldown ({cooldownRemainingDesc} remaining)",
            )
        if targetAmps < thresholds.minChargingAmps:
            if inCooldown and lastCommandWasStart:
                return ChargingDecision(
                    ChargingAction.HOLD_MIN,
                    thresholds.minChargingAmps,
                    f"surplus low ({effectiveSurplusKw:+.2f}kW → {targetAmps}A) within cooldown ({cooldownRemainingDesc} remaining)",
                )
            if lowSurplusCycles < thresholds.stopConfirmCycles - 1:
                return ChargingDecision(
                    ChargingAction.HOLD_STOP,
                    thresholds.minChargingAmps,
                    f"surplus low ({effectiveSurplusKw:+.2f}kW → {targetAmps}A) confirming stop ({lowSurplusCycles + 1}/{thresholds.stopConfirmCycles} cycles)",
                )
            return ChargingDecision(
                ChargingAction.STOP,
                0,
                f"effective surplus {effectiveSurplusKw:+.2f}kW → {targetAmps}A < {thresholds.minChargingAmps}A minimum",
            )
        if pwState.powerwallSocPercent < thresholds.minPowerwallSocPercent:
            return ChargingDecision(
                ChargingAction.STOP,
                0,
                f"Powerwall {pwState.powerwallSocPercent:.1f}% < {thresholds.minPowerwallSocPercent}% minimum",
            )
        if evSocPercent >= thresholds.targetEvChargePercent:
            return ChargingDecision(
                ChargingAction.STOP,
                0,
                f"EV at {evSocPercent:.1f}% >= {thresholds.targetEvChargePercent}% target",
            )
        if abs(targetAmps - prevEvChargingAmps) >= 2:
            return ChargingDecision(ChargingAction.ADJUST, targetAmps, f"{prevEvChargingAmps}A → {targetAmps}A")
        return ChargingDecision(ChargingAction.NONE, prevEvChargingAmps, "")

    if canCharge:
        if inCooldown and not lastCommandWasStart:
            return ChargingDecision(
                ChargingAction.SKIP_START,
                0,
                f"surplus sufficient ({effectiveSurplusKw:+.2f}kW → {targetAmps}A) within cooldown after stop ({cooldownRemainingDesc} remaining)",
            )
        if (
            targetAmps >= thresholds.minChargingAmps
            and pwState.powerwallSocPercent >= thresholds.minPowerwallSocPercent
            and evSocPercent < thresholds.targetEvChargePercent
        ):
            return ChargingDecision(ChargingAction.START, targetAmps, "solar surplus available")

    return ChargingDecision(ChargingAction.NONE, prevEvChargingAmps, "")


async def pollVehicleAndDecide(
    lucid,
    pwState: PowerwallState,
    thresholds: ChargingThresholds,
    webhookUrl: str,
    prevState: VehicleState | None = None,
) -> VehicleState:
    vehicles = await lucid.fetch_vehicles()
    if not vehicles:
        log.error("[Lucid] No vehicles returned from API; cannot make charging decision.")
        raise RuntimeError("No vehicles available from Lucid API")
    vehicle = vehicles[0]
    evSocPercent = vehicle.state.battery.charge_percent
    chargeState = vehicle.state.charging.charge_state
    _dashboard.vehicleRangeKm = vehicle.state.battery.remaining_range
    _dashboard.vehicleOdometerKm = vehicle.state.chassis.odometer_km
    _dashboard.vehicleSoftwareVersion = vehicle.state.chassis.software_version

    prevEvChargingAmps: int = prevState.evChargingAmps if prevState else 0
    lastCommandTime: datetime | None = prevState.lastCommandTime if prevState else None
    lastCommandWasStart: bool = prevState.lastCommandWasStart if prevState else False
    prevLowSurplusCycles: int = prevState.lowSurplusCycles if prevState else 0

    now = datetime.now(timezone.utc)
    _dashboard.vehicleLastPolled = now
    cooldownSecs = thresholds.commandCooldownMinutes * 60
    commandAgeSecs = (now - lastCommandTime).total_seconds() if lastCommandTime else float("inf")  # inf → inCooldown=False when no command has ever been issued
    inCooldown = commandAgeSecs < cooldownSecs
    cooldownRemainingDesc = f"{cooldownSecs - commandAgeSecs:.0f}s"

    # Detect externally-started charging session: car is charging but CHAOS didn't set any amps.
    # Guard against false positives immediately after we issue a STOP — the Lucid API can lag
    # a few cycles before reflecting the stopped state, so prevEvChargingAmps is temporarily 0
    # while the API still reports CHARGING. During this window, hold state and wait for
    # the API to confirm the stop rather than re-issuing commands.
    recentStop = commandAgeSecs < _STOP_LAG_SECS and not lastCommandWasStart
    isCharging = chargeState in CHARGING_STATES
    canCharge = chargeState in CHARGEABLE_STATES

    if recentStop and isCharging:
        log.info(
            f"API still reporting CHARGING after STOP command ({cooldownRemainingDesc} remaining in cooldown) — "
            f"EV={evSocPercent:.1f}%  Charge={ChargeState.Name(chargeState)}  [awaiting confirmation]"
        )
        return VehicleState(
            evSocPercent=evSocPercent,
            chargeState=chargeState,
            evChargingAmps=0,
            lastCommandTime=lastCommandTime,
            lastCommandWasStart=False,
        )

    externalSession = isCharging and prevEvChargingAmps == 0
    externalSessionDesc = ""
    if externalSession:
        actualAmps = vehicle.state.charging.active_session_ac_current_limit or 0
        if not thresholds.allowExternalChargeInterference:
            externalSessionDesc = "observing"
        elif inCooldown and not lastCommandWasStart:
            externalSessionDesc = "observing (stop cooldown)"
        else:
            externalSessionDesc = "taking over"
        log.info(f"External charge session detected at {actualAmps}A — {externalSessionDesc}")
        prevEvChargingAmps = actualAmps

    # Cancel out the EV's own load from homeWatts so surplus reflects only household load.
    # pw.home() includes EV charging, so without this correction we'd stop charging
    # immediately after starting because the apparent surplus would collapse.
    evChargingKw = prevEvChargingAmps * thresholds.chargerVoltage / 1000
    effectiveSurplusKw = pwState.surplusKw + evChargingKw

    # Target amps: floored to whole amps, clamped to [0, maxChargingAmps], and
    # hard-capped to floor(solarWatts / chargerVoltage) so we never command the
    # EV to draw more than the panels are currently producing.
    maxSolarAmps = int(pwState.solarWatts / thresholds.chargerVoltage)
    targetAmps = max(0, min(thresholds.maxChargingAmps, maxSolarAmps, int(effectiveSurplusKw * 1000 / thresholds.chargerVoltage)))

    log.info(
        f"Solar={pwState.solarWatts:.0f}W  Home={pwState.homeWatts:.0f}W  "
        f"Surplus={pwState.surplusKw:+.2f}kW  EffSurplus={effectiveSurplusKw:+.2f}kW  "
        f"PW={pwState.powerwallSocPercent:.1f}%  EV={evSocPercent:.1f}%  "
        f"Charge={ChargeState.Name(chargeState)}  Target={targetAmps}A"
    )

    evChargingAmps = prevEvChargingAmps  # updated below when we issue commands

    decision = _decideChargingAction(
        isCharging, canCharge, targetAmps, prevEvChargingAmps,
        thresholds, inCooldown, lastCommandWasStart, cooldownRemainingDesc,
        pwState, effectiveSurplusKw, evSocPercent,
        externalSession=externalSession,
        chargingMode=_chargingMode,
        lowSurplusCycles=prevLowSurplusCycles,
    )

    # Advance the stop-confirm counter: increment on HOLD_STOP, reset on everything else.
    newLowSurplusCycles = prevLowSurplusCycles + 1 if decision.action == ChargingAction.HOLD_STOP else 0

    def statePayload(event, reason, amps: int):
        emoji = "⚡" if "started" in event else ("🛑" if "stopped" in event else "⚙️")
        text = (
            f"{emoji} CHAOS: {event.replace('_', ' ').title()}\n"
            f"{reason}\n"
            f"Solar: {round(pwState.solarWatts)}W  Home: {round(pwState.homeWatts)}W  "
            f"Surplus: {pwState.surplusKw:+.2f}kW  "
            f"PW: {round(pwState.powerwallSocPercent, 1)}%  "
            f"EV: {round(evSocPercent, 1)}%  Charging: {amps}A"
        )
        return {
            "text": text,
            "event": event,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "solarWatts": round(pwState.solarWatts),
            "homeWatts": round(pwState.homeWatts),
            "surplusKw": round(pwState.surplusKw, 2),
            "effectiveSurplusKw": round(effectiveSurplusKw, 2),
            "powerwallSocPercent": round(pwState.powerwallSocPercent, 1),
            "evSocPercent": round(evSocPercent, 1),
            "evChargingAmps": amps,
        }

    if decision.action == ChargingAction.STOP:
        log.info(f"Stopping charge: {decision.reason}")
        await lucid.stop_charging(vehicle)
        pwState.resetHomeHistory()   # home load drops by ~EV draw; stale readings now skew avg
        chargeState = ChargeState.CHARGE_STATE_CHARGING_STOPPED
        evChargingAmps = 0
        lastCommandTime = now
        lastCommandWasStart = False
        fireWebhook(webhookUrl, statePayload("charging_stopped", decision.reason, 0))

    elif decision.action == ChargingAction.HOLD_MIN:
        log.info(f"Holding at minimum — {decision.reason}")
        if prevEvChargingAmps != thresholds.minChargingAmps:
            await lucid.set_ac_current_limit(vehicle, thresholds.minChargingAmps)
            evChargingAmps = thresholds.minChargingAmps

    elif decision.action == ChargingAction.HOLD_STOP:
        log.info(f"Holding at minimum — {decision.reason}")
        if prevEvChargingAmps != thresholds.minChargingAmps:
            await lucid.set_ac_current_limit(vehicle, thresholds.minChargingAmps)
            evChargingAmps = thresholds.minChargingAmps

    elif decision.action == ChargingAction.ADJUST:
        log.info(f"Adjusting charge current: {decision.reason}")
        await lucid.set_ac_current_limit(vehicle, decision.targetAmps)
        evChargingAmps = decision.targetAmps

    elif decision.action == ChargingAction.SKIP_START:
        log.info(f"Skipping start — {decision.reason}")

    elif decision.action == ChargingAction.OBSERVE:
        log.info(f"Observing external charge — {decision.reason}")

    elif decision.action == ChargingAction.START:
        log.info(
            f"Starting charge at {decision.targetAmps}A: surplus={pwState.surplusKw:+.2f}kW, "
            f"PW={pwState.powerwallSocPercent:.1f}%, EV={evSocPercent:.1f}%"
        )
        if not lucid.vehicle_is_awake(vehicle):
            log.info("Waking vehicle before charging...")
            await lucid.wakeup_vehicle(vehicle)
            deadline = now + timedelta(seconds=60)
            while datetime.now(timezone.utc) < deadline:
                await asyncio.sleep(5)
                vehicles = await lucid.fetch_vehicles()
                if not vehicles:
                    log.warning("[Lucid] fetch_vehicles() returned empty list during wake-up; retrying.")
                    continue
                vehicle = vehicles[0]
                if lucid.vehicle_is_awake(vehicle):
                    log.info("Vehicle is awake.")
                    break
            else:
                log.warning("[Lucid] Vehicle did not wake within 60s; proceeding anyway.")
        await lucid.set_charge_limit(vehicle, int(thresholds.targetEvChargePercent))
        await lucid.set_ac_current_limit(vehicle, decision.targetAmps)
        await lucid.start_charging(vehicle)
        pwState.resetHomeHistory()   # home load rises by ~EV draw; stale readings now skew avg
        chargeState = ChargeState.CHARGE_STATE_ESTABLISHING_SESSION
        evChargingAmps = decision.targetAmps
        lastCommandTime = now
        lastCommandWasStart = True
        fireWebhook(webhookUrl, statePayload("charging_started", decision.reason, decision.targetAmps))

    # Car is not actively charging and we didn't just start it — clear the cached commanded
    # amps so the dashboard doesn't show stale EV load (e.g. after the vehicle self-terminates
    # at CHARGING_END_OK without CHAOS issuing a STOP command).
    if not isCharging and decision.action != ChargingAction.START:
        if prevEvChargingAmps > 0:
            pwState.resetHomeHistory()   # home load dropped; stale readings now skew avg
        evChargingAmps = 0

    actionDesc = decision.reason if decision.action in (ChargingAction.NONE, ChargingAction.ADJUST, ChargingAction.HOLD_STOP) else _ACTION_DESC.get(decision.action, "")
    if externalSession:
        actionDesc = f"ext session — {externalSessionDesc}"
    return VehicleState(evSocPercent=evSocPercent, chargeState=chargeState, evChargingAmps=evChargingAmps, lastCommandTime=lastCommandTime, lastCommandWasStart=lastCommandWasStart, lastActionDesc=actionDesc, lowSurplusCycles=newLowSurplusCycles)


def _chargeStateName(chargeState) -> str:
    """Return the protobuf enum name for a ChargeState value."""
    try:
        return ChargeState.Name(int(chargeState))
    except Exception:
        log.warning("Unknown ChargeState value: %r", chargeState)
        return str(chargeState)


def _updateDashboard(
    dashState: "DashboardState",
    cachedState: "VehicleState | None",
    thresholds: "ChargingThresholds",
    skipReason: "str | None",
) -> None:
    """Merge EV data into the last history entry for this cycle.

    ``_pollAllSites`` already appended a PW-only entry to ``dashState.history``
    (via the history alias on the active site).  This function extends that entry
    with EV-specific fields and updates the dashboard EV card fields.
    """
    dashState.lastUpdated = datetime.now(timezone.utc)
    dashState.chargingMode = _chargingMode
    if cachedState is not None:
        dashState.evSocPercent = round(cachedState.evSocPercent, 1)
        dashState.chargeStateName = _chargeStateName(cachedState.chargeState)
        dashState.evChargingAmps = cachedState.evChargingAmps
    dashState.evChargingKw = round(dashState.evChargingAmps * thresholds.chargerVoltage / 1000, 2)
    if dashState.history:
        dashState.history[-1].update({
            "evSocPercent": dashState.evSocPercent if cachedState is not None else None,
            "chargeStateName": dashState.chargeStateName,
            "evChargingAmps": dashState.evChargingAmps,
            "evChargingKw": dashState.evChargingKw,
            "action": skipReason or (cachedState.lastActionDesc if cachedState else ""),
        })


def _vehicleModelName(vehicle) -> str:
    """Format a human-readable model string from the vehicle config enums."""
    from lucidmotors.gen.vehicle_state_service_pb2 import Model, ModelVariant
    try:
        model = Model.Name(vehicle.config.model).replace("MODEL_", "").replace("_", " ").title()
    except Exception:
        log.warning("Unknown Model value: %r", vehicle.config.model)
        model = str(vehicle.config.model)
    try:
        variant = ModelVariant.Name(vehicle.config.variant).replace("MODEL_VARIANT_", "").replace("_", " ").title()
    except Exception:
        log.warning("Unknown ModelVariant value: %r", vehicle.config.variant)
        variant = str(vehicle.config.variant)
    return f"{model} {variant}".strip()


def _vehicleStateFromProto(vehicle) -> "VehicleState":
    """Build a VehicleState snapshot from a Lucid vehicle proto object."""
    return VehicleState(
        evSocPercent=vehicle.state.battery.charge_percent,
        chargeState=vehicle.state.charging.charge_state,
    )


def _shouldForceEarlyEvPoll(
    history: collections.deque,
    cachedState: "VehicleState | None",
    thresholds: "ChargingThresholds",
) -> bool:
    """Return True when a home-load spike suggests the EV just plugged in and started charging.

    Conditions (all must be true):
    - At least two history entries exist (needs a previous reading to compare)
    - A cached EV state is available (startup guard — avoids first-cycle false positives)
    - The EV was unplugged at the last poll (cable not connected)
    - Home load rose by ≥ minChargingAmps × chargerVoltage since the previous cycle
    - Not currently in post-command cooldown
    """
    if len(history) < 2 or cachedState is None:
        return False
    if not _isUnplugged(cachedState.chargeState):
        return False
    inCooldown = (
        cachedState.lastCommandTime is not None
        and (datetime.now(timezone.utc) - cachedState.lastCommandTime).total_seconds()
        < thresholds.commandCooldownMinutes * 60
    )
    if inCooldown:
        return False
    homeRiseKw = (history[-1]["homeWatts"] - history[-2]["homeWatts"]) / 1000
    return homeRiseKw >= thresholds.minSurplusKw


async def processCycle(
    lucid,
    thresholds: ChargingThresholds,
    webhookUrl: str,
    lucidConfig: dict,
    cachedState: VehicleState | None,
    dashState: DashboardState | None = None,
    forceEvPoll: bool = False,
    pwState: PowerwallState | None = None,
    siteName: str = "",
) -> tuple[VehicleState | None, int]:
    """Run one poll cycle. Returns (updated cachedState, pollIntervalFactor).

    ``pwState`` must be a ``PowerwallState`` already updated this cycle by
    ``_pollAllSites`` — ``processCycle`` does not read Powerwall data itself.
    Pass ``None`` (or omit) to get a fresh instance — useful in tests.
    ``siteName`` is used as a log prefix to disambiguate multi-site output.
    """
    pollIntervalFactor = 1
    if pwState is None:
        pwState = PowerwallState()
    pfx = f"[{siteName}] " if siteName else ""

    # Refresh Lucid auth token before it expires (~5 min window)
    if lucid.session_time_remaining.total_seconds() < 60:
        log.info("Refreshing Lucid auth token...")
        try:
            await lucid.authentication_refresh()
        except APIError as e:
            log.warning(f"[Lucid] Auth refresh failed ({e}), re-logging in...")
            await _lucidLoginWithRetry(lucid, lucidConfig)

    # --- Decide whether to poll Lucid ---
    # Always poll if currently charging (may need to stop), on first cycle, forced by
    # user, or while charging is disabled — CHAOS must keep checking for an
    # externally-started session it needs to stop; the outcome is only "predetermined"
    # (safe to skip) for the surplus/SoC/dark conditions below, which are about whether
    # CHAOS would additionally choose to *start* a charge. Manual mode never stops
    # anything, so it uses the same skip cadence as normal enabled operation.
    isActivelyCharging = cachedState is not None and cachedState.chargeState in CHARGING_STATES
    skipReason = None
    if not isActivelyCharging and not forceEvPoll and _chargingMode != ChargingMode.DISABLED:
        if pwState.isDark:
            skipReason = "dark"
            pollIntervalFactor = _INTERVAL_DARK
        elif pwState.surplusKw < thresholds.minSurplusKw:
            skipReason = "insufficient surplus"
        elif pwState.powerwallSocPercent < thresholds.minPowerwallSocPercent:
            skipReason = "PW SoC below minimum"
            pollIntervalFactor = _INTERVAL_PW_LOW
        elif cachedState is not None and cachedState.evSocPercent >= thresholds.targetEvChargePercent:
            skipReason = "EV at target"
            pollIntervalFactor = _INTERVAL_EV_DONE

    if skipReason:
        log.info(
            f"{pfx}Solar={pwState.solarWatts:.0f}W (avg={pwState.avgSolarWatts:.0f}W)  "
            f"Home={pwState.homeWatts:.0f}W  "
            f"Surplus={pwState.surplusKw:+.2f}kW  PW={pwState.powerwallSocPercent:.1f}%  "
            f"[skipped: {skipReason}]"
        )
    else:
        prevCommandTime = cachedState.lastCommandTime if cachedState else None
        cachedState = await _lucidCallWithRetry(
            lambda: pollVehicleAndDecide(lucid, pwState, thresholds, webhookUrl, cachedState),
            prefix=pfx.strip(),
        )
        # After issuing a start or stop, wait longer before re-polling so the vehicle
        # has time to settle and we don't immediately reverse the command.
        assert cachedState is not None
        if cachedState.lastCommandTime != prevCommandTime:
            pollIntervalFactor = max(pollIntervalFactor, _INTERVAL_AFTER_COMMAND)

    if dashState is not None:
        _updateDashboard(dashState, cachedState, thresholds, skipReason)

    return cachedState, pollIntervalFactor


# Battery PackagePartNumber prefix → model name. NOTE: the gateway's DIN is NOT
# usable here — it identifies the Backup Gateway (e.g. 1232100 = Gateway 2), not
# the batteries. Unknown prefixes are logged and show no model.
_PW_BATTERY_MODELS = {
    "1092170": "Powerwall 2",
    "2012170": "Powerwall 2",
    "3012170": "Powerwall 2",
    "1850000": "Powerwall+",
    "2850000": "Powerwall+",
    "1707000": "Powerwall 3",
}


def _readPowerwallInfo(pw) -> tuple[str | None, str | None]:
    """Best-effort static site info: (battery model summary, gateway firmware version).
    Synchronous pypowerwall calls — run inside asyncio.to_thread. Never raises."""
    model = version = None
    try:
        blocks = (pw.system_status() or {}).get("battery_blocks") or []
        names = []
        for block in blocks:
            part = (block.get("PackagePartNumber") or "").split("-")[0]
            name = _PW_BATTERY_MODELS.get(part)
            if name:
                names.append(name)
            elif part:
                log.info(f"[Powerwall] Unknown battery part number '{block.get('PackagePartNumber')}' — please report")
        if names:
            unique = list(dict.fromkeys(names))
            if len(unique) > 1:
                model = " + ".join(unique)  # mixed installation
            else:
                model = unique[0] if len(names) == 1 else f"{len(names)}× {unique[0]}"
    except Exception as e:
        log.debug(f"[Powerwall] Battery info read failed: {e}")
    try:
        version = (pw.version() or "").split(" ")[0] or None  # strip the build hash
    except Exception as e:
        log.debug(f"[Powerwall] Version read failed: {e}")
    return model, version


async def _connectPowerwall(pwConfig: dict, name: str) -> pypowerwall.Powerwall:
    """Connect to a Powerwall and return the connected instance."""
    authType = pwConfig["authType"]
    authKwargs = (
        {"password": pwConfig["password"]}
        if authType == "local"
        else {"gw_pwd": pwConfig["password"]} # for TEDAPI auth
    )
    cacheFile = pwConfig.get("cachefile", powerwallCacheFile(name, pwConfig["host"]))
    log.info(f"Connecting to Powerwall '{name}' (authType={authType})...")
    pw = pypowerwall.Powerwall(
        host=pwConfig["host"],
        email=pwConfig["email"],
        timezone=pwConfig["timezone"],
        cachefile=cacheFile,
        retry_modes=True,
        **authKwargs,
    )
    # is_connected() validates the cached auth token without re-authenticating.
    # On failure (LoginError or connection error) fall back to connect() for a
    # full re-authentication cycle.
    try:
        await asyncio.wait_for(asyncio.to_thread(pw.is_connected), timeout=_CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        raise PowerwallConnectionError(f"Timeout connecting to Powerwall '{name}' after {_CONNECT_TIMEOUT:.0f}s")
    except (LoginError, PowerwallConnectionError):
        try:
            await asyncio.wait_for(asyncio.to_thread(pw.connect), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise PowerwallConnectionError(f"Timeout reconnecting to Powerwall '{name}' after {_CONNECT_TIMEOUT:.0f}s")
    log.info(f"Powerwall '{name}' connected.")
    return pw


def _longHistoryPath(siteName: str) -> pathlib.Path:
    return _DATA_DIR / f"long_history_{siteName}.json"


def _saveLongHistory(site: "SiteInfo") -> None:
    try:
        path = _longHistoryPath(site.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(site.longHistory)))
    except Exception as e:
        log.warning(f"[{site.name}] Failed to save long history: {e}")


def _loadLongHistory(site: "SiteInfo") -> None:
    path = _longHistoryPath(site.name)
    if not path.exists():
        return
    try:
        entries = json.loads(path.read_text())
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_LONG_HISTORY_MAXLEN * _LONG_HISTORY_INTERVAL_SECS)).isoformat()
        fresh = [e for e in entries if isinstance(e, dict) and e.get("timestamp", "") >= cutoff]
        site.longHistory.extend(fresh)
        if fresh:
            site.lastLongHistoryTime = datetime.fromisoformat(fresh[-1]["timestamp"])
        log.info(f"[{site.name}] Restored {len(fresh)} long history entries from {path.name}")
    except Exception as e:
        log.warning(f"[{site.name}] Failed to load long history: {e}")


def _writePowerwallToConfig(newName: str, path: str = "config.json") -> None:
    """Update activePowerwall in config.json."""
    try:
        p = pathlib.Path(path)
        data = json.loads(p.read_text())
        data["activePowerwall"] = newName
        p.write_text(json.dumps(data, indent=2))
        log.info(f"Updated config.json: activePowerwall = '{newName}'")
    except Exception as e:
        log.warning(f"Failed to write activePowerwall to config: {e}")


def _writeChargingModeToConfig(mode: ChargingMode, path: str = "config.json") -> None:
    """Persist the user's charging mode so it survives a process restart
    (otherwise a restart while charging is disabled/manual silently re-enables it)."""
    try:
        p = pathlib.Path(path)
        data = json.loads(p.read_text())
        data.setdefault("charging", {})["chargingMode"] = mode.value
        p.write_text(json.dumps(data, indent=2))
        log.info(f"Updated config.json: charging.chargingMode = {mode.value}")
    except Exception as e:
        log.warning(f"Failed to write chargingMode to config: {e}")


def _freshTeslaChargingKw(site: "SiteInfo") -> float:
    """Summed charging power of awake Teslas whose data aligns with this site's
    last poll within _TESLA_ATTR_WINDOW_S. 0.0 when none, stale, or malformed
    (display-only heuristic — must never break the poll loop)."""
    if not _dashboard.teslaConfigured or not site.history:
        return 0.0
    try:
        sitePolled = datetime.fromisoformat(site.history[-1]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    total = 0.0
    for v in _dashboard.teslaVehicles:
        if v["asleep"] or not v["lastPolled"] or not v["chargingKw"] or v["chargingKw"] <= 0:
            continue
        try:
            fresh = abs((datetime.fromisoformat(v["lastPolled"]) - sitePolled).total_seconds()) <= _TESLA_ATTR_WINDOW_S
        except (TypeError, ValueError):
            continue
        if fresh:
            total += v["chargingKw"]
    return total


def _lucidChargingHere(isActive: bool) -> bool:
    """True when this site is being used for Lucid charging: commanded/external
    amps, or the API still reports a charging state during the stop-lag window."""
    return isActive and (_dashboard.evChargingKw > 0 or _dashboard.chargeStateName in _CHARGING_STATE_NAMES)


def _siteTeslaCandidateKw(site: "SiteInfo", isActive: bool) -> float:
    """Tesla charging power this site COULD be hosting (attribution ENTRY check).

    A Tesla charging via its own Charge-on-Solar hides inside Home Load while
    holding surplus near zero. A site is a candidate when the fresh charging
    total plausibly fits its readings and the site isn't busy Lucid-charging.
    0.0 when any condition fails. Callers pick at most ONE candidate site and
    then HOLD it for the session (see _attributedTeslaSite) — these strict
    conditions identify where charging started, not whether it continues."""
    if _lucidChargingHere(isActive):
        return 0.0
    total = _freshTeslaChargingKw(site)
    if total <= 0:
        return 0.0
    if site.pwState.homeWatts / 1000 < total + _TESLA_ATTR_HOME_MARGIN_KW:
        return 0.0  # home load can't plausibly contain the draw plus a baseline
    if abs(_rawSurplusKw(site.pwState.solarWatts, site.pwState.homeWatts)) > _TESLA_ATTR_SURPLUS_BAND_KW:
        return 0.0  # missing the near-zero-surplus charge-on-solar signature
    return total


def _teslaPollDelayS(teslaPollIntervalS: float) -> float:
    """Tesla poll delay: site cadence while any Tesla is awake and charging
    (keeps charging attribution fresh), else the configured interval."""
    if any(not v["asleep"] and (v["chargingKw"] or 0) > 0 for v in _dashboard.teslaVehicles):
        return min(_TESLA_CHARGING_POLL_INTERVAL_S, teslaPollIntervalS)
    return teslaPollIntervalS


_teslaAttrSiteName: str | None = None  # site currently holding Tesla-charging attribution


def _attributedTeslaSite(allSites: dict, activeName: str) -> tuple[str | None, float]:
    """Pick at most ONE site to attribute Tesla charging to (a Tesla can only be
    in one place), with session hysteresis: strict signature conditions gate
    ENTRY, but once attributed the site HOLDS the attribution until the Tesla
    actually stops charging (or data goes stale / the Lucid takes the site) —
    household transients like appliance spikes must not drop a live session."""
    global _teslaAttrSiteName
    if _teslaAttrSiteName is not None:
        site = allSites.get(_teslaAttrSiteName)
        holdKw = _freshTeslaChargingKw(site) if site is not None else 0.0
        if site is None:
            reason = "site no longer exists"
        elif holdKw <= 0:
            reason = "no fresh charging data"
        elif _lucidChargingHere(_teslaAttrSiteName == activeName):
            reason = "Lucid charging on site"
        elif site.pwState.homeWatts / 1000 < holdKw:
            reason = "home load below Tesla draw"
        else:
            return _teslaAttrSiteName, holdKw
        log.info(f"[Tesla] Releasing charging attribution from site '{_teslaAttrSiteName}' ({reason})")
        _teslaAttrSiteName = None
    # Entry: among candidate sites, the one with the highest home load is
    # likeliest to host the draw — a coincidental candidate only carries its own
    # baseline while the hosting site carries baseline plus the multi-kW draw.
    best: str | None = None
    bestKw = 0.0
    for name, site in allSites.items():
        kw = _siteTeslaCandidateKw(site, name == activeName)
        if kw > 0 and (best is None or site.pwState.homeWatts > allSites[best].pwState.homeWatts):
            best, bestKw = name, kw
    if best is not None:
        _teslaAttrSiteName = best
        log.info(f"[Tesla] Attributing {bestKw:.1f}kW charging to site '{best}'")
    return best, bestKw


def _buildSiteReadings(allSites: dict, activeName: str) -> list:
    """Build the per-site reading list stored in DashboardState.siteReadings."""
    teslaSite, teslaKw = _attributedTeslaSite(allSites, activeName)
    return [
        {
            "name": site.name,
            "isActive": site.name == activeName,
            "solarWatts": site.pwState.solarWatts,
            "homeWatts": site.pwState.homeWatts,
            "surplusKw": _rawSurplusKw(site.pwState.solarWatts, site.pwState.homeWatts),
            "powerwallSocPercent": site.pwState.powerwallSocPercent,
            "error": site.error,
            "model": site.model,
            "firmwareVersion": site.firmwareVersion,
            "lifetimeChargedWh": site.lifetimeChargedWh,
            "lifetimeDischargedWh": site.lifetimeDischargedWh,
            # ISO timestamp of the last successful poll (history only grows on success),
            # so the UI can show per-site data age even while a site is failing.
            "lastPolled": site.history[-1]["timestamp"] if site.history else None,
            "teslaChargingKw": teslaKw if site.name == teslaSite else 0.0,
        }
        for site in allSites.values()
    ]


def _detectSiteSwitch(
    allSites: dict,
    activeName: str,
    cachedState: VehicleState,
    thresholds: ChargingThresholds,
) -> str | None:
    """Return the name of a non-active site if the EV appears to be charging there, else None.

    Conditions (all must hold):
    - EV is confirmed charging at >= minChargingAmps
    - Active site homeWatts < expectedEvPower * _AUTO_SWITCH_LOAD_RATIO  (missing EV load)
    - Exactly one non-active site has homeWatts >= expectedEvPower * _AUTO_SWITCH_LOAD_RATIO
      and no active poll error  (unambiguous candidate)
    """
    if cachedState.chargeState not in CHARGING_STATES:
        return None
    evAmps = cachedState.evChargingAmps
    if evAmps < thresholds.minChargingAmps:
        return None
    expectedEvW = evAmps * thresholds.chargerVoltage
    threshold = expectedEvW * _AUTO_SWITCH_LOAD_RATIO

    activeSite = allSites[activeName]
    if activeSite.pwState.homeWatts >= threshold:
        return None  # Active site load is consistent with hosting the EV

    candidates = [
        name
        for name, site in allSites.items()
        if name != activeName
        and not site.error
        and site.pwState.homeWatts >= threshold
    ]
    return candidates[0] if len(candidates) == 1 else None



def _formatDuration(seconds: float) -> str:
    """Compact outage duration, e.g. '101h 12m' or '4m'."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _siteUnreachablePayload(
    name: str, error: str, consecutiveFailures: int, diagnosis: str | None, downSince: datetime | None,
) -> dict:
    text = (
        f"\U0001F534 CHAOS: Powerwall '{name}' unreachable\n"
        f"{consecutiveFailures} consecutive poll failures. Last error: {error}"
    )
    if diagnosis:
        text += f"\nDiagnosis: {diagnosis}"
    return {
        "text": text,
        "event": "site_unreachable",
        "site": name,
        "consecutiveFailures": consecutiveFailures,
        "error": error,
        "diagnosis": diagnosis,
        "downSince": downSince.isoformat() if downSince else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _siteRecoveredPayload(name: str, downtimeSeconds: float | None) -> dict:
    text = f"\U0001F7E2 CHAOS: Powerwall '{name}' reachable again"
    if downtimeSeconds is not None:
        text += f" after {_formatDuration(downtimeSeconds)}"
    return {
        "text": text,
        "event": "site_recovered",
        "site": name,
        "downtimeSeconds": round(downtimeSeconds) if downtimeSeconds is not None else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _bothFullPayload(siteName: str, evSocPercent: float, powerwallSocPercent: float) -> dict:
    text = (
        f"\U0001F50B CHAOS: EV and Powerwall both full at '{siteName}'\n"
        f"EV {evSocPercent:.1f}%  PW {powerwallSocPercent:.1f}% — surplus solar has nowhere to go"
    )
    return {
        "text": text,
        "event": "ev_and_powerwall_full",
        "site": siteName,
        "evSocPercent": round(evSocPercent, 1),
        "powerwallSocPercent": round(powerwallSocPercent, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _checkBothFullNotify(
    site: SiteInfo,
    evSocPercent: float | None,
    targetEvChargePercent: float,
    powerwallFullSocPercent: float,
    webhookUrl: str,
) -> None:
    """Fire a webhook once when the EV is at its target SoC and the Powerwall
    is full, so the user knows surplus solar is going unused. Re-arms once
    either drops _BOTH_FULL_RESET_MARGIN_PCT below its threshold (e.g.
    overnight Powerwall discharge) — not on small dips like 100 → 99.5 — so
    it fires again next time both are full. Skipped while the site's last
    poll failed, since its readings are stale."""
    if site.error is not None or evSocPercent is None:
        return
    pwSoc = site.pwState.powerwallSocPercent
    if evSocPercent >= targetEvChargePercent and pwSoc >= powerwallFullSocPercent:
        if not site.bothFullNotified:
            fireWebhook(webhookUrl, _bothFullPayload(site.name, evSocPercent, pwSoc))
            site.bothFullNotified = True
    elif (evSocPercent < targetEvChargePercent - _BOTH_FULL_RESET_MARGIN_PCT
          or pwSoc < powerwallFullSocPercent - _BOTH_FULL_RESET_MARGIN_PCT):
        site.bothFullNotified = False


def _recordPollFailure(site: SiteInfo, error: str, diagnosis: str | None = None) -> None:
    """Count a failed poll; fire the unreachable webhook once at the threshold."""
    if site.consecutiveFailures == 0:
        site.firstFailureTime = datetime.now(timezone.utc)
    site.consecutiveFailures += 1
    if site.consecutiveFailures >= _SITE_UNREACHABLE_NOTIFY_CYCLES and not site.unreachableNotified:
        fireWebhook(_webhookUrl, _siteUnreachablePayload(
            site.name, error, site.consecutiveFailures, diagnosis, site.firstFailureTime))
        site.unreachableNotified = True


# TCP connect errors worth explaining: errno → (short label, what it usually means)
_TCP_ERRNO_HINTS = {
    errno.ECONNREFUSED: ("connection refused", "host is up but nothing is listening on 443"),
    errno.EHOSTUNREACH: ("host unreachable", "no ARP reply — device offline, unplugged, or not on this subnet"),
    errno.EHOSTDOWN: ("host down", "no ARP reply — device offline, unplugged, or not on this subnet"),
    errno.ENETUNREACH: ("network unreachable", "no route to that network"),
}


async def _probeSite(host: str) -> tuple[str, str]:
    """Test a gateway's reachability layer by layer, independently of pypowerwall
    (which hides network failures behind None readings). Returns
    (short label for the dashboard, full detail for the log and webhook)."""
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), _PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return ("TCP timeout",
                f"TCP {host}:443 no response in {_PROBE_TIMEOUT_S:.0f}s"
                " — packets silently dropped (host off or not on the network, or firewall/VLAN blocking)")
    except socket.gaierror as e:
        return ("DNS lookup failed", f"DNS lookup of {host} failed: {e}")
    except OSError as e:
        errName = errno.errorcode.get(e.errno or 0, "OSError")
        short, hint = _TCP_ERRNO_HINTS.get(e.errno or 0, (errName, str(e)))
        return (short, f"TCP {host}:443 {short} after {time.monotonic() - start:.1f}s ({errName}: {hint})")
    tcp = f"TCP {host}:443 connected in {(time.monotonic() - start) * 1000:.0f}ms"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    try:
        async with httpx.AsyncClient(verify=False, timeout=_PROBE_TIMEOUT_S) as client:
            r = await client.get(f"https://{host}/api/status")
    except httpx.TimeoutException:
        return ("HTTPS timeout", f"{tcp}; HTTPS /api/status timed out — gateway web server not responding")
    except httpx.HTTPError as e:  # TLS handshake failures surface as ConnectError
        return ("HTTPS failed", f"{tcp}; HTTPS /api/status failed: {type(e).__name__}: {e}")
    status = f"HTTPS /api/status {r.status_code}"
    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            fields = [f"{k}={data[k]}" for k in ("version", "up_time", "din") if data.get(k)]
            if fields:
                status += f" ({', '.join(fields)})"
    return ("gateway reachable",
            f"{tcp}; {status} — gateway answers on the network, so the failure is in the API/auth layer")


async def _handlePollFailure(site: SiteInfo, reason: str, pypowerwallMessage: str | None = None) -> None:
    """Diagnose a failed poll, then log it, show it on the dashboard, and count it."""
    host = (site.pwConfig or {}).get("host")
    short, detail = await _probeSite(host) if host else (None, None)
    site.error = f"{reason} — {short}" if short else reason
    downFor = (datetime.now(timezone.utc) - site.firstFailureTime).total_seconds() if site.firstFailureTime else 0
    parts = [f"Poll failed: {reason}", f"down {_formatDuration(downFor)} ({site.consecutiveFailures + 1} polls)"]
    if pypowerwallMessage:
        parts.append(f"pypowerwall: {pypowerwallMessage}")
    if detail:
        parts.append(f"probe: {detail}")
    log.warning(f"[Powerwall:{site.name}] " + " | ".join(parts))
    _recordPollFailure(site, site.error, detail)


async def _pollOneSite(site: SiteInfo) -> bool:
    """Poll one site's Powerwall state (solar/home/soc). Returns True on success.

    Fires a webhook after _SITE_UNREACHABLE_NOTIFY_CYCLES consecutive failed
    polls, and again once the site recovers. Failures are exceptions
    (network/API errors) and all-zero readings — pypowerwall returns None for
    everything rather than raising when the gateway is offline or login fails.
    Solar-zero transients below don't count; they have their own inline
    retry/recovery.

    A site that failed to connect at startup (pw=None) counts as failed every
    cycle, with a reconnect attempted at most every _SITE_RECONNECT_INTERVAL_S.
    """
    name = site.name
    if site.pw is None:
        now = datetime.now(timezone.utc)
        due = (site.lastReconnectAttempt is None
               or (now - site.lastReconnectAttempt).total_seconds() >= _SITE_RECONNECT_INTERVAL_S)
        if site.pwConfig is not None and due:
            site.lastReconnectAttempt = now
            try:
                site.pw = await _connectPowerwall(site.pwConfig, name)
                site.model, site.firmwareVersion = await asyncio.to_thread(_readPowerwallInfo, site.pw)
            except Exception as e:
                site.pw = None
                await _handlePollFailure(site, f"Reconnect failed: {e}")
                return False
        if site.pw is None:
            _recordPollFailure(site, site.error or "Not connected")
            return False
    pw = site.pw
    try:
        # Lambda groups the synchronous Powerwall calls into one thread invocation
        # so the event loop doesn't pay the thread-pool dispatch overhead per call.
        # battery(verbose=True) reads the same cached aggregates payload and adds
        # the lifetime energy counters.
        # runCapturing also returns pypowerwall's last failure message, which it
        # otherwise logs only at DEBUG before returning None.
        _pw_read = lambda: (pw.solar(), pw.home(), pw.level(True), pw.battery(verbose=True))
        (solar, home, soc, battery), pwMessage = await asyncio.to_thread(_pypowerwallCapture.runCapturing, _pw_read)
        if (solar or 0) == 0:
            if (home or 0) == 0 and (soc or 0) == 0:
                await _handlePollFailure(site, "All-zero reading", pwMessage)
                return False
            if site.pwState.avgSolarWatts > _SOLAR_ZERO_SUSPECT_W:
                # 0W solar with a healthy rolling average suggests a transient read failure
                # (e.g. ConnectionResetError inside pypowerwall). Retry before giving up.
                for attempt in range(_PW_POLL_RETRY_ATTEMPTS):
                    log.warning(
                        f"[Powerwall:{name}] Solar=0W but rolling avg={site.pwState.avgSolarWatts:.0f}W"
                        f" — retrying ({attempt + 1}/{_PW_POLL_RETRY_ATTEMPTS})"
                    )
                    await asyncio.sleep(_PW_POLL_RETRY_DELAY_S)
                    (solar, home, soc, battery), _ = await asyncio.to_thread(_pypowerwallCapture.runCapturing, _pw_read)
                    if (solar or 0) > 0:
                        break  # retry recovered a valid reading
                if (solar or 0) == 0:
                    log.warning(
                        f"[Powerwall:{name}] Solar=0W after {_PW_POLL_RETRY_ATTEMPTS} retries"
                        " — 0W solar transient, skipping solar update"
                    )
                    soc_f = float(soc) if soc is not None else 0.0
                    if soc_f > 0.0:
                        site.pwState.updateSoc(soc_f)
                    return False
        site.pwState.update(float(solar or 0), float(home or 0), float(soc or 0))
        if isinstance(battery, dict):  # lifetime counters are best-effort display data
            # TEDAPI mode has no lifetime counters (pypowerwall stubs them as 0) —
            # treat zero/None as unavailable rather than displaying "0 kWh".
            site.lifetimeChargedWh = battery.get("energy_imported") or None
            site.lifetimeDischargedWh = battery.get("energy_exported") or None
        site.error = None
        if site.consecutiveFailures:
            downtime = ((datetime.now(timezone.utc) - site.firstFailureTime).total_seconds()
                        if site.firstFailureTime else None)
            log.info(f"[Powerwall:{name}] Reachable again after {site.consecutiveFailures} failed polls"
                     + (f" (down {_formatDuration(downtime)})" if downtime is not None else ""))
            if site.unreachableNotified:
                fireWebhook(_webhookUrl, _siteRecoveredPayload(name, downtime))
                site.unreachableNotified = False
        site.consecutiveFailures = 0
        site.firstFailureTime = None
        return True
    except Exception as e:
        await _handlePollFailure(site, f"{type(e).__name__}: {e}")
        return False


async def _pollAllSites(allSites: dict) -> None:
    """Concurrently poll ALL Powerwall sites and append a PW-only history entry
    to every site that polled successfully (including the active site)."""
    coros = [_pollOneSite(site) for site in allSites.values()]
    await asyncio.gather(*coros)
    now_iso = datetime.now(timezone.utc).isoformat()
    # Last-known Tesla SOCs, keyed by VIN (stable across renames), stamped into every
    # fresh entry at construction: all sites' histories carry the series and the value
    # is persisted with the entry rather than mutated in after _saveLongHistory ran.
    teslaSoc = ({v["vin"]: v["socPercent"] for v in _dashboard.teslaVehicles}
                if _dashboard.teslaConfigured else None)
    for site in allSites.values():
        if site.error is None:
            pw = site.pwState
            entry = {
                "timestamp": now_iso,
                "solarWatts": round(pw.solarWatts),
                "homeWatts": round(pw.homeWatts),
                "surplusKw": _rawSurplusKw(pw.solarWatts, pw.homeWatts),
                "powerwallSocPercent": round(pw.powerwallSocPercent, 1),
            }
            if teslaSoc is not None:
                entry["teslaSoc"] = dict(teslaSoc)  # fresh dict per entry; entries are shared with longHistory and persisted
            site.history.append(entry)
            now_dt = datetime.now(timezone.utc)
            if site.lastLongHistoryTime is None or (now_dt - site.lastLongHistoryTime).total_seconds() >= _LONG_HISTORY_INTERVAL_SECS:
                site.longHistory.append(entry)
                site.lastLongHistoryTime = now_dt
                _saveLongHistory(site)


def _teslaReadVehicles(tesla, wantedNames: list[str]) -> dict:
    """Synchronous teslapy reads — must run inside asyncio.to_thread.

    Read-only and never wakes a vehicle: get_vehicle_data() is only called when
    the cheap vehicle-list call reports the vehicle online."""
    if not tesla.authorized:
        # authorized only checks the in-memory token; teslapy re-reads the cache
        # file solely at construction and after fetch/refresh. Reload it here so a
        # token created by --tesla-login while the daemon runs is picked up.
        tesla._token_updater()
        if not tesla.authorized:
            return {"status": "unauthorized"}
    vehicles = tesla.vehicle_list()
    if not vehicles:
        return {"status": "no_vehicles"}
    if wantedNames:
        byName = {v["display_name"]: v for v in vehicles}
        wanted = [byName[n] for n in wantedNames if n in byName]
        if not wanted:
            # Surface the mismatch (typo/rename) instead of silently showing nothing.
            return {"status": "no_match", "accountNames": [v["display_name"] or v["vin"] for v in vehicles]}
        vehicles = wanted
    results = []
    for v in vehicles:
        vin = v["vin"]
        name = v["display_name"] or vin
        if v["state"] != "online":
            results.append({"vin": vin, "name": name, "status": "asleep"})
            continue
        try:
            data = v.get_vehicle_data()
        except Exception:
            # Car fell asleep between the list and data calls (HTTP 408) or a
            # per-vehicle hiccup — report it asleep rather than failing the fleet.
            results.append({"vin": vin, "name": name, "status": "asleep"})
            continue
        cs = data.get("charge_state") or {}
        vs = data.get("vehicle_state") or {}
        rangeMiles = cs.get("battery_range")   # Owner API reports miles regardless of account units
        odoMiles = vs.get("odometer")
        results.append({
            "vin": vin,
            "name": data.get("display_name") or name,
            "status": "online",
            "socPercent": cs.get("battery_level"),
            "chargeStateName": cs.get("charging_state") or "",
            "chargingKw": float(cs.get("charger_power") or 0),
            "rangeKm": round(rangeMiles * _MILES_TO_KM, 1) if rangeMiles is not None else None,
            "chargeLimitPercent": cs.get("charge_limit_soc"),
            "model": _teslaModelName(data.get("vehicle_config") or {}),
            "odometerKm": round(odoMiles * _MILES_TO_KM, 1) if odoMiles is not None else None,
            "softwareVersion": (vs.get("car_version") or "").split(" ")[0],  # strip the build hash
        })
    return {"status": "ok", "vehicles": results}


_teslaFailed = False  # True while Tesla polling is failing; gates WARNING→DEBUG log demotion
# Data fields of a per-vehicle dashboard entry (identity/asleep/lastPolled aside);
# the authoritative schema is the online dict built in _teslaReadVehicles.
_TESLA_VEHICLE_DATA_KEYS = ("socPercent", "chargeStateName", "chargingKw", "rangeKm", "chargeLimitPercent",
                            "model", "odometerKm", "softwareVersion")
# Owner API vehicle_config.car_type → friendly model name (unknown types display raw).
# "lychee"/"tamarind" are Tesla's internal codenames for the refreshed S/X platforms.
_TESLA_MODEL_NAMES = {"models": "Model S", "models2": "Model S", "lychee": "Model S",
                      "model3": "Model 3", "modelx": "Model X", "tamarind": "Model X",
                      "modely": "Model Y", "cybertruck": "Cybertruck"}


def _teslaModelName(vehicleConfig: dict) -> str:
    """Friendly model + trim from vehicle_config, e.g. 'Model X Plaid'."""
    carType = (vehicleConfig.get("car_type") or "").lower()
    model = _TESLA_MODEL_NAMES.get(carType, carType)
    trim = (vehicleConfig.get("trim_badging") or "").lower()
    if not trim:
        return model
    # Performance badges are reported as internal codes (e.g. 'p100d', 'p74d');
    # the marketing name depends on the platform.
    if trim.startswith("p"):
        if carType in ("lychee", "tamarind"):
            return f"{model} Plaid"   # refreshed S/X performance is Plaid
        if carType in ("model3", "modely"):
            return f"{model}P"        # 3/Y performance badge → 'Model 3P'
    # Legacy platforms keep the literal badge: digits all-caps ('p90d' → 'P90D')
    trim = trim.upper() if any(c.isdigit() for c in trim) else trim.capitalize()
    return f"{model} {trim}".strip()


def _teslaPollFailed(message: str) -> None:
    """Mark Tesla unavailable; log loudly on the first failure of a streak only."""
    global _teslaFailed
    (log.debug if _teslaFailed else log.warning)(f"[Tesla] {message}")
    _teslaFailed = True
    _dashboard.teslaAvailable = False
    _dashboard.teslaError = message


async def pollTesla(tesla, teslaConfig: dict) -> None:
    """Poll Tesla vehicles read-only and update _dashboard. Never raises."""
    global _teslaFailed
    try:
        result = await asyncio.to_thread(_teslaReadVehicles, tesla, teslaConfig.get("vehicleNames") or [])
    except Exception as e:
        _teslaPollFailed(f"poll failed: {e}")
        return
    if result["status"] == "unauthorized":
        _teslaPollFailed("not logged in — run: uv run python chaos.py --tesla-login")
        return
    if result["status"] == "no_vehicles":
        _teslaPollFailed("no vehicles on Tesla account")
        return
    if result["status"] == "no_match":
        _teslaPollFailed("no vehicle matched config vehicleNames; account has: " + ", ".join(result["accountNames"]))
        return
    if _teslaFailed:
        log.info("[Tesla] Polling recovered")
        _teslaFailed = False
    _dashboard.teslaAvailable = True
    _dashboard.teslaError = None
    prevByVin = {v["vin"]: v for v in _dashboard.teslaVehicles}
    merged = []
    for r in result["vehicles"]:
        if r["status"] == "asleep":
            prev = prevByVin.get(r["vin"])
            # Keep last-known values (and their lastPolled age) while the car sleeps.
            entry = dict(prev) if prev else {
                "vin": r["vin"], "name": r["name"], "lastPolled": None,
                **{k: None for k in _TESLA_VEHICLE_DATA_KEYS},
            }
            glitches = (prev or {}).get("glitchPolls", 0) + 1
            wasCharging = bool(prev) and not prev.get("asleep") and (prev.get("chargingKw") or 0) > 0
            if wasCharging and glitches <= _TESLA_CHARGING_GRACE_POLLS:
                # Cars don't sleep mid-charge: an Owner-API non-online flap (or a
                # failed data call) while charging is a glitch — keep last-known
                # awake state so attribution and the fast poll cadence survive it.
                entry["glitchPolls"] = glitches
                log.info(f"[Tesla:{entry['name']}] Non-online report while charging — glitch {glitches}/{_TESLA_CHARGING_GRACE_POLLS}, keeping last-known state")
            else:
                entry["asleep"] = True
                entry["glitchPolls"] = 0
        else:
            entry = {k: v for k, v in r.items() if k != "status"}
            entry["asleep"] = False
            entry["glitchPolls"] = 0
            entry["lastPolled"] = datetime.now(timezone.utc).isoformat()
            log.info(f"[Tesla:{r['name']}] SOC={r['socPercent']}%  State={r['chargeStateName']}  Power={r['chargingKw']:.1f}kW")
        merged.append(entry)
    _dashboard.teslaVehicles = merged


async def runChaos(config):
    global _chargingMode, _webhookUrl
    _validateConfig(config)
    #pypowerwall.set_debug(True)
    log.info("CHAOS version %s starting", __version__)

    activeName = config["activePowerwall"]
    lucidConfig = config["lucid"]
    chargingConfig = config["charging"]
    chargingModeStr = chargingConfig.get("chargingMode", "enabled")
    try:
        _chargingMode = ChargingMode(chargingModeStr)
    except ValueError:
        raise RuntimeError(f"Invalid charging.chargingMode '{chargingModeStr}' — must be 'enabled', 'manual', or 'disabled'")
    _dashboard.chargingMode = _chargingMode
    notificationsConfig = config.get("notifications", {})
    pollingIntervalSeconds = config["pollingIntervalSeconds"]
    dashboard_cfg = config.get("dashboard", {})
    units = dashboard_cfg.get("units", "imperial")
    if units not in ("imperial", "metric"):
        raise RuntimeError(f"Invalid units '{units}' in config — must be 'imperial' or 'metric'")

    webhookUrl = notificationsConfig.get("webhookUrl", "") if notificationsConfig.get("enabled") else ""
    _webhookUrl = webhookUrl
    powerwallFullSocPercent = notificationsConfig.get("powerwallFullSocPercent", 100)
    teslaConfig = config.get("tesla") or {}
    tesla = None
    if _teslaEnabled(config):
        _dashboard.teslaConfigured = True
        try:
            # No network I/O here — just a requests session; auth is checked per poll.
            _ensureTeslaCacheFile()
            tesla = teslapy.Tesla(teslaConfig["email"], cache_file=str(_TESLA_CACHE_FILE))
        except Exception as e:
            _dashboard.teslaAvailable = False
            _dashboard.teslaError = str(e)
            log.warning(f"[Tesla] Failed to initialize client: {e}")
    teslaPollIntervalS = teslaConfig.get("pollIntervalSeconds", _TESLA_POLL_INTERVAL_S_DEFAULT)
    thresholds = ChargingThresholds(
        minPowerwallSocPercent=chargingConfig["minPowerwallSocPercent"],
        targetEvChargePercent=chargingConfig["targetEvChargePercent"],
        chargerVoltage=chargingConfig["chargerVoltage"],
        minChargingAmps=chargingConfig["minChargingAmps"],
        maxChargingAmps=chargingConfig["maxChargingAmps"],
        commandCooldownMinutes=chargingConfig["commandCooldownMinutes"],
        stopConfirmCycles=chargingConfig.get("stopConfirmCycles", 4),
        allowExternalChargeInterference=chargingConfig.get("allowExternalChargeInterference", True),
    )

    # Connect all Powerwalls at startup so their data is immediately available.
    allSites: dict[str, SiteInfo] = {}
    for pwConf in config["powerwalls"]:
        name = pwConf["name"]
        try:
            pw = await _connectPowerwall(pwConf, name)
            allSites[name] = SiteInfo(name=name, pw=pw, pwConfig=pwConf)
            allSites[name].model, allSites[name].firmwareVersion = await asyncio.to_thread(_readPowerwallInfo, pw)
        except Exception as e:
            log.warning(f"[Powerwall] Failed to connect to '{name}' at startup: {e}")
            allSites[name] = SiteInfo(name=name, pw=None, error=str(e), pwConfig=pwConf)
        _loadLongHistory(allSites[name])

    if allSites[activeName].pw is None:
        raise RuntimeError(f"Failed to connect to active Powerwall '{activeName}'")
    global _allSites
    _allSites = allSites

    async with LucidAPI() as lucid:
        log.info("Logging in to Lucid API...")
        await _lucidLoginWithRetry(lucid, lucidConfig)
        log.info("Lucid API authenticated.")

        vehicles = await lucid.fetch_vehicles()
        if not vehicles:
            raise RuntimeError("No vehicles found on Lucid account")
        v = vehicles[0]
        log.info(f"Vehicle: {v.config.nickname}")
        _dashboard.vehicleName = v.config.nickname
        _dashboard.vehicleModel = _vehicleModelName(v)
        # Seed dashboard with startup vehicle state so the web UI shows real values
        # immediately, even if the first poll cycle(s) skip the vehicle poll.
        _dashboard.vehicleRangeKm = v.state.battery.remaining_range
        _dashboard.vehicleOdometerKm = v.state.chassis.odometer_km
        _dashboard.vehicleSoftwareVersion = v.state.chassis.software_version
        _dashboard.vehicleLastPolled = datetime.now(timezone.utc)
        # Seed cachedState from the startup vehicle snapshot so skip guards have
        # real data on cycle 1 (e.g. "EV at target" fires correctly from the start).
        cachedState = _vehicleStateFromProto(v)
        _dashboard.evSocPercent = round(cachedState.evSocPercent, 1)
        _dashboard.chargeStateName = _chargeStateName(cachedState.chargeState)

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(
                sig,
                lambda s=sig: (
                    log.info(f"Received {signal.Signals(s).name}, shutting down..."),
                    task.cancel() if task else None,
                ),
            )

        _dashboard.units = units
        _dashboard.ratedRangeMiles = dashboard_cfg.get("ratedRangeMiles")
        _dashboard.targetEvChargePercent = thresholds.targetEvChargePercent
        _dashboard.version = __version__
        _dashboard.activePowerwall = activeName
        _dashboard.powerwallNames = [p["name"] for p in config["powerwalls"]]
        allSites[activeName].history = _dashboard.history  # alias so _pollAllSites and _updateDashboard share the same deque
        _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)

        # Start web UI
        webUiPort = dashboard_cfg.get("port", 8087)
        global _webApiKey
        _webApiKey = dashboard_cfg.get("apiKey", "")
        if webUiPort:
            allowedHosts = dashboard_cfg.get("allowedHosts")
            if allowedHosts:
                # Reject requests whose Host header isn't in the allowlist (400) —
                # defeats DNS-rebinding attacks from browsers on the local network.
                _webApp.add_middleware(TrustedHostMiddleware, allowed_hosts=allowedHosts)
                log.info(f"Host-header validation enabled for: {', '.join(allowedHosts)}")
            server = uvicorn.Server(uvicorn.Config(_webApp, host="0.0.0.0", port=webUiPort, log_level="warning"))
            _serverTask = asyncio.create_task(server.serve())
            log.info(f"Web UI available at http://0.0.0.0:{webUiPort}")

        forcedByUser = False
        forcedEvPollPending = False
        pendingSwitchAnnotation: str | None = None
        nextEvPollDue: datetime = datetime.min.replace(tzinfo=timezone.utc)
        nextTeslaPollDue: datetime = datetime.min.replace(tzinfo=timezone.utc)
        evPollFactor: int = 1

        async def _doSiteSwitch(newName: str, annotation: str) -> None:
            """Update shared state when switching the active site. Caller logs and does extras."""
            nonlocal activeName, pendingSwitchAnnotation
            oldName = activeName
            activeName = newName
            _dashboard.activePowerwall = newName
            _dashboard.history = allSites[newName].history
            pendingSwitchAnnotation = f"{annotation} {oldName}"
            await asyncio.to_thread(_writePowerwallToConfig, newName)

        while True:
            try:
                # Poll ALL sites concurrently; every successful site gets a history entry.
                await _pollAllSites(allSites)
                # Refresh dashboard site data and poll Tesla BEFORE the Lucid decision
                # chain: a processCycle failure re-raises, and these independent
                # sources must not freeze during a Lucid outage.
                _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)
                if tesla is not None and datetime.now(timezone.utc) >= nextTeslaPollDue:
                    await pollTesla(tesla, teslaConfig)
                    # Jitter tolerance: the due time is set ~1s after cycle start (poll
                    # duration), so without it the next cycle arrives fractionally
                    # early and skips a poll, going 120s between reads.
                    delay = max(0.0, _teslaPollDelayS(teslaPollIntervalS) - _TESLA_POLL_JITTER_S)
                    nextTeslaPollDue = datetime.now(timezone.utc) + timedelta(seconds=delay)
                now = datetime.now(timezone.utc)
                evPollDue = now >= nextEvPollDue or forcedByUser or forcedEvPollPending
                if allSites[activeName].error:
                    log.warning(f"[Powerwall:{activeName}] Active site poll failed — skipping decision cycle")
                elif evPollDue:
                    prevCommandTime = cachedState.lastCommandTime if cachedState else None
                    try:
                        cachedState, evPollFactor = await processCycle(lucid, thresholds, webhookUrl, lucidConfig, cachedState, _dashboard, forceEvPoll=forcedByUser or forcedEvPollPending, pwState=allSites[activeName].pwState, siteName=activeName)
                        _dashboard.lucidError = None
                    except Exception as e:
                        # Attribute the failure to the Lucid vehicle for the dashboard,
                        # then let the outer handlers do their usual work (lastError,
                        # webhook, Powerwall reconnect).
                        _dashboard.lucidError = str(e)
                        raise
                    nextEvPollDue = datetime.now(timezone.utc) + timedelta(seconds=pollingIntervalSeconds * evPollFactor)
                    # processCycle succeeded — clear any pending forced EV poll.
                    forcedEvPollPending = False
                    # Annotate the first history entry after a site switch.
                    if pendingSwitchAnnotation and _dashboard.history:
                        prev = _dashboard.history[-1].get("action", "")
                        _dashboard.history[-1]["action"] = pendingSwitchAnnotation + (" • " + prev if prev else "")
                        pendingSwitchAnnotation = None
                    # Check whether a home-load spike suggests the EV just arrived and plugged in.
                    # If so, skip the sleep and force an immediate EV poll next cycle.
                    if _shouldForceEarlyEvPoll(allSites[activeName].history, cachedState, thresholds):
                        active_hist = allSites[activeName].history
                        homeRiseKw = (active_hist[-1]["homeWatts"] - active_hist[-2]["homeWatts"]) / 1000
                        log.info(
                            f"[{activeName}] Home load spike: +{homeRiseKw:.2f}kW "
                            f"(≥{thresholds.minSurplusKw:.2f}kW threshold) — EV was unplugged, forcing immediate poll"
                        )
                        forcedEvPollPending = True
                        _forcePoll.set()
                    # Auto-detect site switch: if EV is charging but the active site's home load
                    # doesn't reflect the expected EV draw, check if a non-active site fits better.
                    # Skip detection immediately after a command (load may not have settled yet).
                    if len(allSites) > 1 and cachedState is not None:
                        if cachedState.lastCommandTime != prevCommandTime:
                            _autoSwitchConsecutiveCount.clear()
                        else:
                            candidate = _detectSiteSwitch(allSites, activeName, cachedState, thresholds)
                            if candidate:
                                _autoSwitchConsecutiveCount[candidate] = _autoSwitchConsecutiveCount.get(candidate, 0) + 1
                                count = _autoSwitchConsecutiveCount[candidate]
                                log.info(
                                    f"[{activeName}] Auto-switch candidate '{candidate}': "
                                    f"cycle {count}/{_AUTO_SWITCH_CYCLES_REQUIRED} "
                                    f"(active home={allSites[activeName].pwState.homeWatts:.0f}W, "
                                    f"candidate home={allSites[candidate].pwState.homeWatts:.0f}W, "
                                    f"expected EV≥{cachedState.evChargingAmps * thresholds.chargerVoltage:.0f}W)"
                                )
                                if count >= _AUTO_SWITCH_CYCLES_REQUIRED:
                                    log.warning(
                                        f"Auto-switching active site '{activeName}' → '{candidate}' "
                                        f"after {_AUTO_SWITCH_CYCLES_REQUIRED} consecutive mismatch cycles"
                                    )
                                    oldName = activeName  # capture before _doSiteSwitch updates it
                                    allSites[candidate].pwState.resetHomeHistory()
                                    await _doSiteSwitch(candidate, "🔄")
                                    fireWebhook(
                                        webhookUrl,
                                        {
                                            "text": f"🔄 CHAOS auto-switched active site: '{oldName}' → '{candidate}'",
                                            "event": "auto_site_switch",
                                            "from": oldName,
                                            "to": candidate,
                                            "timestamp": datetime.now(timezone.utc).isoformat(),
                                        },
                                    )
                                    _autoSwitchConsecutiveCount.clear()
                            else:
                                _autoSwitchConsecutiveCount.clear()
                _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)  # again: isActive may have changed via auto-switch
                _checkBothFullNotify(
                    allSites[activeName],
                    cachedState.evSocPercent if cachedState else None,
                    thresholds.targetEvChargePercent,
                    powerwallFullSocPercent,
                    webhookUrl,
                )
                _dashboard.lastError = None
            except asyncio.CancelledError:
                log.info("Shutting down.")
                return
            except (LoginError, PowerwallConnectionError) as e:
                # Transient Powerwall auth/connection failure — reconnect silently.
                log.warning(f"[Powerwall] Connection error: {e} — reconnecting...")
                _dashboard.lastError = str(e)
                try:
                    active_pw = allSites[activeName].pw
                    assert active_pw is not None
                    await asyncio.to_thread(active_pw.connect)
                    log.info("Powerwall reconnected.")
                    _dashboard.lastError = None
                except Exception as reconnectError:
                    log.warning(f"[Powerwall] Reconnect failed: {reconnectError}")
            except Exception as e:
                log.error(f"Poll cycle error: {e}", exc_info=True)
                _dashboard.lastError = str(e)
                fireWebhook(
                    webhookUrl,
                    {
                        "text": f"⚠️ CHAOS Error: {e}",
                        "event": "error",
                        "message": str(e),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )

            try:
                isDark = allSites[activeName].pwState.isDark
                pwSleep = _PW_POLLING_INTERVAL_S * (_INTERVAL_DARK if isDark else 1)
                evSleepRemaining = max(0.0, (nextEvPollDue - datetime.now(timezone.utc)).total_seconds())
                sleepSeconds = min(pwSleep, evSleepRemaining)
                _dashboard.nextPollAt = datetime.now(timezone.utc) + timedelta(seconds=sleepSeconds)
                await asyncio.wait_for(_forcePoll.wait(), timeout=sleepSeconds)
                log.info("Poll cycle triggered by user request.")
                forcedByUser = True
                nextEvPollDue = datetime.min.replace(tzinfo=timezone.utc)  # force EV poll on next cycle
                nextTeslaPollDue = datetime.min.replace(tzinfo=timezone.utc)  # refresh Tesla too
            except asyncio.TimeoutError:
                forcedByUser = False
            finally:
                _forcePoll.clear()

            # Check for a pending Powerwall switch between poll cycles
            if _switchPowerwall.is_set():
                _switchPowerwall.clear()
                global _pendingPowerwallName
                newName = _pendingPowerwallName
                _pendingPowerwallName = None
                newPwConfig = next((p for p in config["powerwalls"] if p["name"] == newName), None)
                if newPwConfig and newName and newName != _dashboard.activePowerwall:
                    log.info(f"Switching active Powerwall to '{newName}'…")
                    try:
                        site = allSites[newName]
                        if site.pw is not None:
                            log.info(f"Reusing existing connection to '{newName}'.")
                        else:
                            # Startup connection failed — attempt reconnect now.
                            site.pw = await _connectPowerwall(newPwConfig, newName)
                            site.model, site.firmwareVersion = await asyncio.to_thread(_readPowerwallInfo, site.pw)
                            site.error = None
                        await _doSiteSwitch(newName, "⇄")
                        forcedEvPollPending = True
                        log.info(f"Active Powerwall switched to '{newName}'.")
                    except Exception as switchErr:
                        log.warning(f"[Powerwall] Failed to switch to '{newName}': {switchErr}")
                else:
                    log.warning(f"[Powerwall] Unknown site name '{newName}' — ignoring switch request")

def teslaInteractiveLogin(config) -> None:
    """One-time interactive Tesla OAuth handshake; populates _TESLA_CACHE_FILE.

    Interactive CLI mode (not the daemon), so print/input are used deliberately.
    fetch_token must run on the same Tesla instance that generated the URL —
    the PKCE verifier and state only live in that instance's memory."""
    teslaCfg = config.get("tesla") or {}
    email = teslaCfg.get("email")
    if not email:
        raise SystemExit("config.json needs a 'tesla' block with 'email' before login")
    _ensureTeslaCacheFile()
    tesla = teslapy.Tesla(email, cache_file=str(_TESLA_CACHE_FILE))
    if tesla.authorized:
        print(f"Already authorized; token cache at {_TESLA_CACHE_FILE} is valid.")
        return
    # teslapy's redirect_uri (tesla://auth/callback) is the only one Tesla still
    # registers for the ownerapi client — the classic https://auth.tesla.com/void/
    # callback is rejected with "redirect_uri not registered". The tesla:// link
    # can't render in a browser, so the user copies it from the address bar.
    authUrl = tesla.authorization_url()
    assert isinstance(authUrl, str), "Tesla authorization_url() returned an unexpected type"
    # The state in the URL identifies this handshake; used as fallback when the
    # user pastes a bare code that carries no state of its own.
    expectedState = (parse_qs(urlparse(authUrl).query).get("state") or [""])[0]
    print("Open this URL in a browser (any machine) and log in to your Tesla account:")
    print()
    print(authUrl)
    print()
    print("After signing in, the final redirect targets the Tesla app, so the browser")
    print("shows an error page or gets stuck on 'Verified Successfully' / 'Loading…' —")
    print("that is expected. Get the authorization code one of these ways:")
    print(f"  1. If the address bar now shows {tesla.redirect_uri}?code=... — copy that full URL.")
    print("  2. If the address bar did NOT change (stuck on Loading…): repeat the login in")
    print("     Firefox — its error page always shows the tesla:// URL to copy.")
    print("  3. Or use DevTools: open Network tab, enable 'Preserve log', log in again,")
    print("     click the last request to auth.tesla.com (status 302) and copy the")
    print("     'location' response header — or paste just the code value from it.")
    for _ in range(3):
        try:
            redirected = input("Paste the redirect URL (or just the code) here: ").strip()
        except EOFError:
            raise SystemExit("Aborted — no input.")
        responseUrl = _teslaAuthResponseUrl(redirected, tesla.redirect_uri, expectedState)
        if responseUrl is None:
            print("No authorization code found in that. Paste the tesla://auth/callback?code=...")
            print("URL (or the bare code value), not the login-page URL.")
            continue
        try:
            tesla.fetch_token(authorization_response=responseUrl)
        except Exception as e:
            raise SystemExit(
                f"Token exchange failed: {e}\n"
                "Note: complete the login using the URL printed by THIS run — "
                "a code from an earlier attempt is bound to that run and won't work."
            )
        print(f"Token saved to {_TESLA_CACHE_FILE} — treat this file like a password.")
        return
    raise SystemExit("No valid redirect URL provided after 3 attempts.")


def main():
    config = loadConfig()
    if "--tesla-login" in sys.argv:
        teslaInteractiveLogin(config)
        return
    try:
        asyncio.run(runChaos(config))
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
