#!/usr/bin/env bash
# Operator entry point for THIS device: runs the shared generic OpenWrt driver
# with this directory as the device directory.
#
#   ./driver.sh status | selftest | check | counters | probe
#   ./driver.sh block | allow | install | uninstall
#   ./driver.sh discover [--write] | calibrate --minutes 30 --label idle | pin
#
# The shared code lives in kidsout/generic-openwrt-driver/ (three levels up
# by default); KIDSOUT_OPENWRT_DRIVER overrides the path.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
driver="${KIDSOUT_OPENWRT_DRIVER:-$here/../../../generic-openwrt-driver/driver.py}"
KIDSOUT_DEVICE_DIR="$here" exec python3 "$driver" "$@"
