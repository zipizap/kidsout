# REVIEW.1 remediation — progress

Tracking file for the v2 rewrite driven by [REVIEW.1.md](REVIEW.1.md).

> **Status as of 2026-09-15: v2 is code-complete and hardware-verified enough to
> know it is not deployable yet.** The offline suite is green and the transport
> works against the real router, but hardware verification found four blockers —
> three of which fail silently. They are specified in **[REVIEW.2.md](REVIEW.2.md)**,
> which is the input spec for v3. This file records how we got there.

Decisions taken with Paulo before implementation:

| Question | Decision |
|---|---|
| Auth | Scoped rpcd user `kidsout`, created by `router_bootstrap.sh`. No root credential in `config.json`. |
| State metric | ~~nft byte counters (F-06), thresholded on the outbound MAC-matched counter.~~ **Superseded 2026-09-15** — hardware offload bypasses the forward hook. Now conntrack byte accounting, accumulated router-side. See REVIEW.2 G-01/G-04. |
| Rule shape | One MAC-based outbound REJECT; `_fwd_in` and `_input_out` deleted (F-04, F-08). **Must name both console MACs** — see REVIEW.2 G-02. |
| Scope | Everything: §8.1 + §8.2 + §8.3 + §8.4 + the §8.5 test suite. |
| Bootstrap | Script run over SSH by Paulo; password never seen by the assistant. |
| Calibration | **Done 2026-09-15**: idle 20.8 / downloading 93.7 / gaming 289.5 KB/min outbound. 200 KB/min threshold confirmed — see [F-06.md](F-06.md). |
| Legacy core | `old/` deleted (review Q4 answered: retired). |
| Delivery | `xbox.py` rewritten in place, no backup. |

Paulo also confirmed: **these scripts were never run against the real router**, so
there is no pre-existing `kidsout_xbox*` state on it to clean up or migrate. The
install-time migration still exists, because re-running `install` after a rule-set
change is a normal operation.

## Deviation from the review, and why

REVIEW.1 §7 S-02 proposes an rpcd ACL granting `file exec` on "the specific
conntrack/nft commands". rpcd authorises a command *path* and not its arguments,
and runs it as root — so `file exec` on `/usr/sbin/nft` is complete control of the
firewall, i.e. the credential stays root-equivalent and S-02 is not actually fixed.
v2 installs `/usr/libexec/kidsout-xbox`, a closed-verb helper with validated
arguments, and scopes the ACL to that one path instead. Same capability for the
driver, far smaller blast radius.

## Status

Legend: ☐ not started · ◐ code written, unverified on hardware · ☑ verified ·
⚠ verified-broken, needs rework (see [REVIEW.2.md](REVIEW.2.md))

| ID | Finding | Status | Notes |
|---|---|---|---|
| F-01 | block/allow polarity inverted | ◐ | one MAC rule; `enabled=1` blocks; install lands ALLOWED; prints meanings, not uci values |
| F-02 | `unknown` unreachable; all errors → `down` | ◐ | no bare `except: continue`; nine failure paths asserted in test_failure_modes.py |
| F-03 | getState 61 s vs 10 s budget | ◐ | state_timeout 1.5 s, hard deadline 4 s, memoised login failure, one path, no fallback chain |
| F-04 | IPv6 not blocked | ⚠ | rule keyed on `src_mac`, but device.json names only the **ethernet** MAC while the console is on **WiFi** — the block would not match. Needs both MACs. No IPv6 on this ISP, so the original concern is moot |
| F-05 | block does not cut live sessions | ⚠ | **more severe than reviewed**: with hardware offload an in-progress session bypasses the REJECT entirely, so the flush is the whole mechanism. conntrack-tools still absent → flush is a silent no-op and `cmd_block` still returns success |
| F-06 | Instant-On standby reads `up` forever | ◐ | **nft counters abandoned** (hardware offload bypasses the forward hook); metric moves to conntrack byte accounting. Threshold 200 KB/min now **calibrated against the real console** — see F-06.md |
| F-07 | source-port regex never matches | ☑ | dissolved: conntrack parsing retired entirely; metric now stated in test_state.py's docstring |
| F-08 | third rule was a forward rule | ◐ | deleted, along with `_fwd_in`; install migrates the old sections away |
| F-09 | commit + reload every 60 s | ☑ | `_set_enabled` reads first and writes only when stale; asserted offline |
| F-10 | no reconciliation | ◐ | escape hatch documented; 15-min `audit:` log line; upstream change proposed in the README |
| F-11 | `alt_hosts` unused | ◐ | hosts iterated in `candidate_urls`, bounded by the state deadline |
| F-12 | no HTTP fallback | ☑ | https then http, winner cached; cache honoured only if it still matches a configured host |
| F-13 | luci rpc path is not ubus | ☑ | removed; confirmed 404 on hardware |
| F-14 | `check` probed dead ports | ◐ | rewritten around the router's neighbour table |
| F-15 | rpcd session leaked per run | ☑ | session_timeout 30 s instead of 900 s |
| S-01 | private key in the working tree | ☑ | mock_cert.pem / mock_key.pem deleted; generated into a temp dir at startup |
| S-02 | root password in config.json | ◐ | scoped rpcd user + fixed-verb helper; config.json chmod 600 — see S-02.md |
| S-03 | `verify_tls: false` | ◐ | `./xbox.py pin` records the fingerprint; checked after the handshake, before credentials are sent |
| §8.4 | README corrections | ☑ | README rewritten; every v1 claim in REVIEW.1 §8.4 re-derived from v2's behaviour |
| §8.5 | offline test suite | ☑ | 4 suites, 46 cases, 119 assertions, all passing |

### New blockers found by hardware verification (REVIEW.2)

These are not REVIEW.1 findings; they are defects in v2 itself, each found by
measurement rather than by reading code. All four fail open or invisible.

| ID | Finding | Why it matters |
|---|---|---|
| G-01 | Hardware offload blinds the nft forward-hook counters | The state metric cannot work as designed. `getState.sh` would report `down` throughout real gameplay |
| G-02 | `device.json` names only the ethernet MAC; the console is on WiFi | The block matches nothing and the metric reads zero — both silently |
| G-03 | `block.sh` exits 0 when the conntrack flush fails | Reports success to kidsout while the console keeps playing |
| G-04 | A conntrack byte sum is non-monotonic; `sample_rate` assumes monotonic | Measured **−510 KB/min during active gameplay**; would sit in permanent `unknown` |

## Offline suite

```
block semantics          :  9/9 cases   (20 assertions)
state logic              : 13/13 cases  (30 assertions)
failure modes            : 13/13 cases  (37 assertions)
upstream script contract : 11/11 cases  (32 assertions)
```

Two real defects in v2 were found by writing these tests, not by review:

1. A cached endpoint in `.state.json` overrode an explicit `host` in
   `config.json`, so a renumbered router would have been silently ignored. The
   cache is now honoured only while it still names a configured host and port.
2. `save_state` overwrote rather than merged, so the endpoint cache written
   during `probe()` was discarded by the counter sample written at the end of
   `cmd_state`. It now merges.

## Remaining

### Settled by the 2026-09-15 session

- [x] Flow-offload question — **hardware offload confirmed**; nft forward-hook
      counters cannot work; conntrack accounting verified as the replacement.
- [x] IPv6 — no delegation on this ISP (check A).
- [x] Calibration — threshold 200 KB/min confirmed against the live console.
- [x] Transport, endpoint discovery, `unknown` path and the driver log — all
      proven against the real router.

### Blocked on v3 code changes (REVIEW.2 §8.1)

Do these before touching the router again — four of them are the blockers, and
running `install`/`block` before they are fixed would only prove they are broken.

- [ ] G-01 + G-04 — helper's `counters` verb reads conntrack and returns a
      **monotonic accumulator**; drop the nft table entirely.
- [ ] G-02 — `device.json` carries both interfaces; one rule names both MACs;
      addresses resolved at run time from lease + neighbour tables.
- [ ] G-03 — `cmd_block` returns non-zero when the flush fails; `selftest` fails
      when offload is on and conntrack-tools is absent.
- [ ] G-07 — one source of truth for `state_timeout` / `state_deadline`.
- [ ] G-08 — mock emits the new counters shape; add a non-monotonic fixture and a
      `cmd_block`-flush-failure regression test.

### Then, on hardware, in this order (REVIEW.2 §8.5)

- [ ] `opkg update && opkg install conntrack-tools` (package lists are empty).
- [ ] Run `router_bootstrap.sh`; record the printed facts here.
- [ ] `./xbox.py selftest` green — the first real test of the ACL scope.
- [ ] `./xbox.py pin` (fingerprint already known: `65:F6:D0:…:94:7F`).
- [ ] `./xbox.py discover --write` on **each** interface; confirm both recorded.
- [ ] `./xbox.py install` → **console still online** (proves the F-01 fix).
- [ ] `nft list ruleset | grep -A3 kidsout_xbox` — confirm the rule is *in force*,
      not merely committed; settles whether the explicit `uci apply` is redundant.
- [ ] `block` **while a game is in progress** → the session dies within seconds.
      This is the test G-03 and F-05 both hinge on, and it has never been run.
- [ ] `block` while the console is on the *other* interface → still blocks (G-02).
- [ ] `unblock` → recovery.
- [ ] Pull the router's power mid-tick → `unknown` inside the deadline.
- [ ] Re-calibrate on ethernet; confirm the threshold still separates.

### Deployment

- [ ] Move to `devices/xbox/` — `DiscoverDevices` scans only immediate children of
      `devicesDir`, so the driver is invisible where it currently lives (G-05).
- [ ] Restart kidsout; discovery runs once, at startup.

## Hardware facts — read-only check A, 2026-09-11

Run from an SSH root session on the router (`router_checks_A.sh`), before any
change. Full output kept in `router_checks_A.sh.printout`.

| fact | value | consequence |
|---|---|---|
| OpenWrt | 24.10.5 r29087, mediatek/filogic, Linux 6.6.119 aarch64 | opkg, fw4 |
| firewall | fw4 + nftables 1.1.1, `nftables-json` installed | `nft -j` works, text fallback unused |
| zones | `lan`, `wan`, **`WgZone`** | a third zone lan can forward into — block scope widened to all forwarding |
| `src_mac` | `fw4.uc:2314` `src_mac: [ "mac", null, PARSE_LIST ]`, mapped to `smacs_pos` | **F-04's MAC-keyed rule is supported** |
| nft dry run | named counters + `ether saddr` + `priority -150` all parse | the syntax was valid — but check B showed the *hook* is bypassed by hardware offload, so this told us nothing useful (REVIEW.2 G-01) |
| priority clash | a chain already sits at `forward priority mangle` (-150) | ours moved to -160 — moot: the chain is not used at all in the v3 design |
| rpcd | 2025.09.01 with `rpcd-mod-file`; only one login (`root`, `*`/`*`) | ACL schema confirmed against `luci-app-firewall.json` |
| hashing tools | `cryptpw`, `mkpasswd`, `openssl` **all absent**; only `/bin/passwd` | bootstrap creates a system user and defers to `passwd`, storing `$p$kidsout` |
| conntrack-tools | **not installed** | F-05's flush was a no-op; bootstrap now installs it |
| established timeout | `nf_conntrack_tcp_timeout_established = 7440` (2 h, not 5 days) | a block without a flush is a 2-hour no-op, not a 5-day one |
| established accept | `ct state vmap { established : accept }` sits **above** `jump forward_lan` | **F-05 confirmed on hardware**, not merely inferred |
| IPv6 | no default v6 route; LAN has only ULA `fd0c:cfee:9422::1/60` | **REVIEW.1 Q1 answered: the ISP does not provide IPv6.** F-04 was a latent trap, not a live bug |
| console | MAC `d8:e2:df:92:a9:93`, hostname `XBOX`, static reservation at `dhcp.@host[1]` | `device.json.mac` filled without needing `discover` — **but this is only the ETHERNET interface; see check B** |
| uhttpd | 80 + 443, EC cert `/etc/uhttpd.crt`, `ubus_prefix=/ubus` | transport assumptions confirmed |
| storage | UBIFS overlay, 41.5 MB free of 44.7 MB | room for conntrack-tools; F-09's flash-wear concern is real but wear-levelled |

### Open problem found by the check: flow offloading

```
chain forward {
        meta l4proto { tcp, udp } flow add @ft      <-- offload
        ct state vmap { established : accept, ... }
```

Offloaded flows are forwarded in the flowtable fast path and **skip the forward
hook entirely**, so an accounting chain at `hook forward priority -160` would see
only each flow's first packets. That would under-report gameplay badly enough to
report `down` while the console is in use — i.e. it breaks F-06 as designed. If
*hardware* offload is active on this filogic SoC, packets never reach the CPU at
all and no software counter can see them.

Check B (`router_checks_B1.sh`, `router_checks_B2.sh`) is designed to settle:
whether offload is software or hardware, how many live flows carry `[OFFLOAD]`,
whether `nf_conntrack_acct` offers an alternative byte source, and whether a
`netdev ingress` chain — which runs before the flowtable fast path — is accepted.

## Check B — 2026-09-15: the offload question is SETTLED

Full output in `router_checks_B1.sh.printout`. The console was off the network
during the B1 run, but the design question was answerable without it. The
console-specific measurements were taken afterwards, once the console was woken —
and finding it is what uncovered the two-MAC defect below.

| fact | value | consequence |
|---|---|---|
| `flow_offloading` | `1` | offload is on |
| `flow_offloading_hw` | **`1`** | **hardware** offload, via the MediaTek PPE (`ppe0`, `ppe1` in debugfs) |
| flowtable | `devices = { lan1..lan4, wan }`, `flags offload`, **`counter`** | the `counter` flag is the one that saves us — see below |
| `nf_conntrack_acct` | `1` | conntrack carries `bytes=` per direction |
| offloaded flows | 12–16 of ~143 carry `[HW_OFFLOAD]` | offload is not theoretical, it is carrying real traffic |
| **per-flow byte test** | **9 of 9 HW-offloaded flows gained bytes over 20 s, 0 static** | **conntrack accounting is fed by the PPE hardware path** |
| netdev ingress/egress | multi-device dry runs both OK on `lan1..lan4, phy0-ap0, phy1-ap0` | available, but pointless under HW offload (packets never reach the CPU) |
| forward chain @ -160 | dry run OK; `mangle_forward` occupies -150 | unchanged from check A |
| bootstrap prereqs | uid/gid 6000 free, `kidsout` absent, `/etc/shadow` ok, `file exec` exposed | `router_bootstrap.sh` can run as written |
| `/var/opkg-lists/` | **empty** | `opkg update` is required before conntrack-tools will install |
| `bridge` command | **not installed** | fdb-based port lookup does not work here; use `ip neigh` / per-port counters |
| uhttpd | `redirect_https='1'` | explains the HTTP 307 seen from the kidsout machine |

### The decision: F-06 moves from nft counters to conntrack byte accounting

The shipped design counts bytes with an nft counter at `hook forward priority -160`.
**That design is dead on this router.** Hardware-offloaded packets are forwarded by
the PPE in silicon and never enter the CPU's netfilter forward hook, so the counter
would see only each flow's first packets and report `down` during real gameplay.
No software hook can fix this: `netdev ingress` would also be bypassed.

What rescues it is that fw4 declares its flowtable with `counter`, and in Linux 6.6
`nf_flow_table_offload.c` feeds the hardware's per-flow MIB back into conntrack via
`nf_ct_acct_add()` on a ~1 Hz poll. The per-flow test above confirms this empirically
on *this* SoC, which was the one assumption that could not be taken from source.

So the byte source becomes **conntrack**, summed router-side per direction:

- no nft table, no chain, no priority, no device names, nothing to survive an fw4
  reload, nothing to re-create after a reboot;
- works identically under software offload, hardware offload, or none;
- nothing breaks when the console moves between ethernet and either wifi band —
  which is the failure mode that would have made a `netdev ingress` design report
  `down` forever, silently;
- needs no package: `/proc/net/nf_conntrack` is always there.

Its one real difficulty is that a sum over live flows is **not monotonic** — entries
vanish when flows expire. B1.11 demonstrated this directly: the `[ASSURED]` aggregate
moved **−1,289,885 bytes** in five seconds purely from flows ageing out. The helper
must therefore keep a per-flow accumulator (`total += max(0, cur - last_seen)`, keyed
on conntrack id) rather than diffing a naive sum.

Trade-off accepted: conntrack is keyed on the console's **IP**, not its MAC, which
gives up F-04's IPv6 coverage. Safe here — check A established the ISP delegates no
IPv6 and the console has a static reservation — but it must be revisited if that
changes. The MAC-keyed *enforcement* rule is unaffected and stays as it is.

### Console measurements, 2026-09-15 (console awake, on WiFi)

Taken after the console was woken. Outbound is the deciding metric; every figure
cross-validated against `iwinfo assoclist`, which is mac80211's own per-station
counter and wholly independent of netfilter and of the offload path.

| state | conntrack out | iwinfo out | inbound | verdict @ 200 KB/min |
|---|---|---|---|---|
| idle / dashboard | 20.8 KB/min | — | 23 KB/min | DOWN ✓ |
| downloading | 93.7 KB/min | 131.0 KB/min | 11.8 MB/min | DOWN ✓ |
| **gaming** (82 UDP flows, dport 22222) | **289.5 KB/min** | 340.5 KB/min | — | **UP** ✓ |

Conntrack captured **95.5 %** of iwinfo's inbound bytes and 71–85 % of outbound; the
outbound gap is 802.11 framing on an ACK-dominated stream, not error. The two move
together, which is what the cross-check was for.

**The 200 KB/min threshold holds**, with 3.1× separation between gaming and
downloading. It was a good guess and is now a measurement.

**And it retires F-06's own stated worry.** That document warned a background download
would read as `up` and burn the allowance. It does not: the outbound ACK stream during
a 12 MB/min download is ~0.8 % of downstream, well under the threshold. The metric
being outbound-only is what saves it.

### Second finding: the console has TWO MACs, and device.json only knows one

The console was measured on WiFi as `d8:e2:df:92:a9:90` → `192.168.2.169`
(dynamic lease, hostname `XBOX`, `phy0-ap0`, −60 dBm). But `device.json` and the
static reservation `dhcp.@host[1]` both describe `d8:e2:df:92:a9:93` →
`192.168.2.172` — the **ethernet** interface, which was unplugged during the
session (`lan1`/`lan2` carrier 0, neighbour `FAILED`, zero conntrack flows).

Consoles have separate MACs for wired and wireless. As shipped, therefore:

- **the block would silently fail open.** `block.sh` enables a `src_mac` REJECT
  rule for `...93`. On WiFi the console is `...90`, which that rule does not match.
  The driver would report success while the console kept playing — the exact
  failure class REVIEW.1 F-04 was written to prevent, reached by another route.
- **the state metric would read nothing.** Accounting keyed on `.172` sees zero
  bytes while the console is on `.169`, so `getState.sh` would report `down`
  forever and no time would ever be accrued.

Fix: `device.json` must carry **both** MACs and both addresses, and the driver must
use whichever is live. fw4 parses `src_mac` as a list (`fw4.uc:2314`, `PARSE_LIST`),
so a single rule can name both. Accounting should resolve the console's current
address(es) from the lease/neighbour table by MAC rather than trusting a static IP.

Note also that `.169` has **no static reservation**, so that address can change.
Either add a reservation for `...90`, or resolve dynamically — preferably both.

### Third finding: offload defeats enforcement too, and the flush is currently a no-op

If an established flow is offloaded and the REJECT rule is then enabled, the flow
keeps running in the fast path — `forward_lan` is never reached. An fw4 reload alone
does not help: the conntrack entry survives, hits `ct state established : accept`, and
is immediately re-offloaded. **Only destroying the conntrack entry breaks it.**

That makes F-05's conntrack flush load-bearing rather than an optimisation — and
`conntrack-tools` is still not installed, so today `flush` silently does nothing while
`cmd_block` returns success. `block.sh` would report success to kidsout while the
console kept playing. Fixing that is now a correctness bug, not a contingency.

## Earlier facts, from before any credential existed

Measured 2026-09-11 from the kidsout machine, before any credential existed:

```
http://192.168.2.1:80/ubus                   -> HTTP 307 (redirect to https)
http://192.168.2.1:80/cgi-bin/luci/rpc/ubus  -> HTTP 307
https://192.168.2.1:443/ubus                 -> HTTP 200 {"error":{"code":-32002,"message":"Access denied"}}
https://192.168.2.1:443/cgi-bin/luci/rpc/ubus-> HTTP 404
https://192.168.255.7:443/ubus               -> HTTP 200 (same; WG-side host is live)
ping 192.168.2.1   -> 0.377 ms
ping 192.168.2.172 -> 100% loss (console does not answer ICMP, as documented)
```

Conclusions: TLS is served (so `luci-ssl` or equivalent is present and
`https: true` is correct — F-12 is a non-issue for this deployment, but the
scheme fallback is implemented anyway); `/ubus` is the one working path
(F-13 confirmed, candidate list reduced to one); `alt_hosts` is a genuinely
reachable fallback and worth wiring up (F-11).
