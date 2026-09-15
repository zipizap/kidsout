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
| F-04 | IPv6 not blocked | ◐ | rule keyed on `src_mac`; needs `device.json.mac` from discover |
| F-05 | block does not cut live sessions | ◐ | conntrack flushed for IPv4 + any IPv6 from the neighbour table; needs conntrack-tools |
| F-06 | Instant-On standby reads `up` forever | ◐ | nft byte counters; threshold still a guess until calibrated — see F-06.md |
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
- [ ] `./xbox.py discover --write` with the console on.
- [ ] `./xbox.py install` → **console still online** (proves the F-01 fix).
- [ ] Confirm the live ruleset, not just the committed config:
      `nft list ruleset | grep -A3 kidsout_xbox` after a block — this settles
      REVIEW.1's open question about whether the explicit `uci apply` after
      `uci commit` is redundant.
- [ ] `block` → an *in-progress* game or stream dies within seconds (F-05).
- [ ] Confirm `ether saddr` actually matches in the forward hook on this router:
      `xbox_out` must move while the console is active. If it stays at zero
      while `xbox_in` moves, the MAC match is not working and both the rule and
      the counter need rethinking.
- [ ] Check for a global IPv6 address on the console (REVIEW.1 Q1).
- [ ] `unblock` → recovery.
- [ ] Calibration session → fill in F-06.md and set the real threshold.
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
**The accounting design is on hold until that comes back.**

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
