# generic-openwrt-driver — kidsout device driver for anything behind an OpenWrt router

One shared driver, many devices. It controls a LAN device's internet access by
toggling a firewall rule on an OpenWrt router and measures whether the device is
in use from its byte rate. **Block = internet off, unblock = internet on.**

This directory holds the code, which is the same for every device. Each device
keeps only its own facts and credential under `devices/<name>/`. Two devices on
the same router never interfere: every router-side artefact carries the device
id.

```
kidsout/
├── generic-openwrt-driver/            THIS directory — shared, edit here only
│   ├── driver.py                      the driver (python3 stdlib only)
│   ├── router_bootstrap.sh            per-device router setup, run ON the router
│   ├── new_device.sh                  scaffolds devices/<name>/ (step 1 below)
│   ├── contract/                      the wrapper templates new_device.sh copies
│   ├── device.example.json            device facts template
│   ├── config.example.json            credential template
│   ├── DESIGN.md                      how and why it works
│   └── mock_router.py test_*.py run_tests.sh   offline suite
│
└── devices/<name>/                    ONE per device — what an admin creates
    ├── getState.sh  block.sh  unblock.sh      kidsout's contract (copied, never edited)
    └── generic-openwrt-driver_files/
        ├── driver.sh                  operator entry point: ./driver.sh status
        ├── device.json                id, MACs, thresholds, router address  (committed)
        ├── config.json                rpcd password for this device          (0600, gitignored)
        └── .state.json driver.log calibration.jsonl   runtime files          (gitignored)
```

The wrappers find the shared code at `../../generic-openwrt-driver/driver.py`
relative to the device directory. If your devices directory lives elsewhere
(`KIDSOUT_DEVICES_DIR`), export `KIDSOUT_OPENWRT_DRIVER=/abs/path/to/driver.py`
in kidsout's environment.

What is derived from the device `id` (must equal the directory name; lowercase
letters and digits only):

| on the router | name |
|---|---|
| firewall rule (uci section) | `kidsout_<id>_out` |
| privileged helper | `/usr/libexec/kidsout-<id>` |
| rpcd ACL | `/usr/share/rpcd/acl.d/kidsout-<id>.json`, group `kidsout-<id>` |
| rpcd login | `kidsout-<id>` |
| accumulator state | `/tmp/kidsout-<id>.acct`, `/tmp/kidsout-<id>.addrs` |

---

## Prerequisites

**Router**: OpenWrt with fw4 (22.03 or newer), `uhttpd-mod-ubus`, `rpcd` with
`rpcd-mod-file` (all default in current images), root ssh access once per
device for the bootstrap, and the `conntrack` package (the bootstrap installs
it). If the router uses hardware flow offloading, the fw4 flowtable must carry
the `counter` flag (default in 24.x) — `selftest` checks this.

**Device**: a fixed MAC. Phones and tablets randomise their Wi-Fi MAC per
network by default; set the device to "use device MAC" for your SSID first, or
the block silently fails open whenever the MAC rotates.

**kidsout machine**: python3 (3.8+), bash, ssh.

---

## Adding a device — what an admin does

The example device is called `nintendoswitch2`. Everything below is run on the
kidsout machine from the repository root unless it says ROUTER.

### 1. Scaffold the device directory

```
generic-openwrt-driver/new_device.sh nintendoswitch2
```

This creates `devices/nintendoswitch2/` with the three contract wrappers and
`generic-openwrt-driver_files/` holding `driver.sh`, a `device.json` whose id
is already `nintendoswitch2`, and a `.gitignore` for the credential and runtime
files. It prints the remaining steps.

Manual equivalent, if you prefer:

```
mkdir -p devices/nintendoswitch2/generic-openwrt-driver_files
cp generic-openwrt-driver/contract/{getState,block,unblock}.sh devices/nintendoswitch2/
cp generic-openwrt-driver/contract/driver.sh        devices/nintendoswitch2/generic-openwrt-driver_files/
cp generic-openwrt-driver/contract/files.gitignore  devices/nintendoswitch2/generic-openwrt-driver_files/.gitignore
sed 's/CHANGE_ME/nintendoswitch2/g' generic-openwrt-driver/device.example.json \
    > devices/nintendoswitch2/generic-openwrt-driver_files/device.json
```

Never edit the copied `.sh` files: the test suite checks they are identical to
the templates, so a future driver upgrade only touches this directory.

### 2. Edit `device.json`

In `devices/nintendoswitch2/generic-openwrt-driver_files/device.json`:

- `router.primary_host`: the router's LAN address. `alt_hosts` are tried after
  it (a VPN-side address, for example).
- `network.zone` / `network.wan_zone`: only if your zones are not `lan`/`wan`
  (`uci show firewall | grep name=` on the router lists them).
- `discover.hostname_hints`: substrings of what the device calls itself in DHCP
  (case-insensitive). A Switch does not announce itself as `nintendoswitch2`.
- Leave `interfaces` empty and `state.threshold_*` at the shipped values for
  now; steps 6 and 8 fill them in.

### 3. ROUTER — bootstrap this device (once)

Two commands, and the second **must be interactive** (it prompts for a new
password, which is not the router's root password):

```
ssh root@192.168.1.1 'cat > /tmp/router_bootstrap.sh' < generic-openwrt-driver/router_bootstrap.sh
ssh -t root@192.168.1.1 sh /tmp/router_bootstrap.sh nintendoswitch2
```

Do not pipe the script into ssh; ssh will not allocate a terminal and `passwd`
cannot prompt. `scp` may be unavailable on the router, hence `cat >`.

It installs `/usr/libexec/kidsout-nintendoswitch2`, the ACL, the login
`kidsout-nintendoswitch2`, and the `conntrack` package. Nothing else on the
router is touched; an existing device's helper, login and rule are unaffected.

### 4. Credential on the kidsout machine

```
cd devices/nintendoswitch2/generic-openwrt-driver_files
cp ../../../generic-openwrt-driver/config.example.json config.json
chmod 600 config.json
$EDITOR config.json        # "password": the one you just chose; leave "username": null
```

`username: null` means `kidsout-nintendoswitch2`. Set it only if the router
login has another name.

### 5. Verify the router side

```
./driver.sh probe          # which endpoint answers (no credentials needed)
./driver.sh pin            # record the router's TLS certificate fingerprint
./driver.sh selftest       # every capability the driver needs, PASS/FAIL
```

`selftest` will still report "device is visible on the LAN" as FAIL until the
next step records the device's MAC.

### 6. Record the device's interfaces

Switch the device on and make it use the network, then:

```
./driver.sh discover --write
```

If the device has both a wired and a wireless interface, run it once on each
(cable in, cable out). **A missing interface means the block silently fails
open whenever the device uses it.** If `discover` finds nothing, check
`discover.hostname_hints` against `/tmp/dhcp.leases` on the router, or add the
MAC to `interfaces` by hand.

### 7. Install the firewall rule

```
./driver.sh install        # creates kidsout_nintendoswitch2_out in the ALLOWED state
./driver.sh status         # rule ALLOWED, both MACs, counters registered
./driver.sh block          # test: the device should lose internet within a minute
./driver.sh allow          # and get it back
```

### 8. Calibrate the "in use" thresholds

The shipped thresholds (200 KB/min out, 1 MB/min in) come from one game
console. Measure your device; guessing did not work for the first one either.

```
./driver.sh calibrate --minutes 20 --interval 60 --label idle       # switched on, nobody using it
./driver.sh calibrate --minutes 10 --interval 60 --label active     # someone using it
./driver.sh calibrate --minutes 10 --interval 60 --label streaming  # video, if it can
```

Each run prints min/median/max per direction. Set
`state.threshold_out_bytes_per_min` between idle-out max and active-out min,
and `state.threshold_in_bytes_per_min` between idle-in max and streaming-in
min. Err low on inbound: a missed stream is silent free time, an over-eager
`up` gets noticed. Details and the reasoning in DESIGN.md §5.

### 9. Restart kidsout

Device discovery runs once at startup. After a restart the log shows
`devices: [... nintendoswitch2 ...]` and the device appears in the UI in
free-use mode.

### 10. Commit

`device.json`, the wrappers and `driver.sh` are meant to be committed.
`config.json` and the runtime files are gitignored. Write down the calibration
figures in `device.json`'s `state._comment` or a `devices/<name>/README.md`, as
`devices/xbox/` does.

---

## Day to day

Everything is run from `devices/<name>/generic-openwrt-driver_files/`:

```
./driver.sh status      # endpoint, router facts, rule state, current byte rate
./driver.sh state       # the one word kidsout sees: up | down | unknown
./driver.sh check       # is the device present on the LAN right now
./driver.sh counters    # byte totals and the rate since the last sample
./driver.sh block       # internet off   (what block.sh does)
./driver.sh allow       # internet on    (what unblock.sh does)
./driver.sh selftest    # after any router change or firmware upgrade
./driver.sh uninstall   # remove the rule and the counter registration (destructive)
```

**Device stuck offline** (kidsout crashed while it was blocked, runtimestore
was reset): run `devices/<name>/unblock.sh`. Safe at any time, safe to repeat.

**Diagnostics** are in `driver.log` next to `device.json`, never on stderr:
kidsout discards stderr. Every verdict, every rule change and a 15-minute
`audit:` line of what the router is actually enforcing land there.

---

## Several devices on one router

Repeat "Adding a device" per device; step 3 runs the bootstrap once per id. Each
device gets its own helper, ACL, login, rule and accumulator, and its own
`config.json`, `driver.log` and `.state.json` on the kidsout machine. The
offline suite (`test_two_devices.py`) proves that blocking, allowing, counting
and uninstalling one device leaves another untouched — including when one id is
a prefix of the other.

What IS shared, by rpcd's design: every `kidsout-<id>` login may write the whole
uci `firewall` package (rpcd scopes uci ACLs per package, not per section). A
leaked credential for one device could therefore toggle another device's rule.
The helper, and with it counters and conntrack flushes, is strictly per device.

---

## Upgrading the driver

Edit only in this directory. Devices pick the change up on their next tick;
nothing in `devices/<name>/` needs touching unless the release notes say so.

If the router-side helper changed, refresh it per device **without** a
password prompt:

```
ssh root@<router> 'cat > /tmp/router_bootstrap.sh' < generic-openwrt-driver/router_bootstrap.sh
ssh -t root@<router> sh /tmp/router_bootstrap.sh <id> --helper-only
```

The accumulator state is preserved, so this costs no `unknown` tick.

Run `./run_tests.sh` here before deploying; it also checks that every
`devices/*/` directory still matches the templates and has a valid
`device.json`.

---

## Removing a device

```
cd devices/<name>/generic-openwrt-driver_files && ./driver.sh uninstall    # rule + counters
ssh -t root@<router> sh /tmp/router_bootstrap.sh <name> --uninstall         # helper, ACL, login, user
git rm -r devices/<name>   # and restart kidsout
```

Only that device's artefacts are removed.

---

## Troubleshooting (short form; DESIGN.md §12 has the long one)

| symptom | look at |
|---|---|
| `getState` always `unknown` | `driver.log` names the cause: missing `config.json`, wrong password, router unreachable, no baseline yet. Then `./driver.sh selftest`. |
| `driver.py: device.json: 'id' must match ...` | `id` is not lowercase letters/digits or does not equal the directory name. |
| `rules.prefix is ... but id derives ...` | a `device.json` copied from another device: remove `rules.prefix`, it is derived. |
| always `down` while clearly in use | wrong interface recorded, or thresholds too high: `./driver.sh status`, `discover --write`, re-calibrate. |
| always `up` with nobody there | threshold too low, or a background download: `./driver.sh counters`, re-calibrate. |
| block "succeeds" but the session continues | `block` exits non-zero when the flush fails; `./driver.sh selftest` checks the `conntrack` package. |
| certificate errors after a router upgrade | `./driver.sh pin` |
| `warn: the router helper ... prints legacy counter keys` | a helper from before the generic driver: run the bootstrap with `--helper-only`. |
