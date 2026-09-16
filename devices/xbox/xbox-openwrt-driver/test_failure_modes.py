#!/usr/bin/env python3
"""Failure paths: every way of not knowing must report `unknown`, quickly.

v1 could not emit `unknown` at all — `_conntrack_flows` swallowed every
exception and returned [], which read as "no traffic", which printed `down`
(REVIEW.1 F-02). A router outage, a typo'd password and a quiet console were
indistinguishable, which permanently blinded kidsout's state-history strip.

v1 also took 61 s to fail, against a 10 s contract timeout, because the
per-request timeout multiplied across 3 conntrack attempts x 2 endpoint
candidates (F-03). The timing assertions here use the timeouts from the
SHIPPED config.example.json — not the mock's — because that mismatch is
exactly how the 61 s went unnoticed.
"""
import socket
import sys
import time

from mock_router import MockRouter
from testlib import (Suite, captured, device, load_driver, out_of, shipped_config,
                     tmpdir)

# TEST-NET-1 (RFC 5737): reserved for documentation, black-holes by design.
BLACKHOLE = "192.0.2.1"


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    s = Suite("failure modes")
    ship = shipped_config()
    budget = ship["state_deadline"] + 1.0

    with tmpdir() as tmp, MockRouter() as mock:
        m = load_driver(tmp)
        dev = device()

        def run(cfg_over=None, dev_over=None):
            """cmd_state through the same code path main() uses for `state`."""
            cfg = dict(ship)
            cfg.pop("password", None)
            cfg.update({"host": "127.0.0.1", "port": mock.port,
                        "username": "kidsout", "password": mock.state.password})
            cfg.update(cfg_over or {})
            d = dev_over or dev
            fw = m.Router(cfg, d, timeout=cfg["state_timeout"],
                          deadline=m.Deadline(cfg["state_deadline"]))
            t0 = time.time()
            with captured() as buf:
                rc = m.cmd_state(fw, d, None)
            return out_of(buf), rc, time.time() - t0

        with captured():
            m.cmd_install(m.Router(mock.config(), dev), dev, None)

        cases = [
            ("router unreachable (black-holed address)",
             {"host": BLACKHOLE, "port": 443}, None),
            ("connection refused (nothing listening)",
             {"port": closed_port()}, None),
            ("endpoint 404s (uhttpd-mod-ubus missing)", {}, "http404"),
            ("endpoint serves junk instead of JSON-RPC", {}, "badjson"),
            ("wrong password", {"password": "not-the-password"}, None),
            ("login rejected by the router", {}, "login"),
            ("ACL denies the call", {}, "denied"),
            ("router helper missing or failing", {}, "execfail"),
            ("router responds too slowly", {}, "slow"),
        ]

        for name, cfg_over, fail in cases:
            with s.case(name) as c:
                mock.state.fail = None
                mock.state.slow = 0.0
                if fail == "slow":
                    mock.state.slow = ship["state_deadline"] + 5
                elif fail:
                    mock.state.fail = fail
                word, rc, elapsed = run(cfg_over)
                c.eq(word, "unknown", "reports unknown, not down")
                c.eq(rc, 1, "and a non-zero exit, so upstream sees unknown either way")
                c.check(elapsed < budget,
                        f"finished in {elapsed:.2f}s (budget {budget:.1f}s, "
                        f"kidsout kills at 10s)")
                mock.state.fail = None
                mock.state.slow = 0.0

        # ------------------------------------------------------------------ #
        with s.case("a failed login is not retried endlessly (F-03)") as c:
            mock.state.fail = "login"
            cfg = dict(ship)
            cfg.update({"host": "127.0.0.1", "port": mock.port, "password": "x"})
            fw = m.Router(cfg, dev, timeout=cfg["state_timeout"],
                          deadline=m.Deadline(cfg["state_deadline"]))
            mock.state.calls.clear()
            for _ in range(3):
                try:
                    fw.login()
                except Exception:
                    pass
            logins = [x for x in mock.state.calls if x == ("session", "login")]
            c.eq(len(logins), 1, "three login() calls, one round trip: the failure is memoised")
            mock.state.fail = None

        with s.case("an unreachable endpoint sweep is bounded by the deadline") as c:
            d2 = device()
            d2["router"] = dict(d2["router"], primary_host=BLACKHOLE,
                                alt_hosts=[BLACKHOLE, BLACKHOLE, BLACKHOLE])
            word, rc, elapsed = run({"host": None}, d2)
            c.eq(word, "unknown", "unknown")
            c.check(elapsed < budget,
                    f"four hosts x two schemes still finished in {elapsed:.2f}s "
                    f"(v1 would have multiplied to ~{8 * ship['timeout']:.0f}s)")

        with s.case("the log explains each failure") as c:
            logged = open(m.LOG_FILE).read()
            for fragment in ("state=unknown", "error="):
                c.check(fragment in logged, f"log contains {fragment!r}")
            c.check("RouterUnreachable" in logged, "the failure class is identifiable")
            c.check("Access denied" in logged or "login" in logged,
                    "a rejected login is identifiable in the log")

        # ------------------------------------------------------------------ #
        with s.case("a blocked device still answers, it does not error") as c:
            fw = m.Router(mock.config(), dev)
            with captured():
                m.cmd_block(fw, dev, None)
            word, rc, _ = run()               # establishes a baseline
            c.eq(word, "unknown", "the first sample after a restart has no baseline")
            st = m.load_state()
            st["counters_ts"] = time.time() - 60
            m.save_state(st)
            word, rc, _ = run()
            c.eq(word, "down", "blocked and silent reads as down, not unknown")
            c.eq(rc, 0, "exit 0")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
