# Changelog

All notable changes to kidsout are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project has no version tags yet, so released sections are keyed by date.
New work goes under **Unreleased** first; when a release is cut, rename that
section to the version/date and start a fresh empty Unreleased block.

Categories: `Added`, `Changed`, `Fixed`, `Removed`, `Security`, `Docs`.

---

## [Unreleased]

### Added
- Manual device actions: `POST /api/device/{name}/block` and `/unblock` run the
  device's `block.sh`/`unblock.sh` right away and return the result as JSON
  (`exitCode`, `stdout`, `stderr`, `durationMs`). No kidsout state changes, so
  the engine may undo the effect on a later tick. `kidsoutctl block|unblock
  <device...> | --all` wraps them for external programs (exit 1 if a script
  fails; `-o json|yaml` prints the results as a list).
- **`generic-openwrt-driver/`**: the Xbox OpenWrt driver generalised into one
  shared driver for any device behind an OpenWrt router. Each device keeps only
  its own facts under `devices/<name>/generic-openwrt-driver_files/`
  (`device.json`, `config.json`, runtime files) plus the three contract
  wrappers; the code lives once at the repo root. Everything on the router is
  namespaced by the device id (`kidsout_<id>_out`, `/usr/libexec/kidsout-<id>`,
  ACL and login `kidsout-<id>`, `/tmp/kidsout-<id>.*`), so several devices
  share one router without interfering. `new_device.sh <name>` scaffolds a
  device; `README.md` there is the admin guide.
- `router_bootstrap.sh <id> --helper-only` refreshes the helper and ACL
  without a password prompt and without resetting the accumulator.
- Offline suite: `test_two_devices.py` (two devices, one a prefix of the
  other, against one multi-tenant mock) and a check that every real
  `devices/*/` directory matches the wrapper templates and has a valid
  `device.json`.

### Changed
- `devices/xbox/xbox-openwrt-driver/` is gone: code moved to
  `generic-openwrt-driver/` (`xbox.py` → `driver.py`, `xbox.log` →
  `driver.log`), device facts to
  `devices/xbox/generic-openwrt-driver_files/`, measurements and printouts to
  `devices/xbox/other/`; `devices/xbox/README.md` holds the Xbox specifics and
  the router migration steps (login `kidsout` → `kidsout-xbox`).
- Helper counter keys are `out`/`in`; the driver still accepts the old
  `xbox_out`/`xbox_in` from a not-yet-refreshed helper and asks for
  `--helper-only` in the log. A stored sample with old key names re-baselines
  (`unknown`) instead of producing one huge false `up`.
- `device.json`: `id` is validated (`^[a-z][a-z0-9]{0,23}$`, must equal the
  directory name); `rules.prefix` is derived from it and an explicit
  disagreeing value is refused; new optional `discover.hostname_hints`.

### Fixed
- Firewall section matching used a bare prefix, so a device named `kid` would
  have deleted or toggled `kid2`'s rule. Sections now match on `kidsout_<id>_`.
- Bootstrap gave every device's system user uid 6000; now the first free uid.

### Fixed
- Xbox driver reported `down` while the console was streaming video (YouTube:
  60–80 KB/min out, 4.4–18 MB/min in, against an outbound-only 200 KB/min
  rule). `state` now also reads `up` when the **inbound** rate exceeds a new
  `threshold_in_bytes_per_min` (shipped at 1 MB/min). Accepted trade-off: a
  game download in progress also reads `up`; the console's Energy-saving power
  mode confines that to console-on time. The log line now names which rule
  fired (`by=out` / `by=in`) and both thresholds.

- Xbox driver: operator messages told you to `opkg install conntrack-tools`,
  a package that does not exist on OpenWrt. They now name the real package,
  `conntrack`. `getState.sh`'s header claimed a 7 s deadline; the shipped
  default is 4 s. `check` listed every IPv6 neighbour twice because the router
  helper ran both `ip neigh show` and `ip -6 neigh show`; the driver now
  de-duplicates and the helper drops the redundant call.

### Docs
- Xbox driver: live test session against the real router and console recorded
  in `devices/xbox/other/Tests.20260922.124049.md` — every
  contract path (idle, gaming, streaming, block/unblock, failure modes,
  power-off) passed; ethernet not covered.

### Changed
- Xbox `device.json`: `state.threshold_bytes_per_min` renamed to
  `threshold_out_bytes_per_min` (same value, no alias — update any local copy).
- Xbox `status` shares the verdict logic with `state`; `calibrate` summarises
  inbound as well as outbound.

### In progress
- Tablet device driver (`devices/TODO/tablet/`): contract scripts stubbed,
  no implementation yet.

---

## [1.0.0] — 2026-09-21

First tagged release. Everything below this section was shipped untagged.

### Added
- This changelog.
- Version reporting: `kidsout --version` / `--help` flags, the version logged
  at startup, and a `GET /api/version` endpoint. `kidsoutctl version` now also
  shows the server's version when credentials are set.
- Shared `version` package used by both binaries; `go_build.sh` stamps the git
  commit and honours `VERSION=x.y.z` to override the release string.

---

## 2026-09-17

### Changed
- New devices now start in `enforcementOFF` (free-use-mode) instead of being
  enforced immediately; covered by a new test in `main_test.go`.

### Fixed
- Xbox driver: `state` without a `config.json` now prints `unknown` and logs
  the real cause (missing local config) instead of surfacing a misleading
  rpcd "permission denied". Only `probe` runs credential-free.

### Removed
- Leftover `devices/WIP/openwrts/xbox/` experiment, including the OpenWrt FAQ.

---

## 2026-09-16

### Changed
- Xbox driver moved out of `devices/WIP/complex/` into its final home at
  `devices/xbox/` (contract scripts) and `devices/xbox/xbox-openwrt-driver/`
  (Python driver, installer, router checks, tests).
- Xbox driver `device.json` slimmed down; block-semantics tests extended.

### Docs
- Added `devices/xbox/FUTURE_IDEAS.md`.

---

## 2026-09-15 — Xbox driver verified on real hardware

### Added
- Xbox device driver controlling the console through the OpenWrt router's
  ubus-over-HTTPS API. Block = internet off, unblock = internet on.
- Driver tooling: `install.sh`, `router_bootstrap.sh`, `allow.sh`,
  `status.sh`, `mock_router.py`, and a pytest suite
  (`test_contract.py`, `test_block_semantics.py`, `test_failure_modes.py`,
  `test_state.py`) run via `run_tests.sh`.
- Router check scripts (`router_checks_A.sh`, `router_checks_B1.sh`,
  `router_checks_B2.sh`) with recorded printouts; check B settled the
  flow-offload question.
- End-to-end verification: a live online multiplayer game was blocked and
  then restored on the real router and console.

### Changed
- "In use" detection threshold calibrated against the real console.
- Traffic accumulator keyed on conntrack id.
- Six driver markdown files (`README`, `REVIEW.1`, `REVIEW.2`, `PROGRESS`,
  `F-06`, `S-02`) consolidated into a single `DESIGN.md`.
- WiFi static DHCP reservation recorded for the console (G-06).

### Fixed
- Wrong conntrack package name in the installer.
- Bootstrap could never prompt for a password as documented.
- Four blockers found in REVIEW.2 (v3 of the driver).

### Security
- Bootstrap no longer leaves the rpcd credential hash in world-readable
  `/etc/passwd`.

---

## 2026-08-28 — Initial release

### Added
- Go backend (`main.go`, `engine.go`, `api.go`, `state.go`) with a
  one-minute enforcement tick, per-weekday time budgets, allowed
  time-frame windows, 20-minute pause, and free-use-mode.
- Device status model: `inUse`, `notInUse`, `blockedNoTime`,
  `blockedNotInTimeframe`, `blockedPauseON`, `enforcementOFF`.
- Web UI (`web/`) with a phone-friendly week grid, live updates over
  Server-Sent Events, and an on-screen cell legend.
- Runtime state persisted to `runtimestore.yaml` across restarts.
- Device contract: `block.sh`, `unblock.sh`, `getState.sh` per
  `devices/<name>/`.
- LG webOS TV driver (`devices/tv/`) using the `lg-webos-ssap` tool.
- `kidsoutctl` CLI and `go_*.sh` helper scripts.
- `devices.example.tgz` sample device bundle.

### Docs
- `README.md` administrator guide with screenshots, `README_API.md`,
  and `DESIGN/DESIGN.md`.
- Installation instructions clarified.
