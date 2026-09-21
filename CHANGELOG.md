# Changelog

All notable changes to kidsout are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project has no version tags yet, so released sections are keyed by date.
New work goes under **Unreleased** first; when a release is cut, rename that
section to the version/date and start a fresh empty Unreleased block.

Categories: `Added`, `Changed`, `Fixed`, `Removed`, `Security`, `Docs`.

---

## [Unreleased]

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
