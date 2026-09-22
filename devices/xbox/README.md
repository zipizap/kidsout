# devices/xbox — the family Xbox, driven by the generic OpenWrt driver

This device uses the shared [`generic-openwrt-driver`](../../generic-openwrt-driver/README.md).
Nothing here is Xbox-specific code; only the facts are.

```
devices/xbox/
├── getState.sh block.sh unblock.sh      kidsout's contract — identical to the driver's contract/ templates
├── generic-openwrt-driver_files/
│   ├── driver.sh                        operator entry point:  ./driver.sh status | selftest | block | allow ...
│   ├── device.json                      the Xbox's id, two MACs, calibrated thresholds, router address, TLS pin
│   ├── config.json -> ~/…               rpcd credential (symlink, gitignored, 0600)
│   └── .state.json driver.log           runtime (gitignored)
├── other/                               measurements and artefacts from building the driver on this console
│   ├── Tests.20260922.124049.md         live contract test session against the real router and console
│   ├── router_checks_A.sh(.printout)    read-only router survey and its output
│   ├── router_checks_B1.sh(.printout)   the flow-offload investigation that settled the accounting design
│   ├── router_checks_B2.sh              console visibility / flow counts
│   └── FUTURE_IDEAS.md                  the 2026-09-15 generalisation discussion (now implemented differently)
└── README.md                            this file
```

Router-side names for this device: rule `kidsout_xbox_out`, helper
`/usr/libexec/kidsout-xbox`, ACL `kidsout-xbox`, login `kidsout-xbox`.

---

## The console

Two interfaces, and it switches between them whenever the cable is plugged or
pulled. Both hold a static DHCP reservation, so the addresses are stable; the
driver still resolves the live address at run time from the lease and
neighbour tables (a reservation only takes effect when the console renews).

| | MAC | address | reservation |
|---|---|---|---|
| ethernet | `d8:e2:df:92:a9:93` | `192.168.2.172` | `dhcp.@host[1]` |
| wifi | `d8:e2:df:92:a9:90` | `192.168.2.169` | `dhcp.@host[3]` (added 2026-09-15) |

The console also holds several link-local and ULA IPv6 addresses, which are
flushed but not counted. It does **not** answer ICMP, so `ping` is not a
presence test — `./driver.sh check` uses the neighbour table.

**Power mode must stay Energy-saving** (Settings → General → Power options).
With Instant-on, standby downloads would read `up` overnight and burn the
allowance (see calibration below).

---

## Calibration (the numbers behind device.json)

Measured on the real console on WiFi, with 60 s ticks:

| state | outbound | inbound | verdict @ out 200 KB/min, in 1 MB/min |
|---|---|---|---|
| idle / dashboard | **20.8** KB/min | **23** KB/min | DOWN ✓ |
| downloading | **93.7** KB/min | **11.8** MB/min | UP (by inbound — accepted, see below) |
| gaming (measured twice, 2026-09-15) | **289.5** / **471.6** KB/min | — | **UP** ✓ (by outbound) |
| streaming YouTube (2026-09-21) | **60–80** KB/min | **4.4–18** MB/min | **UP** ✓ (by inbound) |
| blocked | **3.0** KB/min | **8.2** KB/min | DOWN ✓ |

**Outbound threshold: `204800` bytes/min (200 KB/min).** Between downloading
and gaming with ≥3× separation either side.

**Inbound threshold: `1048576` bytes/min (1 MB/min).** 45× above the idle
dashboard, 4× below the slowest streaming tick observed. Low enough to catch
low-bitrate video and music streaming (~1.2 MB/min).

Cross-validated against `iwinfo assoclist` (mac80211's per-station counters):
conntrack captured 95.5 % of iwinfo's inbound bytes and 71–85 % of outbound
(the gap is small ACKs counted as whole 802.11 frames by iwinfo).

The first shipped rule was outbound-only and read YouTube as `down` for a whole
evening (2026-09-21): streaming's outbound rate (60–80 KB/min) is *below* a
download's (93.7 KB/min), so no outbound line separates them. The inbound
threshold fixed that at the cost of a game download reading `up`; with the
console in Energy-saving mode that can only happen while someone has switched
it on.

**Not measured:** ethernet. All figures are WiFi; re-run the calibration the
next time the console is cabled (`./driver.sh calibrate --label gaming`).

---

## This router, as measured

Cudy WR3000E v1 · OpenWrt 24.10.5 r29087 · mediatek/filogic · Linux 6.6.119 aarch64

| fact | value | why it matters |
|---|---|---|
| firewall | fw4 + nftables 1.1.1 | `src_mac` is a `PARSE_LIST` (`fw4.uc:2314`), so one rule can name both MACs |
| zones | `lan`, `wan`, `WgZone` | a third zone lan can forward into |
| **flow offloading** | `flow_offloading=1`, **`flow_offloading_hw=1`**, `flags offload`, PPE `ppe0`/`ppe1` | **hardware** offload — no software packet counter can see established flows |
| **flowtable `counter`** | **present** | the reason conntrack byte accounting works at all |
| `nf_conntrack_acct` | `1` | conntrack carries `bytes=` per direction |
| conntrack | v1.4.8, `-o id` supported | id-keyed accumulator; package is `conntrack` |
| established timeout | `nf_conntrack_tcp_timeout_established = 7440` (2 h) | a block without a flush is a 2-hour no-op at best |
| established accept | `ct state vmap { established : accept }` sits **above** `jump forward_lan` | why the flush is load-bearing |
| **IPv6** | no default v6 route; LAN has only ULA `fd0c:cfee:9422::/60` | the ISP provides no IPv6, so IPv6 leakage is not a live risk here |
| rpcd | 2025.09.01 with `rpcd-mod-file`; `file exec` exposed | the scoped-ACL design is viable |
| uhttpd | 80 + 443, EC cert `/etc/uhttpd.crt`, `ubus_prefix=/ubus`, `redirect_https=1` | `/ubus` is the one working path; the 307 on :80 is this |
| hashing tools | `cryptpw`, `mkpasswd`, `openssl` **all absent**; only `/bin/passwd` | bootstrap defers to `passwd(1)` and stores `$p$<user>` |
| **`bridge` command** | **not installed** | fdb-based port lookup does not work here; use `ip neigh` / `iwinfo` / per-port counters |
| storage | UBIFS overlay, ~40 MB free | room for packages; flash-wear concern is real but wear-levelled |
| `uci apply` after `commit` | returns ubus status 5 (NO_DATA) | commit already applied; the driver tolerates it |

Transport: `https://192.168.2.1:443/ubus`, fallback `192.168.255.7` (WireGuard
side). TLS pin recorded in `device.json` (`65f6d012…e4e6947f`, EC cert valid to
2027-01-18) — re-run `./driver.sh pin` after the router regenerates it.

### What a block looked like, live

Measured during an online multiplayer game (2026-09-15):

```
flushed 32 conntrack entries        (0 on an immediate repeat)
established=0   nonDNS=0   4,613 packets REJECTed
```

The game retried for tens of seconds before disconnecting. 45 of 46 remaining
flows were DNS retries to the router itself, which the rule deliberately does
not touch. Full contract test log: [`other/Tests.20260922.124049.md`](other/Tests.20260922.124049.md).
