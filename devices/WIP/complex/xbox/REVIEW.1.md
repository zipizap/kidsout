# REVIEW.1 — code review of `devices/xbox` against the kidsout device contract

**Date:** 2026-09-11
**Reviewer:** Claude Opus 5 (review requested by Paulo)
**Subject:** `/home/paulo/projs/move/move/kidsout/devices/xbox` @ file mtimes 2026-09-03
**Upstream reference:** `github.com/zipizap/kidsout` (cloned at review time; `engine.go`,
`state.go`, `api.go`, `DESIGN/DESIGN.md`, `devices.example.tgz`)

**Purpose of this document:** it is the input specification for the **next version**
of these scripts. Every finding below is written to be actionable: symptom →
evidence → root cause → exact location → impact → proposed fix.

---

## 0. Scope, method, and safety statement

### 0.1 Nothing was changed on the router or on any device

This review was **read-only with respect to real infrastructure**. Specifically:

- No `config.json` existed in the project directory at any point, so the driver had
  no credentials with which to authenticate to anything.
- All driver execution was performed on a **copy** of the directory placed in a
  scratch directory, driven against the bundled `mock_router.py` bound to
  `127.0.0.1:8443`.
- One timing measurement was pointed at `192.0.2.1` — TEST-NET-1, the IANA-reserved
  documentation range. It is not the router; it black-holes by design.
- The project directory was not modified during the review. This `REVIEW.1.md` file
  is the first and only write.

### 0.2 What was verified, and how

| Claim | Method |
|---|---|
| Upstream script contract | Read the Go source directly (`engine.go`, `state.go`, `main.go`, `main_test.go`), not only the README |
| block/allow polarity | Executed `install.sh` → `getState.sh` → `block.sh` → `unblock.sh` against `mock_router.py`, then dumped the resulting uci firewall state over ubus |
| conntrack parsing | Ran `test_state_logic.py` and inspected the actual port set produced |
| `getState.sh` runtime budget | Wall-clock measured with `/usr/bin/time` against an unroutable address using the shipped `config.example.json` timeout value |
| OpenWrt `enabled` semantics | OpenWrt firewall configuration documentation |
| OpenWrt `dest='*'` semantics | OpenWrt firewall configuration documentation |
| rpcd `commit` vs `apply` | OpenWrt `rpcd/uci.c` source |

### 0.3 Not verified (requires the real router / real console)

- fw3-vs-fw4 established-connection behaviour on *this* router (finding F-05).
- Whether `rpcd-mod-file`'s `file exec` is permitted by the ACL for the login user.
- Whether `/usr/sbin/conntrack` exists (conntrack-tools is not installed by default
  on OpenWrt) — the fallback chain probably always lands on `/proc/net/nf_conntrack`.
- Whether uhttpd serves TLS on 443 (needs `luci-ssl`).
- Whether the ISP provides IPv6 (finding F-04).

---

## 1. Verdict

**The contract *shape* is correct. The contract *semantics* are inverted.**
As shipped, `block.sh` un-blocks the Xbox and `unblock.sh` blocks it.

The three-script interface, the self-locating `cd "$(dirname "$0")"`, the one-word
stdout, and the exit-code handling are all right. What sits behind that interface is
wrong in ways that range from "trivially fixable constant" to "silently defeats the
entire purpose on a real Xbox".

### 1.1 Findings index

| ID | Severity | Finding | Fixed by |
|---|---|---|---|
| F-01 | **Blocker** | `block`/`allow` polarity inverted; `install` blocks on install | one-line constant swap + prints |
| F-02 | **Blocker** | `getState.sh` can never emit `unknown`; all errors report `down` | error propagation rework |
| F-03 | **Blocker** | `getState.sh` measured at **61 s** vs the 10 s contract timeout | timeout budget rework |
| F-04 | **High** | IPv6 traffic is not blocked at all | rules keyed on MAC, not IPv4 |
| F-05 | **High** | Enabling the rules does not cut an in-progress session | conntrack flush after block |
| F-06 | **High** | Xbox Instant-On standby reads as `up` forever → phantom TU accrual | byte-rate detection |
| F-07 | Medium | The source-port regex never matches; threshold works by accident | fix regex + define the metric |
| F-08 | Medium | Third rule is a forward rule, not the input rule its name claims | drop `dest` or drop the rule |
| F-09 | Medium | `uci commit` + firewall reload every 60 s → flash wear | make `_set_enabled` a no-op when already correct |
| F-10 | Medium | No reconciliation: a lost kidsout state leaves the Xbox blocked forever | periodic desired-state enforcement |
| F-11 | Low | `alt_hosts` never used by the driver (contradicts README) | iterate hosts in `Router` |
| F-12 | Low | No HTTP fallback when 443 is closed; misleading error | probe both schemes |
| F-13 | Low | `/cgi-bin/luci/rpc/ubus` is a different protocol, not a ubus fallback | remove the candidate |
| F-14 | Low | `cmd_check` probes ports the console never listens on | rewrite or drop |
| F-15 | Low | One rpcd session leaked per script run (`timeout: 900`) | shorten timeout or log out |
| S-01 | Security | Private key file committed to the working tree | generate at runtime |
| S-02 | Security | Router **root** password in plaintext `config.json` | scoped rpcd user + `chmod 600` |
| S-03 | Security | `verify_tls: false` → LAN MITM can capture that password | pin the cert |

---

## 2. Blockers

### F-01 — `block`/`allow` polarity is inverted (and `install` blocks the console)

**Severity:** Blocker. This is the single defect that makes the device driver do the
exact opposite of its purpose.

**Root cause.** The three firewall sections are `target: REJECT` rules
(`xbox.py:235-248`). In OpenWrt's firewall config, the `enabled` option controls
whether the rule exists in the generated ruleset — `enabled '0'` **disables the
rule**, `enabled '1'` **activates it**. Therefore, for a REJECT rule:

- `enabled=1` → the REJECT is in force → **internet blocked**
- `enabled=0` → the REJECT is absent → **internet allowed**

The driver has this backwards:

| entry point | driver command | sets | actual effect | intended effect |
|---|---|---|---|---|
| `block.sh` | `cmd_block` (`xbox.py:291-292`) | `enabled="0"` | internet **allowed** | internet blocked |
| `unblock.sh` / `allow.sh` | `cmd_allow` (`xbox.py:295-296`) | `enabled="1"` | internet **blocked** | internet allowed |

**Evidence (reproduced against `mock_router.py`):**

```
$ ./install.sh
install: created=['kidsout_xbox_fwd_out', 'kidsout_xbox_fwd_in', 'kidsout_xbox_input_out'] updated=[]
firewall reloaded. kidsout_xbox rules active and ENABLED (internet allowed).

$ ./block.sh
firewall kidsout_xbox* sections set enabled=0 and reloaded

  (uci firewall dump immediately afterwards, read back over ubus:)
  AFTER block.sh: kidsout_xbox_fwd_out    target= REJECT enabled= 0
  AFTER block.sh: kidsout_xbox_fwd_in     target= REJECT enabled= 0
  AFTER block.sh: kidsout_xbox_input_out  target= REJECT enabled= 0

$ ./unblock.sh
firewall kidsout_xbox* sections set enabled=1 and reloaded
```

`block.sh` demonstrably leaves three REJECT rules in the **disabled** state.

**Secondary defect — `install` cuts the console off.** `get_xbox_rules`
(`xbox.py:235-248`) hard-codes `"enabled": "1"` in all three rule dicts, and
`cmd_install` (`xbox.py:251-265`) then prints:

```
firewall reloaded. kidsout_xbox rules active and ENABLED (internet allowed).
```

Running `./xbox.py install` therefore **immediately blocks the Xbox** while printing
a message claiming the opposite. This is the worst possible first-run experience:
setup step 3 of the README silently kills the console's internet.

**Tertiary defect — the same inversion in the legacy core.** `old/kidsout.py`
dispatches its own `block`/`allow` group commands straight to `xbox.py block` /
`xbox.py allow`, so `./kidsout.py block xbox` un-blocks too. If the legacy core is
retained, it inherits the fix automatically; if it is retired, its `README.md`
description of the semantics should go with it.

**Impact.** Total inversion of the product's function. Kids get internet exactly when
they are supposed to be blocked, and are cut off during their allowed timeframe.
Because upstream only calls `block.sh` when `getState.sh` says `up`, and the
inverted block leaves traffic flowing, `getState.sh` keeps reporting `up`, so
`block.sh` is re-run every minute forever, never achieving anything (and see F-09
for what that does to the router's flash).

**Proposed fix.**

```python
# xbox.py — rule definitions: install in the ALLOWED state
def get_xbox_rules(device):
    ...
    ENABLED_WHEN_BLOCKED = "1"   # REJECT active   -> internet off
    ENABLED_WHEN_ALLOWED = "0"   # REJECT inactive -> internet on
    # install with the console usable:
    ... "enabled": ENABLED_WHEN_ALLOWED, ...

def cmd_block(fw, device, _args):
    _set_enabled(fw, device, "1")      # was "0"

def cmd_allow(fw, device, _args):
    _set_enabled(fw, device, "0")      # was "1"
```

Also fix the three user-facing strings:
- `cmd_install` (`xbox.py:265`) — "rules installed in the ALLOWED state (REJECT rules
  present but disabled)".
- `_set_enabled` (`xbox.py:288`) — print the *meaning* (`internet BLOCKED` /
  `internet ALLOWED`), not the raw uci value. Printing `enabled=0` is exactly what
  made this bug easy to miss.

**Regression test to add.** An offline assertion, in `test_state_logic.py` or a new
`test_block_semantics.py`, that after `cmd_block` every `kidsout_xbox*` section in the
mock has `target == "REJECT" and enabled == "1"`, and the inverse after `cmd_allow`.
This is the single most valuable test in the whole suite and it does not exist today.

---

### F-02 — `getState.sh` can never return `unknown`; every failure reports `down`

**Severity:** Blocker (contract violation + loss of diagnosability).

**Root cause.** `_conntrack_flows` (`xbox.py:316-334`) wraps each of its three
attempts in a bare `except Exception: continue`, and falls off the end with
`return []`:

```python
    for cmd, params in attempts:
        try:
            r = fw.file_exec(cmd, params)
            if r.get("code") == 0:
                return [l for l in (r.get("stdout") or "").splitlines() if ip in l]
        except Exception:
            continue
    return []
```

`file_exec` → `call` → `login` → `probe`, so *every* failure mode of the entire
transport stack is swallowed here:

- router unreachable / no route / connection refused
- `config.json` missing (password defaults to `""`)
- wrong password → `Access denied`
- `uhttpd-mod-ubus` not installed → 404 on every candidate URL
- ACL denies `file exec`
- neither `conntrack` nor `/proc/net/nf_conntrack` readable

All of them produce `[]`, which `_active_flows` reads as "no ports", which
`cmd_state` prints as `down`.

Consequently the `except` branch of `cmd_state` (`xbox.py:377-379`) —

```python
    except Exception as e:
        print("unknown")
        raise
```

— is **dead code**. `_active_flows` cannot raise, because `_conntrack_flows` cannot
raise.

**Evidence.** With `config.json` pointed at an unroutable address:

```
$ ./getState.sh
down
rc=0
```

Expected per the shipped documentation: `unknown`.

**Contract impact.** Upstream `DESIGN/DESIGN.md` and `engine.go` treat `unknown` and
`down` identically *for the enforcement decision* (both → `notInUse`, no TU accrual),
so this does not cause wrong blocking. But it is still a real defect:

- `state.go` records a per-tick `stateHistory` ring (`-1 unknown / 0 down / 1 up`)
  which the WeekView renders as coloured dots — grey for unknown. With this bug the
  UI will never show a grey dot, so an outage of the router, a typo'd password, or a
  missing `config.json` is **visually indistinguishable from "the kids aren't
  playing"**. That history strip is described in DESIGN as the operator's main
  window into what the device driver is seeing; this bug blinds it permanently.
- Three separate places in this repo document the `unknown` behaviour and are
  therefore wrong today: `README.md:44-45`, the `getState.sh` header comment, and
  the `cmd_state` docstring (`xbox.py:363-364`).

**Proposed fix.** Distinguish "the router answered and there is nothing to see" from
"we could not ask the router".

```python
class RouterUnreachable(Exception):
    """The router could not be queried at all — state is genuinely unknown."""

def _conntrack_flows(fw, ip):
    """Return conntrack lines mentioning ip.

    Raises RouterUnreachable if no source could be consulted; returns [] only
    when a source WAS consulted and legitimately reported no matching flows.
    """
    last_err = None
    consulted = False
    for cmd, params in attempts:
        try:
            r = fw.file_exec(cmd, params)
        except Exception as e:
            last_err = e
            continue
        if r.get("code") == 0:
            consulted = True
            return [l for l in (r.get("stdout") or "").splitlines() if ip in l]
    if not consulted:
        raise RouterUnreachable(last_err or "no conntrack source available")
    return []

def cmd_state(fw, device, _args):
    try:
        active, _ports = _active_flows(fw, device["ipv4"])
    except Exception as e:
        print("unknown")
        print(f"xbox state: unknown: {e}", file=sys.stderr)
        return 1          # DESIGN.md asks for exit 1 on unknown
    print("up" if active else "down")
    return 0
```

Note the difference between "returned 0 rows" and "could not ask" is the whole point;
today the code cannot tell them apart.

**Caveat on stderr.** Upstream calls the script with Go's `cmd.Output()`, which
buffers stderr internally and **discards it on success**. So stderr diagnostics from
`getState.sh` never reach kidsout's log on the success path. Combined with
`getState.sh`'s own `2>/dev/null`, they go nowhere at all. If diagnostics matter, the
driver should append to its own log file (e.g. `devices/xbox/xbox.log`, size-capped)
rather than rely on stderr. Recommend adding this in v2 — without it, debugging a
misbehaving `getState` in production is guesswork.

---

### F-03 — `getState.sh` runs for 61 seconds against the contract's 10-second timeout

**Severity:** Blocker.

**Measurement.** Using the timeout value the project actually ships in
`config.example.json` (`"timeout": 10`) and an unroutable router address:

```
$ /usr/bin/time -f "elapsed=%es" ./getState.sh
down
elapsed=61.18s
```

**Root cause — the timeout multiplies three ways.**

1. `_conntrack_flows` makes up to **3** attempts (`conntrack -L`,
   `/proc/net/nf_conntrack`, `/proc/net/ip_conntrack`).
2. Each attempt calls `file_exec` → `call` → `login` → `probe`, and `probe`
   (`xbox.py:144-158`) iterates **2** `ubus_path_candidates` from `device.json`.
3. Each of those POSTs uses `self.timeout` = **10 s**.

3 × 2 × 10 s = 60 s, plus overhead. The failed login is never memoised — `login`
short-circuits only on `if self.session:` (`xbox.py:162-163`), and on failure
`self.session` stays `None`, so attempt 2 and attempt 3 each redo the full candidate
sweep from scratch.

**Where the README's claim comes from.** `README.md:46-49` and the `getState.sh`
header both state *"worst case ~4s if the router is unreachable (1s×3-attempt
fallback chain)"*. That figure is only reachable with `config.test.json`'s
`"timeout": 1` — the mock configuration — not with the `config.example.json` the
setup instructions tell you to copy. The documented budget was measured against the
wrong config file.

**Contract impact.** `engine.go` defines `const scriptTimeout = 10 * time.Second` and
runs the script under `exec.CommandContext`. At 10 s the context fires, the process
is killed, `cmd.Output()` returns an error, and `getState` returns `unknown`. So the
*outcome* is coincidentally correct (`unknown` — the one place the driver gets
`unknown` right is by being killed). But:

- Every minute a python process is spawned, hangs for 10 s, and is SIGKILLed. Because
  `EvaluateAll` launches one goroutine per device and `wg.Wait()`s on all of them,
  a hanging xbox `getState.sh` **stalls the entire evaluation tick for all devices**
  for the full 10 s.
- Any *partial* slowness — a router that responds in 3 s rather than instantly — is
  also multiplied by 6 and blows the budget, converting a working setup into
  permanent `unknown`.
- The healthy path is fine (measured ~0.2 s against the mock), so this only bites
  during exactly the incident you most want observability for.

**Proposed fix.** Give the state path its own, much tighter budget, and stop the
multiplication:

1. Add a dedicated `state_timeout` (default **2 s**) used by `cmd_state`, separate
   from the interactive commands' timeout.
2. Memoise login/probe failure on the `Router` instance (`self._login_failed`) so
   attempts 2 and 3 fail instantly instead of re-sweeping.
3. Reduce the fallback chain. `/proc/net/ip_conntrack` has not existed since Linux
   2.6.x and can simply be dropped. `conntrack -L` requires `conntrack-tools`, which
   is not installed by default on OpenWrt — determine once, on the real router,
   which source exists, record it in `device.json`, and query only that one.
4. Enforce a hard wall-clock ceiling inside `cmd_state` (e.g. `signal.alarm(8)` or a
   deadline checked between attempts) so the driver *always* returns before kidsout
   kills it, and returns a truthful `unknown` rather than being SIGKILLed.
5. Trim `ubus_path_candidates` to the single working path once known (see F-13).

Target: worst case ≤ 3 s, typical ≤ 0.3 s.

---

## 3. High — "will it actually control a real Xbox?"

These are the findings that a purely contract-level review would miss. The driver can
be perfectly well-formed and still fail to control the console.

### F-04 — IPv6 is not blocked at all

**Root cause.** All three rules match on IPv4 literals only (`xbox.py:235-248`):
`src_ip: "192.168.2.172"` / `dest_ip: "192.168.2.172"`. No `family` option is set, so
fw3/fw4 infers IPv4 from the address form and generates IPv4 rules only.

**Why this matters specifically for an Xbox.** Xbox Live is aggressively
IPv6-preferring; the console will use native IPv6 whenever the LAN offers it, and
falls back to Teredo tunnelling otherwise. If the ISP delegates a prefix and the
router advertises it, the console holds a global IPv6 address that **none of these
rules touch**. The block then looks perfectly healthy on the router — three enabled
REJECT rules, `uci` clean, no errors anywhere — while the console keeps playing.
This is the most dangerous class of bug in a parental control: silent, and it fails
open.

**Proposed fix.** Key the rules on the **MAC address** instead of the IPv4 address:

```python
        (f"{p}_fwd_out", {
            "name": desc, "src": zone, "dest": wan,
            "src_mac": device["mac"],          # covers IPv4 AND IPv6
            "proto": "all", "target": "REJECT", "enabled": ENABLED_WHEN_ALLOWED,
        }),
```

This has three advantages beyond IPv6 coverage:

- It removes the dependency on the static DHCP reservation (README setup step 2). The
  IP can drift without silently disarming the block.
- It survives the console being moved between LAN and guest SSIDs.
- It is the natural identity for a physical device.

The inbound rule (`_fwd_in`) cannot match on `src_mac` for wan→lan traffic; for that
direction, either keep an IP-based rule *plus* an IPv6 counterpart, or rely on the
outbound block alone (nothing useful reaches a console that cannot send). Given
finding F-08, the honest simplification is: **one outbound MAC-based REJECT rule is
sufficient**, and the other two should be justified or deleted.

**Blocked on:** confirmation that the ISP provides IPv6 (open question Q1), and
filling in `device.json`'s `"mac": null` (already tracked in the README TODO).

### F-05 — Enabling the REJECT rules does not cut a session already in progress

**Root cause.** OpenWrt's generated ruleset accepts established/related flows at the
top of the forward chain, before any user rule is evaluated. Under fw4 the generated
`chain forward` begins with a `ct state established,related accept` statement; fw3
emits the equivalent `-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT`.

Therefore enabling `kidsout_xbox_fwd_out` stops **new** connections only. A game
session, a party chat, or a 4K stream that is already established keeps running until
its conntrack entry expires — which for an ESTABLISHED TCP flow with `[ASSURED]` is
on the order of **5 days** by default (`nf_conntrack_tcp_timeout_established`).

**Practical consequence.** From the kid's point of view, "block" does nothing while
they are mid-game. That is precisely the moment enforcement is supposed to work.

**Interaction with F-06 and the README.** `README.md:50-52` currently asserts:

> NOTE: if the console's firewall rules are DISABLED (blocked), conntrack flows
> drain, so state reads `down` while blocked

Both halves of that sentence are wrong. "Rules DISABLED = blocked" is the F-01
inversion. And flows do **not** drain — pre-existing entries persist for days. So
after a (correctly polarised) block, `getState.sh` will keep reporting `up` from
stale conntrack entries, kidsout will keep firing `block.sh` every minute (F-09), and
the state history will lie.

**Proposed fix.** Flush the console's conntrack entries immediately after enabling
the block, using the `file exec` channel the driver already has:

```python
def cmd_block(fw, device, _args):
    _set_enabled(fw, device, "1")
    _flush_conntrack(fw, device)

def _flush_conntrack(fw, device):
    """Drop existing flows so the block takes effect immediately, not in ~5 days."""
    ip = device["ipv4"]
    for cmd, params in (("/usr/sbin/conntrack", ["-D", "-s", ip]),
                        ("/usr/sbin/conntrack", ["-D", "-d", ip])):
        try:
            fw.file_exec(cmd, params)
        except Exception:
            pass   # best effort; the rules are already in force for new flows
```

Note this reintroduces the `conntrack-tools` dependency. If the package is absent,
the alternatives are `echo f > /proc/net/nf_conntrack` (flushes the **whole** table —
disruptive to the entire household, not acceptable) or an nftables
`ct state` based drop. **Recommendation: install `conntrack-tools` on the router and
depend on it.** It also makes the F-03 fallback chain unnecessary, since
`conntrack -L` then reliably exists.

**Verify on hardware:** `nft list chain inet fw4 forward | head` (fw4) or
`iptables -S FORWARD | head` (fw3) to confirm the early accept, and
`conntrack -L -s 192.168.2.172 | wc -l` before/after a block to confirm the flush.

### F-06 — Xbox Instant-On standby will read as `up` forever

**Root cause.** The state signal is "≥ 2 concurrent conntrack flows" (see F-07 for
why that is the *effective* metric rather than the documented one). The Xbox Series S
ships with **Instant-On** as the default power mode, in which the console maintains
persistent connections to Xbox Live while apparently switched off — for background
downloads, remote wake, and presence.

A standby console therefore comfortably clears a 2-flow threshold.

**Impact on kidsout's core loop.** This is not a cosmetic issue. In `engine.go`,
`decideStatus` returns `StatusInUse` when `upState == "up"` and nothing else blocks,
and `EvaluateAll` then does `day.TUMinutes++`. So a console sitting in standby all
afternoon **burns the day's entire time allowance** without anyone touching a
controller. The kid then arrives to find `blockedNoTime`. This will be reported as
"the app is broken" and it will be the single most visible failure of the deployment.

**Proposed fix — measure bytes, not flows.** The README already sketches the right
mechanism in its *Traffic accounting* section; it just is not wired up:

```
nft add table inet kidsout
nft add chain inet kidsout accounting { type filter hook forward priority -150 \; }
nft add rule inet kidsout accounting ip  saddr 192.168.2.172 counter
nft add rule inet kidsout accounting ip  daddr 192.168.2.172 counter
nft add rule inet kidsout accounting ip6 saddr <console-v6> counter    # see F-04
```

Then `getState` becomes: read the counters, compare against the value persisted from
the previous tick (kidsout calls every 60 s, so the delta *is* a per-minute byte
rate), and report `up` above a threshold. Suggested starting point: **> 200 KB/min**
sustained. Standby presence traffic is a few KB/min; active gameplay or streaming is
orders of magnitude more. Calibrate on the real console (see §8 test plan).

Practical advantages: one cheap read instead of parsing the conntrack table, no
`conntrack-tools` dependency for the *state* path, naturally resistant to idle
remnants, and trivially explainable to a parent ("is it actually using bandwidth?").

State to persist between ticks: last counter value + timestamp, in a small JSON file
next to the driver. Handle counter reset (router reboot / `nft flush`) by treating a
decrease as "no reading this tick" → `unknown`.

**Interim mitigation if byte-counting is deferred:** raise the flow threshold
substantially and require the flows to be *fresh*, and/or set the console to
Energy-Saving power mode (open question Q2). But threshold tuning is a losing game
against a console designed to stay connected; the counter approach is the real fix.

---

## 4. Medium

### F-07 — The source-port regex never matches; the threshold works by accident

**Root cause.** `_active_flows` (`xbox.py:337-353`) intends to count the console's
distinct *source* ports:

```python
            for m in re.finditer(r"src=" + re.escape(ip) + r"\s+sport=(\d+)", line):
                ports.add(int(m.group(1)))
```

But `/proc/net/nf_conntrack` emits the tuple as
`src=<ip> dst=<ip> sport=<n> dport=<n>` — `src=` is followed by `dst=`, never
directly by `sport=`. The regex therefore matches **zero times, always**.

**Evidence.** Running `test_state_logic.py` prints the port set it built:

```
flows for xbox: 2
active: True ports: [('d', 443), ('d', 3074), ('d', 50001), ('d', 50002)]
```

Every element is a `("d", …)` tuple from the `dport` loop. There is not a single bare
integer, which is what the `sport` branch would have contributed.

**What the metric actually is.** Only this loop contributes:

```python
            for m in re.finditer(r"dport=(\d+)", line):
                ports.add(("d", int(m.group(1))))
```

`re.finditer` scans the *whole line*, and each nf_conntrack line contains **two**
tuples (original and reply). The reply tuple's `dport` is the original tuple's
`sport`. So one flow contributes two entries — e.g. the TCP line yields
`('d',443)` and `('d',50001)`. With `min_ports=2` and a strict `>`, the effective
rule is:

> **`up` ⟺ the console has ≥ 2 concurrent conntrack flows.**

which is *roughly* what was wanted, arrived at by two bugs partially cancelling.

**Secondary problems in the same function.**

- The set mixes `int` and `tuple` element types. Harmless only because the `int`
  branch is dead; it becomes an inconsistency the moment F-07 is "fixed" naively.
- `_conntrack_flows` filters lines with a bare substring test `if ip in l`. For
  `192.168.2.172` this also matches a hypothetical `192.168.2.1720`, and matches the
  address wherever it appears — including in the reply tuple of a flow belonging to a
  different host that happens to be NATed toward it. Low practical risk on a /24, but
  it should be anchored: `re.search(r"(?:src|dst)=" + re.escape(ip) + r"\b", line)`.
- The `"established" in low or ("udp" in low and "unreplied" in low)` filter is
  applied to the lowercased whole line. `ESTABLISHED` appears as a TCP state word, so
  this works, but a line containing the substring in any other position would also
  pass. Parse the state field positionally instead.

**Test quality note.** `test_state_logic.py` check #3 asserts
`len(ports3) <= 2, 'single flow should stay below threshold'` and prints
`single-flow port-set size: 2`. It **passes for the wrong reason** — the 2 comes from
the two `dport` matches on one line, not from one source port plus one dest port as
the test's construction implies. A test that passes for the wrong reason is worse
than no test: it certified the parsing as correct. Any v2 must re-derive these
assertions from a documented metric definition.

**Proposed fix.** Decide the metric first, then implement it. If F-06's byte-counter
approach is adopted, this whole function is deleted. If conntrack parsing is kept,
parse fields properly rather than regex-scraping a whole line:

```python
def _parse_conntrack_line(line):
    """nf_conntrack line -> (proto, state, orig_tuple, reply_tuple) with fields
    split into dicts; the ORIGINAL tuple is the first src=/dst=/sport=/dport= group."""
```

and count `(proto, orig.dst, orig.dport)` triples — distinct *remote services* the
console is talking to — which is a defensible definition of "in use" and is immune to
the reply-tuple double count.

### F-08 — `kidsout_xbox_input_out` is a forward rule, not the input rule its name claims

**Root cause.** The rule (`xbox.py:244-247`) is:

```python
        (f"{p}_input_out", {
            "name": desc, "src": zone, "dest": "*", "src_ip": ip, "proto": "udp",
            "dest_port": "53 123", "target": "REJECT", "enabled": "1",
        }),
```

Per the OpenWrt firewall documentation, the presence of `dest` — **including
`dest='*'`** — makes the rule match *forwarded* traffic. Only a rule with `src` and
**no** `dest` at all matches traffic addressed to the router itself (input).

**Consequences.**

1. The rule does **not** do what its name says. Traffic from the console to the
   router's own dnsmasq (`192.168.2.1:53`) and to any NTP service on the router is
   untouched.
2. It is a strict subset of `kidsout_xbox_fwd_out` (which is `proto: all` from the
   same source zone), so it contributes nothing. It is pure noise in the ruleset and
   in `status` output.

**Does it matter for enforcement?** Not much, on its own: blocking forwarding already
kills internet connectivity, and letting the console resolve names while unable to
reach anything is merely untidy. But it matters for *review confidence* — a rule that
does not do what it is named after is a landmine for the next person, and it inflates
`_set_enabled`'s per-minute uci write count by 50 % (F-09).

**Proposed fix — pick one:**

- **Delete it.** Cleanest. `_fwd_out` covers the case.
- **Or make it a genuine input rule** by omitting `dest` entirely:
  ```python
  {"name": desc, "src": zone, "src_ip": ip, "proto": "udp",
   "dest_port": "53 123", "target": "REJECT", "enabled": ...}
  ```
  Only worth it if you want the console to lose DNS resolution too (which produces a
  faster, more obvious "no connection" error on the console than a silent blackhole —
  arguably better UX for the kid, who then sees "check your network" rather than an
  endless spinner).

Whichever is chosen, rename the section so the name matches the behaviour. Note that
renaming a uci section means the old one must be deleted — add a migration step to
`install` that removes `kidsout_xbox*` sections not in the current rule set.

### F-09 — `uci commit` + firewall reload every 60 seconds wears the router's flash

**Root cause.** Per `engine.go`, `block.sh` runs on **every tick** in which the device
status starts with `blocked` **and** `getState.sh` returned `up` — not just on the
transition edge. (`unblock.sh` *is* edge-triggered; `block.sh` is not.)

`_set_enabled` (`xbox.py:280-288`) unconditionally performs, on each call:

- 3 × `uci set` (one per section — 2 if F-08's redundant rule is removed)
- 1 × `uci commit firewall`
- 1 × `uci apply {"rollback": false}`

`uci commit` rewrites `/etc/config/firewall`, which on OpenWrt lives on the
**overlay JFFS2/UBIFS partition in flash**, and per `rpcd/uci.c` it also fires
`rpc_uci_trigger_event()` — a service reload. `uci apply` then triggers the reload
event a second time. So the current code, while a device is blocked and reporting up,
performs a flash write and two firewall reloads **every minute**, indefinitely.

At one write per minute that is ~525,600 rewrites of that file per year against
flash rated for a few thousand erase cycles per block. Even with wear levelling this
is an unnecessary and easily avoided hazard on consumer router hardware. The
per-minute `fw4 reload` also briefly flushes and rebuilds the entire ruleset, which
can perturb other traffic in the house.

**Note on the F-01 interaction:** today the inverted block never actually blocks, so
`getState` never stops saying `up`, so this fires forever. After F-01 and F-05 are
fixed, a successful block drains the flows and `getState` goes `down`, which stops
`block.sh` being called. So fixing F-01/F-05 *mostly* fixes F-09 as a side effect —
but only "mostly", and relying on that is fragile.

**Proposed fix.** Make `_set_enabled` genuinely idempotent by reading before writing:

```python
def _set_enabled(fw, device, value):
    existing = find_sections(fw, device["rules"]["prefix"])
    if not existing:
        sys.exit("no kidsout_xbox firewall sections found — run './xbox.py install' first")
    stale = {n: o for n, o in existing.items() if o.get("enabled") != value}
    if not stale:
        return          # already in the desired state: no write, no commit, no reload
    for name in stale:
        fw.uci_set("firewall", name, {"enabled": value})
    fw.uci_commit("firewall")
    fw.uci_apply(rollback=False)
```

`find_sections` already performs a `uci get firewall`, so the read costs nothing
extra beyond what `block`/`allow` were going to do anyway. This turns the steady
state into one read-only RPC per minute.

Consider also dropping the explicit `uci_apply` after `uci_commit`: per `rpcd/uci.c`,
`rpc_uci_commit` already calls `rpc_uci_trigger_event()` for the committed package,
so the subsequent `apply` is a redundant second reload. Verify on hardware before
removing — if the reload does not happen, the rules are committed to disk but not in
force, which is a silent enforcement failure. **Test this explicitly** (see §8).

### F-10 — No reconciliation: a lost kidsout state strands the console in `blocked`

**Root cause.** `unblock.sh` is edge-triggered — `engine.go` only queues it on the
`prev starts with "blocked"` → `new does not` transition. The router's firewall state
is therefore a piece of state that lives *outside* kidsout, and nothing ever
reconciles the two.

**Failure scenarios.**

- kidsout is stopped, upgraded, or crashes while a device is blocked. On restart,
  `runtimestore.yaml` may load a non-blocked status; the transition never happens; the
  REJECT rules stay enabled and the Xbox is blocked with nothing scheduled to
  un-block it.
- `runtimestore.yaml` is deleted or reset (its own docs describe it as hand-editable).
- Someone toggles `enforcementOFF` in the UI while the process is down.
- Someone runs `./xbox.py block` manually and forgets.

Recovery requires a human running `./unblock.sh` — and knowing that they need to.

**Proposed fix.** Do not rely solely on the edge. Options, in increasing order of
robustness:

1. **Document the manual escape hatch** prominently in the README: *"if the Xbox is
   stuck offline, run `devices/xbox/unblock.sh`"*. Cheapest; do this regardless.
2. **Have `getState.sh` report the enforcement state it observes.** It already reads
   the router each tick; have it note in its log file whether the rules are enabled,
   so a mismatch is at least visible.
3. **Make `unblock.sh` safe to over-call and call it defensively.** Since F-09 makes
   it a no-op when already in the desired state, the driver could enforce desired
   state on every invocation. This needs upstream cooperation (kidsout would have to
   call `unblock.sh` on every non-blocked tick rather than only on the edge) — worth
   raising as an upstream feature request, since it affects every device driver, not
   just this one.
4. **A `fail-open` watchdog**: a cron entry on the router that clears the
   `kidsout_xbox*` rules if they have been enabled for more than N hours without a
   heartbeat file being touched. Belt and braces; consider only if 1–3 prove
   insufficient in practice.

---

## 5. Low

### F-11 — `alt_hosts` is never used by the driver

`Router.__init__` (`xbox.py:95-108`) builds the host list —

```python
        hosts = [device["router"]["primary_host"]] + device["router"].get("alt_hosts", [])
        ...
        self.base = f"{scheme}://{cfg.get('host') or hosts[0]}:{port}"
```

— and then uses only `hosts[0]`. `alt_hosts` is iterated **only** by `cmd_probe`
(`xbox.py:420-445`), a diagnostic command. So the WireGuard-side address
`192.168.255.7` is never a real fallback for `block`/`allow`/`state`.

`README.md:110-111` claims otherwise ("also tries ... the WG-side 192.168.255.7").

**Fix:** iterate `hosts` in `_candidate_urls`, or delete `alt_hosts` and correct the
README. Given F-03's timeout budget, iterating more hosts must be paired with a much
shorter per-attempt timeout — do not add hosts without fixing F-03 first.

### F-12 — No HTTP fallback; a closed 443 produces a misleading error

`cfg.get("https", True)` defaults to HTTPS, and `config.example.json` sets
`"https": true`. Stock OpenWrt does **not** serve TLS unless `luci-ssl` (or
`uhttpd-mod-tls`) is installed, so on a default install nothing listens on 443, every
candidate URL fails, and `probe` raises:

```
no working ubus endpoint found (router unreachable or uhttpd-mod-ubus missing)
```

which points the operator at the wrong package. `cmd_probe` tries both schemes and
would reveal the truth, but only if the operator thinks to run it.

**Fix:** have `_candidate_urls` yield `https` then `http` variants (or auto-detect
once and cache the winner into a small `.state.json`), and make the error message
enumerate what was actually tried.

### F-13 — `/cgi-bin/luci/rpc/ubus` is not a ubus endpoint

`device.json`'s `ubus_path_candidates` lists `/cgi-bin/luci/rpc/ubus` as a fallback.
That path belongs to `luci-mod-rpc`, a **different** JSON-RPC API with a different
request shape and its own `?auth=<token>` query-parameter authentication. It is not
interchangeable with `uhttpd-mod-ubus`'s `/ubus`.

Practically it is harmless — it 403s or 404s and `probe` moves on — but it doubles
the F-03 timeout multiplier for no benefit, and if it ever returned a 200 with a JSON
body, `probe` would latch onto it (`probe` accepts *any* parseable JSON response as
proof of life) and every subsequent call would fail confusingly.

**Fix:** remove the candidate. Once the working path is confirmed on the real router,
reduce the list to exactly one entry.

### F-14 — `cmd_check` probes ports the console does not listen on

`cmd_check` (`xbox.py:466-472`) TCP-connects to ports 53, 80, 443 and 3074 **on the
Xbox**. Those are *outbound* destination ports the console talks *to*; the console
does not run a DNS server, a web server, or a TLS listener. The output will read
`closed/filtered` on every line, always, whether the console is on, off, or blocked —
so the command cannot distinguish any of those states.

Port 3074 is the one plausible listener (Xbox Live uses it bidirectionally), but it is
**UDP**, and the code's own comment concedes only the TCP half is testable.

**Fix:** either delete the command, or replace it with something that actually
discriminates — an ARP-table lookup via `ip neigh` on the router (which works even
though the console ignores ICMP, per the README's own finding) is the honest test of
"is this device present on the LAN".

### F-15 — One rpcd session leaked per script invocation

`login` (`xbox.py:161-173`) requests `"timeout": 900`. Every `getState.sh` /
`block.sh` invocation is a fresh process, so it creates a fresh session and never
logs out. At one invocation per minute with a 900 s idle timeout, roughly **15 stale
sessions** are resident in rpcd at any moment.

Minor on a router with adequate RAM, but it is unbounded if the timeout is ever raised
and it clutters `ubus call session list`.

**Fix:** request a short session timeout (`"timeout": 30`) for the short-lived script
paths, or call `session destroy` before exit. Requesting 900 s makes sense only for
an interactive session that will be reused.

---

## 6. Contract compliance — what is already correct

Verified against the upstream Go source, not the README. These are the parts the next
version must **not** regress.

| Requirement | Source of truth | Status |
|---|---|---|
| Device dir contains `getState.sh`, `block.sh`, `unblock.sh` | `state.go:239` `DiscoverDevices` | ✅ all three present |
| All three are executable | `exec.CommandContext` needs the exec bit; `DiscoverDevices` only `os.Stat`s, so a non-executable script passes discovery and fails at run time | ✅ all `0775` — **keep the exec bit when committing to git** |
| Directory name == `deviceName` | `state.go` `DiscoverDevices` returns `e.Name()` | ✅ `xbox` |
| Scripts must self-locate | `engine.go:44` — `exec.CommandContext(ctx, filepath.Join(...))` with **`cmd.Dir` never set**, so the cwd is kidsout's own | ✅ all three do `cd "$(dirname "$0")"` — this is load-bearing, not decoration |
| `getState.sh` prints one word | `engine.go:53-61` | ✅ |
| Trailing newline tolerated | `strings.TrimSpace(string(out))` | ✅ |
| Only `up`/`down` accepted; anything else → `unknown` | `engine.go:56-61` `switch out` | ✅ (and `unknown` maps through the default branch to `unknown`, so emitting the literal string works) |
| Non-zero exit → `unknown` | `engine.go:54` `if err != nil` | ✅ — DESIGN asks for exit 1 on unknown, Go accepts either exit code |
| `block.sh`/`unblock.sh` exit 0 on success | `engine.go:194-197` logs failures | ✅ (`set -euo pipefail` + `exec`, so python's exit code propagates) |
| Extra files in the device dir are harmless | `DiscoverDevices` only `os.Stat`s the three names | ✅ `xbox.py`, `device.json`, mocks, README all fine |
| Scripts run under a 10 s timeout | `engine.go:13` `const scriptTimeout = 10 * time.Second` | ❌ see F-03 |
| `block.sh` is called repeatedly, `unblock.sh` once on the edge | `engine.go:163-181` | ⚠️ satisfied, but see F-09 |
| Idempotency | implied by the calling pattern | ⚠️ logically idempotent, but not *cheaply* so — see F-09 |

Two subtleties worth recording for v2:

- **stderr is swallowed.** `cmd.Output()` buffers stderr into `ExitError.Stderr` and
  discards it entirely on success. Any diagnostic written to stderr on the happy path
  is unrecoverable. `getState.sh`'s `2>/dev/null` is therefore not the problem — the
  harness is. Use a driver-owned log file (see F-02).
- **A slow `getState.sh` stalls every device.** `EvaluateAll` fans out one goroutine
  per device and `wg.Wait()`s. A 10 s xbox timeout delays the whole tick, including
  the TV and tablet. This raises the priority of F-03 beyond "this one device".

---

## 7. Security notes

### S-01 — A private key file is checked into the working tree

`mock_key.pem` (mode `0600`, 1704 bytes) is an RSA private key sitting in the project
directory. It is a throwaway self-signed key for the mock HTTPS server and it *is*
listed in `.gitignore`, so there is no real exposure — but keeping key material on
disk as a build artefact is a habit worth not forming, and a `.gitignore` entry is one
`git add -f` away from failing.

**Fix:** have `mock_router.py` generate the cert/key into a temp directory at startup
and remove them on exit. It already contains the `openssl req` invocation; it just
needs to target `tempfile.mkdtemp()` instead of `os.path.dirname(__file__)`.

Also: `__pycache__/xbox.cpython-311.pyc` is present in the working tree. Gitignored,
but worth cleaning.

### S-02 — The router's **root** password will live in plaintext `config.json`

`config.example.json` instructs the operator to put the router's root credentials into
`config.json`. That file is gitignored, but nothing enforces its permissions.

**Fix:**
- `chmod 600 config.json`, and have `xbox.py` refuse to run (or at least warn loudly)
  if the file is group- or world-readable.
- Better: stop using `root`. Create a dedicated rpcd user with an ACL scoped to
  exactly what the driver needs — `uci` read/write on `firewall`, and `file exec`
  on the specific conntrack/nft commands. A leaked credential then costs you the
  ability to toggle three firewall rules, not the router.
- Consider reading the password from an environment variable or a systemd credential
  instead of a file, if the deployment allows it.

### S-03 — `verify_tls: false` permits a LAN MITM to capture that password

The default is `"verify_tls": false`, and `_post` (`xbox.py:115-119`) explicitly
disables both hostname checking and certificate verification. Against a router with a
self-signed certificate this is the pragmatic default, but it does mean anything on
the LAN able to intercept the connection can harvest the credentials from S-02.

**Fix:** pin the router's certificate — store its fingerprint in `device.json` and
verify against that. Retains the self-signed cert while closing the MITM window. Low
priority relative to S-02 (which removes most of the value of the intercept), but the
two together are the right end state.

---

## 8. Recommended shape for v2

Not prescriptive, but this is the design the findings point to.

### 8.1 Correctness changes (must)

1. **F-01** — invert `block`/`allow`; install in the ALLOWED state; print meanings,
   not uci values.
2. **F-02** — introduce `RouterUnreachable`; `cmd_state` emits `unknown` + exit 1 on
   any failure to consult the router.
3. **F-03** — dedicated 2 s `state_timeout`, memoised login failure, single conntrack
   source, hard wall-clock ceiling below 10 s.
4. **F-04** — rules keyed on `src_mac`; fill in `device.json.mac`.
5. **F-05** — flush the console's conntrack entries after a successful block.

### 8.2 Behavioural changes (should)

6. **F-06** — replace flow-counting with nft byte counters; persist last reading;
   threshold calibrated on the real console.
7. **F-09** — `_set_enabled` reads current state and writes nothing when already
   correct.
8. **F-08** — delete or correctly reformulate the third rule; add a migration that
   removes obsolete `kidsout_xbox*` sections on `install`.
9. **F-10** — document the manual `unblock.sh` escape hatch; raise the reconciliation
   gap upstream.

### 8.3 Hygiene (nice)

10. F-11/F-12/F-13 — collapse host/scheme/path discovery into one cached
    auto-detection, and correct the README.
11. F-14 — rewrite `check` around `ip neigh` on the router, or delete it.
12. F-15 — short session timeout for script-path logins.
13. S-01/S-02/S-03.
14. Add a driver-owned, size-capped log file — without it, F-02's diagnostics have
    nowhere to go.

### 8.4 Documentation corrections required

The current README states several things that are not true of the current code.
All of these must be re-verified against v2 rather than carried forward:

| Location | Claim | Reality |
|---|---|---|
| `README.md:44-45` | router unreachable → `unknown` | always `down` (F-02) |
| `README.md:46-49` | worst case ~4 s | measured 61 s (F-03) |
| `README.md:50-52` | conntrack flows drain while blocked → reads `down` | flows persist for days (F-05); and "rules DISABLED = blocked" is the F-01 inversion |
| `README.md:54-57` | "`block.sh` already satisfied the upstream contract, so it is unchanged" | it satisfies the *shape*, but inverts the *meaning* (F-01) |
| `README.md:110-111` | "also tries ... the WG-side `192.168.255.7`" | never tried outside `probe` (F-11) |
| `xbox.py:265` | "rules active and ENABLED (internet allowed)" | installs a block (F-01) |
| `xbox.py:337-353` docstring | counts distinct local source ports | source-port regex never matches (F-07) |

### 8.5 Test plan for v2

**Offline (must pass before touching the router):**

- `test_block_semantics.py` — after `cmd_block`, every `kidsout_xbox*` section has
  `target == "REJECT"` and `enabled == "1"`; inverse after `cmd_allow`; after
  `cmd_install`, all sections exist with `enabled == "0"`. *This is the test whose
  absence allowed F-01 to ship.*
- `cmd_state` returns `unknown` (exit 1) for: unreachable host, refused connection,
  404 endpoint, bad password, `file exec` denied, conntrack unavailable. Six cases.
- `cmd_state` completes in < 3 s in every one of those six failure cases, asserted on
  wall clock, using the **shipped** `config.example.json` timeout value — not the
  mock's. F-03 existed because the budget was only ever measured with the test config.
- Idempotency: calling `cmd_block` twice issues zero `uci set`/`commit`/`apply` calls
  the second time (assert against a call-counting mock).
- Conntrack parsing: derive assertions from a written definition of the metric, with
  fixtures for both directions of a flow, a UDP-unreplied entry, an unrelated host,
  and a host whose IP is a prefix of the console's.
- Extend `mock_router.py` to be able to *fail* — refuse logins, deny `file exec`,
  return non-zero `code`, respond slowly — so the failure paths are testable at all.
  Today it only models the happy path, which is why every failure-mode claim in the
  README went unverified.

**On hardware (needs Paulo at home, console present):**

1. `./xbox.py probe` — confirm which scheme/port/path actually answers; record it and
   prune `ubus_path_candidates` (F-12, F-13).
2. Confirm `rpcd-mod-file` is present and `file exec` is permitted for the login user.
3. `opkg list-installed | grep conntrack` — decide the conntrack strategy (F-03, F-05).
4. `nft list chain inet fw4 forward | head` — confirm the early
   `ct state established,related accept` (F-05).
5. `./xbox.py discover` with the console **on** → fill `device.json.mac` (F-04).
6. Check for a global IPv6 address on the console (`ip -6 neigh` on the router, or the
   console's own network settings) → decides whether F-04 is a blocker (Q1).
7. `install` → verify the console is **still online** (proves the F-01 fix).
8. `block` → verify an *in-progress* game or stream dies within seconds (proves F-05).
9. `unblock` → verify recovery.
10. **Commit/apply verification:** after a `block`, read the live ruleset
    (`nft list ruleset | grep kidsout` or `fw4 print`) — not just `uci show firewall` —
    to confirm the rules are actually *in force* and not merely committed to disk
    (F-09's open question about the redundant `apply`).
11. Console in **standby** for 30 minutes → what does `getState.sh` report each
    minute? Calibrate the F-06 threshold from the observed byte rate.
12. Console **actively gaming** for 10 minutes → observed byte rate; confirm the
    threshold cleanly separates the two.
13. Pull the router's power mid-tick → confirm `getState.sh` returns `unknown` within
    the budget and the WeekView shows grey dots.

---

## 9. Open questions

These change the answers above and should be settled before v2 is designed.

**Q1 — Does the ISP provide IPv6?**
If yes, F-04 is a hard blocker and the move to MAC-based rules is mandatory, not an
improvement. If no, F-04 can be deferred — but it becomes a latent trap the day the
ISP enables it, so MAC-based rules are still the right call.

**Q2 — Is the console in Instant-On or Energy-Saving power mode?**
Instant-On makes F-06 severe: standby traffic burns the daily allowance. Energy-Saving
makes the current flow-counting *tolerable* as an interim measure. Either way the
byte-counter approach is the durable answer, but the answer sets the urgency.

**Q3 — OpenWrt version (fw3 or fw4), and is `luci-ssl` installed?**
Determines the conntrack-flush syntax for F-05, whether `nft` is available for F-06's
counters, and whether `https: true` is reachable at all (F-12).

**Q4 — Is `old/kidsout.py` still a live caller, or is upstream kidsout now the only
consumer?**
It dispatches `block`/`allow` with the same inverted meaning and would inherit the
F-01 fix. If it is retired, then `allow.sh`, `status.sh`, and `install.sh` are
operator conveniences rather than contract surface, which simplifies what v2 must
guarantee — and `old/README.md`'s description of the semantics should be removed so it
does not contradict the new one.

**Q5 — Should the driver own a log file?**
F-02's diagnostics have nowhere to go given that upstream discards stderr. A
size-capped `devices/xbox/xbox.log` is the obvious answer, but it is a new artefact in
the device directory and worth an explicit decision.

**Q6 — Should reconciliation be pushed upstream?**
F-10 affects every kidsout device driver, not just this one. Worth an upstream issue
proposing that `unblock.sh` be called on every non-blocked tick (drivers being
required to make it idempotent) rather than only on the edge.
