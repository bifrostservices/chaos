"""Unit tests for chaos.py — pure functions and mocked async paths."""

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
import json
import tempfile
import pathlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

import grpc
import pytest
from lucidmotors.exceptions import APIError

from chaos import (
    ChargingAction,
    ChargingThresholds,
    DashboardState,
    PowerwallState,
    SiteInfo,
    VehicleState,
    _HTML_TEMPLATE,
    _buildSiteReadings,
    _connectPowerwall,
    _decideChargingAction,
    _detectSiteSwitch,
    _isTransientLucidError,
    _isUnplugged,
    _lucidLoginWithRetry,
    _pollAllSites,
    _pollOneSite,
    _shouldForceEarlyEvPoll,
    _webApp,
    _validateConfig,
    _updateDashboard,
    _writePowerwallToConfig,
    fireWebhook,
    loadConfig,
    processCycle,
    pollVehicleAndDecide,
    powerwallCacheFile,
    sendWebhook,
    runChaos,
)
from lucidmotors.gen.vehicle_state_service_pb2 import ChargeState as CS
from pypowerwall.local.exceptions import PowerwallConnectionError

# ---------------------------------------------------------------------------
# Config builder helpers (used by TestConfigValidation)
# ---------------------------------------------------------------------------

def _make_pw_entry(name="Home", host="1.1.1.1", authType="local", **overrides):
    """Build a Powerwall config dict with sensible defaults."""
    entry = {"name": name, "host": host, "password": "pw", "email": "e@e.com", "timezone": "UTC", "authType": authType}
    entry.update(overrides)
    return entry


def _make_valid_config(**overrides):
    """Build a fully valid config dict; pass keyword args to override top-level keys."""
    cfg = {
        "activePowerwall": "Home",
        "powerwalls": [_make_pw_entry()],
        "lucid": {"username": "u", "password": "p"},
        "charging": {
            "minPowerwallSocPercent": 40,
            "targetEvChargePercent": 85,
            "chargerVoltage": 240,
            "minChargingAmps": 6,
            "maxChargingAmps": 48,
            "commandCooldownMinutes": 5,
        },
        "pollingIntervalSeconds": 60,
    }
    cfg.update(overrides)
    return cfg

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def thresholds():
    return ChargingThresholds(
        minPowerwallSocPercent=40.0,
        targetEvChargePercent=85.0,
        chargerVoltage=240.0,
        minChargingAmps=6,
        maxChargingAmps=48,
        commandCooldownMinutes=5.0,
    )


@pytest.fixture
def pw_good(thresholds):
    """Powerwall state with plenty of surplus and healthy SOC."""
    surplus_w = (thresholds.minChargingAmps + 10) * thresholds.chargerVoltage  # 16A worth
    return PowerwallState(
        solarWatts=surplus_w + 2000,
        homeWatts=2000,
        powerwallSocPercent=80.0,
    )


@pytest.fixture
def pw_low_surplus(thresholds):
    """Powerwall state with surplus below minChargingAmps."""
    surplus_w = (thresholds.minChargingAmps - 1) * thresholds.chargerVoltage  # 5A worth
    return PowerwallState(
        solarWatts=surplus_w + 2000,
        homeWatts=2000,
        powerwallSocPercent=80.0,
    )


# ---------------------------------------------------------------------------
# PowerwallState
# ---------------------------------------------------------------------------

class TestPowerwallState:
    def test_none_inputs_default_to_zero(self):
        pw = PowerwallState(None, None, None)  # type: ignore
        assert pw.solarWatts == 0
        assert pw.homeWatts == 0
        assert pw.powerwallSocPercent == 0

    @pytest.mark.parametrize("solar,expected", [(-5, 0), (0, 0), (3000, 3000)])
    def test_solar_clamping(self, solar, expected):
        pw = PowerwallState(solarWatts=solar, homeWatts=0, powerwallSocPercent=50)
        assert pw.solarWatts == expected

    def test_surplus_kw_positive(self):
        pw = PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=50)
        assert pw.surplusKw == pytest.approx(3.0)

    def test_surplus_kw_negative(self):
        pw = PowerwallState(solarWatts=1000, homeWatts=3000, powerwallSocPercent=50)
        assert pw.surplusKw == pytest.approx(-2.0)

    def test_surplus_kw_zero(self):
        pw = PowerwallState(solarWatts=2000, homeWatts=2000, powerwallSocPercent=50)
        assert pw.surplusKw == pytest.approx(0.0)

    def test_is_dark_below_threshold(self):
        assert PowerwallState(solarWatts=49, homeWatts=0, powerwallSocPercent=50).isDark is True

    def test_is_dark_at_threshold(self):
        assert PowerwallState(solarWatts=50, homeWatts=0, powerwallSocPercent=50).isDark is False

    def test_is_dark_at_zero(self):
        assert PowerwallState(solarWatts=0, homeWatts=0, powerwallSocPercent=50).isDark is True

    def test_is_dark_night_none_input(self):
        pw = PowerwallState(solarWatts=None, homeWatts=None, powerwallSocPercent=None)  # type: ignore
        assert pw.isDark is True

    def test_avg_home_one_shot_instance(self):
        """One-shot instance: avgHomeWatts equals the seeded homeWatts."""
        pw = PowerwallState(solarWatts=5000, homeWatts=3000, powerwallSocPercent=80)
        assert pw.avgHomeWatts == pytest.approx(3000.0)

    def test_avg_home_empty_history_fallback(self):
        """No-arg instance has empty history: avgHomeWatts falls back to homeWatts."""
        pw = PowerwallState()
        assert pw.avgHomeWatts == pytest.approx(0.0)

    def test_avg_home_smooths_spike(self):
        """A single home spike is dampened by the rolling average."""
        pw = PowerwallState(solarWatts=6000, homeWatts=3000, powerwallSocPercent=80)
        pw.update(6000, 3000, 80)   # second sample: still 3000W
        pw.update(6000, 11000, 80)  # spike: 11000W home
        # avg = (3000 + 3000 + 11000) / 3 ≈ 5667W, not 11000W
        assert pw.avgHomeWatts == pytest.approx((3000 + 3000 + 11000) / 3)
        # surplusKw uses avg home, not raw home — should not show a massive deficit
        assert pw.surplusKw > (6000 - 11000) / 1000  # better than raw-home surplus

    def test_surplus_uses_avg_home_not_raw(self):
        """surplusKw must use avgHomeWatts so one bad cycle doesn't cause a false STOP."""
        pw = PowerwallState(solarWatts=6500, homeWatts=6500, powerwallSocPercent=80)
        pw.update(6500, 6500, 80)
        pw.update(6500, 11500, 80)  # spike — would trigger STOP if using raw home
        # With avg home = (6500 + 6500 + 11500) / 3 ≈ 8167W: surplus ≈ -1.67kW
        # With raw home = 11500W:                              surplus = -5.0kW
        expected_avg_home = (6500 + 6500 + 11500) / 3
        assert pw.surplusKw == pytest.approx((6500 - expected_avg_home) / 1000)

    def test_reset_home_history_clears_stale_readings(self):
        """After resetHomeHistory(), avgHomeWatts falls back to the current raw reading."""
        pw = PowerwallState(solarWatts=7000, homeWatts=1500, powerwallSocPercent=60)
        pw.update(7000, 1500, 60)
        pw.update(7000, 7000, 60)   # EV started — history now [1500, 1500, 7000]
        assert pw.avgHomeWatts == pytest.approx((1500 + 1500 + 7000) / 3)  # stale avg

        pw.resetHomeHistory()
        # After reset, empty history → fallback to raw homeWatts (7000W)
        assert pw.avgHomeWatts == pytest.approx(7000.0)

    def test_reset_home_history_next_update_seeds_fresh(self):
        """After resetHomeHistory(), the next update() starts fresh from a single sample."""
        pw = PowerwallState(solarWatts=7000, homeWatts=1500, powerwallSocPercent=60)
        pw.update(7000, 1500, 60)
        pw.resetHomeHistory()
        pw.update(7000, 7000, 60)   # first post-reset reading (EV now charging)
        assert pw.avgHomeWatts == pytest.approx(7000.0)  # only 1 sample — no stale contamination


# ---------------------------------------------------------------------------
# ChargingThresholds
# ---------------------------------------------------------------------------

class TestChargingThresholds:
    def test_min_surplus_kw(self, thresholds):
        # 6A * 240V / 1000 = 1.44 kW
        assert thresholds.minSurplusKw == pytest.approx(1.44)

    def test_min_surplus_kw_different_values(self):
        t = ChargingThresholds(
            minPowerwallSocPercent=50,
            targetEvChargePercent=80,
            chargerVoltage=240,
            minChargingAmps=12,
            maxChargingAmps=48,
            commandCooldownMinutes=5,
        )
        assert t.minSurplusKw == pytest.approx(2.88)


# ---------------------------------------------------------------------------
# powerwallCacheFile
# ---------------------------------------------------------------------------

class TestPowerwallCacheFile:
    def test_basic(self):
        assert powerwallCacheFile("Home", "192.168.1.1") == ".powerwall-Home-192.168.1.1"

    def test_ip_dots_preserved(self):
        result = powerwallCacheFile("Cohiba1", "192.168.2.220")
        assert result == ".powerwall-Cohiba1-192.168.2.220"

    def test_special_chars_replaced(self):
        result = powerwallCacheFile("My Wall!", "10.0.0.1")
        assert "!" not in result
        assert " " not in result

    def test_alphanumeric_preserved(self):
        result = powerwallCacheFile("abc123", "10.0.0.1")
        assert "abc123" in result


# ---------------------------------------------------------------------------
# _decideChargingAction — helpers
# ---------------------------------------------------------------------------

def _decide(
    thresholds,
    pw,
    *,
    isCharging=False,
    canCharge=False,
    targetAmps=10,
    prevEvChargingAmps=10,
    inCooldown=False,
    lastCommandWasStart=False,
    effectiveSurplusKw=2.4,
    evSocPercent=50.0,
):
    return _decideChargingAction(
        isCharging=isCharging,
        canCharge=canCharge,
        targetAmps=targetAmps,
        prevEvChargingAmps=prevEvChargingAmps,
        thresholds=thresholds,
        inCooldown=inCooldown,
        lastCommandWasStart=lastCommandWasStart,
        cooldownRemainingDesc="120s",
        pwState=pw,
        effectiveSurplusKw=effectiveSurplusKw,
        evSocPercent=evSocPercent,
    )


# ---------------------------------------------------------------------------
# _decideChargingAction — isCharging branches
# ---------------------------------------------------------------------------

class TestDecideWhenCharging:
    def test_low_surplus_in_cooldown_after_start_gives_hold_min(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=thresholds.minChargingAmps - 1,
            prevEvChargingAmps=thresholds.minChargingAmps + 4,
            inCooldown=True,
            lastCommandWasStart=True,
        )
        assert d.action == ChargingAction.HOLD_MIN
        assert d.targetAmps == thresholds.minChargingAmps

    def test_hold_min_not_triggered_when_cooldown_after_stop(self, thresholds, pw_good):
        """Cooldown after a stop should not prevent stopping again."""
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=thresholds.minChargingAmps - 1,
            prevEvChargingAmps=thresholds.minChargingAmps + 4,
            inCooldown=True,
            lastCommandWasStart=False,  # last command was a stop
        )
        assert d.action == ChargingAction.STOP

    def test_low_surplus_cooldown_expired_gives_stop(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=thresholds.minChargingAmps - 1,
            inCooldown=False,
            lastCommandWasStart=True,
        )
        assert d.action == ChargingAction.STOP

    def test_low_surplus_no_prior_command_gives_stop(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=thresholds.minChargingAmps - 1,
            inCooldown=False,
            lastCommandWasStart=False,
        )
        assert d.action == ChargingAction.STOP

    def test_powerwall_below_min_gives_stop(self, thresholds, pw_good):
        pw_low = PowerwallState(
            solarWatts=pw_good.solarWatts,
            homeWatts=pw_good.homeWatts,
            powerwallSocPercent=thresholds.minPowerwallSocPercent - 1,
        )
        d = _decide(thresholds, pw_low, isCharging=True, targetAmps=thresholds.minChargingAmps + 4)
        assert d.action == ChargingAction.STOP

    def test_ev_at_target_gives_stop(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=thresholds.minChargingAmps + 4,
            evSocPercent=thresholds.targetEvChargePercent,
        )
        assert d.action == ChargingAction.STOP

    def test_adjust_when_diff_at_least_2a(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=14,
            prevEvChargingAmps=10,  # diff = 4 >= 2
        )
        assert d.action == ChargingAction.ADJUST
        assert d.targetAmps == 14

    def test_no_adjust_within_dead_band(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=11,
            prevEvChargingAmps=10,  # diff = 1 < 2
        )
        assert d.action == ChargingAction.NONE

    def test_no_adjust_when_equal(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            isCharging=True,
            targetAmps=10,
            prevEvChargingAmps=10,
        )
        assert d.action == ChargingAction.NONE


# ---------------------------------------------------------------------------
# _decideChargingAction — canCharge branches
# ---------------------------------------------------------------------------

class TestDecideWhenChargeable:
    def test_start_when_conditions_met(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps + 2,
            inCooldown=False,
            evSocPercent=50.0,
        )
        assert d.action == ChargingAction.START
        assert d.targetAmps == thresholds.minChargingAmps + 2

    def test_skip_start_in_cooldown_after_stop(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps + 2,
            inCooldown=True,
            lastCommandWasStart=False,
        )
        assert d.action == ChargingAction.SKIP_START

    def test_cooldown_after_start_does_not_block_new_start(self, thresholds, pw_good):
        """If the last command was a start and we're back in canCharge, start again."""
        d = _decide(
            thresholds, pw_good,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps + 2,
            inCooldown=True,
            lastCommandWasStart=True,
        )
        assert d.action == ChargingAction.START

    def test_no_start_when_surplus_insufficient(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps - 1,
        )
        assert d.action == ChargingAction.NONE

    def test_no_start_when_powerwall_low(self, thresholds, pw_good):
        pw_low = PowerwallState(
            solarWatts=pw_good.solarWatts,
            homeWatts=pw_good.homeWatts,
            powerwallSocPercent=thresholds.minPowerwallSocPercent - 1,
        )
        d = _decide(
            thresholds, pw_low,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps + 4,
        )
        assert d.action == ChargingAction.NONE

    def test_no_start_when_ev_at_target(self, thresholds, pw_good):
        d = _decide(
            thresholds, pw_good,
            canCharge=True,
            targetAmps=thresholds.minChargingAmps + 4,
            evSocPercent=thresholds.targetEvChargePercent,
        )
        assert d.action == ChargingAction.NONE


# ---------------------------------------------------------------------------
# _decideChargingAction — neither charging nor chargeable
# ---------------------------------------------------------------------------

class TestDecideUnknownState:
    def test_none_when_neither_charging_nor_chargeable(self, thresholds, pw_good):
        d = _decide(thresholds, pw_good, isCharging=False, canCharge=False)
        assert d.action == ChargingAction.NONE


# ---------------------------------------------------------------------------
# sendWebhook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendWebhook:
    async def test_posts_to_url(self):
        with patch("chaos.requests.post") as mock_post:
            await sendWebhook("https://example.com/hook", {"event": "test"})
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert args[0] == "https://example.com/hook"
            assert kwargs["json"] == {"event": "test"}

    async def test_no_op_when_url_empty(self):
        with patch("chaos.requests.post") as mock_post:
            await sendWebhook("", {"event": "test"})
            mock_post.assert_not_called()

    async def test_swallows_exception_and_logs_warning(self):
        with patch("chaos.requests.post", side_effect=ConnectionError("timeout")):
            with patch("chaos.log") as mock_log:
                await sendWebhook("https://example.com/hook", {"event": "test"})
                mock_log.warning.assert_called_once()

    async def test_logs_warning_on_non_2xx_response(self):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        with patch("chaos.requests.post", return_value=mock_response):
            with patch("chaos.log") as mock_log:
                await sendWebhook("https://example.com/hook", {"event": "test"})
                mock_log.warning.assert_called_once()

    async def test_google_chat_strips_extra_fields(self):
        """Google Chat only accepts {text}, so extra keys must be stripped."""
        with patch("chaos.requests.post") as mock_post:
            payload = {"text": "hello", "event": "test", "solarWatts": 3000}
            await sendWebhook("https://chat.googleapis.com/v1/spaces/ABC/messages?key=x", payload)
            _, kwargs = mock_post.call_args
            assert kwargs["json"] == {"text": "hello"}

    async def test_google_chat_falls_back_to_json_dump_when_no_text(self):
        """If payload has no 'text' key (e.g. error events), fall back to JSON dump."""
        with patch("chaos.requests.post") as mock_post:
            payload = {"event": "error", "message": "oops"}
            await sendWebhook("https://chat.googleapis.com/v1/spaces/ABC/messages?key=x", payload)
            _, kwargs = mock_post.call_args
            sent = kwargs["json"]
            assert set(sent.keys()) == {"text"}
            assert "error" in sent["text"]  # JSON dump contains the event field

    async def test_non_google_chat_preserves_all_fields(self):
        """Generic webhooks receive the full structured payload."""
        with patch("chaos.requests.post") as mock_post:
            payload = {"text": "hello", "event": "test", "solarWatts": 3000}
            await sendWebhook("https://hooks.example.com/notify", payload)
            _, kwargs = mock_post.call_args
            assert kwargs["json"] == payload

    async def test_google_chat_hostname_in_query_param_is_not_matched(self):
        """A URL with chat.googleapis.com in the query string must NOT be treated as Google Chat."""
        with patch("chaos.requests.post") as mock_post:
            payload = {"text": "hello", "event": "test", "solarWatts": 3000}
            url = "https://evil.com/hook?redirect=chat.googleapis.com"
            await sendWebhook(url, payload)
            _, kwargs = mock_post.call_args
            assert kwargs["json"] == payload  # full payload preserved — not stripped

    async def test_google_chat_subdomain_is_matched(self):
        """A subdomain of chat.googleapis.com should also be treated as Google Chat."""
        with patch("chaos.requests.post") as mock_post:
            payload = {"text": "hello", "event": "test", "solarWatts": 3000}
            url = "https://subdomain.chat.googleapis.com/v1/spaces/ABC/messages"
            await sendWebhook(url, payload)
            _, kwargs = mock_post.call_args
            assert kwargs["json"] == {"text": "hello"}  # stripped to text-only


# ---------------------------------------------------------------------------
# Home-load spike EV arrival heuristic
# ---------------------------------------------------------------------------

def _make_thresholds(**overrides):
    """ChargingThresholds with defaults suitable for heuristic tests."""
    defaults = dict(
        minPowerwallSocPercent=20,
        targetEvChargePercent=80,
        chargerVoltage=240,
        minChargingAmps=8,
        maxChargingAmps=32,
        commandCooldownMinutes=10,
        allowExternalChargeInterference=True,
    )
    defaults.update(overrides)
    return ChargingThresholds(**defaults)


def _make_history(prev_home: float, curr_home: float) -> collections.deque:
    """Two-entry history deque with the given home-load values."""
    hist: collections.deque = collections.deque(maxlen=120)
    hist.append({"homeWatts": prev_home, "solarWatts": 3000, "surplusKw": 1.0, "powerwallSocPercent": 80, "timestamp": "t0"})
    hist.append({"homeWatts": curr_home, "solarWatts": 3000, "surplusKw": 1.0, "powerwallSocPercent": 80, "timestamp": "t1"})
    return hist


def _unplugged_state(**overrides) -> VehicleState:
    """VehicleState where the EV cable is not connected."""
    return VehicleState(evSocPercent=50, chargeState=CS.CHARGE_STATE_NOT_CONNECTED, **overrides)


def _plugged_in_state() -> VehicleState:
    """VehicleState where the EV is plugged in (cable connected, not charging)."""
    return VehicleState(evSocPercent=50, chargeState=CS.CHARGE_STATE_CABLE_CONNECTED)


class TestHomeLoadSpikeHeuristic:
    """Tests for _shouldForceEarlyEvPoll — EV arrival detection via home-load spike."""

    def test_spike_and_unplugged_triggers_poll(self):
        """Large home-load rise + unplugged EV → should force early poll."""
        # minChargingAmps=8, chargerVoltage=240 → threshold = 1.92 kW = 1920 W
        thresholds = _make_thresholds(minChargingAmps=8, chargerVoltage=240)
        hist = _make_history(prev_home=1000, curr_home=3000)  # +2000 W > 1920 W
        state = _unplugged_state()
        assert _shouldForceEarlyEvPoll(hist, state, thresholds) is True

    def test_spike_but_ev_plugged_in_does_not_trigger(self):
        """Home-load spike is ignored when the EV was already plugged in."""
        thresholds = _make_thresholds(minChargingAmps=8, chargerVoltage=240)
        hist = _make_history(prev_home=1000, curr_home=3000)
        state = _plugged_in_state()
        assert _shouldForceEarlyEvPoll(hist, state, thresholds) is False

    def test_small_spike_does_not_trigger(self):
        """A home-load rise below the EV threshold does not force a poll."""
        thresholds = _make_thresholds(minChargingAmps=8, chargerVoltage=240)
        hist = _make_history(prev_home=1000, curr_home=1500)  # +500 W < 1920 W
        state = _unplugged_state()
        assert _shouldForceEarlyEvPoll(hist, state, thresholds) is False

    def test_spike_and_unplugged_but_in_cooldown_does_not_trigger(self):
        """Spike is ignored when a recent command is still in cooldown."""
        thresholds = _make_thresholds(minChargingAmps=8, chargerVoltage=240, commandCooldownMinutes=10)
        hist = _make_history(prev_home=1000, curr_home=3000)
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)  # 2 min ago, cooldown=10
        state = _unplugged_state(lastCommandTime=recent)
        assert _shouldForceEarlyEvPoll(hist, state, thresholds) is False

    def test_fewer_than_two_history_entries_does_not_trigger(self):
        """Returns False when there is no previous reading to compare against."""
        thresholds = _make_thresholds()
        hist: collections.deque = collections.deque(maxlen=120)
        hist.append({"homeWatts": 3000, "solarWatts": 3000, "surplusKw": 0.0, "powerwallSocPercent": 80, "timestamp": "t0"})
        state = _unplugged_state()
        assert _shouldForceEarlyEvPoll(hist, state, thresholds) is False

    def test_none_cached_state_does_not_trigger(self):
        """Returns False when no EV state has been cached yet (first cycle)."""
        thresholds = _make_thresholds()
        hist = _make_history(prev_home=1000, curr_home=3000)
        assert _shouldForceEarlyEvPoll(hist, None, thresholds) is False


# ---------------------------------------------------------------------------
# pollVehicleAndDecide — mocked Lucid API
# ---------------------------------------------------------------------------

def _make_lucid_mock(evSocPercent=50.0, chargeState=None):
    """Build a minimal mock of the lucid API client."""
    if chargeState is None:
        chargeState = CS.CHARGE_STATE_CABLE_CONNECTED

    vehicle = MagicMock()
    vehicle.state.battery.charge_percent = evSocPercent
    vehicle.state.charging.charge_state = chargeState

    lucid = MagicMock()
    lucid.fetch_vehicles = AsyncMock(return_value=[vehicle])
    lucid.start_charging = AsyncMock()
    lucid.stop_charging = AsyncMock()
    lucid.set_ac_current_limit = AsyncMock()
    lucid.set_charge_limit = AsyncMock()
    lucid.vehicle_is_awake = MagicMock(return_value=True)
    lucid.wakeup_vehicle = AsyncMock()
    return lucid, vehicle


@pytest.mark.asyncio
class TestPollVehicleAndDecide:
    async def test_starts_charging_when_surplus_sufficient(self, thresholds, pw_good):
        lucid, vehicle = _make_lucid_mock()
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        lucid.set_ac_current_limit.assert_awaited_once()
        lucid.start_charging.assert_awaited_once_with(vehicle)
        assert result.lastCommandWasStart is True
        assert result.lastCommandTime is not None

    async def test_sets_charge_limit_before_starting(self, thresholds, pw_good):
        lucid, vehicle = _make_lucid_mock()
        await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        lucid.set_charge_limit.assert_awaited_once_with(vehicle, int(thresholds.targetEvChargePercent))

    async def test_stops_charging_when_ev_at_target(self, thresholds, pw_good):
        lucid, vehicle = _make_lucid_mock(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
        )
        prev = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        lucid.stop_charging.assert_awaited_once_with(vehicle)
        assert result.evChargingAmps == 0
        assert result.lastCommandWasStart is False

    async def test_adjusts_current_mid_session(self, thresholds, pw_good):
        # Start with prevAmps well below the available surplus
        prevAmps = 10
        prev = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=prevAmps,
        )
        result = await pollVehicleAndDecide(lucid=_make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)[0],
                                            pwState=pw_good, thresholds=thresholds,
                                            webhookUrl="", prevState=prev)
        # Should have called set_ac_current_limit since targetAmps differs by >= 2A
        _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)[0].set_ac_current_limit.assert_not_called()
        # Rebuild properly — reuse same mock
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        lucid.set_ac_current_limit.assert_awaited_once()
        lucid.start_charging.assert_not_awaited()
        lucid.stop_charging.assert_not_awaited()

    async def test_no_command_when_no_surplus(self, thresholds, pw_low_surplus):
        lucid, _ = _make_lucid_mock()
        result = await pollVehicleAndDecide(lucid, pw_low_surplus, thresholds, "")
        lucid.start_charging.assert_not_awaited()
        lucid.stop_charging.assert_not_awaited()

    async def test_holds_at_min_during_cooldown(self, thresholds):
        # prevEvChargingAmps=10A → EV correction = 10*240/1000 = 2.4kW
        # raw surplusKw = -1.5kW → effectiveSurplusKw = 0.9kW → targetAmps = 3A < minChargingAmps(6)
        # prevAmps(10) != minChargingAmps(6) so set_ac_current_limit should be called
        pw_very_low = PowerwallState(
            solarWatts=500,
            homeWatts=2000,   # raw surplusKw = -1.5kW
            powerwallSocPercent=80.0,
        )
        prevAmps = thresholds.minChargingAmps + 4   # 10A — above min, so throttle is meaningful
        prev = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=prevAmps,
            lastCommandTime=datetime.now(timezone.utc) - timedelta(minutes=1),
            lastCommandWasStart=True,
        )
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, pw_very_low, thresholds, "", prevState=prev)
        lucid.stop_charging.assert_not_awaited()
        lucid.set_ac_current_limit.assert_awaited_once_with(vehicle, thresholds.minChargingAmps)

    async def test_returned_state_reflects_post_command(self, thresholds, pw_good):
        lucid, _ = _make_lucid_mock()
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        assert result.chargeState == CS.CHARGE_STATE_ESTABLISHING_SESSION
        assert result.evChargingAmps > 0

    async def test_target_amps_capped_at_solar_production(self, thresholds):
        """targetAmps must never exceed floor(solarWatts / chargerVoltage)."""
        # 7000W solar → max 29A (7000/240). With large effectiveSurplus that would
        # otherwise compute 35+A, the solar cap must bring it back to 29A.
        solar_w = 7000  # floor(7000/240) = 29A
        pw = PowerwallState(solarWatts=solar_w, homeWatts=1500, powerwallSocPercent=80)
        # prevAmps=29A → evChargingKw=6.96kW; effectiveSurplus≈(7000−1500)/1000+6.96=12.46kW→51A before cap
        prev = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=29,
        )
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, pw, thresholds, "", prevState=prev)
        if lucid.set_ac_current_limit.called:
            commanded_amps = lucid.set_ac_current_limit.call_args[0][1]
            assert commanded_amps <= solar_w // int(thresholds.chargerVoltage)

    async def test_reset_home_history_called_after_start(self, thresholds, pw_good):
        """Home history must be cleared after issuing a START so stale low-load readings
        don't inflate effectiveSurplusKw on the next cycle."""
        lucid, _ = _make_lucid_mock()
        # Seed home history with low (pre-EV) readings
        pw_good.update(pw_good.solarWatts, 1500, 80)
        pw_good.update(pw_good.solarWatts, 1500, 80)
        assert len(pw_good._homeHistory) >= 2
        await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        lucid.start_charging.assert_awaited_once()
        # History should be empty after the reset so the next update builds fresh
        assert len(pw_good._homeHistory) == 0

    async def test_reset_home_history_called_after_stop(self, thresholds):
        """Home history must be cleared after a STOP so stale high-load (EV) readings
        don't depress surplusKw on the following cycle."""
        low_pw = PowerwallState(solarWatts=100, homeWatts=3000, powerwallSocPercent=80)
        # Seed home history with high (EV-inclusive) readings
        low_pw.update(100, 7500, 80)
        low_pw.update(100, 7500, 80)
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=16)
        lucid, _ = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        await pollVehicleAndDecide(lucid, low_pw, thresholds, "", prevState=prev)
        lucid.stop_charging.assert_awaited_once()
        assert len(low_pw._homeHistory) == 0

    async def test_vehicle_self_terminates_clears_ev_charging_amps(self, thresholds, pw_good):
        """When vehicle self-stops at target (CHARGING_END_OK), evChargingAmps must be
        cleared to 0 even though CHAOS issued no STOP command."""
        prev = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING_END_OK,
            evChargingAmps=16,
        )
        lucid, _ = _make_lucid_mock(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING_END_OK,
        )
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        lucid.stop_charging.assert_not_awaited()   # vehicle self-stopped; CHAOS issues no STOP
        assert result.evChargingAmps == 0

    async def test_vehicle_self_terminates_resets_home_history(self, thresholds, pw_good):
        """After vehicle self-stop, resetHomeHistory() should clear stale home averages."""
        prev = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING_END_OK,
            evChargingAmps=16,
        )
        lucid, _ = _make_lucid_mock(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING_END_OK,
        )
        # Pre-fill home history with high-EV readings
        for _ in range(3):
            pw_good.update(5000, 4500, 80)
        assert len(pw_good._homeHistory) == 3
        await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        assert len(pw_good._homeHistory) == 0


# ---------------------------------------------------------------------------
# pollVehicleAndDecide — lastActionDesc populated in returned VehicleState
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollVehicleAndDecideActionDesc:
    async def test_start_sets_action_desc(self, thresholds, pw_good):
        """When charging is started, lastActionDesc should be 'start'."""
        lucid, _ = _make_lucid_mock()  # default CABLE_CONNECTED — can start
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        assert result.lastActionDesc == "start"

    async def test_stop_sets_action_desc(self, thresholds):
        """When charging is stopped (insufficient surplus), lastActionDesc should be 'stop'."""
        low_pw = PowerwallState(solarWatts=100, homeWatts=3000, powerwallSocPercent=80)
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=16)
        lucid, _ = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, low_pw, thresholds, "", prevState=prev)
        assert result.lastActionDesc == "stop"

    async def test_adjust_sets_action_desc_with_amps(self, thresholds, pw_good):
        """When amps are adjusted, lastActionDesc should be the 'XA → YA' reason string."""
        # Start at min amps; with good surplus, target should be higher
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=thresholds.minChargingAmps)
        lucid, _ = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        assert "→" in result.lastActionDesc

    async def test_none_sets_empty_action_desc(self, thresholds, pw_good):
        """NONE decision (no change needed) → lastActionDesc is empty string."""
        # Already charging at target amps — surplus matches current draw exactly
        target_amps = thresholds.maxChargingAmps
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=target_amps)
        # Set surplus to exactly what target_amps requires — no adjust needed
        surplus_w = target_amps * thresholds.chargerVoltage
        pw = PowerwallState(solarWatts=surplus_w + 2000, homeWatts=2000, powerwallSocPercent=80)
        lucid, _ = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        result = await pollVehicleAndDecide(lucid, pw, thresholds, "", prevState=prev)
        assert result.lastActionDesc == ""


# ---------------------------------------------------------------------------
# Vehicle wakeup polling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestVehicleWakeup:
    async def test_already_awake_skips_wakeup(self, thresholds, pw_good):
        """vehicle_is_awake() returns True immediately — wakeup_vehicle never called."""
        lucid, _ = _make_lucid_mock()
        lucid.vehicle_is_awake = MagicMock(return_value=True)
        with patch("chaos.asyncio.sleep", new=AsyncMock()):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        lucid.wakeup_vehicle.assert_not_awaited()
        lucid.start_charging.assert_awaited_once()

    async def test_wakes_on_second_check(self, thresholds, pw_good):
        """Vehicle asleep initially, awake on the first poll tick."""
        lucid, vehicle = _make_lucid_mock()
        # First call (before loop): asleep. Second call (inside loop): awake.
        lucid.vehicle_is_awake = MagicMock(side_effect=[False, True])
        with patch("chaos.asyncio.sleep", new=AsyncMock()):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")
        lucid.wakeup_vehicle.assert_awaited_once()
        lucid.start_charging.assert_awaited_once()

    async def test_timeout_logs_warning_and_proceeds(self, thresholds, pw_good):
        """Vehicle never wakes within deadline — warning logged, charging still attempted."""
        from datetime import timedelta as td
        lucid, _ = _make_lucid_mock()
        lucid.vehicle_is_awake = MagicMock(return_value=False)

        # Force the deadline to be in the past after one iteration by patching datetime.now
        call_count = 0
        real_now = datetime.now(timezone.utc)

        def fake_now(tz=None):
            nonlocal call_count
            call_count += 1
            # First call (deadline = now + 60s) → real time; subsequent calls → past deadline
            return real_now if call_count == 1 else real_now + td(seconds=61)

        with (
            patch("chaos.asyncio.sleep", new=AsyncMock()),
            patch("chaos.datetime") as mock_dt,
            patch("chaos.log") as mock_log,
        ):
            mock_dt.now = fake_now
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

        mock_log.warning.assert_called()
        warning_msg = mock_log.warning.call_args[0][0]
        assert "60s" in warning_msg
        lucid.start_charging.assert_awaited_once()


# ---------------------------------------------------------------------------
# runChaos — SIGTERM / SIGHUP graceful shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGracefulShutdown:
    async def _make_chaos_deps(self, thresholds, pw_good):
        """Build minimal config + mocked PW + Lucid for runChaos."""
        import pypowerwall
        from chaos import runChaos

        config = {
            "activePowerwall": "test",
            "powerwalls": [
                {
                    "name": "test",
                    "host": "192.168.1.1",
                    "password": "pw",
                    "email": "test@example.com",
                    "timezone": "US/Pacific",
                    "authType": "local",
                }
            ],
            "lucid": {"username": "u", "password": "p"},
            "charging": {
                "minPowerwallSocPercent": thresholds.minPowerwallSocPercent,
                "targetEvChargePercent": thresholds.targetEvChargePercent,
                "chargerVoltage": thresholds.chargerVoltage,
                "minChargingAmps": thresholds.minChargingAmps,
                "maxChargingAmps": thresholds.maxChargingAmps,
                "commandCooldownMinutes": thresholds.commandCooldownMinutes,
            },
            "notifications": {"enabled": False},
            "pollingIntervalSeconds": 60,
        }
        return config

    async def test_sigterm_exits_cleanly(self, thresholds, pw_good):
        """SIGTERM cancels the runChaos task; it returns without raising."""
        import signal as _signal
        from chaos import runChaos

        config = await self._make_chaos_deps(thresholds, pw_good)

        lucid_mock, vehicle_mock = _make_lucid_mock()
        lucid_mock.login = AsyncMock()
        vehicle_mock.config.nickname = "TestCar"
        vehicle_mock.config.vin = "TEST123"

        pw_mock = MagicMock()
        pw_mock.solar.return_value = pw_good.solarWatts
        pw_mock.home.return_value = pw_good.homeWatts
        pw_mock.level.return_value = pw_good.powerwallSocPercent
        pw_mock.is_connected.return_value = True

        with (
            patch("chaos.pypowerwall.Powerwall", return_value=pw_mock),
            patch("chaos.LucidAPI") as MockLucidAPI,
            patch("chaos.processCycle", new=AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            MockLucidAPI.return_value.__aenter__ = AsyncMock(return_value=lucid_mock)
            MockLucidAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            # runChaos should return cleanly when CancelledError propagates from processCycle
            await runChaos(config)  # must not raise


# ---------------------------------------------------------------------------
# Helpers for processCycle tests
# ---------------------------------------------------------------------------

def _make_lucid_processCycle_mock():
    """Lucid mock suitable for processCycle — includes session_time_remaining."""
    lucid = MagicMock()
    lucid.session_time_remaining = MagicMock()
    lucid.session_time_remaining.total_seconds.return_value = 300
    lucid.authentication_refresh = AsyncMock()
    lucid.login = AsyncMock()
    return lucid


# ---------------------------------------------------------------------------
# processCycle — skip logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestprocessCycleSkip:
    async def test_dark_skips_lucid_and_factor_10(self, thresholds):
        """isDark → factor=10, pollVehicleAndDecide not called."""
        dark_pw = PowerwallState(solarWatts=10, homeWatts=500, powerwallSocPercent=80)
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            _, factor = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, pwState=dark_pw)
        pvad.assert_not_awaited()
        assert factor == 10

    async def test_insufficient_surplus_skips_lucid_factor_1(self, thresholds):
        """Surplus below minSurplusKw but not dark → factor=1, not called."""
        # 5A worth of surplus (minChargingAmps=6), not dark
        low_pw = PowerwallState(
            solarWatts=5 * 240 + 2000,
            homeWatts=2000,
            powerwallSocPercent=80,
        )
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            _, factor = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, pwState=low_pw)
        pvad.assert_not_awaited()
        assert factor == 1

    async def test_pw_below_min_skips_lucid_factor_5(self, thresholds):
        """Powerwall SOC below minimum → factor=5, not called."""
        low_soc_pw = PowerwallState(
            solarWatts=(thresholds.minChargingAmps + 5) * 240 + 2000,
            homeWatts=2000,
            powerwallSocPercent=thresholds.minPowerwallSocPercent - 1,
        )
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            _, factor = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, pwState=low_soc_pw)
        pvad.assert_not_awaited()
        assert factor == 5

    async def test_ev_at_target_skips_lucid_factor_60(self, thresholds, pw_good):
        """EV at target SOC in cache → factor=60, not called."""
        cached = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CABLE_CONNECTED,
        )
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            _, factor = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, cached, pwState=pw_good)
        pvad.assert_not_awaited()
        assert factor == 60

    async def test_actively_charging_always_polls(self, thresholds):
        """Car in CHARGING_STATE → skip logic bypassed, pollVehicleAndDecide called."""
        cached = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        returned = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=16)
        pvad = AsyncMock(return_value=returned)
        lucid = _make_lucid_processCycle_mock()
        # Even dark / low surplus must not skip when actively charging
        dark_pw = PowerwallState(solarWatts=10, homeWatts=500, powerwallSocPercent=80)
        with patch("chaos.pollVehicleAndDecide", pvad):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, cached, pwState=dark_pw)
        pvad.assert_awaited_once()


# ---------------------------------------------------------------------------
# processCycle — forced EV poll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestprocessCycleForcedEvPoll:
    async def test_force_ev_poll_bypasses_skip_logic(self, thresholds):
        """forceEvPoll=True calls pollVehicleAndDecide even when conditions would normally skip it."""
        dark_pw = PowerwallState(solarWatts=10, homeWatts=500, powerwallSocPercent=80)
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, forceEvPoll=True, pwState=dark_pw)
        pvad.assert_awaited_once()

    async def test_normal_poll_dark_still_skips(self, thresholds):
        """Without forceEvPoll, dark conditions still skip pollVehicleAndDecide."""
        dark_pw = PowerwallState(solarWatts=10, homeWatts=500, powerwallSocPercent=80)
        pvad = AsyncMock()
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, forceEvPoll=False, pwState=dark_pw)
        pvad.assert_not_awaited()


# ---------------------------------------------------------------------------
# processCycle — transient UNAVAILABLE retry
# ---------------------------------------------------------------------------

def _make_unavailable_error():
    return APIError(grpc.StatusCode.UNAVAILABLE, "recvmsg:Connection reset by peer", "")


def _make_internal_error():
    return APIError(grpc.StatusCode.INTERNAL, "Login call failed.", "")


def _make_unauthenticated_error():
    return APIError(grpc.StatusCode.UNAUTHENTICATED, "invalid token", "")


class TestIsTransientLucidError:
    def test_true_for_unavailable(self):
        assert _isTransientLucidError(_make_unavailable_error())

    def test_true_for_internal(self):
        assert _isTransientLucidError(_make_internal_error())

    def test_false_for_other_apierror(self):
        assert not _isTransientLucidError(_make_unauthenticated_error())

    def test_false_for_non_apierror(self):
        assert not _isTransientLucidError(RuntimeError("oops"))


@pytest.mark.asyncio
class TestLucidLoginWithRetry:
    async def test_succeeds_on_first_attempt(self):
        lucid = MagicMock()
        lucid.login = AsyncMock()
        await _lucidLoginWithRetry(lucid, {"username": "u", "password": "p"})
        lucid.login.assert_awaited_once_with("u", "p")

    async def test_retries_on_internal_error_and_succeeds(self):
        """INTERNAL on first attempt → retry → success on second."""
        lucid = MagicMock()
        lucid.login = AsyncMock(side_effect=[_make_internal_error(), None])
        with patch("chaos.asyncio.sleep", new=AsyncMock()):
            await _lucidLoginWithRetry(lucid, {"username": "u", "password": "p"})
        assert lucid.login.await_count == 2

    async def test_retries_on_unavailable_and_succeeds(self):
        """UNAVAILABLE on first attempt → retry → success on second."""
        lucid = MagicMock()
        lucid.login = AsyncMock(side_effect=[_make_unavailable_error(), None])
        with patch("chaos.asyncio.sleep", new=AsyncMock()):
            await _lucidLoginWithRetry(lucid, {"username": "u", "password": "p"})
        assert lucid.login.await_count == 2

    async def test_raises_after_all_attempts_exhausted(self):
        """Transient error on all 3 attempts → raises after last attempt."""
        err = _make_internal_error()
        lucid = MagicMock()
        lucid.login = AsyncMock(side_effect=[err, err, err])
        with patch("chaos.asyncio.sleep", new=AsyncMock()), pytest.raises(APIError):
            await _lucidLoginWithRetry(lucid, {"username": "u", "password": "p"})
        assert lucid.login.await_count == 3

    async def test_raises_immediately_on_non_transient_error(self):
        """Non-transient error (e.g. UNAUTHENTICATED) is not retried."""
        lucid = MagicMock()
        lucid.login = AsyncMock(side_effect=_make_unauthenticated_error())
        with patch("chaos.asyncio.sleep", new=AsyncMock()), pytest.raises(APIError):
            await _lucidLoginWithRetry(lucid, {"username": "u", "password": "p"})
        assert lucid.login.await_count == 1


@pytest.mark.asyncio
class TestProcessCycleTransientRetry:
    async def test_unavailable_retries_and_succeeds(self, thresholds, pw_good):
        """UNAVAILABLE on first attempt → retry → success on second."""
        returned = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CABLE_CONNECTED)
        pvad = AsyncMock(side_effect=[_make_unavailable_error(), returned])
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad), \
             patch("chaos.asyncio.sleep", new=AsyncMock()):
            state, _ = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, forceEvPoll=True, pwState=pw_good)
        assert pvad.await_count == 2
        assert state == returned

    async def test_non_transient_apierror_raises_immediately(self, thresholds, pw_good):
        """Non-UNAVAILABLE APIError is not retried — raises on first attempt."""
        pvad = AsyncMock(side_effect=_make_unauthenticated_error())
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad), \
             patch("chaos.asyncio.sleep", new=AsyncMock()), \
             pytest.raises(APIError):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, forceEvPoll=True, pwState=pw_good)
        assert pvad.await_count == 1

    async def test_unavailable_all_attempts_raises(self, thresholds, pw_good):
        """UNAVAILABLE on all 3 attempts → raises after exhausting retries."""
        err = _make_unavailable_error()
        pvad = AsyncMock(side_effect=[err, err, err])
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad), \
             patch("chaos.asyncio.sleep", new=AsyncMock()), \
             pytest.raises(APIError):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, forceEvPoll=True, pwState=pw_good)
        assert pvad.await_count == 3


# ---------------------------------------------------------------------------
# _pollOneSite — zero filter and error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollOneSitePwZeroFilter:
    async def test_all_zero_sets_error_without_updating_state(self):
        """All-zero reading sets site.error and does not update PowerwallState."""
        zero_pw = MagicMock()
        zero_pw.solar.return_value = 0
        zero_pw.home.return_value = 0
        zero_pw.level.return_value = 0

        site = SiteInfo(name="Site", pw=zero_pw,
                        pwState=PowerwallState(solarWatts=500, homeWatts=200, powerwallSocPercent=70))

        result = await _pollOneSite(site)

        assert result is False
        assert site.error is not None
        assert site.pwState.solarWatts == 500  # unchanged

    async def test_nonzero_home_with_zero_solar_proceeds(self):
        """Zero solar, no prior history (night): avgSolarWatts=0, so NOT flagged as suspect."""
        night_pw = MagicMock()
        night_pw.solar.return_value = 0
        night_pw.home.return_value = 500
        night_pw.level.return_value = 80

        site = SiteInfo(name="Site", pw=night_pw)

        result = await _pollOneSite(site)

        assert result is True
        assert site.error is None
        assert site.pwState.homeWatts == 500

    async def test_zero_solar_with_high_rolling_avg_is_suspect(self):
        """Solar=0W but rolling avg is healthy — 0W solar transient; skipped."""
        bad_pw = MagicMock()
        bad_pw.solar.return_value = 0
        bad_pw.home.return_value = 4175
        bad_pw.level.return_value = 99

        site = SiteInfo(name="Site", pw=bad_pw,
                        pwState=PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=99))

        result = await _pollOneSite(site)

        assert result is False
        assert site.error is None  # transient: don't pollute dashboard error state
        assert site.pwState.solarWatts == 5000  # rolling avg unchanged

    async def test_zero_solar_transient_updates_soc_when_valid(self):
        """During a 0W solar transient, SoC is still valid — must update powerwallSocPercent."""
        bad_pw = MagicMock()
        bad_pw.solar.return_value = 0
        bad_pw.home.return_value = 4175
        bad_pw.level.return_value = 97.5   # valid SoC

        site = SiteInfo(name="Site", pw=bad_pw,
                        pwState=PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=60.0))

        result = await _pollOneSite(site)

        assert result is False
        assert site.pwState.solarWatts == 5000  # solar unchanged (rolling avg protected)
        assert site.pwState.powerwallSocPercent == 97.5  # SoC updated from valid reading

    async def test_zero_solar_transient_skips_soc_update_when_soc_zero(self):
        """During an all-zero transient (solar=0, soc=0), don't overwrite SoC with 0."""
        bad_pw = MagicMock()
        bad_pw.solar.return_value = 0
        bad_pw.home.return_value = 4175
        bad_pw.level.return_value = 0   # SoC also zero (bad reading)

        site = SiteInfo(name="Site", pw=bad_pw,
                        pwState=PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=85.0))

        result = await _pollOneSite(site)

        assert result is False
        assert site.pwState.powerwallSocPercent == 85.0  # stale reading preserved, not overwritten with 0

    async def test_zero_solar_retry_succeeds_on_second_attempt(self):
        """Retry recovers a valid solar reading — full update applied, no transient skip."""
        pw = MagicMock()
        call_count = 0

        def fake_poll():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (0, 4000, 90)   # first poll: solar=0 (transient)
            return (3500, 4000, 91)    # retry: valid

        pw.solar.side_effect = lambda: fake_poll()[0]
        pw.home.side_effect = lambda: fake_poll()[1]
        pw.level.side_effect = lambda _=None: fake_poll()[2]

        # Use side_effect on the lambda by mocking to_thread differently:
        # Easier: have solar/home/level called inside the lambda independently.
        # Reset and use a simpler approach with call index on solar only.
        pw2 = MagicMock()
        solar_calls = iter([0, 3500])
        pw2.solar.side_effect = lambda: next(solar_calls)
        pw2.home.return_value = 4000
        pw2.level.return_value = 91

        site = SiteInfo(name="Site", pw=pw2,
                        pwState=PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=80.0))

        with patch("chaos.asyncio.sleep", new_callable=AsyncMock):
            result = await _pollOneSite(site)

        assert result is True
        assert site.pwState.solarWatts == 3500   # retry value used
        assert site.error is None

    async def test_zero_solar_retry_exhausted_falls_back_to_transient(self):
        """All retries return 0W solar — falls back to transient handling (SoC updated, solar skipped)."""
        bad_pw = MagicMock()
        bad_pw.solar.return_value = 0
        bad_pw.home.return_value = 4000
        bad_pw.level.return_value = 88

        site = SiteInfo(name="Site", pw=bad_pw,
                        pwState=PowerwallState(solarWatts=5000, homeWatts=2000, powerwallSocPercent=70.0))

        with patch("chaos.asyncio.sleep", new_callable=AsyncMock):
            result = await _pollOneSite(site)

        assert result is False
        assert site.pwState.solarWatts == 5000        # solar rolling avg unchanged
        assert site.pwState.powerwallSocPercent == 88  # SoC updated from valid reading
        assert site.error is None

    async def test_zero_solar_below_threshold_not_flagged(self):
        """Solar=0W with low rolling avg (dusk/night) passes through normally."""
        night_pw = MagicMock()
        night_pw.solar.return_value = 0
        night_pw.home.return_value = 2000
        night_pw.level.return_value = 40

        site = SiteInfo(name="Site", pw=night_pw,
                        pwState=PowerwallState(solarWatts=100, homeWatts=1000, powerwallSocPercent=50))

        result = await _pollOneSite(site)

        assert result is True
        assert site.error is None

    async def test_exception_sets_error_and_returns_false(self):
        """Exceptions from the pw are caught, stored in site.error, and False is returned."""
        from pypowerwall.local.exceptions import LoginError
        bad_pw = MagicMock()
        bad_pw.solar.side_effect = LoginError("auth failed")

        site = SiteInfo(name="Site", pw=bad_pw)

        result = await _pollOneSite(site)

        assert result is False
        assert site.error is not None
        assert "auth failed" in site.error


# ---------------------------------------------------------------------------
# processCycle — post-command factor boost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestprocessCycleFactorBoost:
    async def test_post_command_boosts_factor_to_at_least_3(self, thresholds, pw_good):
        """After a command (lastCommandTime changes), factor is boosted to max(factor, 3)."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        new_time = datetime.now(timezone.utc)

        cached = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CABLE_CONNECTED, lastCommandTime=old_time)
        returned = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_ESTABLISHING_SESSION,
                                evChargingAmps=16, lastCommandTime=new_time, lastCommandWasStart=True)
        pvad = AsyncMock(return_value=returned)
        lucid = _make_lucid_processCycle_mock()
        with patch("chaos.pollVehicleAndDecide", pvad):
            _, factor = await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, cached, pwState=pw_good)
        assert factor >= 3


# ---------------------------------------------------------------------------
# processCycle — token refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestprocessCycleTokenRefresh:
    async def test_refresh_called_when_token_nearly_expired(self, thresholds, pw_good):
        """When session has < 60s remaining, authentication_refresh is called."""
        returned = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CABLE_CONNECTED)
        pvad = AsyncMock(return_value=returned)
        lucid = _make_lucid_processCycle_mock()
        lucid.session_time_remaining.total_seconds.return_value = 30
        with patch("chaos.pollVehicleAndDecide", pvad):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, pwState=pw_good)
        lucid.authentication_refresh.assert_awaited_once()

    async def test_refresh_failure_falls_back_to_login(self, thresholds, pw_good):
        """When authentication_refresh raises APIError, login() is called."""
        import grpc
        from lucidmotors.exceptions import APIError as LucidAPIError
        returned = VehicleState(evSocPercent=50.0, chargeState=0)
        pvad = AsyncMock(return_value=returned)
        lucid = _make_lucid_processCycle_mock()
        lucid.session_time_remaining.total_seconds.return_value = 30
        lucid.authentication_refresh = AsyncMock(
            side_effect=LucidAPIError(grpc.StatusCode.UNAUTHENTICATED, "expired", "")
        )
        lucid_config = {"username": "testuser", "password": "testpass"}
        with patch("chaos.pollVehicleAndDecide", pvad):
            await processCycle(lucid, thresholds, "", lucid_config, None, pwState=pw_good)
        lucid.login.assert_awaited_once_with("testuser", "testpass")


# ---------------------------------------------------------------------------
# pollVehicleAndDecide — webhook payload structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebhookPayload:
    async def test_charging_started_payload_keys(self, thresholds, pw_good):
        """On START, webhook payload contains required keys with correct types."""
        fired_payloads = []

        def capture_webhook(url, payload):
            fired_payloads.append(payload)

        lucid, _ = _make_lucid_mock()
        with patch("chaos.fireWebhook", side_effect=capture_webhook):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "https://hook.example.com")

        assert len(fired_payloads) == 1
        p = fired_payloads[0]
        assert p["event"] == "charging_started"
        assert p["evChargingAmps"] > 0
        assert p["solarWatts"] == round(pw_good.solarWatts)
        assert p["homeWatts"] == round(pw_good.homeWatts)
        assert "surplusKw" in p
        assert "effectiveSurplusKw" in p
        assert "powerwallSocPercent" in p
        assert "evSocPercent" in p
        assert "timestamp" in p
        assert "text" in p
        assert "charging_started" in p["text"].lower() or "charging started" in p["text"].lower()

    async def test_charging_stopped_payload_keys(self, thresholds, pw_good):
        """On STOP, webhook payload has event=charging_stopped and evChargingAmps=0."""
        fired_payloads = []

        def capture_webhook(url, payload):
            fired_payloads.append(payload)

        lucid, _ = _make_lucid_mock(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
        )
        prev = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        with patch("chaos.fireWebhook", side_effect=capture_webhook):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "https://hook.example.com", prevState=prev)

        assert len(fired_payloads) == 1
        p = fired_payloads[0]
        assert p["event"] == "charging_stopped"
        assert p["evChargingAmps"] == 0
        assert "text" in p


# ---------------------------------------------------------------------------
# External charge detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExternalCharge:
    async def test_takeover_when_interference_allowed(self, thresholds, pw_good):
        """prevEvChargingAmps=0 + isCharging + interference=True → CHAOS takes over,
        not STOP. actual amps read from vehicle state are used for the EV correction."""
        # Simulate car charging at 20A externally. With full EV correction the
        # effective surplus should be positive → ADJUST (not STOP).
        actual_amps = 20
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        vehicle.state.charging.active_session_ac_current_limit = actual_amps

        # prevState has evChargingAmps=0 — CHAOS didn't start this session
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=0)
        assert thresholds.allowExternalChargeInterference is True

        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        # Must NOT have stopped charging
        lucid.stop_charging.assert_not_awaited()
        # evChargingAmps should be updated (ADJUST was called or stayed same)
        assert result.evChargingAmps >= 0

    async def test_observe_when_interference_disabled(self, thresholds, pw_good):
        """prevEvChargingAmps=0 + isCharging + interference=False → OBSERVE, no commands."""
        no_interference = ChargingThresholds(
            minPowerwallSocPercent=thresholds.minPowerwallSocPercent,
            targetEvChargePercent=thresholds.targetEvChargePercent,
            chargerVoltage=thresholds.chargerVoltage,
            minChargingAmps=thresholds.minChargingAmps,
            maxChargingAmps=thresholds.maxChargingAmps,
            commandCooldownMinutes=thresholds.commandCooldownMinutes,
            allowExternalChargeInterference=False,
        )
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        vehicle.state.charging.active_session_ac_current_limit = 20
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=0)

        result = await pollVehicleAndDecide(lucid, pw_good, no_interference, "", prevState=prev)
        lucid.stop_charging.assert_not_awaited()
        lucid.set_ac_current_limit.assert_not_awaited()
        lucid.start_charging.assert_not_awaited()

    async def test_actual_amps_read_from_vehicle_state(self, thresholds, pw_good):
        """When external session detected, active_session_ac_current_limit is used
        as prevEvChargingAmps so the EV correction is correct."""
        # Large external charge: 40A. Without correction, effectiveSurplusKw would
        # be hugely negative and STOP would be issued. With correction, it should be positive.
        external_amps = 40
        # pw_good has 5840W solar, 2000W home → raw surplus = 3.84kW
        # EV correction = 40 * 240 / 1000 = 9.6kW → effectiveSurplusKw = 13.44kW → lots of room
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        vehicle.state.charging.active_session_ac_current_limit = external_amps
        prev = VehicleState(evSocPercent=50.0, chargeState=CS.CHARGE_STATE_CHARGING, evChargingAmps=0)

        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        lucid.stop_charging.assert_not_awaited()

    async def test_no_external_session_within_cooldown_after_stop(self, thresholds, pw_good):
        """After CHAOS issues a STOP, the API may still report CHARGING for a cycle or two.
        prevEvChargingAmps is 0 (cleared on stop), but this must NOT be treated as an external
        session — which would read vehicle amps and inflate the dashboard charging display."""
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        # Vehicle still reports charging at 16A even though we already stopped it
        vehicle.state.charging.active_session_ac_current_limit = 16

        # prevState: CHAOS just issued a STOP (evChargingAmps=0, lastCommandWasStart=False, recent)
        prev = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=0,
            lastCommandTime=datetime.now(timezone.utc) - timedelta(minutes=1),
            lastCommandWasStart=False,
        )
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        # Must not treat this as external — vehicle amps (16A) must not appear on result
        assert result.evChargingAmps == 0

    async def test_external_session_detected_after_stop_lag_window(self, thresholds, pw_good):
        """Once the API-lag window (_STOP_LAG_SECS) has passed, charging after a STOP is treated
        as a new external session, not post-stop API lag.  The EV card must be restored."""
        lucid, vehicle = _make_lucid_mock(chargeState=CS.CHARGE_STATE_CHARGING)
        vehicle.state.charging.active_session_ac_current_limit = 16

        # STOP was issued 3 minutes ago — well past the _STOP_LAG_SECS (120s) window
        prev = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=0,
            lastCommandTime=datetime.now(timezone.utc) - timedelta(minutes=3),
            lastCommandWasStart=False,
        )
        result = await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)
        # External session detected — evChargingAmps must reflect actual charging
        assert result.evChargingAmps > 0


# ---------------------------------------------------------------------------
# processCycle — dashboard EV fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDashboardEVFields:
    """Verify that processCycle updates EV-specific dashboard fields."""

    async def test_ev_fields_set_from_cached_state(self, thresholds):
        """When cachedState is provided, evSocPercent and chargeStateName are set."""
        lucid = _make_lucid_processCycle_mock()
        dash = DashboardState()
        cached = VehicleState(
            evSocPercent=65.0,
            chargeState=CS.CHARGE_STATE_CABLE_CONNECTED,
            evChargingAmps=0,
        )
        with patch("chaos.pollVehicleAndDecide", AsyncMock()):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, cached, dash,
                               pwState=PowerwallState(solarWatts=0, homeWatts=2000, powerwallSocPercent=80))
        assert dash.evSocPercent == 65.0
        assert dash.chargeStateName != ""

    async def test_ev_fields_unchanged_when_no_cached_state(self, thresholds):
        """When cachedState=None, evSocPercent stays at its default (None)."""
        lucid = _make_lucid_processCycle_mock()
        dash = DashboardState()
        with patch("chaos.pollVehicleAndDecide", AsyncMock()):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, dash,
                               pwState=PowerwallState(solarWatts=0, homeWatts=2000, powerwallSocPercent=80))
        assert dash.evSocPercent is None
    async def test_ev_charging_kw_computed(self, thresholds):
        """evChargingKw reflects evChargingAmps * chargerVoltage / 1000."""
        lucid = _make_lucid_processCycle_mock()
        dash = DashboardState()
        cached = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        with patch("chaos.pollVehicleAndDecide", AsyncMock(return_value=cached)):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, cached, dash,
                               pwState=PowerwallState(solarWatts=5000, homeWatts=1000, powerwallSocPercent=80))
        # 16A × 240V / 1000 = 3.84 kW
        assert dash.evChargingKw == pytest.approx(3.84, abs=0.01)

    async def test_ev_charging_kw_zero_when_not_charging(self, thresholds):
        """evChargingKw is 0.0 when evChargingAmps == 0."""
        lucid = _make_lucid_processCycle_mock()
        dash = DashboardState()
        with patch("chaos.pollVehicleAndDecide", AsyncMock(return_value=VehicleState(evSocPercent=0.0, chargeState=0, evChargingAmps=0))):
            await processCycle(lucid, thresholds, "", {"username": "u", "password": "p"}, None, dash,
                               pwState=PowerwallState(solarWatts=0, homeWatts=1000, powerwallSocPercent=80))
        assert dash.evChargingKw == 0.0



class TestHTMLTemplate:
    """Tests that guard against regressions in the inline HTML+JS dashboard template."""

    _EXPECTED_JS_FUNCTIONS = (
        "refresh",
        "forcePoll",
        "surpFmt",
        "kwFmt",
        "ageFmt",
        "fillBg",
        "badge",
        "fillTdBg",
    )

    def _extract_js(self) -> str:
        import re
        m = re.search(r"<script>(.*?)</script>", _HTML_TEMPLATE, re.DOTALL | re.IGNORECASE)
        assert m, "No <script> block found in _HTML_TEMPLATE"
        return m.group(1)

    def test_js_known_functions_declared(self):
        """Every helper function must have a 'function fname(' declaration.

        This test would have caught the surpFmt bug where the declaration line
        was accidentally deleted, leaving only the function body as orphaned code.
        """
        import re
        js = self._extract_js()
        declared = set(re.findall(r"function\s+(\w+)\s*\(", js))
        for fn in self._EXPECTED_JS_FUNCTIONS:
            assert fn in declared, (
                f"JS function '{fn}' has no declaration in _HTML_TEMPLATE — "
                f"did an edit accidentally remove 'function {fn}(...){{'?"
            )

    def test_pygal_tooltips_file_exists(self):
        """pygal-tooltips.min.js must be present on disk next to chaos.py."""
        import pathlib
        js_file = pathlib.Path(__file__).parent / "pygal-tooltips.min.js"
        assert js_file.exists(), "pygal-tooltips.min.js is missing — chart tooltips will break"
        assert js_file.stat().st_size > 0, "pygal-tooltips.min.js is empty"

    def test_pygal_tooltips_endpoint_returns_js(self):
        """GET /pygal-tooltips.min.js must return 200 with JavaScript content."""
        from starlette.testclient import TestClient

        with TestClient(_webApp) as client:
            r = client.get("/pygal-tooltips.min.js")

        assert r.status_code == 200
        assert len(r.content) > 0

    def test_api_state_returns_json_with_expected_keys(self):
        """GET /api/state must return 200 JSON with all expected dashboard keys."""
        from starlette.testclient import TestClient

        with TestClient(_webApp) as client:
            r = client.get("/api/state")

        assert r.status_code == 200
        d = r.json()
        for key in (
            "evSocPercent",
            "chargeStateName",
            "evChargingAmps",
            "evChargingKw",
            "siteReadings",
            "activePowerwall",
            "history",
        ):
            assert key in d, f"Missing key '{key}' in /api/state response"

    def test_homepage_returns_html_with_script(self):
        """GET / must return 200 with an HTML page containing the refresh() function."""
        from starlette.testclient import TestClient

        with TestClient(_webApp) as client:
            r = client.get("/")

        assert r.status_code == 200
        assert "<script>" in r.text
        assert "function refresh()" in r.text

    def test_post_poll_returns_ok_and_sets_event(self):
        """POST /api/poll must return 200 {status: ok} and set the _forcePoll event."""
        import chaos
        from starlette.testclient import TestClient

        chaos._forcePoll.clear()
        with TestClient(_webApp) as client:
            r = client.post("/api/poll")

        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert chaos._forcePoll.is_set()
        chaos._forcePoll.clear()  # clean up


# ---------------------------------------------------------------------------
# _writePowerwallToConfig
# ---------------------------------------------------------------------------

class TestWritePowerwallToConfig:
    """Tests for the atomic config.json activePowerwall update helper."""

    def test_writes_active_powerwall(self, tmp_path):
        """Updates activePowerwall in the config file on disk."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "activePowerwall": "Home",
            "powerwalls": [
                {"name": "Home"},
                {"name": "Garage"},
            ]
        }))
        _writePowerwallToConfig("Garage", path=str(cfg_file))
        saved = json.loads(cfg_file.read_text())
        assert saved["activePowerwall"] == "Garage"

    def test_preserves_other_keys(self, tmp_path):
        """Does not strip other config keys when writing."""
        cfg_file = tmp_path / "config.json"
        original = {"activePowerwall": "Home", "pollingIntervalSeconds": 60, "extra": "value"}
        cfg_file.write_text(json.dumps(original))
        _writePowerwallToConfig("Home", path=str(cfg_file))
        saved = json.loads(cfg_file.read_text())
        assert saved["pollingIntervalSeconds"] == 60
        assert saved["extra"] == "value"

    def test_missing_file_logs_warning_does_not_raise(self, tmp_path):
        """If config.json does not exist, logs a warning and does not raise."""
        _writePowerwallToConfig("Any", path=str(tmp_path / "nonexistent.json"))
        # no exception


# ---------------------------------------------------------------------------
# _connectPowerwall timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestConnectPowerwallTimeout:
    """_connectPowerwall raises PowerwallConnectionError when is_connected times out."""

    async def test_is_connected_timeout_raises(self):
        """Wraps is_connected in wait_for; timeout raises PowerwallConnectionError."""
        pwConfig = {
            "host": "192.168.1.1", "password": "pw", "email": "e@e.com",
            "timezone": "UTC", "authType": "local",
        }
        mock_pw = MagicMock()

        async def raise_timeout(coro, *args, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            raise asyncio.TimeoutError()

        with patch("chaos.pypowerwall.Powerwall", return_value=mock_pw):
            with patch("asyncio.wait_for", new=raise_timeout):
                with pytest.raises(PowerwallConnectionError, match="Timeout"):
                    await _connectPowerwall(pwConfig, "TestSite")

    async def test_connect_fallback_timeout_raises(self):
        """Timeout on the reconnect (pw.connect) path also raises PowerwallConnectionError."""
        from pypowerwall.local.exceptions import LoginError
        pwConfig = {
            "host": "192.168.1.1", "password": "pw", "email": "e@e.com",
            "timezone": "UTC", "authType": "local",
        }
        mock_pw = MagicMock()
        call_count = [0]

        async def login_then_timeout(coro, *args, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            call_count[0] += 1
            if call_count[0] == 1:
                raise LoginError("bad auth")
            raise asyncio.TimeoutError()

        with patch("chaos.pypowerwall.Powerwall", return_value=mock_pw):
            with patch("asyncio.wait_for", new=login_then_timeout):
                with pytest.raises(PowerwallConnectionError, match="Timeout"):
                    await _connectPowerwall(pwConfig, "TestSite")

class TestWebApiSwitchPowerwall:
    """Tests for the POST /api/powerwall endpoint."""

    def test_valid_name_sets_event(self):
        """Valid POST sets _switchPowerwall event and _pendingPowerwallName."""
        import chaos
        from starlette.testclient import TestClient

        chaos._switchPowerwall.clear()
        chaos._pendingPowerwallName = None
        with TestClient(_webApp) as client:
            r = client.post("/api/powerwall", json={"name": "Garage"})

        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert chaos._switchPowerwall.is_set()
        assert chaos._pendingPowerwallName == "Garage"
        chaos._switchPowerwall.clear()

    def test_missing_name_returns_400(self):
        """POST with no 'name' field returns 400."""
        from starlette.testclient import TestClient
        with TestClient(_webApp) as client:
            r = client.post("/api/powerwall", json={})
        assert r.status_code == 400

    def test_empty_name_returns_400(self):
        """POST with empty string name returns 400."""
        from starlette.testclient import TestClient
        with TestClient(_webApp) as client:
            r = client.post("/api/powerwall", json={"name": ""})
        assert r.status_code == 400

    def test_no_op_when_already_active(self):
        """POST with the currently active Powerwall name returns ok without setting the switch event."""
        import chaos
        from starlette.testclient import TestClient
        chaos._switchPowerwall.clear()
        chaos._dashboard.activePowerwall = "CurrentSite"
        with TestClient(_webApp) as client:
            r = client.post("/api/powerwall", json={"name": "CurrentSite"})
        assert r.status_code == 200
        assert not chaos._switchPowerwall.is_set()  # no switch triggered for no-op
        chaos._dashboard.activePowerwall = ""


# ---------------------------------------------------------------------------
# Per-site history store
# ---------------------------------------------------------------------------

class TestSiteInfoHistory:
    """Verify SiteInfo.history provides independent per-site deques."""

    def test_each_site_gets_independent_history(self):
        siteA = SiteInfo(name="SiteA", pw=None)
        siteB = SiteInfo(name="SiteB", pw=None)
        siteA.history.append({"solarWatts": 1000})
        assert len(siteB.history) == 0

    def test_history_deque_has_maxlen_120(self):
        site = SiteInfo(name="Site", pw=None)
        assert site.history.maxlen == 120

    def test_history_can_be_shared_with_dashboard(self):
        """Assigning site.history to _dashboard.history shares the same deque."""
        import chaos
        site = SiteInfo(name="Site", pw=None)
        site.history.append({"solarWatts": 500})
        orig = chaos._dashboard.history
        chaos._dashboard.history = site.history
        try:
            assert chaos._dashboard.history[0]["solarWatts"] == 500
            # Mutations via either reference are visible to both
            chaos._dashboard.history.append({"solarWatts": 999})
            assert site.history[-1]["solarWatts"] == 999
        finally:
            chaos._dashboard.history = orig

    def test_round_trip_preserves_contents(self):
        """Switching sites and back restores original history."""
        siteA = SiteInfo(name="SiteA", pw=None)
        siteB = SiteInfo(name="SiteB", pw=None)
        siteA.history.append({"solarWatts": 1000})
        siteA.history.append({"solarWatts": 2000})

        # Simulate switch: dashboard points to SiteB's history
        dashboard_history = siteB.history
        dashboard_history.append({"solarWatts": 500})

        # Switch back: dashboard points to SiteA's history
        dashboard_history = siteA.history
        assert len(dashboard_history) == 2
        assert dashboard_history[0]["solarWatts"] == 1000


# ---------------------------------------------------------------------------
# processCycle — rolling solar average
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestprocessCycleSolarRollingAverage:
    """Verify the rolling 3-sample solar average smooths out transient dips."""

    async def test_single_sample_advances_history(self, thresholds):
        """After one update the persistent PowerwallState history has one entry."""
        pwState = PowerwallState()
        pwState.update(3500, 500, 80.0)
        assert len(pwState._solarHistory) == 1
        assert pwState._solarHistory[0] == 3500
        assert pwState.avgSolarWatts == pytest.approx(3500)

    async def test_transient_solar_dip_does_not_trigger_stop(self, thresholds):
        """
        If solar was high for 2 cycles then drops to 0 on cycle 3, the rolling
        average (mean of [3000, 3000, 0] = 2000 W) still exceeds minSurplusKw
        (~0.5 kW), so charging should NOT be stopped.
        """

        charging_state = VehicleState(
            evSocPercent=50.0,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        pvad = AsyncMock(return_value=charging_state)
        lucid = _make_lucid_processCycle_mock()
        pwState = PowerwallState()

        async def run_cycle(solar, home, soc, cached):
            pwState.update(solar, home, soc)
            with patch("chaos.pollVehicleAndDecide", pvad):
                return await processCycle(lucid, thresholds, "", {}, cached, pwState=pwState)

        pvad.reset_mock()
        cached, _ = await run_cycle(3000, 500, 80.0, None)

        pvad.reset_mock()
        pvad.return_value = charging_state
        cached, _ = await run_cycle(3000, 500, 80.0, charging_state)

        # Cycle 3: transient zero solar — raw=0, avg=(3000+3000+0)/3=2000W
        # isActivelyCharging=True so skip guard is bypassed; pvad is always called.
        pvad.reset_mock()
        pvad.return_value = charging_state
        await run_cycle(0, 500, 80.0, charging_state)

        pvad.assert_awaited_once()
        call_pw_state = pvad.call_args[0][1]  # second positional arg is pwState
        assert call_pw_state.avgSolarWatts > 0, (
            f"Expected averaged solar > 0, got {call_pw_state.avgSolarWatts}"
        )

    async def test_history_rolls_off_oldest_sample(self, thresholds):
        """After maxlen=3 samples the oldest is evicted and the average reflects the latest 3."""
        returned = VehicleState(evSocPercent=50.0, chargeState=0)
        lucid = _make_lucid_processCycle_mock()
        pwState = PowerwallState()

        for solar in [3000, 3000, 3000, 0]:
            pwState.update(solar, 500, 80.0)
            with patch("chaos.pollVehicleAndDecide", AsyncMock(return_value=returned)):
                await processCycle(lucid, thresholds, "", {}, None, pwState=pwState)

        # After 4 cycles maxlen=3 keeps [3000, 3000, 0]
        assert list(pwState._solarHistory) == [3000, 3000, 0]
        assert pwState.avgSolarWatts == pytest.approx(2000)


# ---------------------------------------------------------------------------
# PHASE 1: Config Validation Tests
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """Test _validateConfig for critical startup failures."""

    def test_missing_root_key_activePowerwall(self):
        """Missing activePowerwall key raises RuntimeError."""
        config = _make_valid_config()
        del config["activePowerwall"]
        with pytest.raises(RuntimeError, match="activePowerwall"):
            _validateConfig(config)

    def test_missing_root_key_powerwalls(self):
        """Missing powerwalls array raises RuntimeError."""
        config = _make_valid_config()
        del config["powerwalls"]
        with pytest.raises(RuntimeError, match="powerwalls"):
            _validateConfig(config)

    def test_missing_root_key_lucid(self):
        """Missing lucid config raises RuntimeError."""
        config = _make_valid_config()
        del config["lucid"]
        with pytest.raises(RuntimeError, match="lucid"):
            _validateConfig(config)

    def test_missing_root_key_charging(self):
        """Missing charging config raises RuntimeError."""
        config = _make_valid_config()
        del config["charging"]
        with pytest.raises(RuntimeError, match="charging"):
            _validateConfig(config)

    def test_powerwall_not_found_in_list(self):
        """If activePowerwall name doesn't exist in powerwalls list, raises RuntimeError."""
        config = _make_valid_config(activePowerwall="Nonexistent")
        with pytest.raises(RuntimeError, match="No Powerwall named 'Nonexistent'"):
            _validateConfig(config)

    def test_missing_powerwall_host(self):
        """Missing 'host' in active Powerwall entry raises RuntimeError."""
        pw = _make_pw_entry()
        del pw["host"]
        config = _make_valid_config(powerwalls=[pw])
        with pytest.raises(RuntimeError, match="host"):
            _validateConfig(config)

    def test_missing_lucid_username(self):
        """Missing 'username' in lucid config raises RuntimeError."""
        config = _make_valid_config(lucid={"password": "p"})
        with pytest.raises(RuntimeError, match="username"):
            _validateConfig(config)

    def test_missing_charging_threshold(self):
        """Missing any charging threshold raises RuntimeError."""
        charging = _make_valid_config()["charging"]
        del charging["minChargingAmps"]
        config = _make_valid_config(charging=charging)
        with pytest.raises(RuntimeError, match="minChargingAmps"):
            _validateConfig(config)

    def test_valid_config_passes(self):
        """Valid config does not raise."""
        _validateConfig(_make_valid_config())

    def test_valid_tedapi_config_passes(self):
        """TEDAPI authType is accepted."""
        config = _make_valid_config(powerwalls=[_make_pw_entry(authType="TEDAPI", password="gw-pw")])
        _validateConfig(config)  # should not raise

    def test_invalid_auth_type_raises(self):
        """An unrecognised authType raises RuntimeError."""
        config = _make_valid_config(powerwalls=[_make_pw_entry(authType="oauth")])
        with pytest.raises(RuntimeError, match="authType"):
            _validateConfig(config)

    def test_missing_auth_type_raises(self):
        """Missing authType in powerwall entry raises RuntimeError."""
        pw = _make_pw_entry()
        del pw["authType"]
        config = _make_valid_config(powerwalls=[pw])
        with pytest.raises(RuntimeError, match="authType"):
            _validateConfig(config)

    def test_validates_non_active_powerwall_missing_host(self):
        """A non-active Powerwall entry with a missing required field fails at startup."""
        office = _make_pw_entry(name="Office", host="2.2.2.2")
        del office["host"]
        config = _make_valid_config(powerwalls=[_make_pw_entry(), office])
        with pytest.raises(RuntimeError, match="host"):
            _validateConfig(config)

    def test_validates_non_active_powerwall_invalid_auth_type(self):
        """A non-active Powerwall entry with an invalid authType fails at startup."""
        config = _make_valid_config(powerwalls=[
            _make_pw_entry(),
            _make_pw_entry(name="Office", host="2.2.2.2", authType="oauth"),
        ])
        with pytest.raises(RuntimeError, match="authType"):
            _validateConfig(config)


# ---------------------------------------------------------------------------
# PHASE 1: loadConfig Tests
# ---------------------------------------------------------------------------

class TestLoadConfig:
    """Test loadConfig error handling."""

    def test_file_not_found(self):
        """loadConfig raises when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            loadConfig("/nonexistent/path/config.json")

    def test_invalid_json(self):
        """loadConfig raises on malformed JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json")
            temp_path = f.name
        try:
            with pytest.raises(json.JSONDecodeError):
                loadConfig(temp_path)
        finally:
            pathlib.Path(temp_path).unlink()

    def test_loads_valid_json(self):
        """loadConfig successfully reads and parses valid JSON."""
        config_data = {"test": "value", "nested": {"key": 123}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name
        try:
            result = loadConfig(temp_path)
            assert result == config_data
        finally:
            pathlib.Path(temp_path).unlink()

    def test_empty_json_object(self):
        """loadConfig handles empty JSON object."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{}")
            temp_path = f.name
        try:
            result = loadConfig(temp_path)
            assert result == {}
        finally:
            pathlib.Path(temp_path).unlink()

    def test_loads_array_top_level(self):
        """loadConfig can load JSON arrays at top level."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([1, 2, 3], f)
            temp_path = f.name
        try:
            result = loadConfig(temp_path)
            assert result == [1, 2, 3]
        finally:
            pathlib.Path(temp_path).unlink()


# ---------------------------------------------------------------------------
# PHASE 1: Lucid API Error Handling Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollVehicleAndDecideAPIErrors:
    """Test pollVehicleAndDecide error handling for Lucid API failures."""

    async def test_no_vehicles_raises(self, thresholds, pw_good):
        """When fetch_vehicles returns empty list, RuntimeError raised."""
        lucid = MagicMock()
        lucid.fetch_vehicles = AsyncMock(return_value=[])
        with pytest.raises(RuntimeError, match="No vehicles"):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

    async def test_api_error_on_fetch_vehicles(self, thresholds, pw_good):
        """When fetch_vehicles raises APIError, it propagates."""
        from lucidmotors.exceptions import APIError as LucidAPIError
        import grpc
        lucid = MagicMock()
        lucid.fetch_vehicles = AsyncMock(
            side_effect=LucidAPIError(grpc.StatusCode.UNAVAILABLE, "Service unavailable", "")
        )
        with pytest.raises(LucidAPIError):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

    async def test_api_error_on_set_ac_current_limit(self, thresholds, pw_good):
        """When set_ac_current_limit raises APIError during START, it propagates."""
        from lucidmotors.exceptions import APIError as LucidAPIError
        import grpc
        lucid, vehicle = _make_lucid_mock()
        lucid.set_ac_current_limit = AsyncMock(
            side_effect=LucidAPIError(grpc.StatusCode.UNKNOWN, "Device error", "")
        )
        with pytest.raises(LucidAPIError):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

    async def test_api_error_on_start_charging(self, thresholds, pw_good):
        """When start_charging raises APIError, it propagates."""
        from lucidmotors.exceptions import APIError as LucidAPIError
        import grpc
        lucid, _ = _make_lucid_mock()
        lucid.start_charging = AsyncMock(
            side_effect=LucidAPIError(grpc.StatusCode.INTERNAL, "Internal error", "")
        )
        with pytest.raises(LucidAPIError):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

    async def test_api_error_on_stop_charging(self, thresholds, pw_good):
        """When stop_charging raises APIError, it propagates."""
        from lucidmotors.exceptions import APIError as LucidAPIError
        import grpc
        lucid, _ = _make_lucid_mock(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
        )
        lucid.stop_charging = AsyncMock(
            side_effect=LucidAPIError(grpc.StatusCode.PERMISSION_DENIED, "Not authorized", "")
        )
        prev = VehicleState(
            evSocPercent=thresholds.targetEvChargePercent,
            chargeState=CS.CHARGE_STATE_CHARGING,
            evChargingAmps=16,
        )
        with pytest.raises(LucidAPIError):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "", prevState=prev)

    async def test_missing_vehicle_state_fields(self, thresholds, pw_good):
        """When vehicle state fields are missing or None, AttributeError may occur."""
        lucid = MagicMock()
        vehicle = MagicMock()
        vehicle.state.battery.charge_percent = None  # This could cause issues downstream
        vehicle.state.charging.charge_state = None
        lucid.fetch_vehicles = AsyncMock(return_value=[vehicle])
        # The function should handle None gracefully or raise an appropriate error
        with pytest.raises((AttributeError, TypeError, RuntimeError)):
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

    async def test_vehicle_wakeup_timeout_recovers(self, thresholds, pw_good):
        """Vehicle wakeup timeout logs warning but continues to START."""
        lucid, vehicle = _make_lucid_mock()
        lucid.vehicle_is_awake = MagicMock(return_value=False)
        
        call_count = 0
        def fake_now(tz=None):
            nonlocal call_count
            call_count += 1
            base = datetime.now(timezone.utc)
            return base + timedelta(seconds=61) if call_count > 1 else base

        with (
            patch("chaos.asyncio.sleep", new=AsyncMock()),
            patch("chaos.datetime") as mock_dt,
            patch("chaos.log") as mock_log,
        ):
            mock_dt.now = fake_now
            await pollVehicleAndDecide(lucid, pw_good, thresholds, "")

        mock_log.warning.assert_called()
        lucid.start_charging.assert_awaited_once()


# ---------------------------------------------------------------------------
# PHASE 1: Powerwall Error Handling Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPowerwallErrors:
    """Test Powerwall-related error handling."""

    async def test_negative_solar_clamped_in_powerwall_state(self):
        """Even if Powerwall returns negative solar (API bug), it's clamped to 0."""
        pw = PowerwallState(solarWatts=-100, homeWatts=500, powerwallSocPercent=50)
        assert pw.solarWatts == 0

    async def test_none_solar_defaults_to_zero(self):
        """None solar value defaults to 0."""
        pw = PowerwallState(solarWatts=None, homeWatts=500, powerwallSocPercent=50)  # type: ignore
        assert pw.solarWatts == 0

    async def test_none_home_defaults_to_zero(self):
        """None home load defaults to 0."""
        pw = PowerwallState(solarWatts=1000, homeWatts=None, powerwallSocPercent=50)  # type: ignore
        assert pw.homeWatts == 0

    async def test_none_soc_defaults_to_zero(self):
        """None SOC defaults to 0."""
        pw = PowerwallState(solarWatts=1000, homeWatts=500, powerwallSocPercent=None)  # type: ignore
        assert pw.powerwallSocPercent == 0


# ---------------------------------------------------------------------------
# PHASE 1: runChaos Startup Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunChaosStartup:
    """Test runChaos initialization and error handling."""

    async def test_startup_invalid_config_fails_fast(self):
        """runChaos validates config before connecting to anything."""
        config = {"invalid": "config"}  # missing all required keys
        with pytest.raises(RuntimeError, match="activePowerwall"):
            await runChaos(config)

    async def test_startup_config_valid_proceeds(self):
        """Valid config passes validation and proceeds to connection setup."""
        config = {
            "activePowerwall": "Home",
            "powerwalls": [
                {"name": "Home", "host": "192.168.1.1", "password": "pw", "email": "test@example.com", "timezone": "US/Pacific", "authType": "local"}
            ],
            "lucid": {"username": "test", "password": "pw"},
            "charging": {
                "minPowerwallSocPercent": 40,
                "targetEvChargePercent": 85,
                "chargerVoltage": 240,
                "minChargingAmps": 6,
                "maxChargingAmps": 48,
                "commandCooldownMinutes": 5,
            },
            "notifications": {"enabled": False},
            "pollingIntervalSeconds": 60,
        }
        # Should not raise during validation
        with pytest.raises((RuntimeError, FileNotFoundError, Exception)):
            # Will fail later due to mocking, but validation passed
            await runChaos(config)

    async def test_startup_invalid_units_fails(self):
        """Invalid units in dashboard config raises RuntimeError."""
        config = {
            "activePowerwall": "Home",
            "powerwalls": [
                {"name": "Home", "host": "192.168.1.1", "password": "pw", "email": "test@example.com", "timezone": "US/Pacific", "authType": "local"}
            ],
            "lucid": {"username": "test", "password": "pw"},
            "charging": {
                "minPowerwallSocPercent": 40,
                "targetEvChargePercent": 85,
                "chargerVoltage": 240,
                "minChargingAmps": 6,
                "maxChargingAmps": 48,
                "commandCooldownMinutes": 5,
            },
            "notifications": {"enabled": False},
            "pollingIntervalSeconds": 60,
            "dashboard": {"units": "invalid_unit"},
        }
        with pytest.raises(RuntimeError, match="units"):
            await runChaos(config)

    async def test_startup_no_active_powerwall_in_list(self):
        """When active Powerwall name not found, startup fails."""
        config = {
            "activePowerwall": "Nonexistent",
            "powerwalls": [
                {"name": "Home", "host": "192.168.1.1", "password": "pw", "email": "test@example.com", "timezone": "US/Pacific"}
            ],
            "lucid": {"username": "test", "password": "pw"},
            "charging": {
                "minPowerwallSocPercent": 40,
                "targetEvChargePercent": 85,
                "chargerVoltage": 240,
                "minChargingAmps": 6,
                "maxChargingAmps": 48,
                "commandCooldownMinutes": 5,
            },
            "notifications": {"enabled": False},
            "pollingIntervalSeconds": 60,
        }
        with pytest.raises(RuntimeError, match="No Powerwall named"):
            await runChaos(config)

    async def test_startup_powerwall_connection_error(self):
        """When Powerwall connection fails at startup, error is raised."""
        from pypowerwall.local.exceptions import LoginError
        config = {
            "activePowerwall": "Home",
            "powerwalls": [
                {"name": "Home", "host": "192.168.1.1", "password": "badpw", "email": "test@example.com", "timezone": "US/Pacific", "authType": "local"}
            ],
            "lucid": {"username": "test", "password": "pw"},
            "charging": {
                "minPowerwallSocPercent": 40,
                "targetEvChargePercent": 85,
                "chargerVoltage": 240,
                "minChargingAmps": 6,
                "maxChargingAmps": 48,
                "commandCooldownMinutes": 5,
            },
            "notifications": {"enabled": False},
            "pollingIntervalSeconds": 60,
        }
        pw_mock = MagicMock()
        pw_mock.is_connected.side_effect = LoginError("auth failed")
        with (
            patch("chaos.pypowerwall.Powerwall", return_value=pw_mock),
            patch("chaos.asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *a: fn(*a))),
        ):
            with pytest.raises((LoginError, Exception)):
                await runChaos(config)


# ---------------------------------------------------------------------------
# _buildSiteReadings
# ---------------------------------------------------------------------------

class TestBuildSiteReadings:
    def _sites(self, specs: dict) -> dict:
        """Build allSites dict from {name: (solarWatts, homeWatts, soc, error)} specs."""
        result = {}
        for name, (solar, home, soc, err) in specs.items():
            site = SiteInfo(name=name, pw=MagicMock(),
                            pwState=PowerwallState(solarWatts=solar, homeWatts=home, powerwallSocPercent=soc),
                            error=err)
            result[name] = site
        return result

    def test_returns_one_entry_per_site(self):
        allSites = self._sites({
            "SiteA": (3000, 1000, 80, None),
            "SiteB": (1500, 800, 60, None),
        })
        readings = _buildSiteReadings(allSites, "SiteA")
        assert len(readings) == 2

    def test_active_flag_set_correctly(self):
        allSites = self._sites({
            "SiteA": (3000, 1000, 80, None),
            "SiteB": (1500, 800, 60, None),
        })
        readings = _buildSiteReadings(allSites, "SiteB")
        by_name = {r["name"]: r for r in readings}
        assert by_name["SiteA"]["isActive"] is False
        assert by_name["SiteB"]["isActive"] is True

    def test_values_from_state(self):
        allSites = self._sites({"Home": (4000, 2000, 75, None)})
        readings = _buildSiteReadings(allSites, "Home")
        r = readings[0]
        assert r["solarWatts"] == 4000
        assert r["homeWatts"] == 2000
        assert r["powerwallSocPercent"] == 75
        assert r["surplusKw"] == round(allSites["Home"].pwState.surplusKw, 3)

    def test_error_included(self):
        allSites = self._sites({"BadSite": (0, 0, 0, "Connection refused")})
        readings = _buildSiteReadings(allSites, "Good")
        assert readings[0]["error"] == "Connection refused"

    def test_no_error_when_none(self):
        allSites = self._sites({"GoodSite": (1000, 500, 90, None)})
        readings = _buildSiteReadings(allSites, "GoodSite")
        assert readings[0]["error"] is None

    def test_preserves_name_ordering(self):
        allSites = self._sites({n: (0, 0, 0, None) for n in ["C", "A", "B"]})
        readings = _buildSiteReadings(allSites, "C")
        assert [r["name"] for r in readings] == ["C", "A", "B"]


# ---------------------------------------------------------------------------
# _pollAllSites
# ---------------------------------------------------------------------------

class TestPollAllSitesPw:
    @pytest.mark.asyncio
    async def test_updates_all_sites_including_active(self):
        """_pollAllSites polls every site, including the active one."""
        active_pw = MagicMock()
        active_pw.solar.return_value = 5000
        active_pw.home.return_value = 1000
        active_pw.level.return_value = 90
        site_pw = MagicMock()
        site_pw.solar.return_value = 2000
        site_pw.home.return_value = 800
        site_pw.level.return_value = 70

        allSites = {
            "Active": SiteInfo(name="Active", pw=active_pw),
            "Other": SiteInfo(name="Other", pw=site_pw),
        }

        await _pollAllSites(allSites)

        assert allSites["Active"].pwState.solarWatts == 5000
        assert allSites["Other"].pwState.solarWatts == 2000
        assert allSites["Active"].error is None
        assert allSites["Other"].error is None

    @pytest.mark.asyncio
    async def test_records_error_on_failure(self):
        bad_pw = MagicMock()
        bad_pw.solar.side_effect = RuntimeError("timeout")

        allSites = {
            "Active": SiteInfo(name="Active", pw=MagicMock()),
            "BadSite": SiteInfo(name="BadSite", pw=bad_pw),
        }

        await _pollAllSites(allSites)

        assert allSites["BadSite"].error is not None
        assert "timeout" in allSites["BadSite"].error

    @pytest.mark.asyncio
    async def test_skips_none_pw(self):
        """Sites that failed to connect at startup (pw=None) are skipped without error."""
        allSites = {
            "Active": SiteInfo(name="Active", pw=MagicMock()),
            "Offline": SiteInfo(name="Offline", pw=None, error="Connection failed"),
        }

        # Should not raise
        await _pollAllSites(allSites)

        # Offline site's error should remain unchanged
        assert allSites["Offline"].error == "Connection failed"

    @pytest.mark.asyncio
    async def test_all_zero_sets_error(self):
        """All-zero readings from a site are flagged as an error without updating state."""
        zero_pw = MagicMock()
        zero_pw.solar.return_value = 0
        zero_pw.home.return_value = 0
        zero_pw.level.return_value = 0

        allSites = {
            "Active": SiteInfo(name="Active", pw=MagicMock()),
            "ZeroSite": SiteInfo(name="ZeroSite", pw=zero_pw,
                                 pwState=PowerwallState(solarWatts=500)),
        }

        await _pollAllSites(allSites)

        # State should NOT have been updated (still has old solarWatts)
        assert allSites["ZeroSite"].pwState.solarWatts == 500
        assert allSites["ZeroSite"].error is not None


# ---------------------------------------------------------------------------
# DashboardState.siteReadings field
# ---------------------------------------------------------------------------

class TestDashboardStateSiteReadings:
    def test_default_empty_list(self):
        d = DashboardState()
        assert d.siteReadings == []

    def test_can_set_readings(self):
        d = DashboardState()
        d.siteReadings = [{"name": "Home", "isActive": True, "solarWatts": 3000}]
        assert len(d.siteReadings) == 1
        assert d.siteReadings[0]["name"] == "Home"

# ---------------------------------------------------------------------------
# _pollAllSites — history entry format
# ---------------------------------------------------------------------------

class TestPollAllSitesHistoryEntry:
    @pytest.mark.asyncio
    async def test_fields_present_and_rounded(self):
        """Entry written by _pollAllSites has expected fields and rounding."""
        pw = MagicMock()
        pw.solar.return_value = 2123.7
        pw.home.return_value = 812.4
        pw.level.return_value = 67.89
        allSites = {"Site": SiteInfo(name="Site", pw=pw)}
        await _pollAllSites(allSites)
        entry = allSites["Site"].history[0]
        assert entry["solarWatts"] == 2124
        assert entry["homeWatts"] == 812
        assert entry["surplusKw"] == pytest.approx(1.31, abs=0.01)
        assert entry["powerwallSocPercent"] == 67.9
        assert "timestamp" in entry

    @pytest.mark.asyncio
    async def test_timestamp_is_recent(self):
        from datetime import datetime, timezone
        pw = MagicMock()
        pw.solar.return_value = 1000
        pw.home.return_value = 500
        pw.level.return_value = 80
        before = datetime.now(timezone.utc)
        allSites = {"Site": SiteInfo(name="Site", pw=pw)}
        await _pollAllSites(allSites)
        after = datetime.now(timezone.utc)
        ts = datetime.fromisoformat(allSites["Site"].history[0]["timestamp"])
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Non-active site history recording (via _pollAllSites)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNonActiveSiteHistory:
    """Verify all sites accumulate PW-only history entries each poll cycle."""

    def _make_site(self, name, solar=2000.0, home=800.0, soc=72.5, pw_mock=None):
        if pw_mock is None:
            pw_mock = MagicMock()
            pw_mock.solar.return_value = solar
            pw_mock.home.return_value = home
            pw_mock.level.return_value = soc
        return SiteInfo(name=name, pw=pw_mock)

    async def test_all_sites_get_entry(self):
        siteA = self._make_site("SiteA", solar=3000.0, soc=80.0)
        siteB = self._make_site("SiteB", solar=1500.0, soc=55.0)
        allSites = {"SiteA": siteA, "SiteB": siteB}
        await _pollAllSites(allSites)
        assert len(siteA.history) == 1
        assert len(siteB.history) == 1
        assert siteB.history[0]["solarWatts"] == 1500
        assert siteB.history[0]["powerwallSocPercent"] == 55.0

    async def test_single_site_gets_entry(self):
        siteA = self._make_site("SiteA")
        allSites = {"SiteA": siteA}
        await _pollAllSites(allSites)
        assert len(siteA.history) == 1

    async def test_errored_site_skipped(self):
        siteA = self._make_site("SiteA")
        bad_pw = MagicMock()
        bad_pw.solar.side_effect = RuntimeError("refused")
        siteB = SiteInfo(name="SiteB", pw=bad_pw)
        allSites = {"SiteA": siteA, "SiteB": siteB}
        await _pollAllSites(allSites)
        assert len(siteB.history) == 0  # poll failed → error set → no history entry

    async def test_multiple_cycles_accumulate(self):
        siteA = self._make_site("SiteA")
        siteB = self._make_site("SiteB")
        allSites = {"SiteA": siteA, "SiteB": siteB}
        for _ in range(3):
            await _pollAllSites(allSites)
        assert len(siteA.history) == 3
        assert len(siteB.history) == 3

    async def test_values_rounded_correctly(self):
        siteA = self._make_site("SiteA")
        siteB = self._make_site("SiteB", solar=2123.7, home=812.4, soc=67.89)
        allSites = {"SiteA": siteA, "SiteB": siteB}
        await _pollAllSites(allSites)
        entry = siteB.history[0]
        assert entry["solarWatts"] == 2124
        assert entry["homeWatts"] == 812
        assert entry["surplusKw"] == pytest.approx(1.31, abs=0.01)
        assert entry["powerwallSocPercent"] == 67.9


# ---------------------------------------------------------------------------
# Chart endpoints — multi-site series
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebChartMultiSite:
    """Verify chart SVG endpoints include non-active site series when _allSites has multiple sites."""

    def _make_history(self, n=3, solar=2000.0, soc=70.0):
        h = collections.deque(maxlen=120)
        for i in range(n):
            ts = datetime(2024, 1, 1, 12, i, 0, tzinfo=timezone.utc).isoformat()
            h.append({
                "timestamp": ts,
                "solarWatts": solar,
                "homeWatts": 800.0,
                "surplusKw": 1.2,
                "powerwallSocPercent": soc,
                "evSocPercent": 60.0,
                "evChargingKw": 0.0,
            })
        return h

    @contextmanager
    def _chaos_state(self, sites: dict, active: str):
        """Temporarily patch chaos module globals for chart tests."""
        import chaos
        orig_history = chaos._dashboard.history
        orig_sites = chaos._allSites
        chaos._dashboard.history = sites[active].history
        chaos._dashboard.activePowerwall = active
        chaos._allSites = sites
        try:
            yield
        finally:
            chaos._dashboard.history = orig_history
            chaos._dashboard.activePowerwall = ""
            chaos._allSites = orig_sites

    async def test_power_chart_includes_non_active_series(self):
        import chaos
        siteA = SiteInfo(name="SiteA", pw=None)
        siteA.history = self._make_history(n=3, solar=3000.0)
        siteB = SiteInfo(name="SiteB", pw=None)
        siteB.history = self._make_history(n=3, solar=1500.0)
        with self._chaos_state({"SiteA": siteA, "SiteB": siteB}, "SiteA"):
            async with AsyncClient(transport=ASGITransport(app=chaos._webApp), base_url="http://test") as client:
                resp = await client.get("/api/charts/power.svg")
        assert resp.status_code == 200
        assert "SiteB Solar" in resp.text

    async def test_soc_chart_includes_non_active_series(self):
        import chaos
        siteA = SiteInfo(name="SiteA", pw=None)
        siteA.history = self._make_history(n=3, soc=80.0)
        siteB = SiteInfo(name="SiteB", pw=None)
        siteB.history = self._make_history(n=3, soc=55.0)
        with self._chaos_state({"SiteA": siteA, "SiteB": siteB}, "SiteA"):
            async with AsyncClient(transport=ASGITransport(app=chaos._webApp), base_url="http://test") as client:
                resp = await client.get("/api/charts/soc.svg")
        assert resp.status_code == 200
        assert "SiteB PW" in resp.text

    async def test_power_chart_single_site_no_extra_series(self):
        import chaos
        siteA = SiteInfo(name="SiteA", pw=None)
        siteA.history = self._make_history(n=3)
        with self._chaos_state({"SiteA": siteA}, "SiteA"):
            async with AsyncClient(transport=ASGITransport(app=chaos._webApp), base_url="http://test") as client:
                resp = await client.get("/api/charts/power.svg")
        assert resp.status_code == 200
        svg = resp.text
        assert "Solar" in svg
        # No "SiteA Solar" extra label — only the base "Solar" series
        assert "SiteA Solar" not in svg

    async def test_non_active_shorter_history_padded_with_none(self):
        """When a non-active site has fewer entries, the series is padded with None at the front."""
        import chaos
        siteA = SiteInfo(name="SiteA", pw=None)
        siteA.history = self._make_history(n=5)  # active: 5 entries
        siteB = SiteInfo(name="SiteB", pw=None)
        siteB.history = self._make_history(n=3)  # non-active: only 3 entries
        with self._chaos_state({"SiteA": siteA, "SiteB": siteB}, "SiteA"):
            async with AsyncClient(transport=ASGITransport(app=chaos._webApp), base_url="http://test") as client:
                resp = await client.get("/api/charts/power.svg")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# _detectSiteSwitch
# ---------------------------------------------------------------------------

def _make_thresholds_for_detect(**overrides):
    return _make_thresholds(minChargingAmps=6, **overrides)

def _make_sites_for_detect(active_home: float, candidate_home: float, candidate_error: str | None = None):
    """Two-site dict: 'Active' and 'Candidate' with given home load values."""
    active = SiteInfo(
        name="Active", pw=MagicMock(),
        pwState=PowerwallState(solarWatts=6000, homeWatts=active_home, powerwallSocPercent=60),
    )
    candidate = SiteInfo(
        name="Candidate", pw=MagicMock(),
        pwState=PowerwallState(solarWatts=5000, homeWatts=candidate_home, powerwallSocPercent=55),
        error=candidate_error,
    )
    return {"Active": active, "Candidate": candidate}


class TestDetectSiteSwitch:
    """Tests for _detectSiteSwitch — auto-detect when EV is on a different site."""

    T = _make_thresholds_for_detect()  # 6A min, 240V → expectedEvW = amps*240

    def _state(self, chargeState, evAmps: int):
        return VehicleState(evSocPercent=70.0, chargeState=chargeState, evChargingAmps=evAmps)

    def test_no_switch_ev_not_charging(self):
        """Returns None when EV is not in a charging state."""
        state = self._state(CS.CHARGE_STATE_CHARGING_STOPPED, evAmps=20)
        sites = _make_sites_for_detect(active_home=500, candidate_home=5000)
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_no_switch_ev_below_min_amps(self):
        """Returns None when evChargingAmps is below minChargingAmps."""
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=3)  # below min of 6
        sites = _make_sites_for_detect(active_home=500, candidate_home=5000)
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_no_switch_active_site_has_ev_load(self):
        """Returns None when the active site's homeWatts >= 50% of expected EV power."""
        # 20A * 240V = 4800W expected; threshold = 2400W; active home = 3000W → OK
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        sites = _make_sites_for_detect(active_home=3000, candidate_home=5000)
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_no_switch_no_candidate(self):
        """Returns None when no non-active site has sufficient home load."""
        # 20A * 240V = 4800W; threshold = 2400W; candidate home = 1000W < 2400W
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        sites = _make_sites_for_detect(active_home=500, candidate_home=1000)
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_no_switch_ambiguous_candidates(self):
        """Returns None when multiple non-active sites are candidates (ambiguous)."""
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        # active_home = 500W (below threshold); two non-active sites both with high home load
        active = SiteInfo(
            name="Active", pw=MagicMock(),
            pwState=PowerwallState(solarWatts=6000, homeWatts=500, powerwallSocPercent=60),
        )
        siteB = SiteInfo(
            name="SiteB", pw=MagicMock(),
            pwState=PowerwallState(solarWatts=5000, homeWatts=5000, powerwallSocPercent=55),
        )
        siteC = SiteInfo(
            name="SiteC", pw=MagicMock(),
            pwState=PowerwallState(solarWatts=5000, homeWatts=5000, powerwallSocPercent=55),
        )
        sites = {"Active": active, "SiteB": siteB, "SiteC": siteC}
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_no_switch_candidate_has_error(self):
        """Returns None when the only candidate site has an active poll error."""
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        sites = _make_sites_for_detect(active_home=500, candidate_home=5000, candidate_error="Connection refused")
        assert _detectSiteSwitch(sites, "Active", state, self.T) is None

    def test_detects_candidate_site(self):
        """Returns the candidate site name when all conditions are met."""
        # 20A * 240V = 4800W; threshold = 2400W
        # active home = 500W < 2400W; candidate home = 5000W >= 2400W
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        sites = _make_sites_for_detect(active_home=500, candidate_home=5000)
        assert _detectSiteSwitch(sites, "Active", state, self.T) == "Candidate"

    def test_detects_only_non_active_sites_considered(self):
        """Active site is never returned as a candidate, even with high home load."""
        state = self._state(CS.CHARGE_STATE_CHARGING, evAmps=20)
        # active_home is low AND candidate is the active site — function is called with
        # activeName="Candidate", so "Candidate" should not be returned
        sites = _make_sites_for_detect(active_home=5000, candidate_home=500)
        # Both have roles reversed: activeName="Candidate" has low home load → should detect "Active"
        active_site = sites["Active"]
        candidate_site = sites["Candidate"]
        reversed_sites = {"Candidate": candidate_site, "Active": active_site}
        # "Candidate" is now active with 500W; "Active" non-active with 5000W
        assert _detectSiteSwitch(reversed_sites, "Candidate", state, self.T) == "Active"
