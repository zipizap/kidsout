#!/usr/bin/env bash
# kidsout block.sh — upstream kidsout (Go web app) script contract.
# Generic OpenWrt driver wrapper: do not edit, the per-device facts live in
# generic-openwrt-driver_files/device.json.
#
# Internet OFF. Called on every tick while the device is blocked and reads up,
# so it must be cheap when nothing needs changing — it is: the driver reads the
# current rule state and writes only when it differs (no flash write, no
# firewall reload). Exit 0 on success, non-zero on error.
#
# Enables the kidsout_<id>_out REJECT rule AND flushes the device's existing
# conntrack entries, because OpenWrt accepts established flows before any user
# rule — without the flush a session already in progress would survive for hours.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
driver="${KIDSOUT_OPENWRT_DRIVER:-$here/../../generic-openwrt-driver/driver.py}"
KIDSOUT_DEVICE_DIR="$here/generic-openwrt-driver_files" exec python3 "$driver" block "$@"
