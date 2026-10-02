#!/usr/bin/python3.11
"""awnix-console -- the appliance's one web console and its CLI (`awnix console ...`).

Installed as /usr/libexec/awnix/awnix-console; the package lives in
/usr/lib/awnix-console/awnix_console. Contract: `appliance-web-api` and
`awnix-dispatcher-and-cli-conventions`.

    awnix console serve            run the server (awnix-console.service)
    awnix console prepare          codes + certificate + firewalld port (ExecStartPre)
    awnix console url              https://<ip>:9443/
    awnix console code             the login code for the current mode (root only)
    awnix console status [--json]  mode, bind, fingerprint, listening
    awnix-console --self-test [--profile garg]   hermetic, stubbed CLIs, ephemeral port
    awnix-console --dump-fixtures FILE           JSON fixtures for the awkit client tests
    awnix-console --list-verbs

Exit: 0 ok, 1 failed, 2 could not judge.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _p in ("/usr/lib/awnix-console", str(_HERE)):
    if os.path.isdir(os.path.join(_p, "awnix_console")) and _p not in sys.path:
        sys.path.insert(0, _p)

from awnix_console import (  # noqa: E402
    BOOTC_DIR,
    VERSION,
    Settings,
    load_conf,
    read_release,
    resolve_bind,
)
from awnix_console import auth as authmod  # noqa: E402
from awnix_console import server as srvmod  # noqa: E402
from awnix_console import tls as tlsmod  # noqa: E402

VERBS = ("serve", "prepare", "url", "code", "status")


def settings_from_system() -> Settings:
    return load_conf(Settings.from_env())


# --------------------------------------------------------------------------- verbs

def cmd_url(s: Settings) -> int:
    print(srvmod.console_url(s))
    return 0


def cmd_code(s: Settings) -> int:
    path = s.code_path()
    code = authmod.read_secret(path)
    if code is None:
        if os.path.exists(path):
            print(f"awnix console: cannot read {path} -- run it with sudo", file=sys.stderr)
            return 1
        # The mode can flip under a running service (setup-rerun removes setup.json),
        # so the code for the current mode may never have been minted. Mint it here
        # (root only; the server reads the file on every login) and refresh the banner.
        try:
            code = authmod.ensure_code(path)
        except OSError:
            code = None
        if code:
            helper = s.issue_helper
            if os.path.isfile(helper) and os.access(helper, os.X_OK):
                subprocess.run([helper], timeout=30, check=False, capture_output=True)
            print(code)
            return 0
        print(f"awnix console: no code issued yet ({path} absent); is awnix-console "
              "running?", file=sys.stderr)
        return 2
    print(code)
    return 0


def cmd_status(s: Settings, as_json: bool) -> int:
    bind = resolve_bind(s)
    probe_host = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
    listening = False
    try:
        with socket.create_connection((probe_host, s.port), timeout=2):
            listening = True
    except OSError:
        listening = False
    rel = read_release(s)
    st = {
        "version": VERSION,
        "mode": s.mode(),
        "bind": bind,
        "port": s.port,
        "url": srvmod.console_url(s),
        "fingerprint": tlsmod.read_fp(s.tls_dir),
        "listening": listening,
        "static_installed": os.path.isfile(os.path.join(s.static_dir, "index.html")),
        "variant": rel.get("AWNIX_VARIANT"),
        "profile": s.profile,
        "brand": s.brand,
    }
    if as_json:
        print(json.dumps(st, indent=2, sort_keys=True))
    else:
        for k in sorted(st):
            print(f"{k:17} {st[k]}")
    return 0 if listening else 1


# --------------------------------------------------------------------------- harness

STUB = r'''
import json, os, sys
name = os.path.basename(sys.argv[0])
if name.endswith(".py"):
    name = name[:-3]
key = " ".join([name] + sys.argv[1:])
data = sys.stdin.read() if not sys.stdin.isatty() else ""
scen = {}
p = os.environ.get("AWNIX_STUB_SCENARIO")
if p and os.path.isfile(p):
    with open(p, encoding="utf-8") as fh:
        scen = json.load(fh)
log = os.environ.get("AWNIX_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "stdin_len": len(data),
                             "stdin_head": data[:8]}) + "\n")
ent = scen.get(key, {})
out = ent.get("stdout")
if out is None:
    out = json.dumps({"ok": True, "argv": [name] + sys.argv[1:], "stdin_len": len(data)})
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stdout.write("\n")
sys.exit(int(ent.get("exit", 0)))
'''

SETUP_API_STUB = r'''
_STATE = {"hostname": "box", "steps": {}}
def steps():
    return {"steps": [{"schema": 1, "id": "hostname", "title": "Hostname", "why": "Name it",
                       "kind": "text", "required": False}]}
def state():
    return dict(_STATE)
def apply(step_id, value):
    if step_id != "hostname":
        raise KeyError("unknown step " + step_id)
    _STATE["steps"][step_id] = {"status": "done"}
    return {"ok": True, "id": step_id}
def finish():
    return {"ok": True, "restarting": True}
'''

LICENSE_STATES = ("unlicensed", "valid", "expired", "invalid", "revoked", "refused",
                  "offline")
LICENSE_EXIT = {"unlicensed": 1, "valid": 0, "expired": 1, "invalid": 1, "revoked": 1,
                "refused": 1, "offline": 2}


def license_fixture(state: str, product: str = "garg") -> Dict[str, Any]:
    armed = state == "valid"
    return {
        "schema": 1, "state": state,
        "lic_id": None if state == "unlicensed" else "lic_TEST0001",
        "sku": None if state == "unlicensed" else f"{product}-appliance-pro",
        "tier": None if state == "unlicensed" else "pro",
        "exp": 0 if state != "expired" else 1700000000,
        "checked_at": "2026-09-27T12:00:00Z",
        "detail": f"fixture: {state}",
        "entitlements": {"appliance_tier": "pro" if armed else None,
                         "images": [f"{product}-appliance"] if armed else [],
                         "packs": [product] if armed else []},
        "registry": {"state": "armed" if armed else
                     ("offline" if state == "offline" else
                      "refused" if state in ("refused", "revoked", "invalid") else
                      "unconfigured"),
                     "expires_at": "2026-09-27T12:45:00Z" if armed else None,
                     "images": 1 if armed else 0},
    }


UPDATE_FIXTURE = {"checked_at": "2026-09-27T12:00:00Z", "state": "current",
                  "channel": "stable", "booted_digest": "sha256:" + "a" * 64,
                  "available_digest": "sha256:" + "a" * 64, "staged_digest": None,
                  "signer_identity": "https://github.com/Aitherium/awnix/.github/workflows/"
                                     "build-awnix-iso.yml@refs/heads/main",
                  "rollback_available": True, "auto_apply": False, "detail": ""}
COMPONENTS_FIXTURE = {"ok": True, "op": "list", "results": [
    {"id": "awdk", "kind": "pypi", "state": "installed", "version": "3.8.26",
     "pin": "3.8.26", "previous_pin": None, "reason": "", "log_tail": ""},
    {"id": "awdesk", "kind": "container", "state": "needs-license", "version": "1.0.0",
     "pin": "sha256:" + "b" * 64, "previous_pin": None,
     "reason": "requires an activated license", "log_tail": ""},
]}
ENDPOINTS_FIXTURE = {"endpoints": [
    {"var": "AWNIX_LINK_HOST", "value": "https://link.example.net", "source": "default",
     "internal": False},
    {"var": "AITHER_LICENSE_EXCHANGE_URL",
     "value": "https://license.example.net/v1/licenses/exchange-pull-token",
     "source": "vendor-env", "internal": False, "reachable": "ok"},
]}
SETUP_STATUS_FIXTURE = {"version": 2, "hostname": "garg-box", "admin_user": "admin",
                        "ssh_key_fingerprints": ["SHA256:fixture"], "core": [],
                        "installed": [], "linked": False, "link_host": "",
                        "steps": {"hostname": {"status": "done",
                                               "at": "2026-09-27T11:00:00Z"}},
                        "completed_at": "2026-09-27T11:05:00Z"}
LOCK_FIXTURE = {"catalogue_sha256": "c" * 64, "generated_at": "2026-09-27T00:00:00Z",
                "components": [{"id": "awdk"}, {"id": "awdesk"}]}
LICENSE_OK = "AITHER1.eyJza3UiOiJ0ZXN0In0.c2lnbmF0dXJl"


#: The tenant the awkit fixtures are dumped for. awkit (and its tests) mirror to a
#: public repo that must not name a customer (check_adk_publishable AWK003), so the
#: fixtures come from a SYNTHETIC tenant appliance derived from the real garg row of
#: the surfaces matrix: same shape, same delivery rules, neutral names.
FIXTURE_TENANT = {"profile": "acme", "brand": "AcmeBot", "variant": "acme-appliance",
                  "surface": "acme_ui", "hostname": "acme-box"}


def synthetic_surfaces(real: Dict[str, Any]) -> Dict[str, Any]:
    """The surfaces matrix plus an `acme-appliance` row cloned from `garg-appliance`."""
    ft = FIXTURE_TENANT
    data = json.loads(json.dumps(real))
    row = json.loads(json.dumps(data["variants"]["garg-appliance"]))
    row["console"].update(profile=ft["profile"], brand=ft["brand"])
    row["surfaces"][ft["surface"]] = row["surfaces"].pop("gargbot_ui")
    data["variants"][ft["variant"]] = row
    entry = json.loads(json.dumps(data["surface_catalog"]["gargbot_ui"]))
    entry.update(label=ft["brand"], description=f"The {ft['brand']} product on this box.")
    data["surface_catalog"][ft["surface"]] = entry
    return data


def _read_matrix(path: Path) -> Dict[str, Any]:
    body = "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    return json.loads(body)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Harness:
    """A real ConsoleServer on 127.0.0.1:<ephemeral> over a temp tree with stub CLIs."""

    def __init__(self, *, profile: str = "awnix", tls_wanted: bool = True) -> None:
        self.tmp = tempfile.mkdtemp(prefix="awnix-console-selftest-")
        t = Path(self.tmp)
        for d in ("etc", "run/awnix", "run/awnix-console", "tls", "static", "bin",
                  "setupapi/awnix_setup", "lib/console.d", "share", "state"):
            (t / d).mkdir(parents=True, exist_ok=True)
        self.scenario = t / "scenario.json"
        self.log = t / "stub.log"
        self.scenario.write_text("{}", encoding="utf-8")
        (t / "static" / "index.html").write_text("<!doctype html><title>t</title>",
                                                 encoding="utf-8")
        (t / "share" / "guide.json").write_text(json.dumps({"version": 1, "chapters": []}),
                                                encoding="utf-8")
        (t / "share" / "lock.json").write_text(json.dumps(LOCK_FIXTURE), encoding="utf-8")
        (t / "setupapi" / "awnix_setup" / "__init__.py").write_text("", encoding="utf-8")
        (t / "setupapi" / "awnix_setup" / "api.py").write_text(SETUP_API_STUB,
                                                               encoding="utf-8")
        variant = ("garg-appliance" if profile == "garg" else
                   FIXTURE_TENANT["variant"] if profile == FIXTURE_TENANT["profile"] else "awnix")
        (t / "release.env").write_text(f"AWNIX_VARIANT={variant}\n", encoding="utf-8")
        (t / "lib" / "console.conf").write_text(
            "AWNIX_CONSOLE_BIND=auto\nAWNIX_CONSOLE_PORT=9443\nAWNIX_CONSOLE_PROFILE=awnix\n"
            "AWNIX_CONSOLE_BRAND=awnix\n", encoding="utf-8")
        if profile == "garg":
            (t / "lib" / "console.d" / "garg.conf").write_text(
                "AWNIX_CONSOLE_PROFILE=garg\nAWNIX_CONSOLE_BRAND=GargBot\n"
                "AWNIX_CONSOLE_BIND=0.0.0.0\n", encoding="utf-8")
        surfaces_override = None
        if profile == FIXTURE_TENANT["profile"]:
            ft = FIXTURE_TENANT
            (t / "lib" / "console.d" / f"{ft['profile']}.conf").write_text(
                f"AWNIX_CONSOLE_PROFILE={ft['profile']}\nAWNIX_CONSOLE_BRAND={ft['brand']}\n"
                "AWNIX_CONSOLE_BIND=0.0.0.0\n", encoding="utf-8")
            surfaces_override = t / "share" / "surfaces.yaml"
            surfaces_override.write_text(json.dumps(synthetic_surfaces(
                _read_matrix(BOOTC_DIR / "awnix-surfaces.yaml")), indent=2), encoding="utf-8")
        for name in ("aitheros", "awnix-update", "awnix-component", "awnix-setup",
                     "systemd-run"):
            self._stub(name)  # awnix-endpoints deliberately absent -> 501 leg
        s = Settings(
            setup_marker=str(t / "etc" / "setup.json"),
            setup_code=str(t / "run" / "awnix" / "setup-code"),
            console_token=str(t / "run" / "awnix-console" / "token"),
            tls_dir=str(t / "tls"), static_dir=str(t / "static"),
            guide_json=str(t / "share" / "guide.json"),
            # The installed matrix when this runs IN an image (the Containerfile RUN),
            # else the repo copy beside the package (a missing file falls back to it).
            surfaces_file=(str(surfaces_override) if surfaces_override else
                           Settings().surfaces_file
                           if os.path.isfile(Settings().surfaces_file)
                           else str(t / "share" / "no-such-surfaces.yaml")),
            release_env=str(t / "release.env"),
            components_lock=str(t / "share" / "lock.json"),
            license_status=str(t / "state" / "license.json"),
            update_status=str(t / "state" / "update.json"),
            vendor_conf=str(t / "lib" / "console.conf"),
            vendor_conf_d=str(t / "lib" / "console.d"),
            admin_conf=str(t / "etc" / "console.conf"),
            cloud_id=str(t / "run" / "cloud-id"),
            issue_helper=str(t / "bin" / "no-issue-helper"),
            setup_api_path=str(t / "setupapi"),
            tool_path=str(t / "bin"),
            child_env={"AWNIX_STUB_SCENARIO": str(self.scenario),
                       "AWNIX_STUB_LOG": str(self.log)},
        )
        s = load_conf(s, environ={})
        s.port = 0
        self.tls_note = ""
        s.tls = False
        if tls_wanted:
            try:
                tlsmod.ensure_cert(s.tls_dir, hostname="selftest")
                s.tls = True
            except RuntimeError as exc:
                self.tls_note = f"TLS leg SKIPPED: {exc}"
        self.settings = s
        self.clock = FakeClock()
        authmod.ensure_code(s.console_token)
        self.srv = srvmod.build_server(s, bind="127.0.0.1", clock=self.clock)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        self.cookie: Optional[str] = None

    def _stub(self, name: str) -> None:
        b = Path(self.tmp) / "bin"
        if os.name == "nt":
            (b / f"{name}.py").write_text(STUB, encoding="utf-8")
        else:
            p = b / name
            p.write_text(f"#!{sys.executable}\n" + STUB, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def scen(self, mapping: Dict[str, Dict[str, Any]]) -> None:
        self.scenario.write_text(json.dumps(mapping), encoding="utf-8")

    def stub_calls(self) -> List[Dict[str, Any]]:
        if not self.log.is_file():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x]

    def set_mode(self, mode: str, *, mint: bool = True) -> None:
        marker = Path(self.settings.setup_marker)
        if mode == "console":
            marker.write_text(json.dumps({"version": 2}), encoding="utf-8")
        elif marker.exists():
            marker.unlink()
        if mode == "setup" and mint:
            authmod.ensure_code(self.settings.setup_code)
        self.cookie = None

    def code(self) -> str:
        return authmod.read_secret(self.settings.code_path()) or ""

    def request(self, method: str, path: str, body: Any = None, *,
                headers: Optional[Dict[str, str]] = None, origin: Optional[str] = "same",
                console_header: bool = True, cookie: bool = True,
                timeout: float = 30) -> Tuple[int, Any, Dict[str, str]]:
        if self.settings.tls:
            ctx = tlsmod.pinned_client_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                "127.0.0.1", self.port, context=ctx, timeout=timeout)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h: Dict[str, str] = {}
        scheme = "https" if self.settings.tls else "http"
        if method == "POST":
            if console_header:
                h["X-Awnix-Console"] = "1"
            if origin == "same":
                h["Origin"] = f"{scheme}://127.0.0.1:{self.port}"
            elif origin:
                h["Origin"] = origin
        if cookie and self.cookie:
            h["Cookie"] = f"{authmod.COOKIE_NAME}={self.cookie}"
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        if self.settings.tls:
            conn.connect()
            fp = tlsmod.read_fp(self.settings.tls_dir) or ""
            sock = conn.sock
            if not (isinstance(sock, ssl.SSLSocket) and tlsmod.verify_pin(sock, fp)):
                raise AssertionError("TLS pin mismatch against cert.fp")
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        try:
            parsed: Any = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")
        sc = hdrs.get("set-cookie", "")
        if sc.startswith(authmod.COOKIE_NAME + "="):
            val = sc.split(";", 1)[0].split("=", 1)[1]
            self.cookie = val or None
        return resp.status, parsed, hdrs

    def login(self) -> int:
        st, _, _ = self.request("POST", "/api/login", {"code": self.code()})
        return st

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- self-test

def self_test(profile: str = "awnix") -> int:
    os.environ["AWNIX_CONSOLE_QUIET"] = "1"
    fails: List[str] = []

    def check(name: str, cond: bool, got: Any = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  (got {got!r})"))
        if not cond:
            fails.append(name)

    try:
        h = Harness(profile=profile)
    except Exception as exc:  # noqa: BLE001 -- harness failure = could not judge
        print(f"awnix-console self-test: could not start the harness: {exc!r}")
        return 2
    try:
        if h.tls_note:
            print(f"  note {h.tls_note}")
        else:
            check("tls: cert.fp is a sha256 and pins the served cert",
                  len(tlsmod.read_fp(h.settings.tls_dir) or "") == 64)
        h.set_mode("console")
        st, body, hdrs = h.request("GET", "/api/health")
        check("health is open (200)", st == 200 and body.get("ok") is True, st)
        check("CSP default-src 'self' on every response",
              "default-src 'self'" in hdrs.get("content-security-policy", ""))
        st, _, _ = h.request("GET", "/api/appliance/license")
        check("no session -> 401", st == 401, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/update-check", {})
        check("POST without a session -> 401", st == 401, st)

        # lockout: 5 wrong codes -> 423, and the right code is refused while locked
        codes = [h.request("POST", "/api/login", {"code": "WRONGWRONG"})[0] for _ in range(5)]
        check("4 wrong codes -> 401, 5th -> 423", codes == [401, 401, 401, 401, 423], codes)
        st, body, hdrs = h.request("POST", "/api/login", {"code": h.code()})
        check("locked: right code still 423 with Retry-After",
              st == 423 and "retry-after" in hdrs, st)
        h.clock.t += 61
        check("after 60 s the right code logs in (200)", h.login() == 200)
        st, body, _ = h.request("GET", "/api/session")
        check("session reports console mode + authenticated",
              body.get("mode") == "console" and body.get("authenticated") is True, body)

        # reads
        h.scen({"aitheros status --json": {"stdout": license_fixture("valid"), "exit": 0},
                "awnix-update status --json": {"stdout": UPDATE_FIXTURE},
                "awnix-component list --json": {"stdout": COMPONENTS_FIXTURE}})
        st, body, _ = h.request("GET", "/api/appliance/license")
        check("license read with session -> 200 + CLI JSON",
              st == 200 and body["result"]["state"] == "valid", (st, body))
        h.scen({"aitheros status --json": {"stdout": license_fixture("revoked"), "exit": 1}})
        st, body, _ = h.request("GET", "/api/appliance/license")
        check("license exit 1 -> 409 'refused' carrying the status",
              st == 409 and body["state"] == "refused" and body["result"]["state"] == "revoked",
              (st, body))
        st, body, _ = h.request("GET", "/api/appliance/endpoints")
        check("missing binary -> 501 available:false",
              st == 501 and body.get("available") is False, (st, body))
        st, body, _ = h.request("GET", "/api/appliance/surfaces")
        ids = [x["id"] for x in (body.get("result") or {}).get("surfaces", [])] if st == 200 else []
        check("surfaces served for the variant", st == 200 and "console" in ids, (st, body))
        if profile == "garg":
            g = [x for x in body["result"]["surfaces"] if x["id"] == "gargbot_ui"]
            check("garg: gargbot_ui link with <host> substituted",
                  bool(g) and g[0].get("url") == "https://127.0.0.1:8900", g)
            check("garg: brand GargBot from console.d", h.settings.brand == "GargBot",
                  h.settings.brand)
            check("garg: Living Desktop/AOL/Veil link-only",
                  all(x["delivery"] == "link" for x in body["result"]["surfaces"]
                      if x["id"] in ("living_desktop", "aol", "veil")))
        st, body, _ = h.request("GET", "/api/appliance/overview")
        check("overview aggregates (200)", st == 200 and "variant" in body["result"], st)
        st, _, _ = h.request("GET", "/api/appliance/nonsense")
        check("unknown section -> 404", st == 404, st)

        # actions
        h.scen({})
        st, _, _ = h.request("POST", "/api/appliance/actions/update-check", {},
                             console_header=False)
        check("POST without X-Awnix-Console -> 403", st == 403, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/update-check", {},
                             origin="https://evil.example")
        check("cross-origin POST -> 403", st == 403, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/update-check", {}, origin=None)
        check("cookie POST without Origin -> 403", st == 403, st)
        st, body, _ = h.request("POST", "/api/appliance/actions/format-disk", {})
        check("unknown verb -> 404", st == 404, st)
        st, body, _ = h.request("POST", "/api/appliance/actions/update-check", {})
        check("update-check exit 0 -> 200", st == 200 and body["exit"] == 0, (st, body))
        for rc, want in ((1, 409), (2, 503), (3, 402)):
            h.scen({"awnix-update check": {"exit": rc, "stdout": {"state": "x"}}})
            st, body, _ = h.request("POST", "/api/appliance/actions/update-check", {})
            check(f"CLI exit {rc} -> {want}", st == want and body["exit"] == rc, (st, body))
        h.scen({})
        st, _, _ = h.request("POST", "/api/appliance/actions/update-channel",
                             {"channel": "lts"})
        check("channel outside stable|beta -> 422", st == 422, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/component-install",
                             {"id": "not-in-lock"})
        check("component id outside the lock -> 422", st == 422, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/component-install",
                             {"id": "../../etc"})
        check("component id failing the regex -> 422", st == 422, st)
        st, body, _ = h.request("POST", "/api/appliance/actions/component-install",
                                {"id": "awdk"})
        check("component id in the lock -> 200 argv [install awdk --json]",
              st == 200 and body["result"]["argv"] == ["awnix-component", "install", "awdk",
                                                       "--json"], (st, body))
        st, _, _ = h.request("POST", "/api/appliance/actions/license-import",
                             {"license": "not a license; rm -rf /"})
        check("malformed license -> 422", st == 422, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/license-import",
                             {"license": "AITHER1." + "A" * 17000 + ".B"})
        check("license over 16 KiB -> 413", st == 413, st)
        st, body, _ = h.request("POST", "/api/appliance/actions/license-import",
                                {"license": LICENSE_OK})
        calls = [c for c in h.stub_calls() if c["key"] == "aitheros license import - --json"]
        check("license-import feeds the envelope on stdin, never argv",
              st == 200 and calls and calls[-1]["stdin_head"] == "AITHER1.", (st, calls))
        check("stdout_tail redacts the envelope",
              LICENSE_OK not in json.dumps(body), body.get("stdout_tail"))
        st, body, _ = h.request("POST", "/api/appliance/actions/update-apply", {})
        calls = [c["key"] for c in h.stub_calls() if c["key"].startswith("systemd-run")]
        check("update-apply is detached via systemd-run --no-block",
              st == 200 and calls and "--no-block" in calls[-1], (st, calls))
        st, body, _ = h.request("POST", "/api/appliance/actions/endpoints-probe", {})
        check("action whose binary is absent -> 501", st == 501, st)

        # loopback bearer (the awkit-backend proxy)
        tok = authmod.read_secret(h.settings.console_token)
        st, _, _ = h.request("GET", "/api/appliance/license", cookie=False,
                             headers={"Authorization": f"Bearer {tok}"})
        check("loopback Bearer <console token> -> authorised", st in (200, 409), st)
        st, _, _ = h.request("GET", "/api/appliance/license", cookie=False,
                             headers={"Authorization": "Bearer nope"})
        check("wrong Bearer -> 401", st == 401, st)
        st, _, _ = h.request("POST", "/api/appliance/actions/update-check", {}, cookie=False,
                             origin=None, headers={"Authorization": f"Bearer {tok}"})
        check("Bearer POST without Origin (server-side proxy) -> 200", st == 200, st)

        # static + guide
        st, body, hdrs = h.request("GET", "/")
        check("static index served with CSP", st == 200
              and "default-src 'self'" in hdrs.get("content-security-policy", ""), st)
        st, _, _ = h.request("GET", "/..%2f..%2fetc/passwd")
        check("path traversal -> 404", st == 404, st)
        st, body, _ = h.request("GET", "/guide.json")
        check("/guide.json served", st == 200 and body.get("version") == 1, st)

        # setup mode
        h.set_mode("setup")
        st, body, _ = h.request("GET", "/api/session")
        check("no setup.json -> setup mode", body.get("mode") == "setup", body)
        check("setup-mode login with the setup code", h.login() == 200)
        st, _, _ = h.request("GET", "/api/appliance/license")
        check("appliance routes refused in setup mode (409)", st == 409, st)
        st, body, _ = h.request("GET", "/api/setup/steps")
        check("setup steps delegated to awnix_setup.api", st == 200
              and body["steps"][0]["id"] == "hostname", (st, body))
        st, body, _ = h.request("POST", "/api/setup/steps/hostname", {"value": "box"})
        check("setup step apply -> 200", st == 200 and body.get("ok") is True, (st, body))
        st, _, _ = h.request("POST", "/api/setup/steps/unknown", {"value": "x"})
        check("unknown step -> 422", st == 422, st)
        h.srv.setup_api._mod = None
        h.srv.setup_api._err = "awnix_setup.api unavailable: stub removed"
        st, _, _ = h.request("GET", "/api/setup/steps")
        check("setup api import failure -> 501", st == 501, st)

        # the setup code is never rotated by failures (it is the only one the owner has)
        before = h.code()
        for _ in range(20):
            h.clock.t += 1000
            h.request("POST", "/api/login", {"code": "WRONGWRONG"})
        check("setup mode: 20 wrong codes do NOT rotate the setup code", h.code() == before)

        # setup-rerun under a running server: console -> setup with no setup code yet
        h.set_mode("console")
        h.clock.t += 1000
        check("console login before the rerun", h.login() == 200)
        Path(h.settings.setup_code).unlink()
        h.set_mode("setup", mint=False)
        st, body, _ = h.request("GET", "/api/session")
        check("rerun: mode flips to setup", body.get("mode") == "setup", body)
        check("rerun: the setup code is minted on the flip",
              len(authmod.read_secret(h.settings.setup_code) or "") == 10)
        check("rerun: setup-mode login with the fresh code -> 200", h.login() == 200)

        # console mode: rotation after 20 failures
        h.set_mode("console")
        before = h.code()
        for _ in range(20):
            h.clock.t += 1000
            h.request("POST", "/api/login", {"code": "WRONGWRONG"})
        check("console mode: 20 wrong codes rotate the code",
              h.code() != before and len(h.code()) == 10)

        # a client that opens TCP and never sends a ClientHello must not stall others
        if h.settings.tls:
            idle = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            try:
                try:
                    st = h.request("GET", "/api/health", timeout=5)[0]
                except OSError as exc:
                    st = f"{type(exc).__name__}: {exc}"
                check("an idle pre-handshake socket does not stall /api/health", st == 200, st)
            finally:
                idle.close()
    except Exception as exc:  # noqa: BLE001 -- an exception mid-suite is a failure
        check(f"suite ran to completion ({type(exc).__name__}: {exc})", False)
    finally:
        h.close()
    total = "FAIL" if fails else "PASS"
    print(f"awnix-console self-test ({profile}): {total}"
          + (f" -- {len(fails)} failed" if fails else ""))
    return 1 if fails else 0


# --------------------------------------------------------------------------- fixtures

def dump_fixtures(out: str) -> int:
    """Real responses from a real server, for the awkit client contract tests."""
    os.environ["AWNIX_CONSOLE_QUIET"] = "1"
    ft = FIXTURE_TENANT
    h = Harness(profile=ft["profile"], tls_wanted=False)
    fx: Dict[str, Any] = {"_generated_by": "awnix-console.py --dump-fixtures",
                          "_version": VERSION}

    def rec(name: str, st: int, body: Any) -> None:
        fx[name] = {"status": st, "body": body}

    try:
        h.set_mode("setup")
        rec("session_setup", *h.request("GET", "/api/session")[:2])
        h.set_mode("console")
        rec("session_anonymous", *h.request("GET", "/api/session")[:2])
        rec("unauthorized", *h.request("GET", "/api/appliance/license")[:2])
        rec("login_bad", *h.request("POST", "/api/login", {"code": "WRONGWRONG"})[:2])
        for _ in range(3):
            h.request("POST", "/api/login", {"code": "WRONGWRONG"})
        rec("login_locked", *h.request("POST", "/api/login", {"code": "WRONGWRONG"})[:2])
        h.clock.t += 61
        rec("login_ok", h.login(), {"authenticated": True, "mode": "console"})
        rec("session_console", *h.request("GET", "/api/session")[:2])
        for state in LICENSE_STATES:
            h.scen({"aitheros status --json": {"stdout": license_fixture(state, ft["profile"]),
                                               "exit": LICENSE_EXIT[state]}})
            rec(f"license_{state}", *h.request("GET", "/api/appliance/license")[:2])
        h.scen({"awnix-update status --json": {"stdout": UPDATE_FIXTURE},
                "awnix-component list --json": {"stdout": COMPONENTS_FIXTURE},
                "awnix-setup --status --json": {"stdout": dict(SETUP_STATUS_FIXTURE,
                                                               hostname=ft["hostname"])},
                "awnix-component install awdesk --json": {"exit": 3, "stdout": {
                    "ok": False, "op": "install", "results": [dict(
                        COMPONENTS_FIXTURE["results"][1], state="needs-license")]}}})
        rec("updates", *h.request("GET", "/api/appliance/updates")[:2])
        rec("components", *h.request("GET", "/api/appliance/components")[:2])
        rec("setup_status", *h.request("GET", "/api/appliance/setup")[:2])
        rec("endpoints_unavailable", *h.request("GET", "/api/appliance/endpoints")[:2])
        rec("surfaces_tenant", *h.request("GET", "/api/appliance/surfaces")[:2])
        rec("action_ok", *h.request("POST", "/api/appliance/actions/update-check", {})[:2])
        rec("action_not_entitled", *h.request(
            "POST", "/api/appliance/actions/component-install", {"id": "awdesk"})[:2])
        rec("action_invalid", *h.request(
            "POST", "/api/appliance/actions/update-channel", {"channel": "lts"})[:2])
        rec("action_unknown", *h.request("POST", "/api/appliance/actions/nope", {})[:2])
        h.scen({"awnix-update check": {"exit": 1, "stdout": {"state": "unsigned-refused"}}})
        rec("action_refused", *h.request("POST", "/api/appliance/actions/update-check", {})[:2])
        h.scen({"awnix-update check": {"exit": 2, "stdout": {"state": "offline"}}})
        rec("action_offline", *h.request("POST", "/api/appliance/actions/update-check", {})[:2])
        rec("action_unavailable", *h.request(
            "POST", "/api/appliance/actions/endpoints-probe", {})[:2])
    finally:
        h.close()
    data = json.loads(json.dumps(fx, sort_keys=True))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"awnix-console: wrote {len(data) - 2} fixtures to {out}")
    return 0


# --------------------------------------------------------------------------- main

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix-console", description=__doc__.split("\n")[1])
    ap.add_argument("verb", nargs="?", choices=VERBS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--profile", default="awnix", choices=("awnix", "garg"))
    ap.add_argument("--dump-fixtures", metavar="FILE")
    ap.add_argument("--list-verbs", action="store_true")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.list_verbs:
        print("\n".join(VERBS))
        return 0
    if a.version:
        print(VERSION)
        return 0
    if a.self_test:
        return self_test(a.profile)
    if a.dump_fixtures:
        return dump_fixtures(a.dump_fixtures)
    if not a.verb:
        ap.print_help()
        return 2
    try:
        s = settings_from_system()
    except (OSError, ValueError) as exc:
        print(f"awnix-console: cannot read configuration: {exc}", file=sys.stderr)
        return 2
    if a.verb == "serve":
        return srvmod.serve(s)
    if a.verb == "prepare":
        try:
            info = srvmod.prepare(s)
        except (OSError, RuntimeError) as exc:
            print(f"awnix-console prepare: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info, sort_keys=True))
        return 0
    if a.verb == "url":
        return cmd_url(s)
    if a.verb == "code":
        return cmd_code(s)
    return cmd_status(s, a.json)


if __name__ == "__main__":
    sys.exit(main())
