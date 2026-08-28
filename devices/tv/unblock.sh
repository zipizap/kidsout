#!/usr/bin/env bash
# Demo stub: unblocks the device (real impl would e.g. remove a firewall rule).
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "${dir}/.blocked"
exit 0
