# CHAOS — CHarging Automatically On Solar

CHAOS (**CH**arging **A**utomatically **O**n **S**olar) is a daemon that automates charging of a Lucid EV using solar energy. It monitors your Tesla Powerwall for real-time solar production, home consumption, and battery state of charge, then dynamically starts, stops, and adjusts your EV's charge current to maximize use of renewable energy.

CHAOS was motivated by integrating a Lucid Gravity into a previously all-Tesla garage that relied on Tesla's Charge On Solar functionality.  I found myself dearly missing the ability to charge all of my cars on solar, so I created CHAOS.  I hope others find it useful.  Constructive feedback is welcome.

## How it works

1. Configuration information is read from config.json where you can control minimum Powerwall SoC before charging, min,max charging current, target vehicle SoC, etc.
2. Every poll cycle, CHAOS reads solar production, home load, and Powerwall SOC from the local Powerwall API.
3. It computes the solar surplus (solar − home), corrected for the EV's own load so it doesn't double-count charging.
4. A five-sample rolling average smooths out transient clouds before any charging decisions are made.
5. Based on surplus, Powerwall SOC, and EV state of charge, CHAOS issues `start`, `stop`, or `adjust` commands to the Lucid API.
6. A live web dashboard is served on port 8087 showing all state, charts, and recent history.

## Requirements

- Python 3.13 or newer (the Docker image uses 3.14)
- A Tesla Powerwall accessible on the local network using either local API or TEDAPI
- A Lucid EV on a Lucid account
- Optional: Tesla vehicle(s) on a Tesla account, for read-only charge-status display


## Security considerations

- **`config.json` contains plaintext credentials** — Lucid account and Powerwall gateway passwords are stored in plaintext. The file is gitignored, but you should also restrict its permissions: `chmod 600 config.json`.
- **Dashboard has no authentication by default** — Set `dashboard.apiKey` in your config if the dashboard is reachable from outside localhost or a fully trusted network. Without a key, anyone on the network can trigger a poll or switch the active Powerwall.
- **No HTTPS** — The built-in web server has no TLS. CHAOS is designed for a trusted local network. Never expose port 8087 directly to the internet; if remote access is needed, place it behind a VPN or a reverse proxy with TLS termination.
- **DNS rebinding** — A malicious website can trick a browser inside your network into sending requests to the dashboard under the attacker's hostname. Set `dashboard.allowedHosts` to the addresses you actually browse to; requests with any other `Host` header are then rejected.

## Running standalone

```bash
# Create the virtual environment and install dependencies
uv sync

# Copy and fill in the example config
cp config.example.json config.json
$EDITOR config.json

# Run
uv run python chaos.py
```

Logs are written to stdout and to `data/chaos.log`. The web dashboard is available at `http://localhost:8087` (or whichever port you set in `dashboard.port`).

## Running in Docker

### Quick start

```bash
docker compose up -d --build
```

### Build options

```bash
# Single platform — build and load locally
docker buildx build --platform linux/amd64 --load .   # Synology / x86
docker buildx build --platform linux/arm64 --load .   # Apple Silicon

# Multi-arch — requires a registry to push to
docker buildx build --platform linux/amd64,linux/arm64 --push -t yourrepo/chaos:latest .
```

The `docker-compose.yml` bind-mounts `config.json` read-write (CHAOS persists active-Powerwall changes back to it) and a `data/` directory (`chaos.log` and 24-hour history charts) so both survive container rebuilds. The container exposes port 8087.

## Configuration reference

All configuration lives in `config.json`. Copy `config.example.json` as a starting point.

```json
{
  "powerwalls": [ ... ],
  "activePowerwall": "home",
  "lucid": { ... },
  "charging": { ... },
  "pollingIntervalSeconds": 60,
  "dashboard": { ... },
  "notifications": { ... },
  "tesla": { ... }
}
```

---

### `powerwalls` (array, required)

List of one or more Powerwall gateways. Multiple entries enable the site-switching feature from the web dashboard.

| Key | Type | Description |
|-----|------|-------------|
| `name` | string | Unique label for this site (e.g. `"home"`). Referenced by `activePowerwall`. |
| `host` | string | Local IP address or hostname of the Powerwall gateway. |
| `authType` | string | `"local"` for standard local API auth; `"TEDAPI"` for TEDAPI auth. |
| `password` | string | Gateway password (used for both auth types). |
| `email` | string | Tesla account email associated with the gateway. |
| `timezone` | string | IANA timezone string for the gateway (e.g. `"America/Chicago"`). |

**Example:**
```json
"powerwalls": [
  {
    "name": "home",
    "host": "192.168.1.100",
    "authType": "local",
    "password": "your-gateway-password",
    "email": "you@example.com",
    "timezone": "America/Chicago"
  }
]
```

---

### `activePowerwall` (string, required)

The `name` of the Powerwall entry in `powerwalls` that CHAOS uses as the source of truth for charging decisions. Must match exactly one entry's `name`. Can be changed at runtime via the web dashboard.

---

### `lucid` (object, required)

Lucid Motors account credentials used to control EV charging.

| Key | Type | Description |
|-----|------|-------------|
| `username` | string | Lucid account email address. |
| `password` | string | Lucid account password. |

---

### `charging` (object, required)

Thresholds and limits that govern the charging decision engine.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `minPowerwallSocPercent` | number | — | Powerwall state of charge must be at or above this percentage before CHAOS will start or continue charging. Protects your home battery reserve. |
| `targetEvChargePercent` | number | — | CHAOS stops charging when the EV reaches this state of charge. Also set as the charge limit on the vehicle at session start. |
| `chargerVoltage` | number | — | AC voltage at the EVSE (e.g. `240` in North America). Used to convert between amps and kilowatts. |
| `minChargingAmps` | number | — | Minimum charge current in amps. CHAOS will not command a current below this value. Also determines the minimum surplus required to start charging (`minChargingAmps × chargerVoltage / 1000` kW). |
| `maxChargingAmps` | number | — | Maximum charge current in amps. CHAOS will never command more than this, regardless of available surplus. |
| `commandCooldownMinutes` | number | — | After issuing a start or stop command, CHAOS waits this many minutes before considering another stop or start. Protects the vehicle's high-voltage contactors from rapid cycling. |
| `stopConfirmCycles` | number | `4` | Number of consecutive polling cycles where surplus is insufficient before CHAOS issues a STOP. Higher values tolerate short solar dips (clouds, transient loads) without stopping. Set to `1` to stop on the first low-surplus cycle. |
| `allowExternalChargeInterference` | boolean | `true` | If `true`, CHAOS takes over externally-started charging sessions (e.g. sessions you started manually) and manages the current. If `false`, CHAOS observes but does not interfere with sessions it did not start. During the cooldown after a STOP, an external session is observed rather than taken over, unless the last command was a START. |
| `chargingMode` | string | `"enabled"` | `"enabled"` is normal automatic control. `"manual"` is monitor-only: CHAOS keeps polling and updating the dashboard but never issues start, stop, or adjust commands. `"disabled"` forces an immediate stop and blocks new sessions. Normally changed from the dashboard (see below), which saves the choice here so it survives restarts. |

**Example:**
```json
"charging": {
  "minPowerwallSocPercent": 50,
  "targetEvChargePercent": 70,
  "chargerVoltage": 240,
  "minChargingAmps": 12,
  "maxChargingAmps": 32,
  "commandCooldownMinutes": 10,
  "stopConfirmCycles": 3,
  "allowExternalChargeInterference": true,
  "chargingMode": "enabled"
}
```

#### Charging mode

The dashboard's charging control switches between the three modes. The same switch is available over HTTP:

```bash
curl -X POST http://localhost:8087/api/charging -H 'Content-Type: application/json' -H 'X-API-Key: YOUR_KEY' -d '{"mode": "manual"}'
```

`mode` must be `enabled`, `manual`, or `disabled`; anything else returns `400`. The `X-API-Key` header is only needed if `dashboard.apiKey` is set. Use `manual` when you want to watch CHAOS or start and stop charging yourself without CHAOS interfering, and `disabled` to stop charging and keep it off.

---

### `pollingIntervalSeconds` (number, required)

Base interval in seconds between poll cycles. The actual sleep time is multiplied by a state-dependent factor:

| Condition | Multiplier | Effective interval (at 60s base) |
|-----------|-----------|----------------------------------|
| Normal | 1× | 60 s |
| After a charge command | 3× | 3 min |
| Insufficient surplus (not dark) | 1× | 60 s |
| Powerwall SOC below minimum | 5× | 5 min |
| No solar production (dark/overcast) | 10× | 10 min |
| EV at target charge level | 60× | 60 min |

---

### `dashboard` (object, optional)

Web UI settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `port` | number | `8087` | TCP port for the web dashboard. Set to `0` to disable the web server. |
| `units` | string | `"imperial"` | Display units. `"imperial"` shows miles; `"metric"` shows kilometres. |
| `ratedRangeMiles` | number or null | `null` | Rated range of the vehicle in miles, used to display an estimated range alongside SOC. Set to `null` to omit. |
| `apiKey` | string | `""` | If non-empty, the mutation endpoints (`/api/poll`, `/api/charging`, and `/api/powerwall`) require an `X-API-Key` header matching this value. Read-only endpoints (dashboard state, charts) remain open. Leave empty to disable authentication. |
| `allowedHosts` | array of strings | `[]` | If non-empty, every request must carry a `Host` header matching one of these entries — list every address users browse to (e.g. `["192.168.1.50", "chaos.local", "localhost"]`). Port numbers are ignored and `*.example.com` wildcards are supported. Requests with any other `Host` header are rejected with `400`, which defends against DNS-rebinding attacks. Leave empty (or omit) to accept any `Host` header. |

---

### `notifications` (object, optional)

Webhook notifications for charging events, site outages, and errors.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Set to `true` to enable webhook notifications. |
| `webhookUrl` | string | `""` | URL to POST notifications to. Google Chat webhook URLs (`chat.googleapis.com`) are handled automatically — only the `text` field is sent. Other URLs receive a full JSON payload with event details. |
| `powerwallFullSocPercent` | number | `100` | Powerwall charge level that counts as "full" for the `ev_and_powerwall_full` notification below. |

Notifications fire on:

| `event` | When | Extra fields |
|---|---|---|
| `charging_started`, `charging_stopped` | CHAOS starts or stops charging | `reason`, power and SoC readings, `evChargingAmps` |
| `auto_site_switch` | The active site changes automatically (multiple Powerwalls) | `from`, `to` |
| `site_unreachable` | A Powerwall site has failed 5 polls in a row | `site`, `consecutiveFailures`, `error`, `diagnosis`, `downSince` |
| `site_recovered` | A site that triggered `site_unreachable` polls successfully again | `site`, `downtimeSeconds` |
| `ev_and_powerwall_full` | The EV is at its target charge and the active site's Powerwall has reached `powerwallFullSocPercent`, so surplus solar has nowhere to go | `site`, `evSocPercent`, `powerwallSocPercent` |
| `error` | A poll cycle raised an unexpected error | `message` |

`ev_and_powerwall_full` fires once, and fires again only after either level drops 5 points below its threshold. A poll that returns all zeros counts as a failure, because the Powerwall API returns empty values instead of an error when the gateway is offline or login fails. CHAOS also retries a site that failed to connect at startup instead of giving up on it.

#### Site outage diagnostics

When a Powerwall poll fails, CHAOS logs why in a single line, for example:

```
WARNING [Powerwall:Home] Poll failed: All-zero reading | down 2h 5m (125 polls) | pypowerwall: ERROR Timeout waiting for Powerwall API https://192.168.1.50/api/meters/aggregates | probe: TCP 192.168.1.50:443 host unreachable after 3.0s (EHOSTUNREACH: no ARP reply — device offline, unplugged, or not on this subnet)
```

The line has the reason pypowerwall gave, how long the site has been down, and the result of a separate probe of the gateway (a TCP connection to port 443, then an HTTPS request to `/api/status`). A short form appears on the dashboard, and the full `diagnosis` goes into the `site_unreachable` notification. How to read the probe:

| Probe result | Likely cause |
|---|---|
| host unreachable / host down | No reply for that address on the network: gateway offline, unplugged, or on a different subnet |
| TCP timeout | Packets silently dropped: gateway off, or a firewall or VLAN rule is blocking it |
| connection refused | Something answers at that address, but nothing is listening on port 443 |
| HTTPS timeout / HTTPS failed | The connection opens but the gateway's web server or TLS is not responding |
| gateway reachable | The network is fine and the gateway answers (its firmware version and uptime are shown). The problem is in the API or login, so check the `pypowerwall:` part of the line |

The probe only runs after a failed poll and adds at most a few seconds to that cycle.

**Example payload (non-Google-Chat):**
```json
{
  "text": "⚡ CHAOS: Charging Started\nSurplus exceeded threshold\nSolar: 3200W  Home: 800W  Surplus: +2.40kW  PW: 85.0%  EV: 42.0%  Charging: 16A",
  "event": "charging_started",
  "reason": "Surplus exceeded threshold",
  "timestamp": "2026-04-15T14:00:00+00:00",
  "solarWatts": 3200,
  "homeWatts": 800,
  "surplusKw": 2.40,
  "effectiveSurplusKw": 2.40,
  "powerwallSocPercent": 85.0,
  "evSocPercent": 42.0,
  "evChargingAmps": 16
}
```

**Example `site_unreachable` payload:**
```json
{
  "text": "🔴 CHAOS: Powerwall 'Home' unreachable\n5 consecutive poll failures. Last error: All-zero reading — host unreachable\nDiagnosis: TCP 192.168.1.50:443 host unreachable after 3.0s (EHOSTUNREACH: no ARP reply — device offline, unplugged, or not on this subnet)",
  "event": "site_unreachable",
  "site": "Home",
  "consecutiveFailures": 5,
  "error": "All-zero reading — host unreachable",
  "diagnosis": "TCP 192.168.1.50:443 host unreachable after 3.0s (EHOSTUNREACH: no ARP reply — device offline, unplugged, or not on this subnet)",
  "downSince": "2026-10-17T12:00:00+00:00",
  "timestamp": "2026-10-17T12:05:00+00:00"
}
```

### `tesla` (object, optional)

Read-only display of Tesla vehicle charge status alongside the Lucid. When configured, each Tesla on the account gets its own dashboard card group and a series on the 24-hour state-of-charge chart. CHAOS never sends commands to Tesla vehicles and **never wakes a sleeping car** — data is only fetched when a vehicle is already online; otherwise the dashboard shows the last-known values with an "asleep" indicator. Omit the block (or set `enabled: false`) to disable the feature entirely.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Set to `false` to disable Tesla polling without removing the block. |
| `email` | string | — | Tesla account email. Required when the block is present and enabled. |
| `vehicleNames` | array of strings | `[]` | Vehicles to display, by name as shown in the Tesla app; the list order is the display order. Leave empty to show every vehicle on the account. Give your cars distinct names — duplicates would merge on the chart. |
| `pollIntervalSeconds` | number | `300` | How often to poll the Tesla API. Polling an online car does not wake it, but very frequent polling can prevent it from falling asleep — raise this if you observe vampire drain. |

This integration uses the unofficial Tesla Owner API (via [TeslaPy](https://github.com/tdorssers/TeslaPy)). Tesla has been migrating accounts to its newer Fleet API, so this may stop working for some accounts; if it does, the dashboard shows the Tesla section as "unavailable" and everything else keeps running.

**Tesla charging attribution** (display only): when a Tesla is charging and a Powerwall site's readings fit the charge-on-solar signature — home load at least 0.5 kW above the Tesla's charging power, surplus within ±0.5 kW, the site not currently Lucid-charging, and the Tesla and site polled within 30 s of each other — the dashboard subtracts the Tesla's draw from that site's Home Load and shows it in a "Tesla Charging" card. To keep the attribution fresh, Tesla polling automatically speeds up to the site cadence (~60 s) while a Tesla is charging (the car is awake anyway) and reverts to `pollIntervalSeconds` when charging stops. None of this affects charging decisions.

#### First-time login

The Tesla API needs an OAuth token, obtained once interactively:

```bash
# Standalone
uv run python chaos.py --tesla-login

# Docker (interactive terminal; the token persists via the data/ volume)
docker compose run --rm chaos uv run --no-dev python chaos.py --tesla-login
```

The command prints a `https://auth.tesla.com/...` URL. Open it in a browser **on any machine** and log in to your Tesla account (including MFA if enabled). After signing in, the final redirect targets the Tesla mobile app (`tesla://auth/callback?code=...`), which a desktop browser can't open — so ending on an **error page, or stuck on "Verified Successfully" / "Loading…", is expected**. Get the authorization code one of these ways and paste it at the prompt:

- If the address bar now shows `tesla://auth/callback?code=...` — copy that full URL.
- If the address bar didn't change (stuck on "Loading…"): repeat the login in **Firefox**, whose error page always shows the `tesla://` URL.
- Or via DevTools: open the **Network** tab, enable **Preserve log**, log in again, click the last request to `auth.tesla.com` (status 302) and copy the `location` response header — pasting either the full URL or just the `code` value works.

The token is saved to `data/tesla_token_cache.json` and refreshed automatically from then on — the daemon never needs interaction. **Treat that file like a password**: it grants full access to your Tesla account. If Tesla revokes the token (password change, security event), the dashboard marks the Tesla section unavailable; re-run `--tesla-login` (no restart needed — the daemon picks up the new token within one poll interval).

## Running tests

```bash
.venv/bin/python -m pytest test_chaos.py -q
```

Use `python -m pytest` (not the `pytest` binary directly) to avoid hangs.

## Acknowledgments

CHAOS is only possible thanks to several excellent open-source projects:

- **[pypowerwall](https://github.com/jasonacox/pypowerwall)** by Jason Cox — local Powerwall API client that provides all solar, home load, and battery state data.
- **[python-lucidmotors](https://github.com/nshp/python-lucidmotors)** by nshp — community-developed Lucid Motors API client used to start, stop, and adjust EV charging.
- **[FastAPI](https://fastapi.tiangolo.com/)** by Sebastián Ramírez — ASGI web framework serving the live dashboard.
- **[TeslaPy](https://github.com/tdorssers/TeslaPy)** by Tim Dorssers — Unofficial Tesla Motors Owner API client.