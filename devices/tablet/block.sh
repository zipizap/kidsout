#!/usr/bin/env bash
# kidsout block.sh — upstream kidsout (Go web app) script contract.
# Locks the tablet (which also turns its screen off). Exit 0 on success, non-zero on error.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/tools/koTabletctl" lock
