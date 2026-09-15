# REVIEW.1 remediation — progress

Tracking file for the v2 rewrite driven by [REVIEW.1.md](REVIEW.1.md).
Decisions taken with Paulo before implementation:

| Question | Decision |
|---|---|
| Auth | Scoped rpcd user `kidsout`, created by `router_bootstrap.sh`. No root credential in `config.json`. |
| State metric | nft byte counters (F-06), thresholded on the **outbound** MAC-matched counter. |
| Rule shape | One MAC-based outbound REJECT; `_fwd_in` and `_input_out` deleted (F-04, F-08). |
| Scope | Everything: §8.1 + §8.2 + §8.3 + §8.4 + the §8.5 test suite. |
| Bootstrap | Script run over SSH by Paulo; password never seen by the assistant. |
| Calibration | Full session against the real console (standby vs active) — see [F-06.md](F-06.md). |
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

Legend: ☐ not started · ◐ code written, unverified on hardware · ☑ verified

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

## Remaining — needs the router and the console

- [ ] Run `router_bootstrap.sh`; record the printed facts here.
- [ ] `./xbox.py selftest` green (proves the ACL scope is sufficient).
- [ ] `./xbox.py pin`.
- [ ] `./xbox.py discover --write` — must be taught to record **both** MACs
      (ethernet `...93`/.172 and WiFi `...90`/.169), not just whichever is live.
- [ ] `./xbox.py install` → **console still online** (proves the F-01 fix).
- [ ] Confirm the live ruleset, not just the committed config:
      `nft list ruleset | grep -A3 kidsout_xbox` after a block — this settles
      REVIEW.1's open question about whether the explicit `uci apply` after
      `uci commit` is redundant.
- [ ] `block` → an *in-progress* game or stream dies within seconds (F-05).
- [x] ~~Confirm `ether saddr` matches in the forward hook~~ — **answered, negatively**:
      hardware offload bypasses the forward hook entirely, so no counter there can
      work. Metric moved to conntrack byte accounting (verified working).
- [x] ~~Check for a global IPv6 address on the console~~ — no IPv6 delegation (check A).
- [ ] `unblock` → recovery.
- [x] ~~Calibration session~~ — done 2026-09-15: idle 20.8 / downloading 93.7 /
      gaming 289.5 KB/min outbound. 200 KB/min threshold confirmed. See F-06.md.
- [ ] Pull the router's power mid-tick → `unknown` inside the deadline.

## Hardware facts — read-only check A, 2026-09-11

Run from an SSH root session on the router (`router_checks_A.sh`), before any
change. Full output kept in `router_checks_A.sh.printout`.

| fact | value | consequence |
|---|---|---|
| OpenWrt | 24.10.5 r29087, mediatek/filogic, Linux 6.6.119 aarch64 | opkg, fw4 |
| firewall | fw4 + nftables 1.1.1, `nftables-json` installed | `nft -j` works, text fallback unused |
| zones | `lan`, `wan`, **`WgZone`** | a third zone lan can forward into — block scope widened to all forwarding |
| `src_mac` | `fw4.uc:2314` `src_mac: [ "mac", null, PARSE_LIST ]`, mapped to `smacs_pos` | **F-04's MAC-keyed rule is supported** |
| nft dry run | named counters + `ether saddr` + `priority -150` all parse | F-06's counter syntax is valid |
| priority clash | a chain already sits at `forward priority mangle` (-150) | ours moved to **-160** |
| rpcd | 2025.09.01 with `rpcd-mod-file`; only one login (`root`, `*`/`*`) | ACL schema confirmed against `luci-app-firewall.json` |
| hashing tools | `cryptpw`, `mkpasswd`, `openssl` **all absent**; only `/bin/passwd` | bootstrap creates a system user and defers to `passwd`, storing `$p$kidsout` |
| conntrack-tools | **not installed** | F-05's flush was a no-op; bootstrap now installs it |
| established timeout | `nf_conntrack_tcp_timeout_established = 7440` (2 h, not 5 days) | a block without a flush is a 2-hour no-op, not a 5-day one |
| established accept | `ct state vmap { established : accept }` sits **above** `jump forward_lan` | **F-05 confirmed on hardware**, not merely inferred |
| IPv6 | no default v6 route; LAN has only ULA `fd0c:cfee:9422::1/60` | **REVIEW.1 Q1 answered: the ISP does not provide IPv6.** F-04 was a latent trap, not a live bug |
| console | MAC `d8:e2:df:92:a9:93`, hostname `XBOX`, static reservation at `dhcp.@host[1]` | `device.json.mac` filled without needing `discover` |
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

Full output in `router_checks_B1.sh.printout`. The console was **off the network**
during this run (no DHCP lease, neighbour `FAILED`, zero conntrack flows), so the
console-specific parts of B2 are still pending; but the design question was
answerable without it.

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
