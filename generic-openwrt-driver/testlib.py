#!/usr/bin/env python3
"""Shared scaffolding for the offline test suite.

Loads driver.py as a module with its per-device directory pointed at a temp
directory, so tests never touch any real device.json, .state.json or
driver.log.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# The id every fixture uses unless told otherwise. Deliberately NOT a real
# device name, so a test can never be mistaken for a run against production.
TEST_ID = "testdev"


def load_driver(tmp):
    spec = importlib.util.spec_from_file_location("driver_under_test",
                                                  os.path.join(HERE, "driver.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.set_device_dir(tmp)
    return m


def device(mac="aa:bb:cc:dd:ee:ff", macs=None, ipv4="192.168.2.172", id=TEST_ID, **over):
    """A self-contained device.json for the tests.

    Built in code rather than loaded from disk: the shared template ships
    only device.example.json (placeholders), and each real device's
    device.json carries its own calibrated thresholds -- the suite must not
    depend on either. Thresholds are pinned here; the state tests derive
    their inputs from these numbers.

    Pass `macs=[(mac, ipv4, link), ...]` to model a device with both a wired
    and a wireless interface (REVIEW.2 G-02).
    """
    d = {
        "id": id,
        "display_name": f"{id} (test fixture)",
        "network": {"zone": "lan", "wan_zone": "wan"},
        "router": {
            "primary_host": "127.0.0.1",
            "alt_hosts": [],
            "https_port": 443,
            "http_port": 80,
            "ubus_path_candidates": ["/ubus"],
            # never a pin: the mock serves its own self-signed cert
            "tls_sha256": None,
        },
        "state": {
            "threshold_out_bytes_per_min": 204800,
            "threshold_in_bytes_per_min": 1048576,
            "max_sample_age_s": 300,
            "rule_audit_period_s": 900,
        },
        "discover": {"hostname_hints": [id]},
    }
    if macs:
        d["interfaces"] = [{"mac": m, "ipv4": ip, "link": lk} for m, ip, lk in macs]
    else:
        d["interfaces"] = [{"mac": mac, "ipv4": ipv4, "link": "?"}]
    d.update(over)
    return d


def shipped_config():
    """config.example.json — the config the setup instructions tell you to copy.

    Timing assertions must use this and not the mock's own values: REVIEW.1 F-03
    existed precisely because the documented 4 s budget had only ever been
    measured against a test config's timeout of 1.
    """
    with open(os.path.join(HERE, "config.example.json")) as f:
        return json.load(f)


def shipped_device_example():
    with open(os.path.join(HERE, "device.example.json")) as f:
        return json.load(f)


class Case:
    """Minimal test harness: no pytest dependency, runs anywhere python3 runs."""

    def __init__(self, name):
        self.name = name
        self.failures = []
        self.checks = 0

    def check(self, cond, msg):
        self.checks += 1
        if not cond:
            self.failures.append(msg)
            print(f"    FAIL  {msg}")
        return bool(cond)

    def eq(self, got, want, msg):
        return self.check(got == want, f"{msg}: got {got!r}, want {want!r}")


class Suite:
    def __init__(self, title):
        self.title = title
        self.cases = []
        print(f"\n=== {title} ===")

    @contextlib.contextmanager
    def case(self, name):
        print(f"  {name}")
        c = Case(name)
        self.cases.append(c)
        try:
            yield c
        except Exception as e:
            import traceback
            c.failures.append(f"raised {type(e).__name__}: {e}")
            print(f"    FAIL  raised {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)

    def report(self):
        failed = [c for c in self.cases if c.failures]
        total = sum(c.checks for c in self.cases)
        print(f"\n{self.title}: {len(self.cases) - len(failed)}/{len(self.cases)} cases "
              f"passed ({total} assertions)")
        for c in failed:
            for f in c.failures:
                print(f"  FAILED {c.name}: {f}")
        return 1 if failed else 0


@contextlib.contextmanager
def tmpdir():
    d = tempfile.mkdtemp(prefix="kidsout-test-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def out_of(buf):
    return buf.getvalue().strip()
