#!/usr/bin/env bash
# kidsout unblock.sh — upstream kidsout (Go web app) script contract.
# Generic OpenWrt driver wrapper: do not edit, the per-device facts live in
# generic-openwrt-driver_files/device.json.
#
# Internet ON. Upstream calls this ONCE, on the blocked -> not-blocked edge.
# Exit 0 on success, non-zero on error.
#
# Safe to run by hand at any time, and safe to over-call: it disables the
# kidsout_<id>_out REJECT rule only if it is currently enabled. If the device
# is ever stuck offline — kidsout crashed or was upgraded while it was blocked,
# runtimestore.yaml was reset — this script is the escape hatch.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
driver="${KIDSOUT_OPENWRT_DRIVER:-$here/../../generic-openwrt-driver/driver.py}"
KIDSOUT_DEVICE_DIR="$here/generic-openwrt-driver_files" exec python3 "$driver" allow "$@"
