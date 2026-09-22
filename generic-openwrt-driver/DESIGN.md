# generic-openwrt-driver — design and operation

A kidsout device driver that controls any LAN device through an OpenWrt
router's ubus-over-HTTPS API. **Block = internet off, allow = internet on.**
One shared implementation; each device brings its own `device.json` and
credential, and every router-side artefact is namespaced by the device id.

The design was built and verified end to end on one device, an Xbox, against a
real router (first live block of an online game on 2026-09-15; full contract
test session on 2026-09-22). Every mechanism here was forced by a measurement
made then. The Xbox's numbers and that router's facts live in
[`devices/xbox/README.md`](../devices/xbox/README.md); this file is about the
mechanism, which is the same for every device.

For the step-by-step of adding a device, see [README.md](README.md).

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
| 7 | [Several devices on one router](#7-several-devices-on-one-router) |
| 8 | [The upstream kidsout contract](#8-the-upstream-kidsout-contract) |
| 9 | [Testing](#9-testing) |
| 10 | [What the router must provide](#10-what-the-router-must-provide) |
| 11 | [Why it looks like this](#11-why-it-looks-like-this) |
| 12 | [Troubleshooting](#12-troubleshooting) |
| 13 | [Open questions](#13-open-questions) |

---

## 1. What this is

kidsout scans `devices/<name>/` for three executable scripts and runs them on a
one-minute tick. This driver implements them for any device the router can see.

```
generic-openwrt-driver/                    shared code
├── driver.py                              the driver (stdlib only, no pip deps)
├── router_bootstrap.sh                    per-device, ONE-TIME, runs ON THE ROUTER
├── new_device.sh  contract/               scaffolding for a new device
├── device.example.json config.example.json
└── mock_router.py test_*.py run_tests.sh  the offline suite

devices/<name>/                            one per device
├── getState.sh block.sh unblock.sh        the upstream contract — copies of contract/
└── generic-openwrt-driver_files/
    ├── driver.sh                          operator entry point
    ├── device.json                        device facts: id, interfaces, thresholds, router
    ├── config.json                        the rpcd credential (0600, gitignored)
    └── .state.json driver.log             driver-owned sample state and log (gitignored)
```

The wrappers are three-line scripts that set `KIDSOUT_DEVICE_DIR` to the
device's files directory and exec the shared `driver.py`. The driver anchors
every path it owns — `device.json`, `config.json`, `.state.json`, `driver.log`
— on that directory, so nothing depends on the working directory kidsout
happens to use, and nothing in the shared directory is ever written to.

Two facts shape everything else:

- **A device may have several network interfaces**, wired and wireless, each
  with its own MAC, and switch between them whenever a cable is plugged or
  pulled.
- **Many routers offload flows in hardware.** A packet engine forwards
  established connections in silicon, where they are invisible to the firewall
  and to any software packet counter.

Miss either one and the driver fails *silently*: it reports success while the
device keeps going, or reports `down` all afternoon while it is in use.

---

## 2. Daily use

From `devices/<name>/generic-openwrt-driver_files/`:

```
./driver.sh status      # endpoint, router facts, rule state, current byte rate
./driver.sh state       # the one word kidsout sees: up | down | unknown
./driver.sh block       # internet off  (enable REJECT + flush live flows)
./driver.sh allow       # internet on
./driver.sh check       # is the device present on the LAN right now
./driver.sh counters    # byte totals and the rate since the last sample
./driver.sh selftest    # after any router change
./driver.sh discover    # find the device's interfaces (--write to record them)
./driver.sh calibrate --minutes 30 --label idle
./driver.sh pin         # re-record the router's TLS fingerprint
./driver.sh uninstall   # remove the rule and accounting state (destructive)
```

**If the device is ever stuck offline** — kidsout crashed or was upgraded while
it was blocked, `runtimestore.yaml` was reset, someone ran `block` by hand —
run `devices/<name>/unblock.sh`. It is safe at any time and safe to repeat.

Nothing else reconciles the router's state with kidsout's: `unblock.sh` is
edge-triggered, so the router holds enforcement state that lives outside
kidsout's knowledge. The driver mitigates this by logging what the router is
actually enforcing every 15 minutes (`audit:` lines in `driver.log`), which
makes a stuck block visible, but does not fix it.

---

## 3. How enforcement works

One firewall rule per device, `kidsout_<id>_out`:

```
config rule 'kidsout_<id>_out'
        option name    'kidsout <id>: block internet'
        option src     'lan'
        option dest    'wan'
        list   src_mac '02:00:00:00:00:01'      # ethernet
        list   src_mac '02:00:00:00:00:02'      # wifi
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

fw4 parses `src_mac` as a list (`fw4.uc`, `PARSE_LIST`), so one rule covers
every interface; in the live ruleset it becomes
`ether saddr { mac1, mac2 } counter jump reject_to_wan`. A rule naming only one
MAC matches nothing while the device is on the other — the block fails open and
still reports success. An IP-keyed rule would also miss IPv6 entirely.

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
progress.** `block` therefore enables the rule *and* flushes the device's flows,
across every address the router knows for its MACs. If the flush fails, `block`
**exits non-zero** rather than reporting a success it did not achieve.

Once the flush lands the device cannot re-offload while blocked: `flow add @ft`
only acts on established, bidirectionally-seen flows, and new SYNs are REJECTed
before they get there. One successful flush is enough.

**A block is not instantaneous.** New connections are refused at once, but an
application retries for tens of seconds before giving up. "Block" means *within
about a minute*, not *immediately*.

**DNS keeps working while blocked**, deliberately. The device can still reach
the router's own resolver because that is *input* traffic to the router, while
this rule governs *forwarded* traffic. It grants no internet access, so it is
left alone rather than adding a second rule whose only effect is a different
error message on the device.

---

## 4. How "is it in use?" works

`getState.sh` prints one word: `up`, `down` or `unknown`.

The signal is the device's byte rate in **either direction**, each compared
against its own threshold in `device.json` → `state`:

```
rate_out = (out(now) - out(prev)) / (now - prev) * 60
rate_in  = (in(now)  - in(prev))  / (now - prev) * 60
up      rate_out > threshold_out_bytes_per_min      (interactive use uploads)
     or rate_in  > threshold_in_bytes_per_min       (streaming video)
down    neither
unknown the rate is not computable — see below
```

Setting `threshold_in_bytes_per_min` to `null` gives an outbound-only rule.

### The bytes come from conntrack, not from an nft counter

This is the single most important design decision, and it was forced by
measurement.

An nft counter in the forward hook — the obvious implementation, and what an
earlier version shipped — **cannot see a hardware-offloaded flow**. Packets are
forwarded by the packet engine in silicon and never reach any netfilter hook, so
the counter sees only each flow's first few packets and under-reports by
roughly 98 %. A `netdev ingress` chain fails for the same reason.

What rescues it: fw4 declares its flowtable with `counter`, and Linux 6.x feeds
the hardware's per-flow MIB back into conntrack via `nf_ct_acct_add()` on a ~1 Hz
poll. Verified empirically on a MediaTek SoC: 9 of 9 offloaded flows gained
bytes over a 20 s window.

So the helper sums `/proc/net/nf_conntrack` byte counters router-side. This needs
no nft table, no chain, no priority, no device names, nothing to survive an fw4
reload, and no extra package.

### The accumulator, and why a naive sum is wrong

Conntrack counters are **per-flow and vanish when the flow expires**, so a sum
over live flows is not a counter — it is a gauge that jumps up as flows are born
and drops as they die. Measured during real use, a naive sum produced
**−510 KB/min** outbound.

The helper therefore keeps a running accumulator:

```
for each live flow belonging to the device:
    seen before  →  total += max(0, current_bytes - last_seen_bytes)
    new          →  total += current_bytes
    vanished     →  contributes nothing further
```

keyed on the **conntrack id** (`conntrack -L -o id`), so a reused source port is
correctly seen as a new flow rather than as a counter reset. Falls back to
5-tuple keying against `/proc/net/nf_conntrack` where conntrack is unavailable.
State lives in `/tmp/kidsout-<id>.acct` (tmpfs — no flash wear); a reboot clears
it, the total drops, and the driver's decrease-detection yields exactly one
`unknown` tick before re-baselining.

Because the helper guarantees monotonicity, `driver.py`'s `sample_rate()` and
`cmd_state()` need no knowledge of any of this — they just diff two totals. The
helper prints `out <n>` and `in <n>`; a helper installed before the generic
driver printed `<id>_out` / `<id>_in`, which the driver still accepts (with a
log line asking for `--helper-only`).

### Which addresses count

A device typically holds several addresses (one IPv4, link-local and ULA IPv6),
all genuinely its own. Only **routable** ones count toward the rate:
`fe80::/10` and `fd00::/8` traffic never leaves the LAN, so counting it would
inflate the rate with household chatter and invalidate a threshold calibrated on
IPv4. Link-local and ULA addresses are still **flushed**, just not **counted**.

Addresses are resolved at run time from the DHCP lease file and neighbour table,
keyed on the device's MACs, rather than trusting a static IP — a reservation
only takes effect once the device renews.

### When it says `unknown`, it means it

`unknown` is reported when the router cannot be consulted, the login is rejected,
the helper or its state is missing, there is no previous sample yet, the previous
sample is stale (> `max_sample_age_s`, so an hour of bytes is never divided into
one minute), the totals went backwards, or the counter key names changed (a
driver/helper upgrade). Upstream treats `unknown` like `down` for enforcement
but records it as a grey dot in the WeekView, which is how an outage stays
distinguishable from a quiet afternoon.

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

Both thresholds must be **measured, not guessed**, for each device class. The
shipped values (200 KB/min out, 1 MB/min in) are the Xbox's; the table they came
from is in [`devices/xbox/README.md`](../devices/xbox/README.md). Every figure
there was cross-validated against `iwinfo assoclist` (mac80211's own per-station
counters, independent of netfilter and of the offload path): conntrack captured
95 % of inbound bytes and 71–85 % of outbound — the outbound gap is small ACKs
counted as whole 802.11 frames by iwinfo, and what matters is that the two move
together.

### Why there is an inbound threshold, and what it costs

An outbound-only rule separates interactive use from a background download
cleanly: gameplay uploads continuously, a download is almost pure inbound. It
was measured and correct — and it was also the reason **streaming video read
`down`** for a whole evening. Streaming is pure inbound plus TCP
acknowledgements; its outbound rate is *lower* than a download's. So no
outbound threshold can separate the two: a line low enough for video also fires
on every patch and sits next to random ACK bursts, so it would flap constantly.
The metric had to change, not the number.

The inbound rule therefore trades one known false `down` for one known false
`up`: **a large download in progress reads `up`.** Accepted deliberately —

- a false `up` burns allowance and gets noticed and complained about; a false
  `down` is silent, and the allowance never decrements while the video plays;
- on a device that only downloads while switched on (energy-saving power mode
  on consoles), a download can only read `up` while somebody has switched it
  on, which is a fair reading of "in use" for a child's allowance.

If a device downloads in standby (Instant-on modes, phones syncing overnight),
that will burn allowance. Either change the device's power mode, or see §13 Q6.

### Procedure

```
./driver.sh calibrate --minutes 30 --interval 60 --label idle
./driver.sh calibrate --minutes 10 --interval 60 --label active
./driver.sh calibrate --minutes 10 --interval 60 --label streaming
```

Samples append to `calibration.jsonl` in the device directory; each run prints
n / min / median / max in KB/min for **both** directions. Use `--interval 60` —
that is kidsout's tick, and short windows alias badly against bursty ACK
traffic.

- **Outbound** (`threshold_out_bytes_per_min`): comfortably above the idle
  maximum and comfortably below the active minimum — geometric midpoint is a
  reasonable default.
- **Inbound** (`threshold_in_bytes_per_min`): comfortably above the idle
  *inbound* maximum and comfortably below the streaming *inbound* minimum. Err
  low: a missed stream is silent, an over-eager `up` is not.

Write both to `device.json` → `state` **in bytes**. Re-calibrate if the device
moves between WiFi and ethernet.

---

## 6. Security model

### No router root password anywhere

`router_bootstrap.sh <id>` creates a dedicated rpcd login `kidsout-<id>` whose
ACL grants exactly:

| capability | why |
|---|---|
| `ubus session login/access` | to authenticate at all |
| `ubus uci get/set/add/delete/commit/apply/confirm/revert` on **`firewall` only** | to toggle `kidsout_<id>_out` |
| `ubus file exec` on **`/usr/libexec/kidsout-<id>` only** | counters, conntrack flush, neighbour table |
| `ubus system board` | version reporting in `status` |

`config.json` holds that password, never root's, and `driver.py` chmods the file
to `600` on every run if it finds it group- or world-readable.

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

Instead `/usr/libexec/kidsout-<id>` accepts a **closed set of verbs** (`info`,
`counters`, `counters-install`, `counters-remove`, `flush`, `neigh`, `leases`)
and validates its arguments: addresses may contain only hex digits, dots and
colons. rpcd invokes it with an argv array and no shell, so there is no quoting
surface. Verified: `counters-install 'foo;rm -rf /'` is rejected with exit 2.

The helper file is byte-identical for every device: it reads its device id from
its own file name, so refreshing it for one device changes nothing for another.

**Worst case with one `kidsout-<id>` credential:** an attacker can block or
unblock devices whose rules live in the `firewall` package (see §7), read that
device's byte counters, drop conntrack entries for arbitrary addresses, and list
the DHCP leases and neighbour table. Real, but bounded — and the smallest set
that still lets the driver work.

### Transport

ubus JSON-RPC over HTTPS at `https://<router>:443/ubus`, with `alt_hosts` as
genuine fallbacks and HTTP as a last resort. The winning endpoint is cached in
`.state.json` and reused only while it still names a configured host and port.

`verify_tls` stays `false` because routers serve self-signed certificates.
Instead `./driver.sh pin` records that certificate's SHA-256 fingerprint in
`device.json`, and the driver then **refuses to send credentials to any other
certificate** — checked after the TLS handshake and before the request body is
written. That closes the LAN MITM window without needing a real CA. Re-run `pin`
if the router regenerates its certificate.

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
  DoS against one host. Restricting it to the device's own addresses would mean
  hard-coding them into the helper; not worth the loss of flexibility.
- The password sits in a plaintext file readable by the account running kidsout.

---

## 7. Several devices on one router

Everything the driver creates carries the device id, derived from `device.json`
→ `id` in one place (`Names` in `driver.py`) and validated on every run:

| | per device |
|---|---|
| kidsout machine | `devices/<id>/generic-openwrt-driver_files/{device.json, config.json, .state.json, driver.log}` |
| uci firewall | section `kidsout_<id>_out` |
| helper | `/usr/libexec/kidsout-<id>`, state `/tmp/kidsout-<id>.{acct,addrs}` |
| rpcd | ACL `kidsout-<id>`, login `kidsout-<id>`, system user `kidsout-<id>` (uid from 6000 up, first free) |

Two rules keep the boundary honest:

- **The id contains no `_` or `-`** (`^[a-z][a-z0-9]{0,23}$`), so the section
  prefix `kidsout_<id>_` is unambiguous. `find_sections()` matches
  `prefix + "_"`, never a bare `startswith`: with the latter, device `kid`
  would have deleted `kid2`'s rule on `install` and toggled it on every
  `block`.
- **A `device.json` with an explicit `rules.prefix` that disagrees with its id
  is refused**, because a half-edited copy from another device is the likeliest
  operator error and would toggle the other device's rule.

What remains shared is rpcd's granularity: uci ACLs are per **package**, so each
login may write anything in `firewall`. Also, `uci revert firewall` in
`selftest`'s write probe discards *every* staged firewall change in rpcd, not
just ours — the driver never leaves changes staged (each write path commits at
once), and the probe belongs to the manual `selftest` only. kidsout runs every
device's `getState.sh` concurrently each minute; reads are safe, and each
driver's writes are self-contained set+commit sequences.

The offline suite runs two devices, one a prefix of the other, against one mock
and asserts all of this (`test_two_devices.py`).

---

## 8. The upstream kidsout contract

Upstream (`github.com/zipizap/kidsout`) scans `devices/<name>/` for three
executable scripts and runs them with `cmd.Dir` unset, so they self-locate —
here by resolving `$0`, setting `KIDSOUT_DEVICE_DIR`, and exec'ing the shared
driver two directories up (`KIDSOUT_OPENWRT_DRIVER` overrides the path).

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
- **cwd is kidsout's, not the device's.** Hence the self-location.
- **stderr is swallowed.** Upstream uses `cmd.Output()`, which discards stderr on
  the success path, so diagnostics go to `driver.log` (size-capped at 256 KB,
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
  including every other device — which is why §4's timeout budget matters beyond
  one device.

---

## 9. Testing

```
./run_tests.sh
```

| suite | covers |
|---|---|
| `test_block_semantics.py` | polarity, rule shape, every MAC, migration, idempotency, conntrack flush, flush-failure exit code, a sibling device's rule left untouched, hostname hints |
| `test_state.py` | the up/down/unknown metric, stated explicitly then asserted, incl. non-monotonic counters, legacy counter keys, and the download-vs-streaming case |
| `test_failure_modes.py` | nine ways of failing, each → `unknown` inside the deadline; bad and half-edited `device.json` refused |
| `test_two_devices.py` | two device directories against one router: rules, counters, flushes, state, logs and uninstall stay separate |
| `test_contract.py` | the real `.sh` files as subprocesses, from another cwd, in the shipped two-level layout; every `devices/*/` directory in the repo matches the templates |

`mock_router.py` models rpcd including its **failure** modes — refused logins,
ACL denials, 404s, non-JSON responses, helper failures, missing counters, slow
responses — and is multi-tenant like the real router. Its TLS key is generated
into a temp directory and removed on exit.

### What the offline suite cannot tell you

The mock returns counter *values*; it has no flowtable, no hooks, no priorities,
and no notion that a packet might bypass netfilter entirely. It therefore
**cannot** distinguish a counter that sees all traffic from one that sees 2 %.
An earlier design passed 119 assertions against a mechanism the hardware later
disproved.

This is not an argument against the mock — it caught real bugs while being
written, and several tests were mutation-checked to confirm they fail when the
fix is reverted. It is an argument for a boundary: **the offline suite validates
the driver's logic, not the router's behaviour.** Any claim about what the
router does must carry a hardware citation; `selftest` checks the ones the
design depends on, on every router.

---

## 10. What the router must provide

| requirement | checked by | why it matters |
|---|---|---|
| fw4 + nftables | `status` facts line | `src_mac` is a list, so one rule can name every MAC |
| `uhttpd-mod-ubus` on `/ubus`, `rpcd` with `rpcd-mod-file` | `probe`, `selftest` | the whole transport; `/cgi-bin/luci/rpc/ubus` is a different API |
| `conntrack` package (not `conntrack-tools`, which does not exist) | `selftest` "enforcement preconditions" | without it a block cannot cut a session in progress; the established-TCP timeout is typically 2 h |
| `nf_conntrack_acct = 1` | `info` verb | conntrack carries `bytes=` per direction |
| if offloading: flowtable declared with `counter` | `selftest` | the only reason conntrack sees offloaded bytes at all (§4) |
| `passwd(1)` | bootstrap | routers usually lack `cryptpw`/`mkpasswd`/`openssl`; the bootstrap stores `$p$<user>` and defers to `/etc/shadow` |

Measured facts of the router the design was built on (Cudy WR3000E, OpenWrt
24.10.5, MediaTek filogic, hardware offload on) are in
[`devices/xbox/README.md`](../devices/xbox/README.md). Other routers are
expected to differ in detail, which is what `selftest` is for.

---

## 11. Why it looks like this

Condensed history. Kept because each item explains a decision that looks odd
without it, and because several were expensive to find. "The first device" is
the Xbox this was built on.

### From the v1 review (code-level)

| finding | consequence |
|---|---|
| **block/allow polarity was inverted** — `enabled=0` was treated as "blocked" | the driver now prints *meanings*, never raw uci values, and a regression test asserts rule state rather than return values |
| **`unknown` was unreachable** — every failure was swallowed and reported as `down` | a router outage was indistinguishable from "nobody is using it". Now `unknown` is a real, tested path |
| **`getState.sh` took 61 s against a 10 s timeout** — the per-request timeout multiplied across 3 attempts × 2 paths | dedicated state budget, memoised login failure, one endpoint path, no fallback chain |
| **flow counting reported standby as in-use** around the clock | the metric became byte *rate* (§4) — calibration later confirmed it |
| **IPv4-literal rules did not cover IPv6** | the rule became MAC-keyed |
| **`uci commit` + reload every 60 s** would wear the flash | `_set_enabled` reads before writing and does nothing when already correct |
| a rule named `_input_out` was actually a forward rule, and a strict subset of another | deleted; `install` migrates away obsolete `kidsout_<id>_*` sections |
| a source-port regex **never matched**, and the metric worked by two bugs cancelling | the conntrack-parsing metric was retired entirely; the current metric is stated in `test_state.py`'s docstring before being asserted |

### From the hardware review (what measurement overturned)

| finding | consequence |
|---|---|
| **hardware offload blinds any forward-hook counter** | the entire accounting mechanism was replaced with conntrack (§4). The largest single change, and no offline test could have found it |
| **the first device had two MACs** and was on the one `device.json` did not know | multi-interface support throughout; the block would otherwise have failed open whenever the cable was out |
| **a conntrack sum is non-monotonic** — measured at −510 KB/min mid-session | the router-side accumulator (§4) |
| **the flush was a silent no-op** (`conntrack` was not installed) and `block` returned 0 anyway | `block` now exits non-zero on flush failure; `selftest` refuses to pass without `conntrack` |
| the offline suite passed 119 assertions against the dead design | §9's boundary note |

### From generalising to several devices (2026-09-22)

| finding | consequence |
|---|---|
| one helper, one accumulator file, fixed `xbox_out` keys: a second device would have overwritten the first's addresses and both would have read each other's bytes | everything namespaced by id (§7); the helper derives its id from its file name |
| `startswith(prefix)` let one device claim a sibling's sections when one id prefixed the other | `owns_section()` with a `_` boundary and an id charset that excludes `_` |
| a copied `device.json` with a stale `rules.prefix` would have toggled the other device's rule | prefix is derived from the id; an explicit disagreeing value is refused at load |
| the driver's old `.state.json` keys diffed against new ones would have produced one giant false `up` | a key-set change is a re-baseline (`unknown`), like a counter reset |
| a shared uid 6000 for every device's system user | first free uid from 6000 |

### Rejected alternatives, and why

| option | why not |
|---|---|
| `netdev ingress` counters at priority −300 | correct under *software* offload only; blind to the PPE. Also binds to device names, so it breaks silently when the device roams — the exact failure mode being fixed |
| disable `flow_offloading_hw` | would work, and is a reasonable fallback, but costs routing performance to solve a problem conntrack already solves for free |
| exempt the device from offload | not implementable on fw4: the only supported injection point, `chain-prepend`, emits *after* `flow add @ft` |
| `iwinfo assoclist` per-station bytes | works, and is used as an independent cross-check (§5) — but it is wifi-only, so it breaks the moment the device is plugged in |
| switch-port MIB counters | offload-proof and free, but ethernet-only for the same reason |
| `file exec` on `nft`/`conntrack` instead of a helper | root-equivalent in practice (§6) |
| one shared helper taking a device-id argument, one shared login | fewer router artefacts, but one device's bootstrap would overwrite another's helper mid-upgrade, and a leaked credential would cover every device. Per-device copies of the helper are byte-identical anyway |
| a copy of the driver code per device directory | N places to apply the next router-behaviour discovery — precisely the kind of finding this project keeps producing |

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
   allocate a pty when stdin is a redirect (README step 3).

---

## 12. Troubleshooting

**Start with `driver.log`** in the device's files directory. Upstream discards
stderr, so that file is the only diagnostic channel. It is size-capped and
rotated in place.

| symptom | likely cause | check |
|---|---|---|
| `getState` always `unknown` | no credential, wrong password, or router unreachable | `driver.log` names the failure class; then `./driver.sh selftest` |
| `getState` always `down` while the device is clearly in use | accounting registered on the wrong address, or the device moved interface, or thresholds too high for this device | `./driver.sh status` — compare the "device" line against the live address; re-run `discover --write`; re-calibrate |
| `getState` always `up` with nobody there | threshold too low, or a background download | `./driver.sh counters`; re-calibrate (§5) |
| block "succeeds" but the session continues | flush failed → `block` should exit non-zero; or you checked the ruleset too early (async reload) | `./driver.sh selftest` for `conntrack`; re-check `nft list chain inet fw4 forward_lan` after a few seconds |
| block does nothing at all | the rule may not name the MAC the device is currently using | `nft list chain inet fw4 forward_lan` — every MAC must appear in `ether saddr { … }` |
| device stuck offline | kidsout missed the unblock edge | `devices/<name>/unblock.sh` — safe any time, safe to repeat |
| `selftest` fails "enforcement preconditions" | `conntrack` missing, or the flowtable lost its `counter` flag | `opkg install conntrack`; check `./driver.sh status` facts line |
| `selftest` fails "device directory matches id" | `id` in `device.json` differs from the `devices/<name>` directory | fix whichever is wrong; kidsout names the device after the directory |
| `rules.prefix is ... but id derives ...` | `device.json` copied from another device | remove `rules.prefix` |
| `warn: ... prints legacy counter keys` | helper from before the generic driver | `sh /tmp/router_bootstrap.sh <id> --helper-only` |
| certificate errors after a router upgrade | the router regenerated its cert | `./driver.sh pin` |

Useful one-liners on the router:

```
nft list chain inet fw4 forward_lan          # is the REJECT in force, with every MAC?
conntrack -C                                 # total conntrack entries
grep -cF <device-ip> /proc/net/nf_conntrack  # the device's flows
/usr/libexec/kidsout-<id> info               # the facts the design depends on
uci show rpcd | grep kidsout                 # which logins exist
ls /usr/libexec/kidsout-* /tmp/kidsout-*     # which devices are bootstrapped
```

---

## 13. Open questions

**Q1 — What happens on an OpenWrt upgrade?**
The design depends on fw4 emitting `counter` on its flowtable and on the SoC
driver reporting offload MIB back into conntrack. Both are current upstream
behaviour; neither is a stable API. The helper's `info` verb reports
`flowtable_counter` and `flowtable_hw` for exactly this reason, and `selftest`
fails if the counter flag disappears — so a regression surfaces as a failed
check rather than a silent permanent `down`. Re-run `selftest` after any
firmware upgrade.

**Q2 — Should reconciliation be pushed upstream?**
`unblock.sh` is edge-triggered and never retried, so the router holds enforcement
state kidsout does not know about. Every kidsout device driver has this gap, not
just this one. Proposing that upstream call `unblock.sh` on every non-blocked
tick — drivers being required to make it idempotent, as this one is — would fix
it for all of them. The 15-minute `audit:` line makes divergence visible;
nothing repairs it.

**Q3 — Does the DNS exemption matter?**
While blocked the device keeps resolving names (§3). It gains no connectivity,
but it does mean the device shows "connected to network, no internet" rather
than a clean "disconnected". If the clearer error is worth it, add a second rule
with `src` and no `dest` to block input DNS too.

**Q4 — Devices whose "in use" is not a byte rate.**
A console essentially only talks when somebody is using it, so traffic ≈ use. A
tablet chatters for push, sync and background refresh regardless, and "in use"
really means "screen on with a person looking at it". No byte-rate metric
recovers that cleanly; the calibration will be coarser, and a false `down` hands
out silent free time. Decide per device whether that is acceptable.

**Q5 — Telling a download from a stream.**
By byte rate alone they are the same thing (§5), which is why a download reads
`up`. They differ per flow: content downloads often go to CDNs over plain
TCP/80, while video is TCP/443 or QUIC (UDP/443). The `counters` helper already
walks every conntrack tuple, so bucketing inbound bytes by destination port or
protocol is a small change router-side — but it needs a measurement session
with a real download running before any threshold could be written.

**Q6 — Debounce.**
The verdict has no hysteresis: one 60 s window decides. Isolated `up` ticks from
ACK bursts have been observed in the middle of otherwise sub-threshold streams.
Requiring two consecutive ticks would trade one minute of latency for a steadier
reading. Not done — a one-tick flap costs one minute of allowance either way.

---

## Appendix — finding-ID index

Source comments cite short finding IDs from the two reviews that produced this
design. The IDs are kept because they are compact and appear throughout the
code. This is the key.

### v1 code review (`F-` / `S-`)

| ID | Finding | Where it lives now |
|---|---|---|
| F-01 | block/allow polarity inverted | §3, §11 |
| F-02 | `unknown` unreachable; all errors became `down` | §4, §11 |
| F-03 | `getState` took 61 s against a 10 s timeout | §4 (timeout budget) |
| F-04 | IPv6 not covered by IPv4-literal rules | §3 (MAC-keyed) |
| F-05 | blocking did not cut a session already in progress | §3 (the flush) |
| F-06 | standby read as in-use forever | §4, §5 |
| F-07 | source-port regex never matched; metric worked by accident | §11 (metric retired) |
| F-08 | a rule named `_input_out` was a forward rule | §3 (DNS note), §11 |
| F-09 | `uci commit` + reload every 60 s | §3 (read-before-write), §11 (apply is redundant) |
| F-10 | no reconciliation of router state with kidsout | §2 (escape hatch, `audit:`), §13 Q2 |
| F-11 | `alt_hosts` never used | §6 (transport) |
| F-12 | no HTTP fallback | §6 (transport) |
| F-13 | `/cgi-bin/luci/rpc/ubus` is not a ubus endpoint | §10 |
| F-14 | `check` probed ports the device never listens on | §2 (neighbour table) |
| F-15 | one rpcd session leaked per run | `session_timeout` 30 s |
| S-01 | private key in the working tree | §9 (mock cert in a temp dir) |
| S-02 | router root password in `config.json` | §6 (scoped user + helper) |
| S-03 | `verify_tls: false` permitted a LAN MITM | §6 (TLS pin) |

### Hardware review (`G-` / `S2-`)

| ID | Finding | Where it lives now |
|---|---|---|
| G-01 | hardware offload blinds forward-hook counters | §4 (conntrack accounting) |
| G-02 | `device.json` knew only one of the device's two MACs | §3, §4 |
| G-03 | `block.sh` exited 0 when the conntrack flush failed | §3 |
| G-04 | a conntrack byte sum is non-monotonic | §4 (the accumulator) |
| G-05 | driver not discoverable outside `devices/<name>/` | §1, §8 |
| G-06 | the WiFi interface had no static reservation | `devices/xbox/README.md` |
| G-07 | timeout defaults disagreed across four files | §4 (one source of truth) |
| G-08 | the mock cannot model the offload path | §9 (boundary note) |
| G-09 | `bridge` is not installed on the reference router | `devices/xbox/README.md` |
| G-10 | package lists were empty | §10 |
| G-11 | `conntrack-dump` was dead code that widened the ACL | verb removed (§6) |
| G-12 | `discover --write` destroyed `device.json`'s comment | `cmd_discover` preserves it |
| G-13 | `valid_addr` rejected a `/prefix` its comment allowed | helper comment corrected |
| S2-01 | `--uninstall` dropped `/etc/shadow` to the umask default | §6 |
| S2-02 | webOS client keys committed to a repo with a public remote | **not this driver** — `devices/tv/`; rotate that key |
