#!/usr/bin/env bash
# kidsout getState.sh — upstream kidsout (Go web app) script contract.
# Generic OpenWrt driver wrapper: do not edit, the per-device facts live in
# generic-openwrt-driver_files/device.json.
#
# Prints ONE word on stdout: up | down | unknown.
#   up       the device's outbound byte rate is above threshold_out in
#            device.json (active use), OR its inbound rate is above
#            threshold_in (streaming) — someone is actually using it
#   down     the router answered and the device is idle (or blocked)
#   unknown  the router could not be consulted, or there is no usable baseline
#            (first run after a restart, a stale sample, a counter reset)
#
# Why bytes and not connections: many devices keep connections alive in
# standby, so counting flows reports 'up' around the clock and burns the
# whole daily allowance while nobody is using it. See DESIGN.md §4.
#
# Runtime: bounded by state_deadline in config.json (default 4s), inside
# kidsout's 10s limit. Typical healthy run is well under a second; the driver
# returns a truthful 'unknown' rather than being SIGKILLed.
#
# stderr is NOT the diagnostic channel: upstream runs this with Go's
# cmd.Output(), which discards stderr on success. Detail goes to
# generic-openwrt-driver_files/driver.log.
set -uo pipefail
# Upstream leaves cmd.Dir unset, so self-locate from $0 rather than trusting
# the working directory. The shared driver sits two levels up by default
# (kidsout/generic-openwrt-driver/); KIDSOUT_OPENWRT_DRIVER overrides that.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
driver="${KIDSOUT_OPENWRT_DRIVER:-$here/../../generic-openwrt-driver/driver.py}"
KIDSOUT_DEVICE_DIR="$here/generic-openwrt-driver_files" exec python3 "$driver" state
