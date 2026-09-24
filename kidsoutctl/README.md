# kidsoutctl

A kubectl-style command-line client for the [kidsout](../README.md) parental
device blocker. Human-friendly colored tables by default, `-o json|yaml` for
scripts, live watching via SSE, and leveled verbosity for debugging.

```
$ kidsoutctl get
today: fri
DEVICE  STATUS          ENFORCE  PAUSE         ALLOWED  USED   REMAINING  TIMEFRAME
tablet  blockedPauseON  on       on (12m left) 2h00m    40m    1h20m      09:00-21:00
tv      blockedNoTime   on       off           2h00m    2h00m  0m         09:00-21:00
xbox    inUse           on       off           2h00m    40m    1h20m      09:00-21:00
```

## Install

```bash
cd kidsoutctl && ./go_build.sh          # static linux binary ./kidsoutctl
# or simply:
go build -o kidsoutctl ./kidsoutctl     # from the repo root
```

## Configure

Two environment variables (same ones the [API docs](../README_API.md) use):

```bash
export KO="http://localhost:8080"   # API base URL (or KIDSOUT_SERVER, or --server)
export KOAUTH="user:pass"           # Basic Auth credentials (or KIDSOUT_AUTH)
```

Credentials are **only** accepted via environment — never as a flag, so they
don't leak into shell history or `ps` output.

## Commands

| Command | What it does |
|---|---|
| `get [device...]` | Today's state as a colored table (alias: `state`) |
| `get --week [device...]` | Full weekly schedule per device |
| `watch` | Live updates via SSE; `--once` prints one snapshot and exits |
| `pause <device...> \| --all` | Block now — 20-minute countdown |
| `unpause <device...> \| --all` | Cancel an active pause (alias: `resume`) |
| `enforce <device...> \| --all` | Enforcement ON — rules apply again |
| `unenforce <device...> \| --all` | Enforcement OFF — free use, no time accrual |
| `block <device...> \| --all` | Run the device's `block.sh` now — no kidsout state change |
| `unblock <device...> \| --all` | Run the device's `unblock.sh` now — no kidsout state change |
| `ta <device> <weekday\|today> <±minutes>` | Adjust TimeAllowed (delta) |
| `tf <device> <weekday\|today> <HH:MM> <HH:MM>` | Set TimeFrame window (when it can be allowed)  |
| `completion bash` | Bash tab-completion script |
| `version` | Client version; also the server's (via `GET /api/version`) when `KOAUTH` is set |
| `help` | Usage text |

Weekdays: `sun mon tue wed thu fri sat`, or `today` (resolved server-side).

## Flags

| Flag | Meaning |
|---|---|
| `-o table\|json\|yaml` | Output format (default `table`; JSON/YAML mirror the API payload) |
| `--week` | With `get`: weekly schedule instead of today's summary |
| `--all` | Apply `pause`/`unpause`/`enforce`/`unenforce`/`block`/`unblock` to every device |
| `--once` | With `watch`: exit after the first snapshot |
| `--server URL` | Override the API base URL |
| `--color auto\|always\|never` | Color control (`auto` detects TTY; [`NO_COLOR`](https://no-color.org) honored) |
| `--timeout 10s` | HTTP timeout for non-streaming requests |
| `-v1` … `-v5` | Verbosity: 1=requests · 2=+timings · 3=+headers (auth redacted) · 4=+bodies · 5=+connection trace |

Flags may appear before or after the command, kubectl-style:
`kidsoutctl get -o yaml` ≡ `kidsoutctl -o yaml get`.

## Examples

```bash
kidsoutctl get                          # colored table, all devices
kidsoutctl get xbox -o yaml             # one device, YAML
kidsoutctl get --week                   # weekly schedule
kidsoutctl watch                        # follow live changes (Ctrl-C to stop)

kidsoutctl pause --all                  # dinner time!
kidsoutctl unpause tablet               # ok, back
kidsoutctl unenforce tv                 # weekend treat: free use on the tv
kidsoutctl enforce tv                   # treat's over

kidsoutctl block tv                     # run tv's block.sh right now
kidsoutctl unblock --all -o json        # run every unblock.sh, results as a JSON list

kidsoutctl ta xbox today 30             # reward: +30 min right now
kidsoutctl ta xbox fri -15              # -15 min every Friday
kidsoutctl tf tablet sat 10:00 22:00    # Saturday window

kidsoutctl get -v2                      # show request timings on stderr
kidsoutctl get -o json | jq -r '.devices.xbox.deviceStatus'

kidsoutctl version                      # client and server build identity
# kidsoutctl 1.0.0 (commit 83544b0, go1.25.5 linux/amd64)
# server: kidsout 1.0.0 (commit 83544b0, go1.25.5 linux/amd64)
```

`version` never fails: without credentials it prints only the client line, and an
unreachable server is reported as `server: unavailable (...)` with exit code 0.

`block`/`unblock` are for external programs that want to act on a device
through kidsoutctl. They only run the script; kidsout's own rules keep
running, so a manual `unblock` of a device kidsout considers blocked is undone
on the next 1-minute tick (see the [API docs](../README_API.md)). The command
exits `1` if any script exits non-zero, and prints its stderr.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime/API error (including partial failure with `--all`) |
| `2` | Usage error (bad arguments, unknown command) |
| `3` | Authentication failure (missing `KOAUTH` or HTTP 401) |
| `4` | Conflict — HTTP 409 (e.g. pausing an already-blocked device) |

## Tab completion

```bash
kidsoutctl completion bash | sudo tee /etc/bash_completion.d/kidsoutctl
```

## Development

```bash
go test ./kidsoutctl/    # unit tests run against an httptest server — no live API needed
go vet ./kidsoutctl/
```

The version string comes from the shared `kidsout/version` package, so the client
and the server report the same release. `./go_build.sh` stamps the git commit;
`VERSION=1.2.3 ./go_build.sh` overrides the version.

See [DESIGN.md](DESIGN.md) for architecture and decisions.
