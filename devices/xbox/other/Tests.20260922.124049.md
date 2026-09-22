# Xbox driver — live test session 2026-09-22

Scope: the three contract scripts in `devices/xbox/` and the driver beneath
them, exercised against the real router (Cudy WR3000E, OpenWrt 24.10.5) and
the real console. kidsout itself was **not** running; a shell loop called
`getState.sh` every 60 s from `/tmp` to reproduce kidsout's tick and cwd.
Verdicts and byte rates below are copied from `xbox.log`; wall-clock timings
are from the loop. Console actions were performed by the operator and
reported verbally.

Console was on **WiFi** (`192.168.2.169`) throughout. Ethernet was not tested
(no cable available).

---

## Summary

| Phase | Test | Result |
|---|---|---|
| 0 | offline suite `run_tests.sh` | PASS — all suites |
| 0 | `selftest` against the live router | PASS — 11/11 |
| 0 | `status` / `check`, console off | PASS — rule ALLOWED, both MACs, IPv4 not answering |
| 1 | idle at the Home dashboard, 3 ticks | PASS — `down` |
| 2 | online multiplayer, 5 ticks | PASS — `up by=out` |
| 3 | block / repeat block / rejoin / unblock / repeat unblock | PASS |
| 4 | YouTube, 10 ticks; block and unblock while streaming | PASS |
| 5 | ethernet interface | **SKIPPED** — no cable |
| 6 | missing `config.json`, unreachable router, stale sample | PASS — `unknown` each time |
| 7 | console shut down (Energy-saving), 4 ticks | PASS — `down` near zero |

Every `getState.sh` call that reached the router finished in **0.5–1.0 s**
(kidsout's limit is 10 s, the driver's own deadline 4 s).

End state: rule `kidsout_xbox_out` = internet ALLOWED, console powered off,
working tree clean apart from the fixes listed at the end.

---

## Phase 0 — no console needed

- `run_tests.sh`: ALL SUITES PASSED (53 cases).
- `selftest`: endpoint, login, uci read, helper, neighbours (197), leases (11),
  counters, rule ALLOWED, uci write dry-run, enforcement preconditions
  (`conntrack` present, flowtable counter=yes, hw=yes), console visible on LAN.
- `status`: `facts: firewall=fw4 conntrack_tools=yes ct_acct=1 flowtable_counter=yes offload_hw=yes`;
  rule ALLOWED with `src_mac=[d8:e2:df:92:a9:93, d8:e2:df:92:a9:90]`.
- `check` with the console off: both IPv4 addresses `FAILED`, all IPv6
  neighbours `STALE`. Correct — the console does not answer ICMP, the
  neighbour table is the honest presence test.
- First tick from `/tmp` (foreign cwd): `down`, exit 0, 0.67 s. The `cd` in the
  wrapper self-locates correctly.

## Phase 1 — console on, idle at the Home dashboard

| tick | verdict | out KB/min | in KB/min |
|---|---|---|---|
| 11:55 (power-on) | **up by=out** | 977.1 | 2655.1 |
| 11:56 | down | 44.1 | 72.8 |
| 11:57 | down | 15.3 | 21.1 |
| 11:58 | down | 1.4 | 2.1 |

`check` after power-on: `192.168.2.169 … lladdr d8:e2:df:92:a9:90 DELAY`
(WiFi), `192.168.2.172 FAILED` (ethernet, not in use).

**Interpretation.** Idle sits far below both thresholds, consistent with the
2026-09-15 calibration (20.8 KB/min out). The single `up` tick is the boot
burst — sign-in, dashboard tiles, update check — and costs one minute of
allowance per power-on. Not a defect; a fair reading of "somebody switched it
on". A two-tick debounce (DESIGN §13 Q7) would hide it at the cost of one
minute of latency everywhere.

## Phase 2 — online multiplayer game

| tick | verdict | out KB/min | in KB/min |
|---|---|---|---|
| 12:01 | up by=out | 473.5 | 3969.2 |
| 12:02 | up by=out | 311.7 | 379.7 |
| 12:03 | up by=out | 484.6 | 645.2 |
| 12:04 | up by=out | 445.7 | 610.8 |
| 12:05 | up by=out | 449.0 | 629.9 |

**Interpretation.** Gameplay outbound 310–485 KB/min, matching the calibrated
289–472 KB/min and sitting ≥1.5× above the 200 KB/min line on every tick.
Inbound stays under 1 MB/min, so the game is identified purely by uploads, as
designed.

## Phase 3 — block during gameplay

| step | command / observation | result |
|---|---|---|
| 3.1 | `block.sh` from `/tmp` | `internet BLOCKED — 1 rule(s) updated and firewall reloaded`, `flushed 42 conntrack entries`, exit 0, **1.47 s** |
| 3.2 | operator watches the match | disconnected in **1–10 s** |
| 3.3 | `block.sh` again (level-triggered contract) | `already internet BLOCKED (no change, nothing written)`, `flushed 2 conntrack entries`, exit 0, **1.05 s** |
| 3.4 | operator starts a new match | **failed**, as required |
| 3.5 | ticks 12:06, 12:07 | `down` at 1.7 / 1.1 KB/min out |
| 3.6 | `unblock.sh` from `/tmp` | `internet ALLOWED — 1 rule(s) updated and firewall reloaded`, exit 0, **0.71 s** |
| 3.6 | operator retries | reconnected in **1–10 s** |
| 3.7 | `unblock.sh` again (escape hatch) | `already internet ALLOWED (no change, nothing written)`, exit 0, **0.41 s** |

`xbox.log`: `set enabled=1 (internet BLOCKED)` at 12:05:24, flush of all 27
known addresses (IPv4, link-local, ULA), `set enabled=0 (internet ALLOWED)` at
12:07:34.

**Interpretation.** The conntrack flush is doing its job: the 2026-09-15
verification measured "tens of seconds" to bite, today it was under ten. The
repeat block flushed 2 entries — the game's reconnect attempts — and wrote
nothing to uci, so the every-minute call kidsout makes while a device is
blocked costs one read RPC plus a flush and no flash write. The very first tick
after the block already reads `down`, so a blocked console does not accrue
time.

## Phase 4 — YouTube

| tick | verdict | out KB/min | in KB/min | note |
|---|---|---|---|---|
| 12:09 | up by=out | 491.5 | 6274.9 | game still open in background |
| 12:10 | up by=out | 560.6 | 4025.9 | |
| 12:11 | up by=out | 563.9 | 12829.6 | |
| 12:12 | up by=out | 355.9 | 106658.6 | |
| 12:13 | up by=out | 563.6 | 59840.4 | |
| 12:14 | up by=out | 242.1 | 85623.3 | |
| 12:15 | up by=out | 230.1 | 60522.4 | game quit via Guide → Quit around here |
| 12:16 | up by=out | 407.2 | 45642.0 | |
| 12:17 | up by=out | 338.7 | 103728.4 | |
| 12:18 | **up by=in** | 185.0 | 52627.0 | inbound rule exercised |

Block while streaming (12:18:5x): `internet BLOCKED`, `flushed 25 conntrack
entries`, exit 0, 1.29 s. Operator skipped forward in the video → **stopped
immediately**. Ticks 12:19 and 12:20: `down` at 0.0 and 32.8 KB/min out.
`unblock.sh`: `internet ALLOWED`, exit 0, 0.78 s. Video **recovered after
~20 s**.

**Interpretation.** The verdict is right on all ten ticks, but nine of them
fired on the *outbound* rule, not the inbound one the 2026-09-21 fix added.
Yesterday's YouTube session measured 60–80 KB/min out against 4.4–18 MB/min
in; today's ran 185–564 KB/min out against 4–107 MB/min in. Two causes:

1. The game was still active in the background for the first six ticks
   (Xbox keeps a title running until it is explicitly quit; a multiplayer
   title keeps talking to its servers).
2. After the quit, outbound stayed at 185–407 KB/min. Streaming outbound is
   almost entirely TCP acknowledgements, which scale with the inbound
   bitrate, and today's inbound was 5–10× yesterday's (a higher-resolution
   video, or aggressive buffering — 107 MB/min ≈ 14 Mbit/s).

So a high-bitrate stream crosses the 200 KB/min outbound line on its own, and
the calibration table's "downloading 93.7 KB/min out" separation from gaming is
bitrate-dependent rather than a fixed property of the traffic type. This does
not change any verdict today — both rules say `up` — but it means the outbound
threshold cannot be relied on to distinguish a heavy stream from gameplay, and
a heavy *download* would likely read `up by=out` as well as `by=in`. The
inbound rule remains the one that catches low-bitrate streaming, which is the
case it was added for. Worth re-measuring outbound during a real game update
before any threshold is touched (DESIGN §13 Q6).

## Phase 5 — ethernet

Skipped; no cable. Still open:
- `check` must show `192.168.2.172` REACHABLE/DELAY once cabled, with no
  `discover --write` needed.
- `block.sh` while wired must cut the session (the rule lists both MACs, but
  this has not been exercised end to end on the wired MAC).

## Phase 6 — failure modes (operator-free)

| test | how | stdout | exit | time | `xbox.log` |
|---|---|---|---|---|---|
| 6.1 missing credential | `config.json` moved aside, restored after | `unknown` | 1 | 0.09 s | `state=unknown error=no config.json — copy config.example.json …` |
| 6.2 router unreachable | `primary_host` → `192.0.2.1`, `alt_hosts` → `[]`, restored via `git checkout` | `unknown` | 1 | **3.17 s** | `state=unknown error=RouterUnreachable: no working ubus endpoint …` |
| 6.3a stale sample | 340 s after the previous sample | `unknown` | 0 | 0.92 s | `state=unknown reason=previous sample is 340s old (> 300s), too stale to use` |
| 6.3b re-baseline | 6 s later | `down` | 0 | 0.99 s | `state=down out=0.0KB/min in=0.0KB/min` |

**Interpretation.** All four paths print exactly one word and stay well
inside both the 4 s driver deadline and kidsout's 10 s kill. 6.1 and 6.2 exit
non-zero; kidsout maps a non-zero exit to `unknown` regardless of stdout, so
the outcome is identical either way, and the offline suite asserts this
behaviour. The stale path yields exactly one `unknown` tick and then
re-baselines, as DESIGN §4 promises. The stderr message on 6.1 is precise and
names the fix.

## Phase 7 — console shut down (Energy-saving)

| tick | verdict | out KB/min | in KB/min |
|---|---|---|---|
| 12:22 | up by=out | 323.3 | 93306.5 (last streaming tick before shutdown) |
| 12:23 | down | 81.3 | 127.1 |
| 12:24 | down | 0.4 | 0.3 |
| 12:25 | down | 7.9 | 23.5 |
| 12:26 | down | 0.2 | 0.2 |

`check`: both IPv4 addresses `FAILED`.

**Interpretation.** In Energy-saving mode the console is silent on the
network, so standby cannot burn allowance and a shutdown console reads `down`
within one tick. The 15-minute `audit:` lines (12:05, 12:20) both read
`internet ALLOWED` — the rule state matched what the driver believed at each
audit.

---

## Defects found and fixed in this session

| # | defect | fix |
|---|---|---|
| D1 | `getState.sh` header said the state deadline default is 7 s; `config.example.json` and DESIGN §4 say 4 s | comment corrected to 4 s |
| D2 | `xbox.py` told the operator to `opkg install conntrack-tools` in the flush-failure and selftest messages; that package does not exist on OpenWrt (DESIGN §7, §11 item 4), the package is `conntrack`. `router_bootstrap.sh` installed the right package but printed the wrong name | messages in `xbox.py`, `router_bootstrap.sh` and `mock_router.py` now name `conntrack` and say explicitly that `conntrack-tools` is wrong. The internal fact key `conntrack_tools=yes/no` is unchanged |
| D3 | `check` listed every IPv6 neighbour twice: the helper's `neigh` verb ran `ip neigh show` (which already covers both families) followed by `ip -6 neigh show` | `cmd_check` de-duplicates rows (takes effect immediately); the helper in `router_bootstrap.sh` drops the redundant second command (takes effect on the next bootstrap run) |

Offline suite after the fixes: ALL SUITES PASSED. Live `check` after the
fixes: 29 lines instead of ~50, each neighbour once.

## Observations, not defects

- **One `up` tick per power-on** from the boot burst (Phase 1).
- **Streaming outbound is bitrate-dependent** and can exceed the 200 KB/min
  outbound line by itself (Phase 4). The calibration table in DESIGN §5 lists
  60–80 KB/min for streaming; today's session shows 185–564 KB/min. Consider
  adding today's figures to the table so the next reader does not assume the
  outbound line separates streaming from gaming.
- **Failure paths exit 1** with `unknown` on stdout (Phase 6). Harmless under
  the contract; noted so nobody "fixes" it into exit 0 without checking the
  offline suite, which asserts the current behaviour.
- **A quick `status`, `counters` or manual `state` call resets the rate
  baseline.** Running one between kidsout ticks shortens the next window and
  extrapolates a few seconds of traffic to a per-minute rate (yesterday's log
  shows an 8 s window reading 1402 KB/min). Avoid these while kidsout is
  running, or read `xbox.log` instead.

## Not covered

- Ethernet interface (Phase 5).
- A real game download in progress — expected to read `up` (accepted
  trade-off, DESIGN §5) but not observed today.
- Instant-on power mode (deliberately not used).
- kidsout end to end (out of scope for this session by request).
