#!/usr/bin/env python3
# Run with:
#   source .venv/bin/activate
#   python3 chaos.py

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
import itertools
import json
import logging
import pathlib
import signal
import requests
from urllib.parse import urlparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any, Sequence

import pypowerwall
from pypowerwall.local.exceptions import LoginError, PowerwallConnectionError
from lucidmotors import LucidAPI, ChargeState  # type: ignore[attr-defined]
from lucidmotors.exceptions import APIError
import fastapi
import pygal
import uvicorn

__version__ = "0.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chaos.log"),
    ],
)
log = logging.getLogger(__name__)

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

# Poll-interval multipliers (applied as a factor on pollingIntervalSeconds)
_INTERVAL_DARK = 10           # No solar production — check infrequently
_INTERVAL_PW_LOW = 5          # Powerwall SoC too low — wait for recharge
_INTERVAL_EV_DONE = 60        # EV at target — rarely changes while idle
_INTERVAL_AFTER_COMMAND = 3   # Let vehicle settle after a start/stop command
_CONNECT_TIMEOUT = 15.0       # Seconds before a Powerwall connection attempt times out


def loadConfig(path="config.json"):
    with open(path) as f:
        return json.load(f)


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

    require(config["lucid"], ["username", "password"], "config['lucid']")
    require(
        config["charging"],
        ["minPowerwallSocPercent", "targetEvChargePercent", "chargerVoltage",
         "minChargingAmps", "maxChargingAmps", "commandCooldownMinutes"],
        "config['charging']",
    )


def powerwallCacheFile(activeName: str, host: str) -> str:
    # Build a stable, filesystem-safe cache filename per gateway.
    raw = f"{activeName}-{host}"
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in raw)
    return f".powerwall-{safe}"


class PowerwallState:
    """Holds the latest Powerwall reading and maintains a rolling solar average.

    The constructor mirrors the old dataclass signature so existing call-sites
    and tests that build a one-shot instance require no changes.  A persistent
    instance updated via ``update()`` accumulates history across poll cycles so
    that ``surplusKw`` and ``isDark`` automatically reflect the smoothed value.
    """

    _HISTORY_SIZE = 3

    def __init__(
        self,
        solarWatts: float = 0,
        homeWatts: float = 0,
        powerwallSocPercent: float = 0,
    ) -> None:
        self._solarHistory: collections.deque = collections.deque(maxlen=self._HISTORY_SIZE)
        self.solarWatts: float = max(0, solarWatts or 0)
        self.homeWatts: float = homeWatts or 0
        self.powerwallSocPercent: float = powerwallSocPercent or 0
        # Seed history only when non-zero values are provided (i.e. one-shot test/mock
        # instances).  A persistent instance created with no args starts with an empty
        # history; the first update() call will populate it.
        if self.solarWatts > 0:
            self._solarHistory.append(self.solarWatts)

    def update(self, solarWatts: float, homeWatts: float, powerwallSocPercent: float) -> None:
        """Record a new Powerwall reading and advance the rolling solar average."""
        self.solarWatts = max(0, solarWatts or 0)
        self.homeWatts = homeWatts or 0
        self.powerwallSocPercent = powerwallSocPercent or 0
        self._solarHistory.append(self.solarWatts)
        if abs(self.avgSolarWatts - self.solarWatts) > 50:
            log.info(
                f"Solar rolling avg: {self.avgSolarWatts:.0f}W  "
                f"(raw: {self.solarWatts:.0f}W  over {len(self._solarHistory)} sample(s))"
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
    def surplusKw(self) -> float:
        """Smoothed solar minus home load in kW; positive = excess available for EV."""
        return (self.avgSolarWatts - self.homeWatts) / 1000

    @property
    def isDark(self) -> bool:
        """True when smoothed solar production is negligible (< 50W)."""
        return self.avgSolarWatts < 50

    def __repr__(self) -> str:
        return (
            f"PowerwallState(solar={self.solarWatts:.0f}W"
            f" avg={self.avgSolarWatts:.0f}W"
            f" home={self.homeWatts:.0f}W"
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


@dataclass
class DashboardState:
    lastUpdated: datetime | None = None
    evSocPercent: float | None = None
    chargeStateName: str = "unknown"
    evChargingAmps: int = 0
    evChargingKw: float = 0.0
    lastError: str | None = None
    vehicleName: str = ""
    vehicleModel: str = ""
    vehicleRangeKm: float | None = None   # remaining_range from Lucid proto (km)
    vehicleOdometerKm: float | None = None
    vehicleLastPolled: datetime | None = None  # last time pollVehicleAndDecide ran
    vehicleSoftwareVersion: str = ""
    ratedRangeMiles: float | None = None  # from config; enables range card gradient fill
    units: str = "imperial"               # "imperial" (mi) or "metric" (km)
    version: str = ""                     # set once at startup from __version__
    nextPollAt: datetime | None = None    # when the next backend poll cycle is expected to run
    activePowerwall: str = ""            # name of currently active Powerwall
    powerwallNames: list[str] = field(default_factory=list)  # all configured Powerwall names
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=120))
    siteReadings: list = field(default_factory=list)  # per-site Powerwall data for dashboard; see _buildSiteReadings


_dashboard = DashboardState()
_allSites: dict = {}  # dict[str, SiteInfo]; set in runChaos; read by chart endpoints
_NON_ACTIVE_SOLAR_COLOR = "rgba(255, 240, 100, 0.85)"  # light yellow — deemphasized variant of active Solar (#fbbf24)
_NON_ACTIVE_HOME_COLOR  = "rgba(56, 189, 248, 0.45)"   # dimmed sky   — matches active Home Load (#38bdf8)
_NON_ACTIVE_PW_COLOR    = "rgba(255, 240, 100, 0.85)"  # light yellow — matches non-active Solar color for visual consistency
_DASHED_PREFIX          = "╌╌ "                        # legend label prefix indicating a dashed line series
_DASHED_STYLE           = {"stroke_style": {"dasharray": "8 3", "width": 2.5}}
_PYGAL_DEFAULT_COLORS   = ("#fbbf24", "#38bdf8", "#4ade80", "#a78bfa")
_CHART_NO_DATA = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
    b'<text x="50%" y="50%" fill="#64748b" text-anchor="middle" font-family="system-ui">'
    b'No data yet</text></svg>'
)
# Inter-task signals: set by FastAPI POST handlers, cleared and consumed by the main poll loop.
_forcePoll = asyncio.Event()       # /api/poll — trigger an immediate poll cycle
_switchPowerwall = asyncio.Event() # /api/powerwall — switch the active Powerwall site
_pendingPowerwallName: str | None = None
_webApiKey: str = ""  # optional; if non-empty, POST endpoints require X-API-Key header


@dataclass
class SiteInfo:
    """Bundles all per-site Powerwall information into one object."""
    name: str
    pw: pypowerwall.Powerwall | None  # None if startup connection failed
    pwState: PowerwallState = field(default_factory=PowerwallState)
    error: str | None = None
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=120))


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
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.8em;font-weight:600}
  .g{background:#166534;color:#bbf7d0}.s{background:#374151;color:#9ca3af}
  .error-bar{background:#7f1d1d;color:#fca5a5;padding:10px 16px;border-radius:8px;margin-bottom:16px;font-size:.85em}
  h2{color:#94a3b8;font-size:.75em;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
  table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden;font-size:.82em}
  th{background:#334155;padding:8px 10px;text-align:left;font-size:.72em;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;white-space:nowrap}
  td{padding:7px 10px;border-bottom:1px solid #0f172a}
  tr:last-child td{border-bottom:none}
  .pos{color:#4ade80}.neg{color:#f87171}.muted{color:#94a3b8}
  .vehicle-info{color:#cbd5e1;font-size:.85em;margin-bottom:16px;min-height:1.2em}
  .vehicle-info .vname{font-weight:600;color:#f1f5f9}
  .vehicle-info .vsep{color:#475569;margin:0 6px}
  .vehicle-info .vmuted{color:#64748b;font-size:.9em}
  .footer{margin-top:24px;color:#334155;font-size:.72em;text-align:right}
  .hi{color:#fbbf24}
  .poll-btn{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:6px;padding:4px 12px;font-size:.8em;cursor:pointer;margin-left:10px;vertical-align:middle}
  .poll-btn:hover{background:#334155;color:#e2e8f0}
  .poll-btn:disabled{opacity:.5;cursor:default}
  .chart-section{margin-bottom:24px}
  .chart-section h2{margin-bottom:8px}
  .chart-wrap{background:#1e293b;border-radius:10px;padding:4px;border:1px solid #334155;margin-bottom:10px;max-width:50%}
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
<button class="poll-btn" id="pollBtn" onclick="forcePoll()">Update now</button>
</div>
<div class="tagline"><span class="hi">CH</span>arging <span class="hi">A</span>utomatically <span class="hi">O</span>n <span class="hi">S</span>olar</div>
<div class="subtitle" id="sub">Loading…</div>
<div class="vehicle-info" id="vinfo"></div>
<div id="err" class="error-bar" style="display:none"></div>
<div id="siteRows"></div>
<div class="card-group-label">Electric Vehicle</div>
<div class="cards">
  <div class="card" id="card-ev"><div class="card-label">EV Battery</div><div class="card-value" id="ev">—</div></div>
  <div class="card" id="card-range"><div class="card-label">Range</div><div class="card-value" id="range">—</div></div>
  <div class="card"><div class="card-label">Charge State</div><div class="card-value" id="cs">—</div></div>
  <div class="card"><div class="card-label">Charging</div><div class="card-value" id="amps">—</div></div>
</div>
<h2>History</h2>
<div class="chart-section">
  <div class="chart-wrap" id="chartPower"></div>
  <div class="chart-wrap" id="chartSoc"></div>
</div>
<h2>Recent Cycles</h2>
<table><thead><tr>
  <th>Time</th><th>Solar</th><th>Home</th><th>Surplus</th><th>PW%</th><th>EV%</th><th>State</th><th>Amps</th><th>Action</th>
</tr></thead><tbody id="hist"></tbody></table>
<script>
function badge(s){
  if(!s||s==='unknown')return '<span class="badge s">—</span>';
  const n=s.replace('CHARGE_STATE_','').replace(/_/g,' ');
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
function fillBg(id,pct,color){
  const el=document.getElementById(id);
  if(!el)return;
  const p=Math.max(0,Math.min(100,pct||0));
  el.style.background='linear-gradient(to right,'+color+' '+p+'%,transparent '+p+'%)';
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
    const vi=document.getElementById('vinfo');
    if(d.vehicleName||d.vehicleModel){
      const age=ageFmt(d.vehicleLastPolled);
      const odo=d.vehicleOdometerKm!=null?(imperial?Math.round(d.vehicleOdometerKm/1.60934).toLocaleString()+' mi':Math.round(d.vehicleOdometerKm).toLocaleString()+' km'):null;
      const fw=d.vehicleSoftwareVersion?'fw '+d.vehicleSoftwareVersion:null;
      let html=(d.vehicleName?'<span class="vname">'+escHtml(d.vehicleName)+'</span>':'');
      if(d.vehicleName&&d.vehicleModel)html+='<span class="vsep">·</span>';
      if(d.vehicleModel)html+=escHtml(d.vehicleModel);
      if(odo)html+='<span class="vsep">·</span><span class="vmuted">'+odo+'</span>';
      if(fw)html+='<span class="vsep">·</span><span class="vmuted">fw '+escHtml(d.vehicleSoftwareVersion)+'</span>';
      if(age)html+='<span class="vsep">·</span><span class="vmuted">EV data: '+age+'</span>';
      vi.innerHTML=html;
    }

    // Build one row per Powerwall site
    const siteRows=document.getElementById('siteRows');
    siteRows.innerHTML='';
    const multiSite=d.siteReadings&&d.siteReadings.length>1;
    (d.siteReadings||[]).forEach(site=>{
      const isActive=site.isActive;
      const btnHtml=multiSite
        ?'<button class="activate-btn'+(isActive?' active':'')+'" data-name="'+escHtml(site.name)+'"'+(isActive?' disabled':'')+' onclick="switchPowerwall(this.dataset.name)">'+(isActive?'Active ✓':'Use for charging')+'</button>'
        :'';
      const hdr='<div class="site-row-header">'
        +'<span class="card-group-label" style="margin:0">'+escHtml(site.name)+'</span>'
        +btnHtml+'</div>';
      const errHtml=site.error?'<div class="site-row-error">⚠ '+escHtml(site.error)+'</div>':'';
      const cardId='card-pw-'+site.name.replace(/\\s+/g,'_');
      const cards='<div class="cards">'
        +'<div class="card"><div class="card-label">Solar</div><div class="card-value">'+kwFmt(site.solarWatts)+'</div></div>'
        +'<div class="card"><div class="card-label">Home Load</div><div class="card-value">'+kwFmt(site.homeWatts)+'</div></div>'
        +'<div class="card"><div class="card-label">Surplus</div><div class="card-value">'+surpFmt(site.surplusKw)+'</div></div>'
        +'<div class="card" id="'+cardId+'"><div class="card-label">Powerwall</div><div class="card-value">'+(site.powerwallSocPercent!=null?site.powerwallSocPercent.toFixed(1)+'%':'—')+'</div></div>'
        +(isActive&&d.evChargingKw>0?'<div class="card"><div class="card-label">EV Charging</div><div class="card-value">'+kwFmt(d.evChargingKw*1000)+'</div></div>':'')
        +'</div>';
      siteRows.insertAdjacentHTML('beforeend',hdr+errHtml+cards);
      fillBg(cardId,site.powerwallSocPercent,'rgba(56,189,248,0.22)');
    });

    document.getElementById('ev').textContent=d.evSocPercent!=null?d.evSocPercent+'%':'—';
    const rangeFmt=d.vehicleRangeKm!=null?(imperial?Math.round(d.vehicleRangeKm/1.60934)+' mi':Math.round(d.vehicleRangeKm)+' km'):'—';
    document.getElementById('range').textContent=rangeFmt;
    document.getElementById('cs').innerHTML=badge(d.chargeStateName);
    document.getElementById('amps').textContent=d.evChargingAmps>0?d.evChargingAmps+'A':'—';
    fillBg('card-ev',d.evSocPercent,'rgba(74,222,128,0.22)');
    if(d.ratedRangeMiles!=null&&d.vehicleRangeKm!=null)
      fillBg('card-range',d.vehicleRangeKm/(d.ratedRangeMiles*1.60934)*100,'rgba(251,191,36,0.22)');
    const eb=document.getElementById('err');
    if(d.lastError){eb.style.display='';eb.textContent='⚠ '+d.lastError;}
    else eb.style.display='none';
    function _injectSvg(id,s){const el=document.getElementById(id);el.innerHTML='';el.appendChild(document.createRange().createContextualFragment(s));}
    const t=Date.now();
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
        +'<td>'+kwFmt(h.homeWatts)+'</td>'
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
<div class="footer" id="ver"></div>
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

def _paddedSiteHistory(site: "SiteInfo", n: int) -> list:
    """Return the site's history padded with None on the left to length n."""
    hist = list(site.history)[-n:]
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

@_webApp.get("/pygal-tooltips.min.js", response_class=fastapi.responses.Response)
async def _webPygalJs():
    js = (pathlib.Path(__file__).parent / "pygal-tooltips.min.js").read_bytes()
    return fastapi.Response(content=js, media_type="application/javascript")

@_webApp.post("/api/poll")
async def _webApiPoll(_: None = fastapi.Depends(_checkApiKey)):
    _forcePoll.set()
    return {"status": "ok"}

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
    result = _chartActiveHistory()
    if result is None:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    active_hist, labels = result
    active = _dashboard.activePowerwall
    n = len(active_hist)
    datasets: list[tuple[str, list, dict] | tuple[str, list]] = [
        ("Solar",       [round(h["solarWatts"] / 1000, 2) for h in active_hist]),
        ("Home Load",   [round(h["homeWatts"]  / 1000, 2) for h in active_hist]),
        ("EV Charging", [h.get("evChargingKw", 0) or 0     for h in active_hist]),
    ]
    for siteName, site in ((sn, s) for sn, s in _allSites.items() if sn != active):
        pad = _paddedSiteHistory(site, n)
        datasets.append((f"{_DASHED_PREFIX}{siteName} Solar",     [round(h["solarWatts"] / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_SOLAR_COLOR}))
        datasets.append((f"{_DASHED_PREFIX}{siteName} Home Load", [round(h["homeWatts"]  / 1000, 2) if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_HOME_COLOR}))
    svg = _buildChartSvg(pygal.Line, "Power", "kW", datasets, labels)
    return fastapi.Response(content=svg, media_type="image/svg+xml")

@_webApp.get("/api/charts/soc.svg")
async def _webChartSoc():
    result = _chartActiveHistory()
    if result is None:
        return fastapi.Response(content=_CHART_NO_DATA, media_type="image/svg+xml")
    active_hist, labels = result
    active = _dashboard.activePowerwall
    n = len(active_hist)
    datasets: list[tuple[str, list, dict] | tuple[str, list]] = [
        ("Powerwall", [h["powerwallSocPercent"] for h in active_hist]),
        ("EV Battery", [h.get("evSocPercent") for h in active_hist]),
    ]
    for siteName, site in ((sn, s) for sn, s in _allSites.items() if sn != active):
        pad = _paddedSiteHistory(site, n)
        datasets.append((f"{_DASHED_PREFIX}{siteName} PW", [h["powerwallSocPercent"] if h else None for h in pad], {**_DASHED_STYLE, "color": _NON_ACTIVE_PW_COLOR}))
    svg = _buildChartSvg(
        pygal.Line, "State of Charge", "%",
        datasets,
        labels,
        y_range=(0, 100),
        colors=("#38bdf8", "#4ade80"),  # sky=Powerwall, green=EV (matches power chart)
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
        # requests is used intentionally here — a single fire-and-forget POST
        # doesn't warrant adding an async httpx dependency.
        response = await asyncio.to_thread(requests.post, url, json=payload, timeout=10)
        if not response.ok:
            log.warning(f"Webhook returned {response.status_code}: {response.text[:200]}")
    except Exception as e:
        log.warning(f"Webhook delivery failed: {e}")


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
    SKIP_START = auto()  # surplus ok but in cooldown after stop — defer start
    OBSERVE = auto()     # externally-started charge; allowExternalChargeInterference=False


@dataclass
class ChargingDecision:
    action: ChargingAction
    targetAmps: int
    reason: str


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
) -> ChargingDecision:
    """Pure function: given current state, return the charging action to take.
    Contains no I/O — fully unit-testable without mocking Lucid or Powerwall."""
    if isCharging:
        if externalSession and not thresholds.allowExternalChargeInterference:
            return ChargingDecision(
                ChargingAction.OBSERVE,
                prevEvChargingAmps,
                "externally-started charge — not interfering",
            )
        if targetAmps < thresholds.minChargingAmps:
            if inCooldown and lastCommandWasStart:
                return ChargingDecision(
                    ChargingAction.HOLD_MIN,
                    thresholds.minChargingAmps,
                    f"surplus low ({effectiveSurplusKw:+.2f}kW → {targetAmps}A) within cooldown ({cooldownRemainingDesc} remaining)",
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
        log.error("No vehicles returned from Lucid API; cannot make charging decision.")
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
    recentStop = inCooldown and not lastCommandWasStart
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
    if externalSession:
        actualAmps = vehicle.state.charging.active_session_ac_current_limit or 0
        action_desc = "taking over" if thresholds.allowExternalChargeInterference else "observing"
        log.info(f"External charge session detected at {actualAmps}A — {action_desc}")
        prevEvChargingAmps = actualAmps

    # Cancel out the EV's own load from homeWatts so surplus reflects only household load.
    # pw.home() includes EV charging, so without this correction we'd stop charging
    # immediately after starting because the apparent surplus would collapse.
    evChargingKw = prevEvChargingAmps * thresholds.chargerVoltage / 1000
    effectiveSurplusKw = pwState.surplusKw + evChargingKw

    # Target amps floored to whole amps; clamped to [0, maxChargingAmps]
    targetAmps = max(0, min(thresholds.maxChargingAmps, int(effectiveSurplusKw * 1000 / thresholds.chargerVoltage)))

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
    )

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
                    log.warning("fetch_vehicles() returned empty list during wake-up; retrying.")
                    continue
                vehicle = vehicles[0]
                if lucid.vehicle_is_awake(vehicle):
                    log.info("Vehicle is awake.")
                    break
            else:
                log.warning("Vehicle did not wake within 60s; proceeding anyway.")
        await lucid.set_charge_limit(vehicle, int(thresholds.targetEvChargePercent))
        await lucid.set_ac_current_limit(vehicle, decision.targetAmps)
        await lucid.start_charging(vehicle)
        chargeState = ChargeState.CHARGE_STATE_ESTABLISHING_SESSION
        evChargingAmps = decision.targetAmps
        lastCommandTime = now
        lastCommandWasStart = True
        fireWebhook(webhookUrl, statePayload("charging_started", decision.reason, decision.targetAmps))

    return VehicleState(evSocPercent=evSocPercent, chargeState=chargeState, evChargingAmps=evChargingAmps, lastCommandTime=lastCommandTime, lastCommandWasStart=lastCommandWasStart)


def _chargeStateName(chargeState) -> str:
    """Return the protobuf enum name for a ChargeState value."""
    try:
        return ChargeState.Name(int(chargeState))
    except Exception:
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
            "action": skipReason or "",
        })


def _vehicleModelName(vehicle) -> str:
    """Format a human-readable model string from the vehicle config enums."""
    from lucidmotors.gen.vehicle_state_service_pb2 import Model, ModelVariant
    try:
        model = Model.Name(vehicle.config.model).replace("MODEL_", "").replace("_", " ").title()
    except Exception:
        model = str(vehicle.config.model)
    try:
        variant = ModelVariant.Name(vehicle.config.variant).replace("MODEL_VARIANT_", "").replace("_", " ").title()
    except Exception:
        variant = str(vehicle.config.variant)
    return f"{model} {variant}".strip()


def _vehicleStateFromProto(vehicle) -> "VehicleState":
    """Build a VehicleState snapshot from a Lucid vehicle proto object."""
    return VehicleState(
        evSocPercent=vehicle.state.battery.charge_percent,
        chargeState=vehicle.state.charging.charge_state,
    )


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
            log.warning(f"Auth refresh failed ({e}), re-logging in to Lucid...")
            await lucid.login(lucidConfig["username"], lucidConfig["password"])

    # --- Decide whether to poll Lucid ---
    # Always poll if currently charging (may need to stop), on first cycle, or forced by user.
    # Otherwise skip if the outcome is predetermined.
    isActivelyCharging = cachedState is not None and cachedState.chargeState in CHARGING_STATES
    skipReason = None
    if not isActivelyCharging and not forceEvPoll:
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
        cachedState = await pollVehicleAndDecide(lucid, pwState, thresholds, webhookUrl, cachedState)
        # After issuing a start or stop, wait longer before re-polling so the vehicle
        # has time to settle and we don't immediately reverse the command.
        if cachedState.lastCommandTime != prevCommandTime:
            pollIntervalFactor = max(pollIntervalFactor, _INTERVAL_AFTER_COMMAND)

    if dashState is not None:
        _updateDashboard(dashState, cachedState, thresholds, skipReason)

    return cachedState, pollIntervalFactor


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


def _writePowerwallToConfig(newName: str, path: str = "config.json") -> None:
    """Atomically update activePowerwall in config.json."""
    try:
        p = pathlib.Path(path)
        data = json.loads(p.read_text())
        data["activePowerwall"] = newName
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(p)
        log.info(f"Updated config.json: activePowerwall = '{newName}'")
    except Exception as e:
        log.warning(f"Failed to write activePowerwall to config: {e}")


def _buildSiteReadings(allSites: dict, activeName: str) -> list:
    """Build the per-site reading list stored in DashboardState.siteReadings."""
    return [
        {
            "name": site.name,
            "isActive": site.name == activeName,
            "solarWatts": site.pwState.solarWatts,
            "homeWatts": site.pwState.homeWatts,
            "surplusKw": _rawSurplusKw(site.pwState.solarWatts, site.pwState.homeWatts),
            "powerwallSocPercent": site.pwState.powerwallSocPercent,
            "error": site.error,
        }
        for site in allSites.values()
    ]


async def _pollOneSite(site: SiteInfo) -> bool:
    """Poll one site's Powerwall state (solar/home/soc). Returns True on success."""
    pw = site.pw
    assert pw is not None, "_pollOneSite called with pw=None"
    name = site.name
    try:
        # Lambda groups all three synchronous Powerwall calls into one thread invocation
        # so the event loop doesn't pay the thread-pool dispatch overhead three times.
        solar, home, soc = await asyncio.to_thread(
            lambda: (pw.solar(), pw.home(), pw.level(True))
        )
        if (solar or 0) == 0 and (home or 0) == 0 and (soc or 0) == 0:
            site.error = "All-zero reading (transient?)"
            log.warning(f"[{name}] All-zero reading — skipping update")
            return False
        site.pwState.update(float(solar or 0), float(home or 0), float(soc or 0))
        site.error = None
        return True
    except Exception as e:
        site.error = str(e)
        log.warning(f"[{name}] Poll failed: {e}")
        return False


async def _pollAllSites(allSites: dict) -> None:
    """Concurrently poll ALL Powerwall sites and append a PW-only history entry
    to every site that polled successfully (including the active site)."""
    coros = [_pollOneSite(site) for site in allSites.values() if site.pw is not None]
    await asyncio.gather(*coros)
    now_iso = datetime.now(timezone.utc).isoformat()
    for site in allSites.values():
        if site.error is None:
            pw = site.pwState
            site.history.append({
                "timestamp": now_iso,
                "solarWatts": round(pw.solarWatts),
                "homeWatts": round(pw.homeWatts),
                "surplusKw": _rawSurplusKw(pw.solarWatts, pw.homeWatts),
                "powerwallSocPercent": round(pw.powerwallSocPercent, 1),
            })


async def runChaos(config):
    _validateConfig(config)
    #pypowerwall.set_debug(True)
    log.info("CHAOS version %s starting", __version__)

    activeName = config["activePowerwall"]
    lucidConfig = config["lucid"]
    chargingConfig = config["charging"]
    notificationsConfig = config.get("notifications", {})
    pollingIntervalSeconds = config["pollingIntervalSeconds"]
    dashboard_cfg = config.get("dashboard", {})
    units = dashboard_cfg.get("units", "imperial")
    if units not in ("imperial", "metric"):
        raise RuntimeError(f"Invalid units '{units}' in config — must be 'imperial' or 'metric'")

    webhookUrl = notificationsConfig.get("webhookUrl", "") if notificationsConfig.get("enabled") else ""
    thresholds = ChargingThresholds(
        minPowerwallSocPercent=chargingConfig["minPowerwallSocPercent"],
        targetEvChargePercent=chargingConfig["targetEvChargePercent"],
        chargerVoltage=chargingConfig["chargerVoltage"],
        minChargingAmps=chargingConfig["minChargingAmps"],
        maxChargingAmps=chargingConfig["maxChargingAmps"],
        commandCooldownMinutes=chargingConfig["commandCooldownMinutes"],
        allowExternalChargeInterference=chargingConfig.get("allowExternalChargeInterference", True),
    )

    # Connect all Powerwalls at startup so their data is immediately available.
    allSites: dict[str, SiteInfo] = {}
    for pwConf in config["powerwalls"]:
        name = pwConf["name"]
        try:
            pw = await _connectPowerwall(pwConf, name)
            allSites[name] = SiteInfo(name=name, pw=pw)
        except Exception as e:
            log.warning(f"Failed to connect to Powerwall '{name}' at startup: {e}")
            allSites[name] = SiteInfo(name=name, pw=None, error=str(e))

    if allSites[activeName].pw is None:
        raise RuntimeError(f"Failed to connect to active Powerwall '{activeName}'")
    global _allSites
    _allSites = allSites

    async with LucidAPI() as lucid:
        log.info("Logging in to Lucid API...")
        await lucid.login(lucidConfig["username"], lucidConfig["password"])
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
        _dashboard.version = __version__
        _dashboard.activePowerwall = activeName
        _dashboard.powerwallNames = [p["name"] for p in config["powerwalls"]]
        allSites[activeName].history = _dashboard.history  # alias so _pollAllSites and _updateDashboard share the same deque
        _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)

        # Start web UI
        webUiPort = dashboard_cfg.get("port", 8086)
        global _webApiKey
        _webApiKey = dashboard_cfg.get("apiKey", "")
        if webUiPort:
            server = uvicorn.Server(uvicorn.Config(_webApp, host="0.0.0.0", port=webUiPort, log_level="warning"))
            asyncio.create_task(server.serve())
            log.info(f"Web UI available at http://0.0.0.0:{webUiPort}")

        forcedByUser = False
        while True:
            try:
                # Poll ALL sites concurrently; every successful site gets a history entry.
                await _pollAllSites(allSites)
                if allSites[activeName].error:
                    log.warning(f"[{activeName}] Active site PW poll failed — skipping decision cycle")
                    pollIntervalFactor = 1
                else:
                    cachedState, pollIntervalFactor = await processCycle(lucid, thresholds, webhookUrl, lucidConfig, cachedState, _dashboard, forceEvPoll=forcedByUser, pwState=allSites[activeName].pwState, siteName=activeName)
                _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)
                _dashboard.lastError = None
            except asyncio.CancelledError:
                log.info("Shutting down.")
                return
            except (LoginError, PowerwallConnectionError) as e:
                # Transient Powerwall auth/connection failure — reconnect silently.
                pollIntervalFactor = 1
                log.warning(f"Powerwall connection error: {e} — reconnecting...")
                _dashboard.lastError = str(e)
                try:
                    active_pw = allSites[activeName].pw
                    assert active_pw is not None
                    await asyncio.to_thread(active_pw.connect)
                    log.info("Powerwall reconnected.")
                    _dashboard.lastError = None
                except Exception as reconnectError:
                    log.warning(f"Powerwall reconnect failed: {reconnectError}")
            except Exception as e:
                pollIntervalFactor = 1
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
                sleepSeconds = pollingIntervalSeconds * pollIntervalFactor
                _dashboard.nextPollAt = datetime.now(timezone.utc) + timedelta(seconds=sleepSeconds)
                await asyncio.wait_for(_forcePoll.wait(), timeout=sleepSeconds)
                log.info("Poll cycle triggered by user request.")
                forcedByUser = True
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
                            site.error = None
                        activeName = newName
                        _dashboard.activePowerwall = newName
                        _dashboard.history = allSites[newName].history
                        _dashboard.siteReadings = _buildSiteReadings(allSites, activeName)
                        await asyncio.to_thread(_writePowerwallToConfig, newName)
                        log.info(f"Active Powerwall switched to '{newName}'.")
                    except Exception as switchErr:
                        log.warning(f"Failed to switch to Powerwall '{newName}': {switchErr}")
                else:
                    log.warning(f"Unknown Powerwall name '{newName}' — ignoring switch request")

def main():
    config = loadConfig()
    try:
        asyncio.run(runChaos(config))
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
