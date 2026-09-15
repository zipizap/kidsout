#!/usr/bin/env bash
# kidsout xbox: block.sh — upstream kidsout (Go web app) script contract.
# Internet OFF. Called on every tick while the device is blocked and reads up,
# so it must be cheap when nothing needs changing — it is: the driver reads the
# current rule state and writes only when it differs (no flash write, no
# firewall reload). Exit 0 on success, non-zero on error.
#
# Enables the kidsout_xbox_out REJECT rule AND flushes the console's existing
# conntrack entries, because OpenWrt accepts established flows before any user
# rule — without the flush a game already in progress would survive for days.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py block "$@"
