#!/usr/bin/env python3
"""Shared scaffolding for the offline test suite.

Loads xbox.py as a module with its on-disk state redirected into a temp
directory, so tests never touch the real device.json, .state.json or xbox.log.
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


def load_driver(tmp):
    spec = importlib.util.spec_from_file_location("xbox_under_test",
                                                  os.path.join(HERE, "xbox.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.DEVICE_FILE = os.path.join(tmp, "device.json")
    m.CONFIG_FILE = os.path.join(tmp, "config.json")
    m.STATE_FILE = os.path.join(tmp, ".state.json")
    m.LOG_FILE = os.path.join(tmp, "xbox.log")
    return m


def device(mac="aa:bb:cc:dd:ee:ff", **over):
    """The shipped device.json, with a MAC filled in."""
    with open(os.path.join(HERE, "device.json")) as f:
        d = json.load(f)
    d["mac"] = mac
    d.update(over)
    return d


def shipped_config():
    """config.example.json — the config the setup instructions tell you to copy.

    Timing assertions must use this and not the mock's own values: REVIEW.1 F-03
    existed precisely because the documented 4 s budget had only ever been
    measured against config.test.json's timeout of 1.
    """
    with open(os.path.join(HERE, "config.example.json")) as f:
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
