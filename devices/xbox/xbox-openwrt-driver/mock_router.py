#!/usr/bin/env python3
"""
Mock OpenWrt rpcd ubus-over-HTTP server, for offline testing of xbox.py.

Emulates the subset the driver uses:

    session login / list
    uci get / set / add / delete / commit / apply / revert   (in-memory firewall)
    file exec  of /usr/libexec/kidsout-xbox                  (the router helper)
    system board

and — unlike the v1 mock, which only modelled the happy path and so let every
failure-mode claim in the docs go unverified — it can be told to fail:

    mock.state.fail = "http404"       serve 404 on the ubus path
    mock.state.fail = "badjson"       serve junk instead of JSON-RPC
    mock.state.fail = "login"         reject the login (wrong password)
    mock.state.fail = "denied"        accept the login, deny every call (ACL)
    mock.state.fail = "execfail"      helper returns a non-zero exit code
    mock.state.fail = "nocounters"    helper reports the accounting table missing
    mock.state.fail = "noconntrack"   helper reports the conntrack CLI missing
    mock.state.slow = 5.0             delay every response by N seconds

Use as a library (preferred, for tests):

    from mock_router import MockRouter
    with MockRouter() as mock:          # random port, TLS
        mock.state.add_traffic(out=1_000_000)
        ...

or standalone:  python3 mock_router.py [port]
"""
import atexit
import json
import re
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import http.server

HELPER = "/usr/libexec/kidsout-xbox"


class MockState:
    """Everything the mock router pretends to be."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fail = None
        self.slow = 0.0
        self.password = "test"
        self.sessions = {}
        self.calls = []                     # every (obj, method) seen, for assertions
        self.firewall = {
            "cfg010101": {".type": "zone", ".name": "cfg010101", "name": "lan",
                          "input": "ACCEPT", "forward": "ACCEPT"},
            "cfg020202": {".type": "zone", ".name": "cfg020202", "name": "wan",
                          "input": "REJECT", "forward": "REJECT"},
        }
        self.uncommitted = {}
        self.commits = 0
        self.applies = 0
        self.counters = {"xbox_out": 0, "xbox_in": 0}
        self.counters_installed = False
        self.counter_addrs = []
        self.flushed = []
        self.leases = "1690000000 aa:bb:cc:dd:ee:ff 192.168.2.172 xbox 01:aa:bb:cc:dd:ee:ff\n"
        self.neigh = (
            "192.168.2.172 dev br-lan lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
            "192.168.2.50 dev br-lan lladdr 11:22:33:44:55:66 STALE\n"
            "2a01:db8::172 dev br-lan lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
        )

    # ---- knobs for tests ---------------------------------------------- #
    def add_traffic(self, out=0, inbound=0):
        with self.lock:
            self.counters["xbox_out"] += out
            self.counters["xbox_in"] += inbound

    def reset_counters(self):
        with self.lock:
            self.counters = {"xbox_out": 0, "xbox_in": 0}

    def sections(self, prefix="kidsout_xbox"):
        with self.lock:
            return {n: dict(o) for n, o in self.firewall.items() if n.startswith(prefix)}

    def write_calls(self):
        return [c for c in self.calls if c in (("uci", "set"), ("uci", "add"),
                                               ("uci", "delete"), ("uci", "commit"),
                                               ("uci", "apply"))]


def helper_exec(st, params):
    """Emulate /usr/libexec/kidsout-xbox. Returns {"code":n,"stdout":..,"stderr":..}."""
    verb = params[0] if params else ""
    args = params[1:]

    if st.fail == "execfail":
        return {"code": 1, "stdout": "", "stderr": "mock: forced failure"}

    if verb == "info":
        return {"code": 0, "stderr": "", "stdout": (
            "firewall=fw4\nnft=yes\n"
            f"conntrack_tools={'no' if st.fail == 'noconntrack' else 'yes'}\n"
            "proc_nf_conntrack=yes\nct_acct=1\n"
            f"flowtable_counter={'NO' if st.fail == 'nocounterflag' else 'yes'}\n"
            "flowtable_hw=yes\n"
            f"registered_addrs={' '.join(st.counter_addrs)}\n"
            "openwrt=24.10.0-mock\nipv6_wan=0\n")}

    if verb == "counters":
        # v3 shape: monotonic totals as plain key/value lines. The real helper
        # accumulates per-flow router-side so these never decrease; the
        # 'wentbackwards' failure mode exists to prove the driver copes if the
        # guarantee is ever violated (REVIEW.2 G-04).
        if st.fail == "nocounters" or not st.counters_installed:
            return {"code": 4, "stdout": "", "stderr": "no addresses registered"}
        with st.lock:
            out = int(st.counters.get("xbox_out", 0))
            inb = int(st.counters.get("xbox_in", 0))
        return {"code": 0, "stderr": "",
                "stdout": f"xbox_out {out}\nxbox_in {inb}\nsource conntrack\n"}

    if verb == "counters-install":
        # v3 takes a list of ADDRESSES (the console can be on either of its two
        # interfaces), not a mac/ip pair.
        if not args:
            return {"code": 2, "stdout": "", "stderr": "bad args"}
        for a in args:
            if not re.fullmatch(r"[0-9a-fA-F.:]+", a):
                return {"code": 2, "stdout": "", "stderr": f"bad address: {a}"}
        new = sorted(set(args))
        unchanged = st.counters_installed and st.counter_addrs == new
        st.counters_installed = True
        st.counter_addrs = new
        if not unchanged:
            st.reset_counters()
        return {"code": 0, "stderr": "",
                "stdout": "unchanged\n" if unchanged else "installed\n"}

    if verb == "counters-remove":
        st.counters_installed = False
        st.counter_addrs = []
        st.reset_counters()
        return {"code": 0, "stdout": "removed\n", "stderr": ""}

    if verb == "flush":
        if st.fail == "noconntrack":
            return {"code": 3, "stdout": "", "stderr": "conntrack CLI not installed (opkg install conntrack)"}
        if not args:
            return {"code": 2, "stdout": "", "stderr": "flush needs at least one address"}
        st.flushed.append(list(args))
        return {"code": 0, "stderr": "",
                "stdout": f"flushed {2 * len(args)} entries for " + " ".join(args) + "\n"}

    if verb == "neigh":
        return {"code": 0, "stdout": st.neigh, "stderr": ""}

    if verb == "leases":
        return {"code": 0, "stdout": st.leases, "stderr": ""}

    return {"code": 2, "stdout": "", "stderr": f"unknown verb: {verb}"}


def handle_call(st, params):
    def ok(payload=None):
        return {"jsonrpc": "2.0", "id": 1, "result": [0, payload if payload is not None else {}]}

    def status(n):
        return {"jsonrpc": "2.0", "id": 1, "result": [n]}

    if len(params) < 3:
        return {"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32602, "message": "Invalid params"}}
    sid, obj, method = params[0], params[1], params[2]
    args = params[3] if len(params) > 3 else {}
    st.calls.append((obj, method))

    if obj == "session":
        if method == "login":
            if st.fail == "login" or args.get("password") != st.password:
                return {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": -32002, "message": "Access denied"}}
            token = "mock" + "0" * 28
            st.sessions[token] = args.get("username", "kidsout")
            return ok({"ubus_rpc_session": token,
                       "timeout": args.get("timeout", 300),
                       "acls": {"access-group": {"kidsout-xbox": ["read", "write"]}},
                       "data": {"username": args.get("username")}})
        if method == "list":
            return ok({})
        return status(3)

    if sid not in st.sessions:
        return status(6)                       # permission denied
    if st.fail == "denied":
        return status(6)

    if obj == "system" and method == "board":
        return ok({"model": "Mock Router X1", "release": {"version": "24.10.0-mock"}})

    if obj == "uci":
        with st.lock:
            if method == "get":
                if args.get("config") != "firewall":
                    return ok({"values": {}})
                return ok({"values": {n: dict(o) for n, o in st.firewall.items()}})
            if method == "set":
                sec = args["section"]
                if sec not in st.firewall:
                    return status(4)
                st.firewall[sec].update(args["values"])
                return ok()
            if method == "add":
                name = args.get("name") or f"cfg{len(st.firewall):06d}"
                st.firewall[name] = dict(args.get("values") or {})
                st.firewall[name][".type"] = args["type"]
                st.firewall[name][".name"] = name
                return ok({"section": name})
            if method == "delete":
                st.firewall.pop(args["section"], None)
                return ok()
            if method == "commit":
                st.commits += 1
                return ok()
            if method == "apply":
                st.applies += 1
                return ok({"timeout": args.get("timeout", 30)})
            if method == "revert":
                return ok()
        return status(3)

    if obj == "file" and method == "exec":
        cmd = args.get("command", "")
        if cmd != HELPER:
            # the real ACL only permits this one path
            return status(6)
        return ok(helper_exec(st, args.get("params") or []))

    return status(4)


class Handler(http.server.BaseHTTPRequestHandler):
    state = None
    quiet = True

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        st = self.state
        if st.slow:
            time.sleep(st.slow)
        if st.fail == "http404" or self.path != "/ubus":
            return self._send(404, b"<html>not found</html>", "text/html")
        if st.fail == "badjson":
            return self._send(200, b"<html>this is not json</html>", "text/html")

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
            if req.get("method") == "call":
                resp = handle_call(st, req.get("params", []))
            else:
                resp = {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": -32601, "message": "Method not found"}}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32700, "message": f"Parse error: {e}"}}
        self._send(200, json.dumps(resp).encode())

    def log_message(self, fmt, *a):
        if not self.quiet:
            sys.stderr.write("mock: " + fmt % a + "\n")


def make_cert():
    """Generate a throwaway cert/key in a temp dir, removed at exit.

    v1 kept these in the project directory. They were gitignored, but key
    material as a build artefact is one `git add -f` away from a real problem
    (REVIEW.1 S-01), and nothing needs them to outlive the process.
    """
    d = tempfile.mkdtemp(prefix="kidsout-mock-")
    atexit.register(shutil.rmtree, d, True)
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "1",
                        "-subj", "/CN=mock-router"],
                       capture_output=True)
    if r.returncode != 0 or not os.path.exists(cert):
        raise RuntimeError(f"could not generate a mock certificate: {r.stderr.decode()[:200]}")
    os.chmod(key, 0o600)
    return cert, key


class MockRouter:
    """A mock router running on a background thread."""

    def __init__(self, port=0, tls=True, quiet=True):
        self.state = MockState()
        handler = type("BoundHandler", (Handler,), {"state": self.state, "quiet": quiet})
        # threading, like real rpcd: a single-threaded server would let the
        # `slow` failure mode block every subsequent request in a test run
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        if tls:
            cert, key = make_cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.tls = tls
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}/ubus"

    def config(self, **over):
        cfg = {"host": "127.0.0.1", "port": self.port, "https": self.tls,
               "allow_http_fallback": False, "verify_tls": False,
               "username": "kidsout", "password": self.state.password,
               "timeout": 5, "state_timeout": 2, "state_deadline": 7}
        cfg.update(over)
        return cfg

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    m = MockRouter(port=port, quiet=False).start()
    print(f"mock rpcd on {m.url}   (password: {m.state.password})")
    print("counters start at zero; the console is 'idle' until you feed it traffic.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        m.stop()


if __name__ == "__main__":
    main()
