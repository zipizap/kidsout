#!/usr/bin/env bash
# Offline test suite — no router, no credentials, no network beyond loopback
# (except one deliberately black-holed address in the timeout tests).
# Must pass before anything is run against the real router.
set -uo pipefail
cd "$(dirname "$0")"

rc=0
for t in test_block_semantics.py test_state.py test_failure_modes.py test_contract.py; do
	python3 "$t" 2>/dev/null || rc=1
done

echo
if [ "$rc" -eq 0 ]; then
	echo "ALL SUITES PASSED"
else
	echo "FAILURES — do not deploy"
fi
exit "$rc"
