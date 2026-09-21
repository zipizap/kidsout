# Future ideas — generalising the driver beyond the Xbox

**Status: hypothesis, not a plan.** Nothing here has been built or measured.
Written 2026-09-15 from a design discussion; the implemented, verified design is
[xbox-openwrt-driver/DESIGN.md](xbox-openwrt-driver/DESIGN.md).

---

## The question

Could this driver control internet access for *other* kidsout devices — an
Android tablet, say — instead of being Xbox-only? And if so, is it one shared
installation with per-device config, or a copy per `devices/<name>/`?

Short answer: **yes, and most of it is already generic.** The split is not where
you would expect. Enforcement is the easy, portable part. The one piece that
genuinely cannot be shared is the definition of "in use".

---

## Three layers, with very different portability

### Layer 1 — transport and enforcement: already device-agnostic

Everything expensive this project learned is about the **router**, not the
console: ubus-over-HTTPS with a pinned certificate, the scoped rpcd ACL,
`src_mac` as an fw4 list, the asynchronous firewall reload, hardware flow
offloading defeating both accounting *and* enforcement, and the conntrack flush
being load-bearing rather than an optimisation (§3, §10, §11 of DESIGN.md).

None of that mentions a game console. A REJECT rule keyed on `src_mac` plus a
conntrack flush blocks any L2 device on this LAN.

The rule prefix is **already parameterised** — `get_xbox_rules()` reads
`device["rules"]["prefix"]` from `device.json`, so a tablet would get
`kidsout_tablet_out` with no code change. Two MACs was the Xbox's quirk and the
code already handles *N* MACs, so a single-interface tablet is just N=1.

**This layer transfers for free.**

### Layer 2 — the measurement mechanism: generic, but single-tenant today

The router-side accumulator is the piece that needs real work, and the work is
on the router rather than in Python:

```sh
HELPER=/usr/libexec/kidsout-xbox
STATE=/tmp/kidsout-xbox.acct
ADDRS=/tmp/kidsout-xbox.addrs
...
printf "xbox_out %d\n", tot_out
```

One helper, one address list, one accumulator file, two fixed counter keys.
Install counters for a second device and they collide: the tablet's addresses
would overwrite the Xbox's and both would then read each other's bytes. That is
a real bug, not just awkward naming.

The fix is small and obvious. The helper verbs already take arguments
(`counters-install ADDR...`), so threading a device id through — `counters <id>`,
state in `/tmp/kidsout-<id>.acct`, keys `out`/`in` instead of
`xbox_out`/`xbox_in` — makes it multi-tenant. One helper, one ACL and one rpcd
account can then serve every device: the ACL is scoped to the **helper path**,
not to a device, so it needs no change at all.

### Layer 3 — what "in use" means: this does NOT generalise

This is the crux, and it is not a threshold to retune.

> The Xbox metric was **outbound** byte rate, deliberately. Measured on the real
> console: idle 20.8, downloading 93.7, gaming 289.5–471.6 KB/min *outbound*. A
> download is almost pure inbound; gameplay uploads continuously. That asymmetry
> is what lets one number separate "playing" from "patching" with 3.1× margin.

**Update 2026-09-21:** the first row below turned out to apply to the Xbox too —
YouTube on the console read `down` for an evening. The fix that landed is a
second, independent **inbound** threshold (`threshold_out_bytes_per_min` +
`threshold_in_bytes_per_min`, OR-ed, in `verdict_for()`), not a `metric` enum;
see DESIGN.md §4–§5 for the accepted cost (a download now reads `up`).

An Android tablet still inverts the *second* case:

| Situation | Tablet traffic | Xbox metric says | Truth |
|---|---|---|---|
| Kid watching YouTube for two hours | huge **in**, trivial **out** | `up` (since 2026-09-21) | in use |
| Overnight photo backup or OS update | large **out** | `up` | nobody touching it |

So a tablet does not want the Xbox's numbers — it needs its own two thresholds,
and probably a much lower outbound one is *wrong* for it (backups). The
two-threshold shape is already generic; what does not generalise is the values.

**The cost is not the code — it is that every new device class needs its own
empirical calibration.** The Xbox's numbers took a session of real gameplay,
downloading and idling to establish; `./xbox.py calibrate` exists precisely
because guessing did not work (DESIGN.md §5). A tablet needs the same treatment,
with the device in hand.

---

## Two hazards specific to Android

**MAC randomisation.** Android randomises its Wi-Fi MAC per SSID by default. A
MAC-keyed REJECT rule against a rotating MAC **silently fails open** — exactly
the G-02 failure class this design was rebuilt to eliminate, except here it
recurs on its own schedule. The tablet must be set to *Use device MAC* for the
home SSID. That is a device-configuration prerequisite the driver cannot detect
or fix, and it should be settled before any code is written.

**Bytes are a weaker proxy for a tablet.** An Xbox essentially only talks when
somebody is using it, so traffic ≈ use. A tablet chatters for push, sync and
background refresh regardless, and "in use" really means "screen on with a
person looking at it". No byte-rate metric recovers that cleanly.

The failure mode that matters is a false `down`: kidsout stops decrementing the
allowance, and the result is unmetered viewing. A false `up` merely burns
allowance and gets noticed and complained about; a false `down` is silent.

---

## Deployment shape

Secondary to the feasibility question, but: **shared implementation with
per-device config** is clearly right.

The current layout already sets it up. `getState.sh`, `block.sh` and
`unblock.sh` are three-line wrappers, and the driver anchors everything it owns
— `device.json`, `config.json`, `.state.json`, `xbox.log` — on its own location.
Point each device's wrappers at one shared install, give each its own config
directory, done.

A copy per `devices/<name>/` means N places to apply the next
router-behaviour discovery, which is precisely the kind of finding this project
keeps producing (see DESIGN.md §11).

One open question either way: one shared rpcd account for all devices, or one
per device. Shared is simpler and the ACL does not care; per-device narrows the
blast radius if a config file leaks.

---

## Effort, honestly

| | Work |
|---|---|
| **Mechanical** | de-Xbox the names; namespace the helper's state per device. Well covered by the existing 53-case suite, which would catch a botched rename. |
| **Small** | a per-device metric selector in `sample_rate()`. |
| **Real but unavoidable** | calibrating each new device class empirically, plus pinning the MAC on anything that randomises. |

The reusable asset here is **not the Python — it is the router knowledge**.
`getState` is the only genuinely per-device part, and it is per-device
*semantically*, so no amount of abstraction removes the measurement work.

---

## To settle before starting

1. Can the target device's MAC be pinned? (Blocking, for Android.)
2. What does "in use" mean for it, operationally — and is any byte-based proxy
   good enough, or is the honest answer "coarser than the Xbox"?
3. Which way does its idle/active traffic split fall: outbound-tell like a
   console, or inbound-dominated like a media consumer?
4. Is a false `down` acceptable for this device, given it hands out free time
   silently?
