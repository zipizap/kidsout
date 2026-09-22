#!/usr/bin/env python3
"""Two devices, one router, one shared driver: no interference.

The whole point of the generic driver is that devices/xbox and
devices/nintendoswitch2 can run the SAME code against the SAME router and
never touch each other's rule, counters, flushes, state file or log. This
suite drives two device directories against one multi-tenant mock and
asserts exactly that, including the nasty case where one id is a prefix of
the other (`kid` vs `kid2`).
"""
import os
import sys
import time

from mock_router import MockRouter
from testlib import Suite, captured, device, load_driver, out_of, tmpdir

A, B = "kid", "kid2"          # B's names all start with A's -- the trap
MAC_A, MAC_B = "02:00:00:00:0a:01", "02:00:00:00:0b:01"
IP_A, IP_B = "192.168.2.101", "192.168.2.102"


def backdate(m, seconds):
    st = m.load_state()
    st["counters_ts"] = time.time() - seconds
    m.save_state(st)


def main():
    s = Suite("two devices on one router")

    with tmpdir() as tmp, MockRouter(device_id=A) as mock:
        dir_a, dir_b = os.path.join(tmp, A), os.path.join(tmp, B)
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        # two independent module instances, each anchored on its own directory,
        # exactly like two kidsout wrappers invoking the shared driver.py
        ma, mb = load_driver(dir_a), load_driver(dir_b)
        dev_a = device(id=A, mac=MAC_A, ipv4=IP_A)
        dev_b = device(id=B, mac=MAC_B, ipv4=IP_B)
        mock.state.leases = (f"1690000000 {MAC_A} {IP_A} {A.upper()} 01:a\n"
                             f"1690000000 {MAC_B} {IP_B} {B.upper()} 01:b\n")
        mock.state.neigh = (f"{IP_A} dev br-lan lladdr {MAC_A} REACHABLE\n"
                            f"{IP_B} dev br-lan lladdr {MAC_B} REACHABLE\n")
        fw_a = lambda: ma.Router(mock.config(), dev_a)
        fw_b = lambda: mb.Router(mock.config(username=f"kidsout-{B}"), dev_b)
        rule_a, rule_b = f"kidsout_{A}_out", f"kidsout_{B}_out"
        ta, tb = mock.state.tenant(A), mock.state.tenant(B)

        with s.case("each device talks to its own helper and login") as c:
            c.eq(fw_a().names.helper, f"/usr/libexec/kidsout-{A}", "A's helper")
            c.eq(fw_b().names.helper, f"/usr/libexec/kidsout-{B}", "B's helper")
            c.eq(fw_a().username, f"kidsout-{A}", "A's login")
            c.eq(fw_b().username, f"kidsout-{B}", "B's login")

        with s.case("both install: two rules, two counter registrations") as c:
            with captured():
                ma.cmd_install(fw_a(), dev_a, None)
                mb.cmd_install(fw_b(), dev_b, None)
            all_rules = {k for k in mock.state.firewall if k.startswith("kidsout_")}
            c.eq(all_rules, {rule_a, rule_b}, "one rule per device")
            c.eq(mock.state.firewall[rule_a]["src_mac"], MAC_A, "A's rule has A's MAC")
            c.eq(mock.state.firewall[rule_b]["src_mac"], MAC_B, "B's rule has B's MAC")
            c.eq(ta.counter_addrs, [IP_A], "A's helper counts A's address only")
            c.eq(tb.counter_addrs, [IP_B], "B's helper counts B's address only")

        with s.case("A sees only its own rule, even though B's name starts with A's") as c:
            c.eq(sorted(ma.find_sections(fw_a(), f"kidsout_{A}")), [rule_a], "A's view")
            c.eq(sorted(mb.find_sections(fw_b(), f"kidsout_{B}")), [rule_b], "B's view")

        with s.case("blocking A leaves B online and flushes only A") as c:
            tb.flushed.clear()
            with captured():
                ma.cmd_block(fw_a(), dev_a, None)
            c.eq(mock.state.firewall[rule_a]["enabled"], "1", "A blocked")
            c.eq(mock.state.firewall[rule_b]["enabled"], "0", "B still allowed")
            c.check(ta.flushed and IP_A in ta.flushed[-1], f"A's flows flushed: {ta.flushed}")
            c.eq(tb.flushed, [], "B's helper never asked to flush")
            c.check(all(IP_B not in f for f in ta.flushed), "and A never flushed B's address")

        with s.case("blocking B, then allowing A, leaves B blocked") as c:
            with captured():
                mb.cmd_block(fw_b(), dev_b, None)
                ma.cmd_allow(fw_a(), dev_a, None)
            c.eq(mock.state.firewall[rule_a]["enabled"], "0", "A allowed")
            c.eq(mock.state.firewall[rule_b]["enabled"], "1", "B still blocked")
            with captured():
                mb.cmd_allow(fw_b(), dev_b, None)

        with s.case("A's traffic does not make B read 'up'") as c:
            with captured():
                ma.cmd_state(fw_a(), dev_a, None)      # baselines
                mb.cmd_state(fw_b(), dev_b, None)
            backdate(ma, 60)
            backdate(mb, 60)
            mock.state.add_traffic(out=5_000_000, device_id=A)
            with captured() as ba:
                ma.cmd_state(fw_a(), dev_a, None)
            with captured() as bb:
                mb.cmd_state(fw_b(), dev_b, None)
            c.eq(out_of(ba), "up", "A is in use")
            c.eq(out_of(bb), "down", "B is idle")

        with s.case("state files and logs are per device") as c:
            c.check(os.path.exists(os.path.join(dir_a, ".state.json")), "A has a state file")
            c.check(os.path.exists(os.path.join(dir_b, ".state.json")), "B has a state file")
            log_a = open(os.path.join(dir_a, "driver.log")).read()
            log_b = open(os.path.join(dir_b, "driver.log")).read()
            c.check("state=up" in log_a, "A's log records A's verdict")
            c.check("state=up" not in log_b, "B's log does not contain A's verdict")

        with s.case("uninstalling A removes A only") as c:
            with captured():
                ma.cmd_uninstall(fw_a(), dev_a, None)
            c.check(rule_a not in mock.state.firewall, "A's rule gone")
            c.check(rule_b in mock.state.firewall, "B's rule intact")
            c.eq(tb.counter_addrs, [IP_B], "B's counters still registered")
            c.eq(ta.counter_addrs, [], "A's counters removed")

        with s.case("the periodic audit names only the device's own rule") as c:
            st = mb.load_state()
            st["rule_audit_ts"] = 0
            mb.save_state(st)
            backdate(mb, 60)
            with captured():
                mb.cmd_state(fw_b(), dev_b, None)
            audit = [l for l in open(os.path.join(dir_b, "driver.log")) if "audit:" in l][-1]
            c.check(rule_b in audit, f"B's audit line names B's rule: {audit.strip()}")
            c.check(rule_a not in audit, "and not A's")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
