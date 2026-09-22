#!/usr/bin/env python3
"""The upstream kidsout script contract, exercised through the real .sh files.

Everything else in the suite calls python functions. This one runs the three
scripts kidsout actually runs, as subprocesses, from a different working
directory, against the mock — which is the only way to catch a broken shebang,
a missing exec bit, a lost self-location, a wrong relative path to the shared
driver, or stdout pollution.

Contract, from upstream's engine.go and state.go:
  - devices/<name>/ contains getState.sh, block.sh, unblock.sh, all executable
  - they are run with cmd.Dir unset, so they must self-locate
  - getState.sh prints one word; anything that is not up/down is unknown
  - non-zero exit is also unknown
  - all three run under a 10 s context timeout

Layout under test (what new_device.sh produces, and what devices/xbox uses):

  <root>/generic-openwrt-driver/driver.py
  <root>/devices/<id>/getState.sh block.sh unblock.sh
  <root>/devices/<id>/generic-openwrt-driver_files/driver.sh device.json config.json

It also checks that every REAL device directory in this repository still
carries byte-identical copies of the wrappers, and a device.json the driver
accepts -- so a device that drifts from the template fails the suite.
"""
import filecmp
import json
import os
import shutil
import stat
import subprocess
import sys
import time

from mock_router import MockRouter
from testlib import TEST_ID, Suite, device, load_driver, shipped_device_example, tmpdir

HERE = os.path.dirname(os.path.abspath(__file__))          # generic-openwrt-driver/
REPO = os.path.dirname(HERE)
CONTRACT = os.path.join(HERE, "contract")
FILES_SUBDIR = "generic-openwrt-driver_files"
SCRIPTS = ("getState.sh", "block.sh", "unblock.sh")


def deploy(tmp, mock):
    """A fresh <root> with the shared driver and one device, pointed at the mock."""
    shared = os.path.join(tmp, "generic-openwrt-driver")
    os.makedirs(shared)
    shutil.copy2(os.path.join(HERE, "driver.py"), shared)
    d = os.path.join(tmp, "devices", TEST_ID)
    files = os.path.join(d, FILES_SUBDIR)
    os.makedirs(files)
    for name in SCRIPTS:
        shutil.copy2(os.path.join(CONTRACT, name), os.path.join(d, name))
    shutil.copy2(os.path.join(CONTRACT, "driver.sh"), os.path.join(files, "driver.sh"))
    with open(os.path.join(files, "device.json"), "w") as f:
        json.dump(device(), f)
    cfg = os.path.join(files, "config.json")
    with open(cfg, "w") as f:
        json.dump(mock.config(), f)
    os.chmod(cfg, 0o644)                 # deliberately loose: S-02 should fix it
    return d, files


def run(script, cwd, dirname, *args, env=None):
    """Run like upstream does: absolute path, cmd.Dir unset, 10 s ceiling."""
    t0 = time.time()
    e = dict(os.environ)
    e.pop("KIDSOUT_OPENWRT_DRIVER", None)
    e.pop("KIDSOUT_DEVICE_DIR", None)
    e.update(env or {})
    p = subprocess.run([os.path.join(dirname, script), *args], cwd=cwd,
                       capture_output=True, text=True, timeout=10, env=e)
    return p, time.time() - t0


def main():
    s = Suite("upstream script contract")

    with tmpdir() as tmp, MockRouter(device_id=TEST_ID) as mock:
        d, files = deploy(tmp, mock)
        rule = f"kidsout_{TEST_ID}_out"

        with s.case("the three contract scripts exist and are executable") as c:
            for name in SCRIPTS:
                p = os.path.join(d, name)
                c.check(os.path.isfile(p), f"{name} exists")
                c.check(os.stat(p).st_mode & stat.S_IXUSR, f"{name} has the exec bit")

        with s.case("driver.sh install from an unrelated working directory") as c:
            p, _ = run("driver.sh", "/", files, "install")
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            c.check("ALLOWED" in p.stdout, "installs in the allowed state")
            secs = mock.state.sections()
            c.eq(sorted(secs), [rule], "the rule is on the router")
            c.eq(secs[rule]["enabled"], "0", "device still online")

        with s.case("config.json permissions are tightened (S-02)") as c:
            mode = os.stat(os.path.join(files, "config.json")).st_mode & 0o777
            c.eq(oct(mode), oct(0o600), "the credential file is owner-only")

        with s.case("getState.sh prints one contract word") as c:
            p, elapsed = run("getState.sh", "/", d)
            out = p.stdout.strip()
            c.eq(len(out.split()), 1, f"exactly one word, got {p.stdout!r}")
            c.check(out in ("up", "down", "unknown"), f"a contract word: {out!r}")
            c.check(elapsed < 10, f"inside the 10 s timeout ({elapsed:.2f}s)")

        with s.case("getState.sh reports 'up' when the device is busy") as c:
            run("getState.sh", "/", d)                    # baseline
            st = os.path.join(files, ".state.json")
            with open(st) as f:
                data = json.load(f)
            data["counters_ts"] = time.time() - 60
            with open(st, "w") as f:
                json.dump(data, f)
            mock.state.add_traffic(out=4 * 1024 * 1024)
            p, elapsed = run("getState.sh", "/", d)
            c.eq(p.stdout.strip(), "up", "4 MB in the last minute")
            c.eq(p.returncode, 0, "exit 0")
            c.check(elapsed < 3, f"and it is quick ({elapsed:.2f}s)")

        with s.case("block.sh turns the internet off") as c:
            p, elapsed = run("block.sh", "/", d)
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            r = mock.state.sections()[rule]
            c.eq(r["target"], "REJECT", "REJECT rule")
            c.eq(r["enabled"], "1", "and it is ENABLED — internet blocked")
            c.check(mock.state.flushed, "live flows were flushed")
            c.check(elapsed < 10, f"inside the timeout ({elapsed:.2f}s)")

        with s.case("block.sh is cheap to repeat (kidsout calls it every tick)") as c:
            mock.state.calls.clear()
            p, _ = run("block.sh", "/", d)
            c.eq(p.returncode, 0, "exit 0")
            c.eq([x for x in mock.state.write_calls() if x[1] in ("set", "commit", "apply")],
                 [], "no uci write, no commit, no firewall reload")

        with s.case("unblock.sh turns the internet back on") as c:
            p, _ = run("unblock.sh", "/", d)
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            c.eq(mock.state.sections()[rule]["enabled"], "0",
                 "REJECT disabled — internet allowed")

        with s.case("unblock.sh is safe to over-call (the manual escape hatch)") as c:
            p, _ = run("unblock.sh", "/", d)
            c.eq(p.returncode, 0, "exit 0 when already allowed")

        with s.case("a dead router does not hang the tick") as c:
            mock.state.fail = "denied"
            p, elapsed = run("getState.sh", "/", d)
            c.eq(p.stdout.strip(), "unknown", "unknown, not down")
            c.check(elapsed < 5, f"and fast ({elapsed:.2f}s) — a slow device driver "
                                 f"stalls kidsout's whole evaluation tick")
            mock.state.fail = None

        with s.case("a missing config.json is reported as such, not as the router's fault") as c:
            cfg = os.path.join(files, "config.json")
            logf = os.path.join(files, "driver.log")
            mark = os.path.getsize(logf)
            os.rename(cfg, cfg + ".away")
            try:
                p, elapsed = run("getState.sh", "/", d)
            finally:
                os.rename(cfg + ".away", cfg)
            c.eq(p.stdout.strip(), "unknown", "still exactly one contract word")
            c.check(elapsed < 5, f"and it fails locally, without a round trip ({elapsed:.2f}s)")
            with open(logf) as f:
                f.seek(mark)
                tail = f.read()
            c.check("config.json" in tail, "the log names the file that is missing")
            c.check("permission denied" not in tail,
                    "and does not blame rpcd for a fault on this machine")

        with s.case("the driver keeps its own log next to device.json") as c:
            logf = os.path.join(files, "driver.log")
            c.check(os.path.exists(logf), "driver.log was created")
            body = open(logf).read()
            c.check("state=" in body, "state decisions are recorded")
            c.check("internet BLOCKED" in body, "so are enforcement changes")

        with s.case("a missing shared driver is a clear failure, not a hang") as c:
            p, elapsed = run("getState.sh", "/", d,
                             env={"KIDSOUT_OPENWRT_DRIVER": "/nonexistent/driver.py"})
            c.check(p.returncode != 0, "non-zero exit -> kidsout reads unknown")
            c.check(elapsed < 3, "immediately")

        with s.case("KIDSOUT_OPENWRT_DRIVER overrides the relative path") as c:
            p, _ = run("getState.sh", "/", d,
                       env={"KIDSOUT_OPENWRT_DRIVER": os.path.join(HERE, "driver.py")})
            c.check(p.stdout.strip() in ("up", "down", "unknown"),
                    f"the repo's own driver.py served the request: {p.stdout!r}")

        # ---- the template itself ------------------------------------------ #
        with s.case("device.example.json becomes valid once CHANGE_ME is replaced") as c:
            ex = shipped_device_example()
            c.eq(ex["id"], "CHANGE_ME", "ships with the placeholder")
            c.eq(ex["interfaces"], [], "and no MACs")
            m = load_driver(tmp)
            path = os.path.join(tmp, "example.json")
            with open(path, "w") as f:
                json.dump(json.loads(json.dumps(ex).replace("CHANGE_ME", "newdev")), f)
            c.eq(m.load_device(path)["id"], "newdev", "loads and validates")
            try:
                with open(path, "w") as f:
                    json.dump(ex, f)
                m.load_device(path)
                c.check(False, "the untouched placeholder must be refused")
            except SystemExit as e:
                c.check("'id'" in str(e), f"refused with a pointer at id: {e}")

        # ---- the real devices in this repository -------------------------- #
        with s.case("every real device directory matches the template") as c:
            m = load_driver(tmp)
            devices_dir = os.path.join(REPO, "devices")
            found = 0
            for name in sorted(os.listdir(devices_dir)):
                dd = os.path.join(devices_dir, name)
                ff = os.path.join(dd, FILES_SUBDIR)
                if not os.path.isdir(ff):
                    continue
                found += 1
                for sname in SCRIPTS:
                    c.check(filecmp.cmp(os.path.join(CONTRACT, sname), os.path.join(dd, sname),
                                        shallow=False),
                            f"devices/{name}/{sname} is byte-identical to contract/{sname}")
                    c.check(os.stat(os.path.join(dd, sname)).st_mode & stat.S_IXUSR,
                            f"devices/{name}/{sname} is executable")
                c.check(filecmp.cmp(os.path.join(CONTRACT, "driver.sh"),
                                    os.path.join(ff, "driver.sh"), shallow=False),
                        f"devices/{name}/{FILES_SUBDIR}/driver.sh matches the template")
                dev = m.load_device(os.path.join(ff, "device.json"))
                c.eq(dev["id"], name, f"devices/{name}: device.json id equals the directory name")
                c.check(dev.get("interfaces"), f"devices/{name}: has at least one interface")
                gi = os.path.join(ff, ".gitignore")
                c.check(os.path.exists(gi) and "config.json" in open(gi).read(),
                        f"devices/{name}: config.json is gitignored")
            c.check(found >= 1, f"at least one real device uses the driver (found {found})")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
