# Kidsout — HTTP API Reference

REST-ish JSON API used by the webpage — and available to any automation
(curl, cron, Home Assistant, …). See [README.md](README.md) for the admin guide.

- **Base URL:** `http://<host>:8080` (override with `KIDSOUT_LISTEN`, e.g. `KIDSOUT_LISTEN=:9090`)
- **Weekday keys:** `sun mon tue wed thu fri sat`
- **Times:** 24h `HH:MM` strings (e.g. `"09:00"`)
- Request bodies are JSON; no `Content-Type` header is required.
- Every mutation triggers an **immediate** status recompute (runs `block.sh`/`unblock.sh`
  on edges) and an SSE broadcast — no waiting for the 1-minute tick.

## Authentication

All routes require **HTTP Basic Auth**. Credentials live in `runtimestore.yaml`
(`auth.username` / `auth.password`) and are re-read on every request — changes
apply without restart.

```bash
export KO="http://localhost:8080"
export KOAUTH="user:pass"   # your runtimestore.yaml credentials

curl -u "$KOAUTH" "$KO/api/state"
```

A successful Basic Auth also sets an HttpOnly session cookie (`kidsout_session`,
random per server run). Browsers need it for the SSE `EventSource`; curl scripts
can ignore it and just pass `-u` on every call.

## Endpoints

### GET /api/state — full state snapshot

```bash
curl -u "$KOAUTH" "$KO/api/state"
```

Response (trimmed):

```json
{
  "today": "fri",
  "devices": {
    "xbox": {
      "deviceStatus": "inUse",
      "enforcementToggle": "enforcementON",
      "pauseToggle": "pauseOFF",
      "pauseMinutesRemaining": 0,
      "days": {
        "fri": {
          "taMinutes": 120,
          "tuMinutes": 80,
          "trMinutes": 40,
          "tfStart": "09:00",
          "tfEnd": "21:00"
        }
      }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `today` | Current weekday key (server-local time) |
| `deviceStatus` | See [status values](#devicestatus-values) |
| `enforcementToggle` | `enforcementON` \| `enforcementOFF` |
| `pauseToggle` / `pauseMinutesRemaining` | `pauseON`\|`pauseOFF`, countdown 20→0 |
| `taMinutes` / `tuMinutes` / `trMinutes` | Time allowed / used / remaining, `tr = max(0, ta-tu)` |
| `tfStart` / `tfEnd` | Allowed daily time-frame |

### GET /api/events — live updates (SSE)

Server-Sent Events stream. Each event's `data:` is the same JSON as `/api/state`.
The current snapshot is sent immediately on connect; keepalive comments every 25s.

```bash
curl -N -u "$KOAUTH" "$KO/api/events"
```

### POST /api/device/{name}/pause — pause / unpause

Body: `{"toggle":"pauseON"|"pauseOFF"}`. `pauseON` blocks the device and starts a
20-minute countdown; at 0 it auto-reverts to `pauseOFF`.

```bash
curl -u "$KOAUTH" -X POST "$KO/api/device/xbox/pause" -d '{"toggle":"pauseON"}'
curl -u "$KOAUTH" -X POST "$KO/api/device/xbox/pause" -d '{"toggle":"pauseOFF"}'
```

Returns `409 Conflict` if the device is already blocked by no-time/timeframe
(pausing would have no effect). An active pause is auto-cancelled when the device
goes outside its timeframe.

### POST /api/device/{name}/enforcement — free-use-mode on/off

Body: `{"toggle":"enforcementON"|"enforcementOFF"}`. `enforcementOFF` = never
block, time used does not accrue.

```bash
curl -u "$KOAUTH" -X POST "$KO/api/device/tv/enforcement" -d '{"toggle":"enforcementOFF"}'
```

### POST /api/device/{name}/ta — adjust time allowed

Body: `{"weekday":"fri","deltaMinutes":10}`. **Delta**, not absolute: adds/subtracts
minutes from that weekday's recurring allowance, clamped to 0..1440.

```bash
# +30 minutes every Friday
curl -u "$KOAUTH" -X POST "$KO/api/device/xbox/ta" -d '{"weekday":"fri","deltaMinutes":30}'
# -10 minutes
curl -u "$KOAUTH" -X POST "$KO/api/device/xbox/ta" -d '{"weekday":"fri","deltaMinutes":-10}'
```

### POST /api/device/{name}/tf — set time-frame

Body: `{"weekday":"fri","tfStart":"09:00","tfEnd":"21:00"}`. Both must be valid
`HH:MM` with `tfStart < tfEnd` — crossing midnight is forbidden (`400` otherwise).

```bash
curl -u "$KOAUTH" -X POST "$KO/api/device/tablet/tf" \
  -d '{"weekday":"sat","tfStart":"10:00","tfEnd":"22:00"}'
```

## Response codes

| Code | Meaning |
|---|---|
| `200` | OK (`/api/state`, `/api/events`) |
| `204` | Mutation applied |
| `400` | Unknown device, invalid weekday/toggle/time, or malformed JSON |
| `401` | Missing/wrong credentials |
| `409` | `pauseON` while device already blocked by no-time/timeframe |

## deviceStatus values

| Status | Meaning |
|---|---|
| `inUse` | Allowed and device is up (time used accrues) |
| `notInUse` | Allowed but device is down/unknown |
| `blockedNoTime` | Time remaining reached 0 |
| `blockedNotInTimeframe` | Outside the allowed window |
| `blockedPauseON` | Manually paused (20-min countdown) |
| `enforcementOFF` | Free-use-mode; never blocks |

## Automation recipes

```bash
# Dinner time: pause all devices for 20 minutes
for d in xbox tv tablet; do
  curl -su "$KOAUTH" -X POST "$KO/api/device/$d/pause" -d '{"toggle":"pauseON"}'
done

# Weekend treat: free-use-mode on the tv
curl -u "$KOAUTH" -X POST "$KO/api/device/tv/enforcement" -d '{"toggle":"enforcementOFF"}'

# Reward: +30 minutes on today's xbox allowance
today=$(curl -su "$KOAUTH" "$KO/api/state" | grep -o '"today":"[a-z]*"' | cut -d'"' -f4)
curl -u "$KOAUTH" -X POST "$KO/api/device/xbox/ta" -d "{\"weekday\":\"$today\",\"deltaMinutes\":30}"

# Is the xbox currently blocked?
curl -su "$KOAUTH" "$KO/api/state" | jq -r '.devices.xbox.deviceStatus'
```
