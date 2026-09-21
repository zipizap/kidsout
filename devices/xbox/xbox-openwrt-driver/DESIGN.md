# Xbox device driver — design and operation

A kidsout device driver that controls the family Xbox through the OpenWrt
router's ubus-over-HTTPS API. **Block = internet off, allow = internet on.**

Verified end to end on the real router and console on 2026-09-15: a live online
multiplayer game was blocked and then restored.

This file is the single source of truth for this driver. It replaces the earlier
`README.md`, `REVIEW.1.md`, `REVIEW.2.md`, `PROGRESS.md`, `F-06.md` and
`S-02.md`; §11 keeps the parts of that history that still explain why things are
the way they are.

---

## Table of contents

| § | |
|---|---|
| 1 | [What this is](#1-what-this-is) |
| 2 | [Daily use](#2-daily-use) |
| 3 | [How enforcement works](#3-how-enforcement-works) |
| 4 | [How "is it in use?" works](#4-how-is-it-in-use-works) |
| 5 | [Calibration](#5-calibration) |
| 6 | [Security model](#6-security-model) |
| 7 | [First-time setup](#7-first-time-setup) |
| 8 | [The upstream kidsout contract](#8-the-upstream-kidsout-contract) |
| 9 | [Testing](#9-testing) |
| 10 | [This router, as measured](#10-this-router-as-measured) |
| 11 | [Why it looks like this](#11-why-it-looks-like-this) |
| 12 | [Troubleshooting](#12-troubleshooting) |
| 13 | [Open questions](#13-open-questions) |

---

## 1. What this is

kidsout scans `devices/<name>/` for three executable scripts and runs them on a
one-minute tick. This directory implements them for the Xbox.

```
devices/xbox/
├── getState.sh block.sh unblock.sh       the upstream contract — these three
│                                         names are all kidsout ever calls
└── xbox-openwrt-driver/                  everything else lives here
    ├── xbox.py                           the driver (stdlib only, no pip deps)
    ├── allow.sh install.sh status.sh     operator conveniences
    ├── device.json                       device facts: interfaces, thresholds, router
    ├── config.example.json               credential template → copy to config.json
    ├── config.json                       the router credential (0600, gitignored)
    ├── router_bootstrap.sh               ONE-TIME, runs ON THE ROUTER
    ├── DESIGN.md                         this file
    ├── router_checks_*.sh                read-only diagnostics + their printouts
    ├── mock_router.py test_*.py run_tests.sh   the offline suite
    └── xbox.log .state.json              driver-owned log and sample state (gitignored)
```

The split exists because kidsout only ever looks for those three names, so
anything else beside them is noise to it. Keeping the implementation in one
clearly-named subdirectory makes the device directory legible at a glance and
leaves the contract surface impossible to mistake. Each contract script is a
three-line wrapper that `cd`s into `xbox-openwrt-driver/` and execs `xbox.py`;
the driver anchors every path it owns — `device.json`, `config.json`,
`.state.json`, `xbox.log` — on its own location, so nothing depends on the
working directory kidsout happens to use.

**Unless a command says otherwise, run it from `xbox-openwrt-driver/`.**

Two facts shape everything else:

- **The console has two network interfaces**, wired and wireless, each with its
  own MAC, and it switches between them whenever the cable is plugged or pulled.
- **The router offloads flows in hardware.** A MediaTek PPE forwards established
  connections in silicon, where they are invisible to the firewall and to any
  software packet counter.

Miss either one and the driver fails *silently*: it reports success while the
console keeps playing, or reports `down` all afternoon while a game is running.

---

## 2. Daily use

```
./xbox.py status      # endpoint, router facts, rule state, current byte rate
./xbox.py state       # the one word kidsout sees: up | down | unknown
./xbox.py block       # internet off  (enable REJECT + flush live flows)
./xbox.py allow       # internet on
./xbox.py check       # is the console present on the LAN right now
./xbox.py counters    # byte totals and the rate since the last sample
./xbox.py selftest    # after any router change
./xbox.py discover    # find the console's interfaces (--write to record them)
./xbox.py calibrate --minutes 30 --label standby
./xbox.py pin         # re-record the router's TLS fingerprint
./xbox.py uninstall   # remove the rule and accounting state (destructive)
```

**If the console is ever stuck offline** — kidsout crashed or was upgraded while
the Xbox was blocked, `runtimestore.yaml` was reset, someone ran `block` by hand
— run `../unblock.sh`. It is safe at any time and safe to repeat.

Nothing else reconciles the router's state with kidsout's: `unblock.sh` is
edge-triggered, so the router holds enforcement state that lives outside
kidsout's knowledge. The driver mitigates this by logging what the router is
actually enforcing every 15 minutes (`audit:` lines in `xbox.log`), which makes a
stuck block visible, but does not fix it.

---

## 3. How enforcement works

One firewall rule, `kidsout_xbox_out`:

```
config rule 'kidsout_xbox_out'
        option name    'kidsout xbox: block internet'
        option src     'lan'
        option dest    'wan'
        list   src_mac 'd8:e2:df:92:a9:93'      # ethernet
        list   src_mac 'd8:e2:df:92:a9:90'      # wifi
        option proto   'all'
        option target  'REJECT'
        option enabled '0'          <-- the toggle
```

| `enabled` | ruleset | meaning |
|---|---|---|
| `1` | the REJECT is in force | **internet BLOCKED** |
| `0` | the REJECT is absent | **internet ALLOWED** |

The driver never prints the raw value — it prints `internet BLOCKED` /
`internet ALLOWED`. Printing `enabled=0` is what made an earlier polarity
inversion easy to miss.

### Keyed on every MAC, not on the IP

fw4 parses `src_mac` as a list, so one rule covers both interfaces. Confirmed in
the live ruleset:

```
chain forward_lan {
    ether saddr { d8:e2:df:92:a9:90, d8:e2:df:92:a9:93 } counter jump reject_to_wan
```

A rule naming only one MAC matches nothing while the console is on the other —
the block fails open and still reports success. An IP-keyed rule would also miss
IPv6 entirely, though on this ISP that is moot (§10).

### Blocking must also flush conntrack — this is the real mechanism

fw4's forward chain begins:

```
chain forward {
        meta l4proto { tcp, udp } flow add @ft        <-- hardware offload
        ct state vmap { established : accept, ... }   <-- established bypass
        iifname "br-lan" jump forward_lan             <-- our rule is in here
```

Two things sit above the rule. An established flow is accepted before reaching
it, and an *offloaded* flow never enters netfilter at all. So enabling the REJECT
stops **new** connections only. Reloading the firewall does not help either: the
conntrack entry survives, matches `established : accept`, and is immediately
re-offloaded.

**Destroying the conntrack entry is the only thing that cuts a session already in
progress.** `block` therefore enables the rule *and* flushes the console's flows,
across every address the router knows for its MACs. If the flush fails, `block`
**exits non-zero** rather than reporting a success it did not achieve.

Once the flush lands the console cannot re-offload while blocked: `flow add @ft`
only acts on established, bidirectionally-seen flows, and new SYNs are REJECTed
before they get there. One successful flush is enough.

### What a block actually looks like

Measured during a live multiplayer game:

```
flushed 32 conntrack entries        (0 on an immediate repeat)
established=0   nonDNS=0   4,613 packets REJECTed
```

**A block is not instantaneous.** New connections are refused at once, but the
game retried for tens of seconds before giving up and disconnecting. "Block"
means *within about a minute*, not *immediately*.

**DNS keeps working while blocked**, deliberately. The console can still reach
the router's own resolver on `192.168.2.1:53` because that is *input* traffic to
the router, while this rule governs *forwarded* traffic. It grants no internet
access — 45 of 46 remaining flows during the test were DNS retries — so it is
left alone rather than adding a second rule whose only effect is a different
error message on the console.

---

## 4. How "is it in use?" works

`getState.sh` prints one word: `up`, `down` or `unknown`.

The signal is the console's byte rate in **either direction**, each compared
against its own threshold in `device.json` → `state`:

```
rate_out = (xbox_out(now) - xbox_out(prev)) / (now - prev) * 60
rate_in  = (xbox_in(now)  - xbox_in(prev))  / (now - prev) * 60
up      rate_out > threshold_out_bytes_per_min      (gameplay uploads)
     or rate_in  > threshold_in_bytes_per_min       (streaming video)
down    neither
unknown the rate is not computable — see below
```

Outbound alone was the rule until 2026-09-21, when an evening of YouTube read
`down` on every tick; §5 explains why a second, inbound threshold was the only
fix and what it costs. Setting `threshold_in_bytes_per_min` to `null` restores
the outbound-only rule.

### The bytes come from conntrack, not from an nft counter

This is the single most important design decision, and it was forced by
measurement.

An nft counter in the forward hook — the obvious implementation, and what an
earlier version shipped — **cannot see a hardware-offloaded flow**. Packets are
forwarded by the PPE in silicon and never reach any netfilter hook, so the
counter sees only each flow's first few packets and under-reports gameplay by
roughly 98 %. The driver would have reported `down` all afternoon while a game
was running. A `netdev ingress` chain fails for the same reason.

What rescues it: fw4 declares its flowtable with `counter`, and Linux 6.6 feeds
the hardware's per-flow MIB back into conntrack via `nf_ct_acct_add()` on a ~1 Hz
poll. Verified empirically on this SoC — **9 of 9 offloaded flows gained bytes
over a 20 s window, none static.**

So the helper sums `/proc/net/nf_conntrack` byte counters router-side. This needs
no nft table, no chain, no priority, no device names, nothing to survive an fw4
reload, and no extra package.

### The accumulator, and why a naive sum is wrong

Conntrack counters are **per-flow and vanish when the flow expires**, so a sum
over live flows is not a counter — it is a gauge that jumps up as flows are born
and drops as they die. Measured during real gameplay, a naive sum produced
**−510.5 KB/min and −27.1 KB/min** outbound.

The helper therefore keeps a running accumulator:

```
for each live flow belonging to the console:
    seen before  →  total += max(0, current_bytes - last_seen_bytes)
    new          →  total += current_bytes
    vanished     →  contributes nothing further
```

keyed on the **conntrack id** (`conntrack -L -o id`), so a reused source port is
correctly seen as a new flow rather than as a counter reset. Falls back to
5-tuple keying against `/proc/net/nf_conntrack` where conntrack is unavailable.
State lives in `/tmp` (tmpfs — no flash wear); a reboot clears it, the total
drops, and the driver's decrease-detection yields exactly one `unknown` tick
before re-baselining.

Because the helper guarantees monotonicity, `xbox.py`'s `sample_rate()` and
`cmd_state()` need no knowledge of any of this — they just diff two totals.

### Which addresses count

The console had **8 addresses** at test time (one IPv4, four link-local, three
ULA), all genuinely its own. Only **routable** ones count toward the rate:
`fe80::/10` and `fd00::/8` traffic never leaves the LAN, so counting it would
inflate the rate with household chatter and invalidate a threshold calibrated on
IPv4. Link-local and ULA addresses are still **flushed**, just not **counted**.

Addresses are resolved at run time from the DHCP lease file and neighbour table,
keyed on the console's MACs, rather than trusting a static IP — a reservation
only takes effect once the console renews.

### When it says `unknown`, it means it

`unknown` is reported when the router cannot be consulted, the login is rejected,
the helper or its state is missing, there is no previous sample yet, the previous
sample is stale (> `max_sample_age_s`, so an hour of bytes is never divided into
one minute), or the totals went backwards. Upstream treats `unknown` like `down`
for enforcement but records it as a grey dot in the WeekView, which is how an
outage stays distinguishable from a quiet afternoon.

### Timeout budget

kidsout kills scripts at 10 s, and a slow `getState.sh` stalls the tick for every
other device. The state path therefore has its own budget — `state_timeout`
(1.5 s per request) and a hard `state_deadline` (4 s wall clock) — a single
endpoint path, a memoised login failure so a dead router is not swept repeatedly,
and no fallback chain. Every failure mode in the offline suite returns inside
that deadline, measured with the timeouts from the **shipped**
`config.example.json`.

---

## 5. Calibration

Both thresholds are **measured, not guessed**. Against the real console on WiFi:

| state | outbound | inbound | verdict @ out 200 KB/min, in 1 MB/min |
|---|---|---|---|
| idle / dashboard | **20.8** KB/min | **23** KB/min | DOWN ✓ |
| downloading | **93.7** KB/min | **11.8** MB/min | UP (by inbound — accepted, see below) |
| gaming (measured twice) | **289.5** / **471.6** KB/min | — | **UP** ✓ (by outbound) |
| streaming YouTube (2026-09-21, 60 s ticks) | **60–80** KB/min | **4.4–18** MB/min | **UP** ✓ (by inbound) |
| blocked | **3.0** KB/min | **8.2** KB/min | DOWN ✓ |

**Outbound threshold: `204800` bytes/min (200 KB/min).** It sits between
downloading and gaming with ≥3× separation either side.

**Inbound threshold: `1048576` bytes/min (1 MB/min).** 45× above the idle
dashboard, 4× below the slowest streaming tick observed. It is deliberately low
enough to catch low-bitrate video and music streaming (~1.2 MB/min) — the
failure mode that matters is a false `down`, which is silent unmetered viewing.

Every figure was cross-validated against `iwinfo assoclist` — mac80211's own
per-station byte counters, wholly independent of netfilter and of the offload
path. Conntrack captured **95.5 %** of iwinfo's inbound bytes and 71–85 % of
outbound. The outbound gap is expected and is not an error: outbound is dominated
by small TCP ACKs, and iwinfo counts whole 802.11 frames where conntrack counts
L3 payload. What matters is that the two move together.

### Why there is an inbound threshold, and what it costs

The first shipped rule was outbound-only, and for a reason: during a 12 MB/min
game download the outbound stream was 93.7 KB/min — 0.8 % of downstream and well
under 200 KB/min — so a background update did not burn the allowance. That was
measured and correct.

It was also the reason **streaming video read `down`**. On 2026-09-21 the console
played YouTube for a quarter of an hour and every 60 s tick logged
`state=down out=60–80KB/min in=4.4–18MB/min`. Streaming is pure inbound plus
TCP acknowledgements; its outbound rate is *lower* than a download's. So no
outbound threshold can separate the two: a line low enough for YouTube also
fires on every patch, and sits right next to random ACK bursts (single ticks of
`out=406` and `out=1372` appear in the same log), so it would flap constantly.
The metric had to change, not the number.

The inbound rule therefore trades one known false `down` for one known false
`up`: **a game download in progress now reads `up`.** That was accepted
deliberately —

- a false `up` burns allowance and gets noticed and complained about; a false
  `down` is silent, and the allowance never decrements while the video plays;
- the console is in **Energy-saving (Shutdown) power mode**, so nothing downloads
  while it is off. A download can only read `up` while somebody has switched the
  console on, which is a fair reading of "in use" for a child's allowance.

If the power mode is ever changed to Instant-on, standby updates will burn
allowance overnight. Either keep Energy-saving, or see §13 Q6 for the per-flow
discrimination that would remove the trade-off altogether.

### Re-calibrating

```
./xbox.py calibrate --minutes 30 --interval 60 --label standby
./xbox.py calibrate --minutes 10 --interval 60 --label gaming
./xbox.py calibrate --minutes 10 --interval 60 --label streaming
```

Samples append to `calibration.jsonl`; each run prints n / min / median / max in
KB/min for **both** directions. Use `--interval 60` — that is kidsout's tick,
and short windows alias badly against bursty ACK traffic.

- **Outbound** (`threshold_out_bytes_per_min`): pick comfortably above the
  standby maximum and comfortably below the gaming minimum — geometric midpoint
  is a reasonable default.
- **Inbound** (`threshold_in_bytes_per_min`): pick comfortably above the standby
  *inbound* maximum and comfortably below the streaming *inbound* minimum. Err
  low: a missed stream is silent, an over-eager `up` is not.

Write both to `device.json` → `state` **in bytes**. Re-calibrate if the console
moves to ethernet, since the measurements above are from WiFi.

---

## 6. Security model

### No router root password anywhere

`router_bootstrap.sh` creates a dedicated rpcd login named `kidsout` whose ACL
grants exactly:

| capability | why |
|---|---|
| `ubus session login/access` | to authenticate at all |
| `ubus uci get/set/add/delete/commit/apply/confirm/revert` on **`firewall` only** | to toggle `kidsout_xbox_out` |
| `ubus file exec` on **`/usr/libexec/kidsout-xbox` only** | counters, conntrack flush, neighbour table |
| `ubus system board` | version reporting in `status` |

`config.json` holds that password, never root's, and `xbox.py` chmods the file to
`600` on every run if it finds it group- or world-readable.

### Why a helper script, not `file exec` on nft/conntrack

The obvious approach — scope the ACL to "the specific conntrack/nft commands" —
does not actually achieve anything:

- rpcd authorises the **command path**, not its arguments, and runs it **as
  root**.
- `file exec` on `/usr/sbin/nft` therefore permits `nft flush ruleset` or any
  other complete rewrite of the firewall.
- A credential that can rewrite the household router's firewall is
  root-equivalent for every purpose that matters, so the scoping would be
  cosmetic.

Instead `/usr/libexec/kidsout-xbox` accepts a **closed set of verbs** (`info`,
`counters`, `counters-install`, `counters-remove`, `flush`, `neigh`, `leases`)
and validates its arguments: MACs must look like MACs, addresses may contain only
hex digits, dots and colons. rpcd invokes it with an argv array and no shell, so
there is no quoting surface. Verified: `counters-install 'foo;rm -rf /'` is
rejected with exit 2.

**Worst case with the `kidsout` credential:** an attacker can block or unblock
the Xbox, read its byte counters, drop its conntrack entries, and list the DHCP
leases and neighbour table. Real, but bounded — and the smallest set that still
lets the driver work.

### Transport

ubus JSON-RPC over HTTPS at `https://192.168.2.1:443/ubus`, with
`192.168.255.7` (WireGuard side) as a genuine fallback and HTTP as a last resort.
The winning endpoint is cached in `.state.json` and reused only while it still
names a configured host and port.

`verify_tls` stays `false` because the router serves a self-signed certificate.
Instead `./xbox.py pin` records that certificate's SHA-256 fingerprint in
`device.json`, and the driver then **refuses to send credentials to any other
certificate** — checked after the TLS handshake and before the request body is
written. That closes the LAN MITM window without needing a real CA. Re-run `pin`
if the router regenerates its certificate.

Current pin: `65f6d012…e4e6947f` (EC cert, valid to 2027-01-18).

### Two traps worth remembering

**The credential hash must land in `/etc/shadow`, not `/etc/passwd`.** busybox
`passwd` writes the hash inline into `/etc/passwd` — which is world-readable
(`0644`) — if no `/etc/shadow` entry exists for the user yet. The bootstrap
therefore creates the shadow entry under `umask 077` *before* calling `passwd`,
and migrates any hash it finds in the wrong place.

**`--uninstall` must preserve file modes.** The obvious
`grep -v … > tmp && mv tmp "$f"` gives the replacement file the shell's umask,
which would drop `/etc/shadow` from `0600` to `0644`. It uses a mode-preserving
copy and prints the resulting mode.

### Residual risk

- `leases` and `neigh` disclose the LAN's device inventory — information any
  device on the LAN can obtain anyway.
- `flush` can drop conntrack entries for an arbitrary address: a nuisance-level
  DoS against one host. Restricting it to the console's own addresses would mean
  hard-coding them into the helper; not worth the loss of flexibility.
- The password sits in a plaintext file readable by the account running kidsout.

---

## 7. First-time setup

### 1. Install `conntrack` on the router

```
ssh root@192.168.2.1 'opkg update && opkg install conntrack'
```

The OpenWrt package is **`conntrack`**, not `conntrack-tools` — that is the
upstream project name and does not exist as a package. Without it the block
cannot cut a session already in progress (§3), and `selftest` will fail loudly.

### 2. Create the scoped user

Two commands, and **the second must be interactive**:

```
ssh root@192.168.2.1 'cat > /tmp/router_bootstrap.sh' < router_bootstrap.sh
ssh -t root@192.168.2.1 sh /tmp/router_bootstrap.sh
```

Do **not** pipe the script into ssh. ssh refuses to allocate a pseudo-terminal
when its stdin is a redirect, so the remote side has no `/dev/tty` and `passwd`
cannot prompt — the run dies half way, after creating the user. `scp` may not
work either: this router has no `sftp-server`, hence `cat >`.

It prompts for a **new** password — not root's — and prints the router's facts.
`sh /tmp/router_bootstrap.sh --uninstall` reverses everything.

### 3. Credentials on the kidsout machine

```
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json          # username: kidsout, password: the one you chose
./xbox.py selftest           # verifies every capability the driver needs
./xbox.py pin                # record the router's certificate fingerprint
```

### 4. Record the console's interfaces and install

Turn the console on, then:

```
./xbox.py discover --write   # records EVERY interface into device.json
./xbox.py install            # creates the rule (ALLOWED) and registers counters
```

`install` leaves the console **online**. Confirm it is still working before
testing enforcement.

Run `discover --write` once on each interface — with the cable in, and with it
out — so both MACs are recorded. A missing interface means the block silently
fails open whenever the console is using it.

### 5. Calibrate

Follow §5. The shipped 200 KB/min outbound and 1 MB/min inbound are correct for
this console on WiFi. Make sure the console's power mode is **Energy-saving**
(Settings → General → Power options) — with Instant-on, standby downloads would
read `up` overnight.

### 6. Deploy

kidsout's `DiscoverDevices` scans only the **immediate children** of its devices
directory — it does not recurse. So the *device* directory must sit at
`devices/xbox/`, and `getState.sh`, `block.sh` and `unblock.sh` must be
immediate children of it. `xbox-openwrt-driver/` being a subdirectory is fine:
kidsout never looks inside it, and the three wrappers reach in from above.

kidsout must be **restarted** — discovery runs once, at startup.

---

## 8. The upstream kidsout contract

Upstream (`github.com/zipizap/kidsout`) scans `devices/<name>/` for three
executable scripts and runs them with `cmd.Dir` unset, so they self-locate —
here with `cd "$(dirname "$0")/xbox-openwrt-driver"`, since the driver sits one
level below the names kidsout calls.

| script | called | must do |
|---|---|---|
| `getState.sh` | every minute, and on each UI action | print `up`/`down`/`unknown` on stdout |
| `block.sh` | **every tick** while blocked **and** reading `up` | exit 0 |
| `unblock.sh` | **once**, on leaving the blocked state | exit 0 |

Details that are load-bearing:

- **10-second timeout**, enforced with `exec.CommandContext`. On expiry the
  process is SIGKILLed and the result is `unknown`.
- **stdout must be exactly one word.** The comparison is a whole-string match
  after `TrimSpace`; any extra token makes it `unknown`.
- **Non-zero exit → `unknown`**, and stdout is discarded even if it said `up`.
- **cwd is kidsout's, not the device's.** Hence the `cd` in every script.
- **stderr is swallowed.** Upstream uses `cmd.Output()`, which discards stderr on
  the success path, so diagnostics go to `xbox.log` (size-capped at 256 KB,
  rotated in place) instead. That file is the first place to look.
- **`block.sh` is level-triggered, not edge-triggered.** It runs every minute
  while the device is blocked and reading `up`, so it must be cheap when nothing
  needs changing — it is: the driver reads the current rule state first and
  writes nothing if it already matches, so the steady state costs one read-only
  RPC and neither a flash write nor a firewall reload.
- **`unblock.sh` is edge-triggered and never retried.** If it fails, the device
  stays blocked and only a log line records it. Hence the manual escape hatch.
- **Only `inUse` accrues time**; `down` and `unknown` both accrue nothing.
- A device whose `getState.sh` is slow **delays the whole evaluation tick**,
  including the TV and tablet — which is why §4's timeout budget matters beyond
  this one device.

---

## 9. Testing

```
./run_tests.sh
```

| suite | covers |
|---|---|
| `test_block_semantics.py` | polarity, rule shape, both MACs, migration, idempotency, conntrack flush, flush-failure exit code |
| `test_state.py` | the up/down/unknown metric, stated explicitly then asserted, incl. non-monotonic counters and the download-vs-gameplay case |
| `test_failure_modes.py` | nine ways of failing, each → `unknown` inside the deadline |
| `test_contract.py` | the real `.sh` files as subprocesses, from another cwd |

Currently **53 cases / 137 assertions**, all passing.

`mock_router.py` models rpcd including its **failure** modes — refused logins,
ACL denials, 404s, non-JSON responses, helper failures, missing counters, slow
responses. Its TLS key is generated into a temp directory and removed on exit,
rather than living in the working tree.

### What the offline suite cannot tell you

The mock returns counter *values*; it has no flowtable, no hooks, no priorities,
and no notion that a packet might bypass netfilter entirely. It therefore
**cannot** distinguish a counter that sees all traffic from one that sees 2 %.
An earlier design passed 119 assertions against a mechanism the hardware later
disproved.

This is not an argument against the mock — it caught two real bugs while being
written, and two of its newer tests were mutation-checked to confirm they fail
when the fix is reverted. It is an argument for a boundary: **the offline suite
validates the driver's logic, not the router's behaviour.** Any claim about what
the router does must carry a hardware citation.

---

## 10. This router, as measured

Cudy WR3000E v1 · OpenWrt 24.10.5 r29087 · mediatek/filogic · Linux 6.6.119 aarch64

| fact | value | why it matters |
|---|---|---|
| firewall | fw4 + nftables 1.1.1 | `src_mac` is a `PARSE_LIST` (`fw4.uc:2314`), so one rule can name both MACs |
| zones | `lan`, `wan`, **`WgZone`** | a third zone lan can forward into |
| **flow offloading** | `flow_offloading=1`, **`flow_offloading_hw=1`**, `flags offload`, PPE `ppe0`/`ppe1` | **hardware** offload — no software packet counter can see established flows |
| **flowtable `counter`** | **present** | the reason conntrack byte accounting works at all (§4) |
| `nf_conntrack_acct` | `1` | conntrack carries `bytes=` per direction |
| conntrack | v1.4.8, `-o id` supported | id-keyed accumulator; package is `conntrack` |
| established timeout | `nf_conntrack_tcp_timeout_established = 7440` (2 h) | a block without a flush is a 2-hour no-op at best |
| established accept | `ct state vmap { established : accept }` sits **above** `jump forward_lan` | why the flush is load-bearing |
| **IPv6** | no default v6 route; LAN has only ULA `fd0c:cfee:9422::/60` | the ISP provides no IPv6, so IPv6 leakage is not a live risk |
| rpcd | 2025.09.01 with `rpcd-mod-file`; `file exec` exposed | the scoped-ACL design is viable |
| uhttpd | 80 + 443, EC cert `/etc/uhttpd.crt`, `ubus_prefix=/ubus`, `redirect_https=1` | `/ubus` is the one working path; the 307 on :80 is this |
| hashing tools | `cryptpw`, `mkpasswd`, `openssl` **all absent**; only `/bin/passwd` | bootstrap defers to `passwd(1)` and stores `$p$kidsout` |
| **`bridge` command** | **not installed** | fdb-based port lookup does not work here; use `ip neigh` / `iwinfo` / per-port counters |
| storage | UBIFS overlay, ~40 MB free | room for packages; flash-wear concern is real but wear-levelled |

### The console

| | MAC | address | reservation |
|---|---|---|---|
| ethernet | `d8:e2:df:92:a9:93` | `192.168.2.172` | `dhcp.@host[1]` |
| wifi | `d8:e2:df:92:a9:90` | `192.168.2.169` | `dhcp.@host[3]` |

Both are statically reserved. The console also holds several link-local and ULA
IPv6 addresses, which are flushed but not counted (§4). It does **not** answer
ICMP, so `ping` is not a presence test — `check` uses the neighbour table.

---

## 11. Why it looks like this

Condensed history. Kept because each item explains a decision that looks odd
without it, and because several were expensive to find.

### From the v1 review (code-level)

v1 was reviewed against the upstream contract before it ever ran. The findings
that still shape the design:

| finding | consequence |
|---|---|
| **block/allow polarity was inverted** — `enabled=0` was treated as "blocked" | the driver now prints *meanings*, never raw uci values, and a regression test asserts rule state rather than return values |
| **`unknown` was unreachable** — every failure was swallowed and reported as `down` | a router outage was indistinguishable from "nobody is playing". Now `unknown` is a real, tested path |
| **`getState.sh` took 61 s against a 10 s timeout** — the per-request timeout multiplied across 3 attempts × 2 paths | dedicated state budget, memoised login failure, one endpoint path, no fallback chain |
| **flow counting reported Instant-On standby as in-use** around the clock | the metric became byte *rate* (§4) — this was the right call, and calibration later confirmed it |
| **IPv4-literal rules did not cover IPv6** | the rule became MAC-keyed |
| **`uci commit` + reload every 60 s** would wear the flash | `_set_enabled` reads before writing and does nothing when already correct |
| a rule named `_input_out` was actually a forward rule, and a strict subset of another | deleted; `install` migrates away obsolete `kidsout_xbox*` sections |
| a source-port regex **never matched**, and the metric worked by two bugs cancelling | the conntrack-parsing metric was retired entirely; the current metric is stated in `test_state.py`'s docstring before being asserted |

### From the hardware review (what measurement overturned)

| finding | consequence |
|---|---|
| **hardware offload blinds any forward-hook counter** | the entire accounting mechanism was replaced with conntrack (§4). This was the largest single change, and no offline test could have found it |
| **the console has two MACs** and was on the one `device.json` did not know | dual-interface support throughout; the block would otherwise have failed open whenever the cable was out |
| **a conntrack sum is non-monotonic** — measured at −510 KB/min mid-game | the router-side accumulator (§4) |
| **the flush was a silent no-op** (`conntrack` was not installed) and `block` returned 0 anyway | `block` now exits non-zero on flush failure; `selftest` refuses to pass without `conntrack` |
| the offline suite passed 119 assertions against the dead design | §9's boundary note |

### Rejected alternatives, and why

| option | why not |
|---|---|
| `netdev ingress` counters at priority −300 | correct under *software* offload only; blind to the PPE. Also binds to device names, so it breaks silently when the console roams — the exact failure mode being fixed |
| disable `flow_offloading_hw` | would work, and is a reasonable fallback, but costs routing performance to solve a problem conntrack already solves for free |
| exempt the console from offload | not implementable on fw4: the only supported injection point, `chain-prepend`, emits *after* `flow add @ft` |
| `iwinfo assoclist` per-station bytes | works, and is used as an independent cross-check (§5) — but it is wifi-only, so it breaks the moment the console is plugged in |
| switch-port MIB counters | offload-proof and free, but ethernet-only for the same reason |
| `file exec` on `nft`/`conntrack` instead of a helper | root-equivalent in practice (§6) |

### Things measurement corrected about the router itself

1. **`uci apply` after `uci commit` is redundant** — it returns ubus status 5
   (`NO_DATA`) because commit already flushed the change set and fired the reload
   event itself. Treating that as an error made `install` exit 1 despite having
   worked. The call is kept but tolerant, because other rpcd builds do stage
   changes and a missed reload is a silent enforcement failure.
2. **The fw4 reload is asynchronous.** The rule appears in the live ruleset a
   moment *after* `block.sh` returns. Checking immediately reports it missing and
   is wrong — this produced a false alarm during verification.
3. **A block takes tens of seconds to bite** (§3).
4. **The package is `conntrack`, not `conntrack-tools`.** The bootstrap had the
   project name and would have failed silently, leaving the flush inoperative.
5. **`ssh -t … 'sh -s' < script` cannot prompt for a password** — ssh will not
   allocate a pty when stdin is a redirect (§7).

---

## 12. Troubleshooting

**Start with `xbox.log`.** Upstream discards stderr, so that file is the only
diagnostic channel. It is size-capped and rotated in place.

| symptom | likely cause | check |
|---|---|---|
| `getState` always `unknown` | no credential, wrong password, or router unreachable | `xbox.log` names the failure class; then `./xbox.py selftest` |
| `getState` always `down` while the console is clearly in use | accounting registered on the wrong address, or the console moved interface | `./xbox.py status` — compare "console" line against the live address; re-run `discover --write` |
| `getState` always `up` with nobody playing | threshold too low, or a background download | `./xbox.py counters`; re-calibrate (§5) |
| block "succeeds" but the game continues | flush failed → `block` should now exit non-zero; or you checked the ruleset too early (async reload) | `./xbox.py selftest` for `conntrack`; re-check `nft list chain inet fw4 forward_lan` after a few seconds |
| block does nothing at all | the rule may not name the MAC the console is currently using | `nft list chain inet fw4 forward_lan` — both MACs must appear in `ether saddr { … }` |
| console stuck offline | kidsout missed the unblock edge | `../unblock.sh` — safe any time, safe to repeat |
| `install` exits 1 with `uci.apply` NO_DATA | old build; the apply is redundant here | update; the driver now tolerates it |
| `selftest` fails "enforcement preconditions" | `conntrack` missing, or the flowtable lost its `counter` flag | `opkg install conntrack`; check `./xbox.py status` facts line |
| certificate errors after a router upgrade | the router regenerated its cert | `./xbox.py pin` |

Useful one-liners on the router:

```
nft list chain inet fw4 forward_lan          # is the REJECT in force, with both MACs?
conntrack -C                                 # total conntrack entries
grep -cF 192.168.2.169 /proc/net/nf_conntrack   # the console's flows
/usr/libexec/kidsout-xbox info                # the facts the design depends on
```

---

## 13. Open questions

**Q1 — What happens on an OpenWrt upgrade?**
The design depends on fw4 emitting `counter` on its flowtable and on
`mtk_eth_soc` reporting PPE MIB back into conntrack. Both are current upstream
behaviour; neither is a stable API. The helper's `info` verb reports
`flowtable_counter` and `flowtable_hw` for exactly this reason, and `selftest`
fails if the counter flag disappears — so a regression surfaces as a failed check
rather than a silent permanent `down`. Re-run `selftest` after any firmware
upgrade.

**Q2 — Should reconciliation be pushed upstream?**
`unblock.sh` is edge-triggered and never retried, so the router holds enforcement
state kidsout does not know about. Every kidsout device driver has this gap, not
just this one. Proposing that upstream call `unblock.sh` on every non-blocked
tick — drivers being required to make it idempotent, as this one is — would fix
it for all of them. The 15-minute `audit:` line makes divergence visible;
nothing repairs it.

**Q3 — Ethernet calibration.**
All calibration figures are from WiFi. The console's wired behaviour is probably
similar but unmeasured. Re-run §5 the next time it is cabled.

**Q4 — Does the DNS exemption matter?**
While blocked the console keeps resolving names (§3). It gains no connectivity,
but it does mean the console shows "connected to network, no internet" rather
than a clean "disconnected". If the clearer error is worth it, add a second rule
with `src` and no `dest` to block input DNS too.

**Q5 — Multiple children / multiple consoles.**
Everything here assumes one console. A second would need its own device
directory, its own rpcd ACL entry or a shared one, and its own accumulator state
file — the helper currently keeps exactly one.

**Q6 — Telling a download from a stream.**
By byte rate alone they are the same thing (§5), which is why a download now
reads `up`. They differ per flow: Xbox content downloads historically go to
Microsoft CDNs, often over plain TCP/80, while video is TCP/443 or QUIC (UDP/443)
to Google/Netflix/etc. The `counters` helper already walks every conntrack
tuple, so bucketing inbound bytes by destination port or protocol is a small
change router-side — but it needs a measurement session with a real game update
running before any threshold could be written, and it is only worth doing if the
Energy-saving power mode stops being enough.

**Q7 — Debounce.**
The verdict has no hysteresis: one 60 s window decides. The 2026-09-21 log shows
isolated `up` ticks from ACK bursts (`out=406.6` in the middle of an otherwise
sub-threshold stream) and a near miss at `out=191.1`. Requiring two consecutive
ticks would trade one minute of latency for a steadier reading. Not done — a
one-tick flap costs one minute of allowance either way.

---

## Appendix — finding-ID index

Source comments cite short finding IDs from the two reviews that produced this
design. Those review files have been folded into this document; the IDs are kept
because they are compact and appear throughout the code. This is the key.

### v1 code review (`F-` / `S-`)

| ID | Finding | Where it lives now |
|---|---|---|
| F-01 | block/allow polarity inverted | §3, §11 |
| F-02 | `unknown` unreachable; all errors became `down` | §4, §11 |
| F-03 | `getState` took 61 s against a 10 s timeout | §4 (timeout budget) |
| F-04 | IPv6 not covered by IPv4-literal rules | §3 (MAC-keyed), §10 (moot here) |
| F-05 | blocking did not cut a session already in progress | §3 (the flush) |
| F-06 | Instant-On standby read as in-use forever | §4, §5 |
| F-07 | source-port regex never matched; metric worked by accident | §11 (metric retired) |
| F-08 | a rule named `_input_out` was a forward rule | §3 (DNS note), §11 |
| F-09 | `uci commit` + reload every 60 s | §3 (read-before-write), §11 (apply is redundant) |
| F-10 | no reconciliation of router state with kidsout | §2 (escape hatch, `audit:`), §13 Q2 |
| F-11 | `alt_hosts` never used | §6 (transport) |
| F-12 | no HTTP fallback | §6 (transport) |
| F-13 | `/cgi-bin/luci/rpc/ubus` is not a ubus endpoint | §10 (`/ubus` is the one path) |
| F-14 | `check` probed ports the console never listens on | §2, §10 (neighbour table) |
| F-15 | one rpcd session leaked per run | `session_timeout` 30 s |
| S-01 | private key in the working tree | §9 (mock cert in a temp dir) |
| S-02 | router root password in `config.json` | §6 (scoped user + helper) |
| S-03 | `verify_tls: false` permitted a LAN MITM | §6 (TLS pin) |

### Hardware review (`G-` / `S2-`)

| ID | Finding | Where it lives now |
|---|---|---|
| G-01 | hardware offload blinds forward-hook counters | §4 (conntrack accounting) |
| G-02 | `device.json` knew only one of the console's two MACs | §3, §4, §7 |
| G-03 | `block.sh` exited 0 when the conntrack flush failed | §3 |
| G-04 | a conntrack byte sum is non-monotonic | §4 (the accumulator) |
| G-05 | driver not discoverable outside `devices/<name>/` | §7 step 6 |
| G-06 | the WiFi interface had no static reservation | §10 (both reserved) |
| G-07 | timeout defaults disagreed across four files | §4 (one source of truth) |
| G-08 | the mock cannot model the offload path | §9 (boundary note) |
| G-09 | `bridge` is not installed on this router | §10 |
| G-10 | package lists were empty | §7 step 1 |
| G-11 | `conntrack-dump` was dead code that widened the ACL | verb removed (§6) |
| G-12 | `discover --write` destroyed `device.json`'s comment | §7 step 4 |
| G-13 | `valid_addr` rejected a `/prefix` its comment allowed | §6 (comment corrected) |
| S2-01 | `--uninstall` dropped `/etc/shadow` to the umask default | §6 |
| S2-02 | webOS client keys committed to a repo with a public remote | **not this driver** — `devices/tv/`; rotate that key |
