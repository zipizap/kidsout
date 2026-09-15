# REVIEW.2 — hardware verification of `devices/xbox` v2

**Date:** 2026-09-15
**Reviewer:** Claude Opus 5 (session requested by Paulo)
**Subject:** `devices/WIP/complex/xbox` @ commit `e5b9517`, verified against the
real OpenWrt router and the real console
**Predecessor:** [REVIEW.1.md](REVIEW.1.md) — the spec v2 was written against
**Remediation status:** all four blockers (G-01…G-04) plus G-06, G-07, G-08,
G-11, G-12 and S2-01 were fixed on 2026-09-15 — see [PROGRESS.md](PROGRESS.md).
What remains is the write phase on the router, which nothing here can pre-verify.
**Tracking:** [PROGRESS.md](PROGRESS.md) · [F-06.md](F-06.md) · [S-02.md](S-02.md)

**Purpose of this document:** REVIEW.1 was a code review against a contract, and
said so: its §0.3 listed what could not be checked without the hardware. This is
that missing half. Every finding below is anchored to a measurement taken on the
live router, and each is written to be actionable: symptom → evidence → root cause
→ impact → proposed fix. It is the input specification for **v3**.

---

## 0. Scope, method, and safety statement

### 0.1 What was done to the router: nothing

The router was **not modified**. Verified at the end of the session:

```
nft tables                : table inet fw4          (no kidsout table)
uci firewall kidsout rules: 0
uci rpcd kidsout login    : 0
/usr/libexec/kidsout-xbox : absent
stray /tmp files          : none
```

All router access was read-only: `nft list`, `nft -c` (check-only, never loads),
`uci show`, `cat /proc/net/nf_conntrack`, `ubus call iwinfo assoclist`, and
`/sys/class/net/*/statistics`. Two short-lived files under `/tmp` were created for a
timed measurement and removed.

`router_bootstrap.sh` has **still never been run**. There is no `kidsout` rpcd user,
no helper, no ACL, no firewall rule and no accounting table on the router. No
`config.json` exists on the kidsout machine.

### 0.2 What was verified, and how

| Claim | Method |
|---|---|
| Offload is hardware, not software | `uci get firewall.@defaults[0].flow_offloading_hw` = `1`; `nft list flowtable inet fw4 ft` shows `flags offload`; `ppe0`/`ppe1` in debugfs |
| Offloaded flows still update conntrack bytes | Tracked **individual** flows by 5-tuple across a 20 s window: **9 of 9 `[HW_OFFLOAD]` flows gained bytes, 0 static** |
| A naive conntrack sum is unusable | Same table summed naively during gameplay: **−510.5 and −27.1 KB/min** |
| Console identity | `ubus call iwinfo assoclist` on `phy0-ap0`; `/tmp/dhcp.leases`; `ip neigh`; per-port `carrier` |
| Threshold calibration | Three timed windows against the live console, per-flow accumulator |
| Calibration is not an artefact of the method | Cross-validated against `iwinfo` per-station counters, which are mac80211's and wholly independent of netfilter |
| Transport works | `POST /ubus` from the kidsout machine → `{"jsonrpc":"2.0","id":1,"result":[6]}` |
| Driver's failure path works | `xbox.log` from a real run: `state=unknown error=RouterUnreachable: login as 'kidsout' failed: ubus status 6` |
| Offline suite | `./run_tests.sh` executed: 46 cases / 119 assertions, all passing |

### 0.3 Not verified — still requires a write to the router

Everything downstream of `router_bootstrap.sh` remains untested:

- whether the scoped rpcd ACL is sufficient (`./xbox.py selftest`);
- whether the `src_mac` REJECT rule actually blocks (`install` → `block` → `unblock`);
- whether the explicit `uci apply` after `uci commit` is redundant (REVIEW.1 F-09's
  open question);
- whether `./xbox.py pin`, `discover`, `check`, `status`, `calibrate` work at all —
  **none of the 13 subcommands except `state` and `probe` has ever run against the
  real router**;
- `unknown` on a router power-cut, inside the deadline.

---

## 1. Verdict

**v2's engineering is sound and its central design assumption is wrong.**

REVIEW.1's findings were remediated competently: the polarity is right, `unknown`
is reachable, the timeout budget is bounded, the rule is idempotent, the tests are
real. Where REVIEW.1 could be answered by reading code, v2 answers it.

But the state metric — the feature the whole driver exists to provide — was designed
against a router model that does not match this router. An nft counter in the forward
hook cannot see a hardware-offloaded flow, and this router offloads in hardware. The
offline suite passed 119 assertions against a mock that cannot model a flowtable, so
it certified a mechanism the hardware disproves. That is not a testing failure so
much as a reminder of what a mock can and cannot underwrite.

Three defects would each, alone, have made the deployment fail silently in
production. All three fail **open or invisible**, never loud.

### 1.1 Findings index

New findings carry `G-` identifiers. REVIEW.1 identifiers are reused where a prior
finding's *status* changed.

| ID | Severity | Finding | Verified |
|---|---|---|---|
| G-01 | **Blocker** | Hardware offload blinds the nft forward-hook counters; the state metric cannot work as designed | hardware |
| G-02 | **Blocker** | `device.json` names only the ethernet MAC; the console is on WiFi → block fails open *and* state reads zero | hardware |
| G-03 | **Blocker** | `block.sh` exits 0 when the conntrack flush fails, and conntrack-tools is absent → it reports success while the console keeps playing | hardware + code |
| G-04 | **Blocker** | A conntrack byte sum is non-monotonic; `sample_rate` assumes monotonic counters | hardware |
| G-05 | High | The driver is not discoverable by kidsout from `devices/WIP/complex/xbox` | code |
| G-06 | High | ~~The WiFi interface has no static DHCP reservation~~ **RESOLVED** — `dhcp.@host[3]` added 2026-09-15 | hardware |
| G-07 | High | `state_timeout` / `state_deadline` defaults disagree across four files; the live values are neither documented pair | code |
| G-08 | Medium | `mock_router.py` cannot model the offload path, so the suite validated a dead design | code |
| G-09 | Medium | `bridge` is not installed on the router; fdb-based port lookup fails | hardware |
| G-10 | Medium | `/var/opkg-lists/` is empty — `opkg install conntrack-tools` needs an `opkg update` first | hardware |
| G-11 | Low | `conntrack-dump` is dead code and widens S-02's blast radius | code |
| G-12 | Low | `discover --write` destroys `device.json`'s provenance comment | code |
| G-13 | Low | `valid_addr` rejects a `/prefix` its own comment says it accepts | code |
| F-05 | **Blocker** (was High) | Re-rated: with offload, the flush is the *entire* enforcement mechanism, not an optimisation | hardware |
| F-04 | Resolved / re-scoped | No IPv6 delegation, so the original concern is moot — but see G-02 | hardware |
| F-06 | Re-designed | Threshold now calibrated; mechanism replaced — see G-01 | hardware |
| S2-01 | Security | `--uninstall` rewrites `/etc/shadow` without preserving mode `0600` | code |
| S2-02 | Security | webOS client keys committed to a repo with a GitHub remote | code |

---

## 2. Blockers

### G-01 — Hardware offload blinds the byte counters; F-06's mechanism cannot work

**Severity:** Blocker. This invalidates the driver's core signal.

**Evidence.**

```
firewall.@defaults[0].flow_offloading    = 1
firewall.@defaults[0].flow_offloading_hw = 1

flowtable ft {
        hook ingress priority filter
        devices = { lan1, lan2, lan3, lan4, wan }
        flags offload          <-- hardware
        counter
}

chain forward {
        meta l4proto { tcp, udp } flow add @ft     <-- before everything else
        ...
}

/sys/kernel/debug: ppe0  ppe1                      <-- MediaTek PPE
```

12–16 of ~143 live conntrack entries carried `[HW_OFFLOAD]`.

**Root cause.** Once a flow is offloaded, packets are forwarded by the PPE in
silicon. They never enter `__netif_receive_skb_core`, so they reach **no** netfilter
hook — not `forward`, and not `netdev ingress` either. The accounting chain the
helper installs at `hook forward priority -160`
(`router_bootstrap.sh:155`) would therefore count only each flow's first few packets.

**Impact.** `getState.sh` would report `down` throughout real gameplay. No time would
ever be accrued, no block would ever be triggered by use, and the parental control
would appear to work while doing nothing. The failure is silent: the rule is present,
`uci` is clean, the counters exist and return plausible small numbers.

**Why the offline suite did not catch it.** `mock_router.py` synthesises counter
values on request. It has no notion of a flowtable, a hook, or a priority, so no
assertion in 119 could distinguish a counter that sees all traffic from one that sees
2 %. See G-08.

**What rescues the design.** fw4 declares the flowtable with `counter`, and Linux 6.6
feeds the hardware's per-flow MIB back into conntrack via `nf_ct_acct_add()` on a
~1 Hz poll from `flow_offload_work_stats()`. This is the one assumption that could
not be taken from source alone — whether *this* SoC's `mtk_eth_soc` reports MIB —
so it was measured directly:

> Tracking individual `[HW_OFFLOAD]` flows by 5-tuple across 20 s:
> **9 of 9 gained bytes, 0 static.**

Confirmed independently: `nf_conntrack_acct = 1`, and conntrack lines carry `bytes=`.

**Proposed fix.** Replace the nft counter with **conntrack byte accounting**, summed
router-side. Concretely, in `/usr/libexec/kidsout-xbox`:

- `counters` reads `/proc/net/nf_conntrack` (or `conntrack -L -o id`), selects entries
  for the console's current address(es), and sums per direction — the counter block
  belonging to the tuple whose `src=` is the console is **outbound**;
- `counters-install` no longer builds an nft table; it initialises the accumulator
  state file. `counters-remove` deletes it.

This removes the nft table, the chain, the priority, the device list, and everything
that had to survive an fw4 reload or a reboot. It needs no package:
`/proc/net/nf_conntrack` is always present. It also works identically under software
offload, hardware offload, or none — so it does not have to be revisited if the
router's offload configuration changes.

**Keep the driver-side contract identical.** `read_counters()` already returns
`{"xbox_out": int, "xbox_in": int}` and `sample_rate()` already does the delta. If the
helper returns a **monotonic accumulator** (see G-04), *nothing in `xbox.py`'s state
path needs to change*, and `test_state.py` keeps passing unmodified. Make the branch
decision a helper concern, not a driver concern.

**Trade-off to record.** Conntrack is keyed on the console's **IP**, not its MAC,
which gives up the IPv6 coverage that motivated REVIEW.1 F-04. Safe here — this ISP
delegates no IPv6 — but it must be revisited if that changes. *Enforcement* stays
MAC-keyed and is unaffected.

**Rejected alternatives**, for the record:

| option | why not |
|---|---|
| `netdev ingress` chain at priority -300 | Correct under *software* offload only. Blind to the PPE. Also binds to device names, so it breaks silently when the console roams — the exact failure in G-02. |
| Disable `flow_offloading_hw` | Would work, and is a defensible fallback, but costs routing performance to solve a problem conntrack already solves for free. |
| Exempt the console from offload | Not implementable on fw4: the only supported injection point, `chain-prepend`, emits *after* `flow add @ft`. |
| `iwinfo assoclist` per-station bytes | Works, and was used here as an independent cross-check — but it is wifi-only, so it breaks the moment the console is plugged in. |

---

### G-02 — `device.json` names the ethernet MAC; the console is on WiFi

**Severity:** Blocker. Fails open.

**Evidence.**

```
device.json / dhcp.@host[1] :  d8:e2:df:92:a9:93  ->  192.168.2.172
  ip neigh                  :  192.168.2.172 dev br-lan FAILED
  conntrack flows           :  0
  lan1/lan2 carrier         :  0

actually on the network     :  d8:e2:df:92:a9:90  ->  192.168.2.169
  /tmp/dhcp.leases          :  d8:e2:df:92:a9:90 192.168.2.169 XBOX
  iwinfo phy0-ap0           :  "mac": "D8:E2:DF:92:A9:90", "signal": -60
  ip neigh                  :  REACHABLE
  conntrack flows           :  15, of which 2 offloaded
```

**Root cause.** A console has separate MAC addresses for its wired and wireless
interfaces. `device.json`'s `mac` was filled from the check-A lease on 2026-09-11,
when the console happened to be on ethernet. It has since been unplugged and is on
WiFi. Nothing in the driver notices.

**Impact — two independent silent failures.**

1. **Enforcement fails open.** `get_xbox_rules` (`xbox.py:441-460`) builds a single
   rule with `src_mac = require_mac(device)` — `...93`. On WiFi the console's frames
   carry `...90`, which that rule does not match. `block.sh` would enable a REJECT
   that matches nothing, print `internet BLOCKED`, and exit 0 while the child keeps
   playing. This is precisely the failure class REVIEW.1 F-04 was written to prevent,
   arrived at by a different route.
2. **The state metric reads zero.** `install_counters` (`xbox.py:588`) passes
   `device["ipv4"]` — `.172` — and `console_addresses` (`xbox.py:539-552`) seeds its
   list from the same static value, then filters the neighbour table by `device["mac"]`.
   Neither finds `.169`. Accounting would be keyed on an address with no traffic, so
   `getState.sh` reports `down` forever and no time is ever accrued.

**Proposed fix.**

- `device.json` carries a **list** of interfaces, not a scalar:
  ```json
  "interfaces": [
    {"mac": "d8:e2:df:92:a9:93", "ipv4": "192.168.2.172", "link": "ethernet"},
    {"mac": "d8:e2:df:92:a9:90", "ipv4": "192.168.2.169", "link": "wifi"}
  ]
  ```
  Keep `mac`/`ipv4` as deprecated aliases for the first entry so nothing breaks
  abruptly.
- **One rule, both MACs.** fw4 parses `src_mac` as a list
  (`fw4.uc:2314`, `src_mac: [ "mac", null, PARSE_LIST ]`), so a single
  `kidsout_xbox_out` section can name both. Confirmed available on this firmware.
- **Resolve addresses dynamically.** Accounting and flushing should ask the router
  which addresses currently belong to the console's MACs (lease file + neighbour
  table) rather than trusting a static IP. The helper already has `leases` and
  `neigh` verbs; this is a small change to `console_addresses`.
- `discover` must find **all** interfaces and write them, rather than the first match.
- `selftest` should fail if none of the configured MACs is currently visible, and warn
  if the live interface is one the rule does not name.

**Regression test to add.** Install with two interfaces configured, assert the single
uci section's `src_mac` contains both; assert `console_addresses` returns the address
of whichever MAC the mocked neighbour table reports as live, not the static one.

---

### G-03 — `block.sh` reports success when the flush fails, and the flush cannot work

**Severity:** Blocker. Lies to the caller.

**Evidence.** `conntrack` is not installed (`conntrack tool : NO`, check A; still
absent at check B). The helper's `flush` verb exits 3 in that case
(`router_bootstrap.sh:171-181`), which surfaces as `UbusError`. And:

```python
def flush_conntrack(fw, device):
    ...
    except UbusError as e:
        log(f"warn: conntrack flush failed: {e}")
        return (f"NOT flushed ({e}). Install conntrack-tools ...")   # a STRING

def cmd_block(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_BLOCKED)
    print("conntrack:", flush_conntrack(fw, device))                 # returns None
```

`cmd_block` returns `None`, so `main` does `sys.exit(rc or 0)` → **exit 0**.
`block.sh` therefore reports success to kidsout whether or not the block took effect.

**Why this is now a Blocker rather than the "High" REVIEW.1 rated it.** REVIEW.1 F-05
framed the flush as protection against an established flow surviving until its
conntrack entry expires — "up to five days", later measured as 7440 s. With hardware
offload the framing is materially worse:

- an offloaded flow bypasses the forward chain entirely, so the REJECT never applies
  to it regardless of timeout;
- an fw4 reload does not help — the conntrack entry survives, matches
  `ct state established : accept` at the top of `forward`, and is immediately
  re-offloaded;
- an *active* flow keeps refreshing its flowtable timeout, so it does not age out
  while the child is playing.

**Destroying the conntrack entry is the only mechanism that cuts a live session.**
The flush is not a refinement of the block; under offload it *is* the block, for any
session already in progress. Shipping with it silently disabled means "block" does
nothing to the one case that matters.

**Proposed fix.**

```python
def cmd_block(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_BLOCKED)
    ok, detail = flush_conntrack(fw, device)
    print("conntrack:", detail)
    if not ok:
        log("block: FLUSH FAILED — an in-progress session may still be running")
        return 1          # tell upstream the block did not fully take effect
```

and make `install` / `selftest` refuse to go green when the live ruleset contains
`flow add @ft` while `conntrack_tools=no`. The helper's `info` verb already reports
`conntrack_tools`; nothing consumes it yet.

Note the interaction with the contract: `engine.go:189-191` only logs a failed
`block.sh`, and `block.sh` is re-run every tick while the device is blocked and reads
`up`. So returning non-zero is safe — it produces a log line per minute rather than a
silent lie, and stops as soon as the flush succeeds.

**One reassuring property**, worth keeping in mind so the fix is not over-engineered:
once the flush lands, the console *cannot* re-offload while blocked. `flow add @ft`
only acts on established, bidirectionally-seen flows, and the console's new SYNs and
UDP probes are REJECTed before they can reach that state. A single successful flush
is sufficient; no re-flush loop is needed.

---

### G-04 — A conntrack byte sum is not monotonic; `sample_rate` assumes it is

**Severity:** Blocker for the G-01 fix. Would make the new metric worse than the old.

**Evidence.** Summing the console's conntrack `bytes=` fields naively, during active
gameplay, over two consecutive 60 s windows:

```
min 1: conntrack-out  -510.5 KB/min     iwinfo-out  490.3 KB/min
min 2: conntrack-out   -27.1 KB/min     iwinfo-out  431.6 KB/min
```

The console was demonstrably transmitting ~490 KB/min. The naive sum reported a large
*negative* rate, because the preceding download's flows expired between samples and
took their byte counters out of the table with them. The same effect at the whole-table
level: the `[ASSURED]` aggregate moved **−1,289,885 bytes in five seconds**.

**Root cause.** Conntrack counters are per-flow and vanish with the flow. A sum over
live flows jumps up as flows are born and drops as they die; it is a gauge, not a
counter. `sample_rate` (`xbox.py:624-652`) treats `read_counters()` as cumulative and
monotonic — correct for nft named counters, false for a conntrack sum. Worse, its
safety guard

```python
if any(cur.get(k, 0) < prev.get(k, 0) for k in cur):
    return None, None, "counters decreased (router reboot or nft flush)"
```

would fire on most ticks, so the driver would sit in permanent `unknown`.

**Proposed fix — accumulate router-side, keep the driver unchanged.** The helper
maintains a per-flow accumulator in tmpfs (`/tmp/kidsout-xbox.acct`, so no flash wear):

```
for each live flow belonging to the console:
    if seen before:  total += max(0, current_bytes - last_seen_bytes)
    else:            total += current_bytes
# flows that vanished simply stop contributing; total never decreases
```

keyed on the conntrack **id** (`conntrack -L -o id`) where conntrack-tools is present,
since a 5-tuple can be reused by a new flow and would otherwise look like a counter
reset. Keying on the 5-tuple with the `max(0, …)` clamp is an acceptable fallback that
under-counts by at most one flow's worth on reuse.

The helper then emits a monotonic total, which is exactly what `sample_rate` already
expects — so `cmd_state`, the decrease-detection, the stale-sample guard and every
assertion in `test_state.py` keep working untouched. A router reboot clears tmpfs, the
total drops to zero, and the existing decrease guard correctly yields one `unknown`
tick before re-baselining.

**Residual inaccuracy, to be documented rather than engineered around:** a flow born
*and* destroyed entirely between two 60 s samples is missed. For Xbox Live's long-lived
flows this is a few KB against a 200 KB/min threshold, and it errs toward `down` — the
same direction as the existing safety bias.

**Validation.** Implemented correctly, the accumulator produced **289.5 KB/min**
outbound during the same gameplay that broke the naive sum, against `iwinfo`'s
340.5 KB/min — i.e. it works, and the remaining gap is 802.11 framing, not error.

---

## 3. High

### G-05 — kidsout cannot discover the driver where it lives

`DiscoverDevices` (`state.go:227-250`) reads **only the immediate children** of
`devicesDir` and checks each for the three script names. There is no recursion.
`devices/WIP/complex/xbox` is therefore invisible to kidsout, and so is
`devices/TODO/tablet`. `runtimestore.yaml` confirms it: only `tv` is registered.

The driver goes live only when it sits at `devices/xbox/` **and kidsout is restarted**
(discovery runs once, at startup — `main.go:24-31`).

**Fix:** move or symlink when deploying, and add the restart to the README's setup
steps. Worth stating explicitly because everything else about the driver can be
correct while it is simply never called.

### G-06 — the WiFi interface has no static DHCP reservation

`dhcp.@host[1]` reserves `.172` for the ethernet MAC `...93`. The WiFi MAC `...90`
holds `.169` on a **dynamic** lease, which can change at any renewal.

Any design that records `.169` in `device.json` therefore has a shelf life. Two
mitigations, and they are complementary rather than alternative:

1. add a second `dhcp host` section reserving an address for `...90`;
2. resolve the console's current addresses at run time from the lease and neighbour
   tables, keyed on the MAC list (G-02) — which is the durable fix and is needed
   anyway.

### G-07 — timeout defaults disagree in four places, and the live values are neither

| location | `state_timeout` | `state_deadline` |
|---|---|---|
| `xbox.py:1048-1049` (code default) | **2** | **7** |
| `config.example.json` | 1.5 | 4 |
| `getState.sh:15` (header comment) | — | "default 7s" |
| `README.md:94` | 1.5 | 4 |
| `PROGRESS.md` | 1.5 | 4 |

Because no `config.json` exists, the **live** values are the code defaults — 2 s and
7 s — which no document states as the shipped pair. A 7 s deadline against kidsout's
10 s kill leaves 3 s of margin for process startup and the final write; the documented
4 s leaves 6 s. Neither is wrong, but the driver should not be running on numbers that
appear nowhere in its own documentation.

**Fix:** pick one pair, put it in `config.example.json`, make the code defaults
identical, and delete the numbers from the prose so there is one source of truth.
`test_failure_modes.py` already asserts against `shipped_config()`, so make the code
default match that file rather than the reverse.

---

## 4. Medium

### G-08 — the mock cannot model the thing that broke

`mock_router.py` fakes the ubus surface faithfully — logins, ACL denials, 404s, slow
responses, helper exit codes — and `helper_exec()` synthesises `nft -j` counter JSON on
request. What it cannot model is **what the counter is attached to**: there is no
flowtable, no hook, no priority, no notion that a packet might bypass netfilter.

So the suite could assert that the driver reads a counter, computes a rate, and
thresholds it correctly — all true — while the counter it was reading could never have
moved on real hardware. 119 assertions passed against a dead design.

This is not an argument against the mock, which caught two real bugs while being
written. It is an argument for being explicit about its boundary: **the offline suite
validates the driver's logic, not the router's behaviour.** Any claim about what the
router does must carry a hardware citation.

**Fix:**
- update the mock to emit the new helper `counters` shape (see §5) so the suite keeps
  covering the driver;
- add a fixture exercising the non-monotonic case — counters that go *down* between
  ticks — and assert the driver reports `unknown` once and re-baselines;
- record in the suite's own docstring that mechanism questions are out of its scope,
  and keep the hardware evidence in `PROGRESS.md`.

### G-09 — `bridge` is not installed

`bridge fdb show` is absent on this router (`sh: bridge: not found`), so any
port-resolution logic built on the forwarding database silently yields nothing. This
affected the check scripts during this session and would affect any `netdev`-based
design that needs to know which port the console is on.

Not a problem for the recommended conntrack design, which needs no device names. Worth
recording so it is not rediscovered: use `ubus call iwinfo assoclist` for wireless and
per-port `/sys/class/net/*/statistics` + `carrier` for wired, or install `ip-bridge`.

### G-10 — the package list is empty

`/var/opkg-lists/` contains no files, so `opkg install conntrack-tools` fails until
`opkg update` has run. `router_bootstrap.sh:283-301` does run `opkg update` first and
degrades to a warning rather than failing, so this is handled — but the overlay has
41.5 MB free and the bootstrap has never been exercised, so confirm the install
actually succeeds rather than assuming the warning path is unreachable.

---

## 5. Low

### G-11 — `conntrack-dump` is dead code that widens the credential's reach

Implemented at `router_bootstrap.sh:192-194`, documented at `:83`, granted by the ACL,
and called by nothing in `xbox.py`. It `cat`s the entire conntrack table — the whole
household's connections — to anyone holding the `kidsout` credential.

S-02.md's threat model says the worst case with that credential is "block or unblock
the Xbox, read its byte counters, drop its conntrack entries, and list the DHCP leases
and neighbour table". A full conntrack dump is broader than that and is not needed.

**Fix:** delete the verb. If the new `counters` verb needs to read the same file, it
does so *inside* the helper and returns two integers, which is strictly less exposure.

### G-12 — `discover --write` destroys `device.json`'s provenance

`cmd_discover` (`xbox.py:945`) does `dev.pop("comment", None)` before rewriting the
file, deleting the ~1,400-character comment recording where the MAC came from and why
the rule is MAC-keyed. Deliberate, but it removes exactly the context a future reader
needs — and G-02 shows that context was already stale and misleading, which is an
argument for maintaining it, not deleting it.

**Fix:** preserve unknown keys on rewrite; update `comment` with the discovery date
instead of dropping it.

### G-13 — `valid_addr` contradicts its own comment

`router_bootstrap.sh:96-104` says the validator permits "a /prefix", but the character
class `*[!0-9a-fA-F.:]*` rejects `/`. Harmless today (no caller passes a CIDR) but the
comment will mislead whoever next extends the helper.

---

## 6. What v2 got right — do not regress these

Verified this session, against hardware rather than the mock:

| Property | Evidence |
|---|---|
| Endpoint discovery works against the real router | `.state.json` cached `https://192.168.2.1:443/ubus` on a real run |
| `unknown` is genuinely reachable and truthful | `xbox.log`: `state=unknown error=RouterUnreachable: login as 'kidsout' failed: ubus status 6 (permission denied) for session.login` — precise, actionable, and correct |
| The failure is fast, not a 61 s hang | that run returned inside the deadline with a real error, not a SIGKILL |
| The driver-owned log is the right call | upstream discards stderr; this line is the only reason the failure was diagnosable at all |
| `/ubus` is the one working path | `POST` → `{"result":[6]}`; `/cgi-bin/luci/rpc/ubus` 404s (F-13 confirmed) |
| TLS is served and pinnable | EC cert, SHA-256 `65:F6:D0:…:94:7F`, valid to 2027-01-18 |
| The threshold was a good guess | 200 KB/min separates gaming (289.5) from downloading (93.7) with 3.1× margin |
| REVIEW.1's F-06 reasoning was sound | flow-counting really would have failed; byte-rate really does discriminate |
| The offline suite is honest about what it covers | 46 cases / 119 assertions, re-run and green at every commit this session |

**And one REVIEW.1 worry that measurement retires:** F-06.md warned that byte rate
could not separate playing from downloading, and that a background update would read
`up` and burn the allowance. Measured, it does not — during a 12 MB/min download the
outbound stream was 93.7 KB/min, about 0.8 % of downstream, comfortably below the
threshold. The metric being outbound-only is what saves it. Mitigations (a) and (b)
in F-06.md are unnecessary for this deployment.

---

## 7. Security

### S2-01 — `--uninstall` drops `/etc/shadow`'s permissions

`router_bootstrap.sh:58-61`:

```sh
grep -v "^${RPCD_USER}:" "$f" > "$f.kidsout.tmp" && mv "$f.kidsout.tmp" "$f"
```

applied to `/etc/passwd`, `/etc/group` **and `/etc/shadow`**. The new file is created
with the shell's umask — typically `0644` — and `mv` preserves the *new* file's mode,
not the original's. `/etc/shadow` is `0600 root:root`. Running `--uninstall` would
therefore leave every account's password hash world-readable.

**Fix:** `chmod --reference` the original before `mv`, or edit in place:
```sh
cp -p "$f" "$f.kidsout.bak"
grep -v "^${RPCD_USER}:" "$f.kidsout.bak" > "$f"
rm -f "$f.kidsout.bak"
```
and verify with `ls -l /etc/shadow` in the uninstall output.

### S2-02 — webOS client keys are committed to a repo with a GitHub remote

Out of scope for the xbox driver, but found while reading the repository and it should
not wait: `devices/tv/.lg-webos-ssap.key` and `devices/tv/tools/.lg-webos-ssap.key` are
tracked (mode `100644`) in a repository whose history includes a commit named
"upload to github". The contents were not opened.

**Treat that key as compromised.** Rotate it on the TV, add both paths to
`.gitignore`, and decide whether to purge it from history. Separately,
`runtimestore.yaml` holds plaintext Basic Auth credentials in the working tree —
deliberate per DESIGN.md, but relevant if this repository is ever public.

### S2-03 — the TLS pin is still unset

`device.json.router.tls_sha256` is `null` and `verify_tls` defaults to `false`, so
`Router.pin` is `""` and the check at `xbox.py:250` is inert. Until `./xbox.py pin`
runs, credentials would be sent over an unauthenticated TLS session. The fingerprint
is already known from this session and can be pinned before the first credential
exists.

---

## 8. Recommended shape for v3

### 8.1 Must — correctness

1. **G-01** — move the byte source to conntrack, summed in the helper. Keep
   `read_counters()`'s `{"xbox_out", "xbox_in"}` contract so `xbox.py`'s state path and
   `test_state.py` are untouched.
2. **G-04** — the helper returns a **monotonic accumulator**, keyed on conntrack id,
   state in tmpfs. This is what makes (1) safe.
3. **G-02** — `device.json` carries both interfaces; one rule names both MACs;
   addresses resolved at run time from lease + neighbour tables.
4. **G-03** — `cmd_block` returns non-zero when the flush fails; `selftest` and
   `install` refuse to go green when offload is on and conntrack-tools is absent.
5. **F-05 / G-10** — actually install conntrack-tools, and verify the flush works by
   watching a live session die.

### 8.2 Should

6. **G-06** — reserve an address for the WiFi MAC, and stop trusting static IPs.
7. **G-07** — one source of truth for the timeout pair.
8. **G-08** — mock emits the new counters shape; add a non-monotonic fixture.
9. **G-05** — document the `devices/xbox/` placement and the restart requirement.

### 8.3 Nice

10. G-11 delete `conntrack-dump`; G-12 preserve `device.json` comments; G-13 fix the
    validator comment; S2-01 preserve `/etc/shadow`'s mode.
11. Add an `offload-facts` helper verb (`flowtable_counter`, `flowtable_hw`,
    `ct_acct`, live interface) and log it from the 15-minute audit, so a firmware
    upgrade that changes the offload story shows up in `xbox.log` as a line rather
    than as a silent `down`.

### 8.4 Documentation corrections required

| Location | Claim | Reality |
|---|---|---|
| `F-06.md` (pre-update) | nft counter at `hook forward priority -150` | mechanism replaced; the helper used -160 and now uses neither (G-01) |
| `F-06.md` (pre-update) | a background download reads as `up` | measured 93.7 KB/min outbound → `down` |
| `PROGRESS.md` decisions table | "State metric: nft byte counters" | superseded by conntrack accounting |
| `device.json.comment` | "MAC confirmed … the firewall rule matches on the MAC, so it covers IPv6 and survives the IP changing" | it does not survive the console changing *interface* (G-02) |
| `README.md:60` | flows survive "up to five days" | 7440 s measured; and under offload the flush is the only mechanism at all |
| `getState.sh:15` | "default 7s" | disagrees with `config.example.json` (G-07) |

### 8.5 Test plan for v3

**Offline, before touching the router:**

- helper `counters` output parsing, including the new key/value shape and the
  monotonic guarantee;
- a fixture where the underlying sum *decreases* → assert one `unknown`, then a clean
  re-baseline;
- two-interface install → assert one uci section whose `src_mac` names both MACs;
- `console_addresses` returns the *live* interface's address, not the static one;
- `cmd_block` returns non-zero when the mocked flush fails — the G-03 regression test,
  which does not exist today.

**On hardware, in this order** (each gates the next):

1. `router_bootstrap.sh`, then `./xbox.py selftest` green — the first real test of the
   ACL scope.
2. `./xbox.py pin`.
3. `./xbox.py discover --write` with the console on **each** interface in turn;
   confirm both are recorded.
4. `./xbox.py install` → **console still online**.
5. `nft list ruleset | grep -A3 kidsout_xbox` — confirm the rule is *in force*, not
   merely committed, and settle whether the explicit `uci apply` is redundant.
6. `block` while a game is in progress → the session dies within seconds. This is the
   test that G-03 and F-05 both hinge on, and it has never been run.
7. `block` while the console is on the **other** interface → still blocks (G-02).
8. `unblock` → recovery.
9. Router power-cut mid-tick → `unknown` inside the deadline.
10. Re-run the calibration on ethernet; confirm the threshold still separates.

---

## 9. Open questions

**Q1 — Should the console be pinned to one interface?**
Much of G-02's complexity exists because the console can appear on either. If it is
always going to be on WiFi, reserving an address for `...90` and treating ethernet as
a documented edge case is simpler than full dual-interface support. This is a
household decision, not a technical one.

**Q2 — Is `conntrack -L -o id` available once conntrack-tools is installed?**
The id-keyed accumulator is meaningfully more correct than the 5-tuple fallback.
Confirm the busybox-adjacent build supports `-o id` before depending on it.

**Q3 — Should enforcement also move to a hook offload cannot bypass?**
A `drop` in a `netdev ingress` chain would cut packets before the flowtable lookup,
making a block bite without any flush. It only helps under *software* offload, and it
splits enforcement across two mechanisms. Recommended only if conntrack-tools turns
out to be uninstallable.

**Q4 — What happens on an OpenWrt upgrade?**
The design now depends on fw4 emitting `counter` on its flowtable and on
`mtk_eth_soc` reporting PPE MIB. Both are current upstream behaviour, neither is a
stable API. The `offload-facts` verb in §8.3 exists so that a regression is visible in
the log rather than silent — worth implementing for that reason alone.

**Q5 — Does REVIEW.1 F-10's reconciliation gap matter more now?**
`unblock.sh` is edge-triggered, and the router holds enforcement state kidsout does
not know about. With the block now depending on a conntrack flush that can fail, the
gap between "kidsout believes it unblocked" and "the router is actually forwarding"
has more ways to open. The 15-minute `audit:` line makes divergence visible; nothing
repairs it.
