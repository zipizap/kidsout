#!/usr/bin/env bash
# kidsout xbox: one-time firewall + traffic-counter installation.
# Idempotent, and installs in the ALLOWED state — the console stays online.
# Requires device.json to have a 'mac' (./xbox.py discover --write).
set -euo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py install "$@"
