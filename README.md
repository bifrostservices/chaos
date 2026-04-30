# CHAOS — CHarging Automatically On Solar

CHAOS (**CH**arging **A**utomatically **O**n **S**olar) is a daemon that automates charging of a Lucid EV using solar energy. It monitors your Tesla Powerwall for real-time solar production, home consumption, and battery state of charge, then dynamically starts, stops, and adjusts your EV's charge current to maximize use of renewable energy.

CHAOS was motivated by integrating a Lucid Gravity into a previously all-Tesla garage that relied on Tesla's Charge On Solar functionality.  I found myself dearly missing the ability to charge all of my cars on solar, so I created CHAOS.  I hope others find it useful.  Constructive feedback is welcome.

## How it works

1. Configuration information is read from config.json where you can control minimum Powerwall SoC before charging, min,max charging current, target vehicle SoC, etc.
2. Every poll cycle, CHAOS reads solar production, home load, and Powerwall SOC from the local Powerwall API.
3. It computes the solar surplus (solar − home), corrected for the EV's own load so it doesn't double-count charging.
4. A rolling average smooths out transient clouds before any charging decisions are made.
5. Based on surplus, Powerwall SOC, and EV state of charge, CHAOS issues `start`, `stop`, or `adjust` commands to the Lucid API.
6. A live web dashboard is served on port 8086 showing all state, charts, and recent history.

## Requirements

- Python 3.13 (grpcio wheels are not yet available for 3.14)
- A Tesla Powerwall accessible on the local network using either local API or TEDAPI
- A Lucid EV on a Lucid account


## Security considerations

- **`config.json` contains plaintext credentials** — Lucid account and Powerwall gateway passwords are stored in plaintext. The file is gitignored, but you should also restrict its permissions: `chmod 600 config.json`.
- **Dashboard has no authentication by default** — Set `dashboard.apiKey` in your config if the dashboard is reachable from outside localhost or a fully trusted network. Without a key, anyone on the network can trigger a poll or switch the active Powerwall.
- **No HTTPS** — The built-in web server has no TLS. CHAOS is designed for a trusted local network. Never expose port 8086 directly to the internet; if remote access is needed, place it behind a VPN or a reverse proxy with TLS termination.

## Running standalone

```bash
# Create and activate a virtual environment
python3.13 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in the example config
cp config.example.json config.json
$EDITOR config.json

# Run
python chaos.py
```

Logs are written to stdout and to `chaos.log` in the working directory. The web dashboard is available at `http://localhost:8086` (or whichever port you set in `dashboard.port`).

## Running in Docker

### Quick start

```bash
# The log file must exist before Docker mounts it
touch chaos.log

# Build and start
docker compose up -d
```

### Build options

```bash
# Single platform — build and load locally
docker buildx build --platform linux/amd64 --load .   # Synology / x86
docker buildx build --platform linux/arm64 --load .   # Apple Silicon

# Multi-arch — requires a registry to push to
docker buildx build --platform linux/amd64,linux/arm64 --push -t yourrepo/chaos:latest .
```

The `docker-compose.yml` bind-mounts `config.json` read-only and `chaos.log` for persistence. The container exposes port 8086.

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
  "notifications": { ... }
}
```

---

### `powerwalls` (array, required)

List of one or more Powerwall gateways. Multiple entries enable the site-switching feature from the web dashboard.

| Key | Type | Description |
|-----|------|-------------|
| `name` | string | Unique label for this site (e.g. `"home"`). Referenced by `activePowerwall`. |
| `host` | string | Local IP address or hostname of the Powerwall gateway. |
| `authType` | string | `"local"` for standard local API auth; `"TEDAPI"` for TEDAPl auth. |
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
| `allowExternalChargeInterference` | boolean | `true` | If `true`, CHAOS takes over externally-started charging sessions (e.g. sessions you started manually) and manages the current. If `false`, CHAOS observes but does not interfere with sessions it did not start. |

**Example:**
```json
"charging": {
  "minPowerwallSocPercent": 50,
  "targetEvChargePercent": 70,
  "chargerVoltage": 240,
  "minChargingAmps": 12,
  "maxChargingAmps": 32,
  "commandCooldownMinutes": 10,
  "allowExternalChargeInterference": true
}
```

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
| EV at target charge level | 60× | 1 hr |

---

### `dashboard` (object, optional)

Web UI settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `port` | number | `8086` | TCP port for the web dashboard. Set to `0` to disable the web server. |
| `units` | string | `"imperial"` | Display units. `"imperial"` shows miles; `"metric"` shows kilometres. |
| `ratedRangeMiles` | number or null | `null` | Rated range of the vehicle in miles, used to display an estimated range alongside SOC. Set to `null` to omit. |
| `apiKey` | string | `""` | If non-empty, the mutation endpoints (`/api/poll` and `/api/powerwall`) require an `X-API-Key` header matching this value. Read-only endpoints (dashboard state, charts) remain open. Leave empty to disable authentication. |

---

### `notifications` (object, optional)

Webhook notifications for charging events and errors.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Set to `true` to enable webhook notifications. |
| `webhookUrl` | string | `""` | URL to POST notifications to. Google Chat webhook URLs (`chat.googleapis.com`) are handled automatically — only the `text` field is sent. Other URLs receive a full JSON payload with event details. |

Notifications fire on: charging started, charging stopped, and poll cycle errors.

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
