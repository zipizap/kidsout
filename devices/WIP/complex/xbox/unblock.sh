#!/usr/bin/env bash
# kidsout xbox: unblock.sh — upstream kidsout (Go web app) script contract.
# Internet ON. Upstream calls this ONCE, on the blocked -> not-blocked edge.
# Exit 0 on success, non-zero on error.
#
# Safe to run by hand at any time, and safe to over-call: it disables the
# kidsout_xbox_out REJECT rule only if it is currently enabled. If the console
# is ever stuck offline — kidsout crashed or was upgraded while it was blocked,
# runtimestore.yaml was reset — this script is the escape hatch.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py allow "$@"
