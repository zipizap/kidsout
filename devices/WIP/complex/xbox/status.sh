#!/usr/bin/env bash
# kidsout xbox: status report (endpoint, router facts, rule state, traffic).
set -euo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py status "$@"
