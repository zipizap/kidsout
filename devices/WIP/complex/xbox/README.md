# kidsout device: Xbox

Device driver for the family Xbox (`xbox.infracon.ovh` → `192.168.2.172` on the
LAN), driven through the OpenWrt router's ubus-over-HTTP JSON-RPC API.
**Block = internet off, allow = internet on.**

This is v2. It is a rewrite against [REVIEW.1.md](REVIEW.1.md), which found that
v1 inverted its own block/allow semantics, could never report `unknown`, took
61 s to fail against a 10 s contract timeout, and did not block IPv6 at all.
[PROGRESS.md](PROGRESS.md) tracks what was changed and what is verified.

## Layout

```
devices/xbox/
├── device.json           device facts: IP/MAC, router hosts, thresholds
├── config.example.json   template for credentials  → copy to config.json
├── xbox.py               the driver (stdlib only, no pip dependencies)
├── router_bootstrap.sh   ONE-TIME, runs ON THE ROUTER: scoped user + helper
├── getState.sh block.sh unblock.sh    the upstream kidsout contract
├── allow.sh install.sh status.sh      operator conveniences
├── mock_router.py        offline test double (models failures, not just success)
├── test_*.py run_tests.sh  the offline suite — run before touching the router
└── xbox.log .state.json  driver-owned log and sample state (git-ignored)
```

## How enforcement works

One firewall rule, `kidsout_xbox_out`:

```
config rule 'kidsout_xbox_out'
        option name    'kidsout xbox: block internet'
        option src     'lan'
        option dest    'wan'
        option src_mac '<ethernet MAC>' '<wifi MAC>'
        option proto   'all'
        option target  'REJECT'
        option enabled '0'          <-- the toggle
```

| `enabled` | ruleset | meaning |
|---|---|---|
| `1` | the REJECT is in force | **internet BLOCKED** |
| `0` | the REJECT is absent | **internet ALLOWED** |

This is the polarity v1 had backwards. The driver never prints the raw value —
it prints `internet BLOCKED` / `internet ALLOWED`, because printing `enabled=0`
is what made the inversion easy to miss.

**Keyed on the MAC, not the IP** — and on *every* MAC the console has. An
IPv4-literal rule would not touch IPv6 traffic, and the block would look perfectly
healthy on the router while the console kept playing.

**The console has two MACs**, one for ethernet and one for WiFi, and it switches
between them whenever the cable is plugged or pulled. A rule naming only one of
them matches nothing while the console is on the other — the block fails open and
still reports success. fw4 parses `src_mac` as a list, so the single rule names
both and does not care which interface is in use today.

**Blocking also flushes conntrack — and that flush is the real mechanism.** This
router has flow offloading enabled *in hardware*. An established flow is forwarded
by the MediaTek PPE in silicon and never enters any netfilter hook, so enabling the
REJECT rule does nothing to a session already in progress. Reloading the firewall
does not help either: the conntrack entry survives, matches
`ct state established : accept`, and is immediately re-offloaded. Destroying the
conntrack entry is the *only* thing that cuts a live session.

So `block` enables the rule **and** flushes the console's flows — and if the flush
fails it **exits non-zero** rather than reporting a success it did not achieve.
This needs the `conntrack` package on the router (the OpenWrt package is named
`conntrack`, not `conntrack-tools`); `./xbox.py selftest` fails loudly if it is
missing.

## How state detection works

`getState.sh` prints one word: `up`, `down` or `unknown`.

The signal is the console's **outbound byte rate**, compared against
`device.json` → `state.threshold_bytes_per_min`:

```
rate = (xbox_out(now) - xbox_out(prev)) / (now - prev) * 60
up      rate >  threshold
down    rate <= threshold
unknown the rate is not computable — see below
```

**The bytes come from conntrack, not from an nft counter.** An nft counter in the
forward hook — which is what v2 shipped — cannot see a hardware-offloaded flow, so
it under-reports gameplay by roughly 98 % and would report `down` all afternoon.
fw4 declares its flowtable with `counter`, so the hardware's per-flow byte counts
are fed back into conntrack instead. The helper sums those router-side into a
**monotonic** total; it has to accumulate rather than sum naively, because
conntrack entries vanish when a flow expires and a bare sum goes *backwards*
(measured at −510 KB/min during real gameplay).

Counting *connections* instead (what v1 did) reports Instant-On standby as
in-use around the clock, which silently burns the whole daily allowance while
nobody is playing.

The threshold is **calibrated, not guessed** — measured against the real console:
idle 20.8 KB/min, downloading 93.7 KB/min, gaming 289.5 KB/min outbound. 200 KB/min
separates play from download with 3.1× margin. This also retires the worry that a
background download would read as "in use": it does not. See [F-06.md](F-06.md).

`unknown` is reported — and means it — when the router cannot be consulted at
all, when the login is rejected, when the helper or the counter table is missing,
when there is no previous sample yet, when the previous sample is stale
(> `max_sample_age_s`, so an hour of bytes is never divided into one minute), or
when the counters went backwards after a router reboot. Upstream treats `unknown`
like `down` for enforcement, but records it as a grey dot in the WeekView, which
is how an outage stays distinguishable from a quiet afternoon.

**Timeout budget.** kidsout kills scripts at 10 s, and because `EvaluateAll`
waits on every device, a slow `getState.sh` stalls the tick for the TV and tablet
too. The state path therefore has its own budget — `state_timeout` (1.5 s per
request) and a hard `state_deadline` (4 s wall clock) — a single endpoint path, a
memoised login failure so a dead router is not swept repeatedly, and no fallback
chain. Every failure mode in the offline suite returns inside that deadline,
measured with the timeouts from the **shipped** `config.example.json`. v1's
documented "~4 s worst case" was only ever measured against the test config; the
real answer with the shipped one was 61 s.

## One-time setup

### 1. On the router — create the scoped user

```
ssh root@192.168.2.1 'cat > /tmp/router_bootstrap.sh' < router_bootstrap.sh
ssh -t root@192.168.2.1 sh /tmp/router_bootstrap.sh
```

Two commands, and the second one must be interactive. Do **not** pipe the script
into ssh — ssh will not allocate a pseudo-terminal when its stdin is a redirect,
so the remote side has no `/dev/tty` and the password prompt fails. `scp` may not
work either: this router has no `sftp-server`, hence `cat >`.

It prompts for a **new** password (not root's), installs
`/usr/libexec/kidsout-xbox` and an rpcd ACL scoped to it, and prints the router's
facts. `sh /tmp/router_bootstrap.sh --uninstall` reverses everything.

Why a helper rather than a root credential, and what the ACL does and does not
permit: [S-02.md](S-02.md).

Also install the `conntrack` package if `conntrack_tools=no` in the printed facts —
without it, blocking cannot cut a session already in progress:

```
ssh root@192.168.2.1 'opkg update && opkg install conntrack'   # NOT 'conntrack-tools'
```

### 2. On this machine — credentials

```
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json          # username: kidsout, password: the one you chose
./xbox.py selftest           # verifies every capability the driver needs
./xbox.py pin                # record the router's certificate fingerprint (S-03)
```

### 3. Find the console's MAC and install

Turn the console on and make it talk to the network, then:

```
./xbox.py discover --write   # records EVERY interface into device.json
./xbox.py install            # creates the rule (ALLOWED) and registers the counters
```

`install` leaves the console **online**. Confirm it is still working before
testing enforcement — that is the check v1 would have failed.

### 4. Calibrate the threshold

Follow [F-06.md](F-06.md). Until then the driver uses a conservative default of
200 KB/min, which is a guess.

## Upstream kidsout contract

Upstream (`github.com/zipizap/kidsout`) scans `devices/<name>/` for three
executable scripts and runs them with `cmd.Dir` unset, so they self-locate with
`cd "$(dirname "$0")"`.

| script | called | must do |
|---|---|---|
| `getState.sh` | every minute | print `up`/`down`/`unknown` on stdout |
| `block.sh` | every tick while blocked **and** up | exit 0 |
| `unblock.sh` | once, on leaving the blocked state | exit 0 |

`block.sh` being called every tick is why it must be cheap when nothing needs
changing: the driver reads the current rule state first and writes nothing if it
already matches, so the steady state costs one read-only RPC and neither a flash
write nor a firewall reload. v1 rewrote `/etc/config/firewall` and reloaded the
firewall every 60 seconds indefinitely.

**stderr goes nowhere.** Upstream uses Go's `cmd.Output()`, which discards stderr
on the success path, so diagnostics are written to `xbox.log` (size-capped at
256 KB, rotated in place) instead. That file is the first place to look when
something misbehaves.

**If the console is ever stuck offline** — kidsout crashed or was upgraded while
the Xbox was blocked, `runtimestore.yaml` was reset, someone ran `block` by hand —
run `./unblock.sh`. It is safe at any time and safe to repeat. Nothing else
reconciles the router's state with kidsout's: `unblock.sh` is edge-triggered, so
the router holds enforcement state that lives outside kidsout's knowledge. The
driver mitigates this by logging what the router is actually enforcing every
15 minutes (`audit:` lines in `xbox.log`), which makes a stuck block visible, but
it does not fix it. Proposing that upstream call `unblock.sh` on every non-blocked
tick — drivers being required to make it idempotent, as this one is — would fix it
for every device driver, not just this one.

## Daily use

```
./xbox.py status      # endpoint, router facts, rule state, current byte rate
./xbox.py state       # the one word kidsout sees
./xbox.py block       # internet off  (enable REJECT + flush live flows)
./xbox.py allow       # internet on
./xbox.py check       # is the console present on the LAN right now
./xbox.py counters    # byte totals and the rate since the last sample
./xbox.py calibrate --minutes 30 --label standby
./xbox.py selftest    # after any router change
./xbox.py uninstall   # remove the rule and the accounting state (destructive)
```

`check` asks the router's neighbour table. v1 TCP-connected to ports 53/80/443
and 3074 **on the console**, which are ports it talks *to* and listens on none of,
so the answer was `closed/filtered` whatever the console was doing.

## Offline testing (no router, no credentials)

```
./run_tests.sh
```

| suite | covers |
|---|---|
| `test_block_semantics.py` | polarity, rule shape, migration, idempotency, conntrack flush |
| `test_state.py` | the up/down/unknown metric, stated explicitly then asserted |
| `test_failure_modes.py` | nine ways of failing, each → `unknown` inside the deadline |
| `test_contract.py` | the real `.sh` files as subprocesses, from another cwd |

`mock_router.py` models rpcd including its **failure** modes — refused logins,
ACL denials, 404s, non-JSON responses, helper failures, missing counters, slow
responses. v1's mock modelled only the happy path, which is why every
failure-mode claim in v1's README went unverified and was wrong.

Run the mock by hand with `python3 mock_router.py 8443`; its TLS key is generated
into a temp directory and removed on exit, rather than living in the working tree.

## How it talks to the router

ubus JSON-RPC over HTTPS at `https://192.168.2.1:443/ubus`, with
`192.168.255.7` (WireGuard side) as a genuine fallback and HTTP as a
last resort. The winning endpoint is cached in `.state.json` and only reused
while it still matches a configured host and port.

`/cgi-bin/luci/rpc/ubus` is **not** a fallback and has been removed from the
candidate list: it belongs to `luci-mod-rpc`, a different API with a different
request shape and its own `?auth=` token, and it 404s on this router. Leaving it
in doubled the failure-path timeout for nothing.

Requires on the router: `uhttpd-mod-ubus`, `rpcd`, `rpcd-mod-file`, and nftables
(fw4). `./xbox.py selftest` checks all of it.

## Status

Offline: 46 cases / 119 assertions passing. On hardware: see
[PROGRESS.md](PROGRESS.md) — the verification session is what turns ◐ into ☑.
