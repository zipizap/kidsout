#!/usr/bin/env bash
# Demo stub: blocks the device (real impl would e.g. add a firewall rule).
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
touch "${dir}/.blocked"
exit 0
