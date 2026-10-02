#!/usr/bin/python3.11
"""awnix endpoints -- every vendor endpoint this box will dial, and where it came from.

Installed as /usr/libexec/awnix/awnix-endpoints, so `awnix endpoints ...` runs it.

    awnix endpoints show [--json]     merged env chains, one row per variable
    awnix endpoints probe [--json]    the same, plus a 2 s TCP/TLS reachability check
    awnix endpoints apply             restart what reads the files (root): stops the garg
                                      backend on :8900, then re-runs garg-firstboot
    awnix endpoints --list-verbs
    awnix endpoints --self-test

Chains (contract endpoints-env), lowest precedence first, process env beats both:
    awnix  /usr/lib/awnix/endpoints.env     < /etc/awnix/endpoints.env
    garg   /usr/lib/gargbot/appliance.env   < /etc/gargbot/appliance.env
Plain KEY=VALUE, no expansion. A chain whose vendor file is absent is not this box's.

JSON: {"endpoints": [{"var", "value", "source": default|vendor-env|admin-env|process-env,
       "chain": awnix|garg, "file": <the /etc file an admin edits>, "internal": bool,
       "reachable"?: ok|unreachable|off}]}
Only URL-ish values and the documented keys are listed; values are never secrets.

Applying a change: the awnix chain is read by every `awnix` command on its next run,
so it needs nothing. The garg backend reads its chain ONCE at start, and
`systemctl restart garg-firstboot` alone does NOT restart it: garg-firstboot skips any
daemon whose port already answers, and KillMode=process leaves the detached backend
running. `apply` therefore stops the backend itself (SIGTERM, then SIGKILL after 15 s),
re-runs garg-firstboot, and waits up to 90 s for :8900 to answer again.

Exit: 0 ok · 1 (probe) an endpoint that is set is unreachable, or an internal default
is set; (apply) the backend did not come back · 2 could not judge (no chain present on
this box; apply not run as root or systemctl missing). Stdlib only, py3.10-safe.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

VERBS = ("show", "probe", "apply")
CHAINS: Tuple[Tuple[str, str, str], ...] = (
    ("awnix", "/usr/lib/awnix/endpoints.env", "/etc/awnix/endpoints.env"),
    ("garg", "/usr/lib/gargbot/appliance.env", "/etc/gargbot/appliance.env"),
)
PROBE_TIMEOUT = 2.0
GARG_BACKEND_PORT = 8900
GARG_UNIT = "garg-firstboot"
APPLY_HINT = ("edit /etc/awnix/endpoints.env (read by every awnix command, nothing to restart) "
              "or /etc/gargbot/appliance.env, then `sudo awnix endpoints apply` "
              "(a bare `systemctl restart garg-firstboot` skips the running backend)")

#: Fleet-internal hosts (the PRT004 set in check_awnix_portability.py).
_INTERNAL = re.compile(
    r"^(?:aitheros-[a-z0-9-]+|host\.docker\.internal|[a-z0-9.-]+\.aitherium\.internal"
    r"|[a-z0-9.-]+\.ts\.net|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+"
    r"|192\.168\.\d+\.\d+)$", re.IGNORECASE)
_URLISH = re.compile(r"^(?:https?://|[a-z0-9.-]+\.[a-z]{2,}(?:[:/]|$))", re.IGNORECASE)
_ENDPOINT_KEY = re.compile(r"(?:_URL|_HOST|_PING|_ORG|_PYPI|_REGISTRY|_BASE)$")


def read_env(path: str) -> Optional[Dict[str, str]]:
    """KEY=VALUE pairs, or None when the file does not exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        out[k] = v.strip()
    return out


def host_of(value: str) -> str:
    if "://" in value:
        return (urlsplit(value).hostname or "").lower()
    return value.split("/", 1)[0].split(":", 1)[0].lower()


def is_internal(value: str) -> bool:
    return bool(value) and bool(_INTERNAL.match(host_of(value)))


def collect(chains=CHAINS, environ=None) -> Optional[List[dict]]:
    """Merged rows, or None when no chain's vendor file exists on this box."""
    env = os.environ if environ is None else environ
    rows: List[dict] = []
    seen_chain = False
    for name, vendor_path, admin_path in chains:
        vendor = read_env(vendor_path)
        if vendor is None:
            continue
        seen_chain = True
        admin = read_env(admin_path) or {}
        for var in sorted(set(vendor) | set(admin)):
            if var in env:
                value, source = env[var], "process-env"
            elif var in admin:
                value, source = admin[var], "admin-env"
            elif var in vendor:
                value, source = vendor[var], "vendor-env"
            else:  # pragma: no cover - the union makes this unreachable
                continue
            if not (_ENDPOINT_KEY.search(var) or _URLISH.match(value or "")):
                continue  # a mode switch, not an endpoint
            rows.append({"var": var, "value": value, "source": source, "chain": name,
                         "file": admin_path, "internal": is_internal(value)})
    return rows if seen_chain else None


def probe_one(value: str, timeout: float = PROBE_TIMEOUT) -> str:
    """ok | unreachable | off. A TCP connect, plus a TLS handshake for https."""
    if not value:
        return "off"
    if "://" not in value:
        return "ok" if not _URLISH.match(value) else probe_one("https://" + value, timeout)
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "off"
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout) as sock:
            if u.scheme == "https":
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=u.hostname):
                    pass
        return "ok"
    except (OSError, ssl.SSLError):
        return "unreachable"


def render(rows: List[dict]) -> str:
    out = []
    for r in rows:
        flag = "  FLEET-INTERNAL DEFAULT" if r["internal"] else ""
        reach = f" [{r['reachable']}]" if "reachable" in r else ""
        val = r["value"] or "(off)"
        out.append(f"{r['chain']:5} {r['var']:32} {val}  <{r['source']}>{reach}{flag}")
    out.append(APPLY_HINT)
    return "\n".join(out)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def garg_backend_pids(proc_root: str = "/proc", port: int = GARG_BACKEND_PORT) -> List[int]:
    """PIDs of the uvicorn garg backend garg-firstboot starts (app.main:app --port 8900)."""
    pids: List[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return pids
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if "app.main:app" not in argv or not any("uvicorn" in a for a in argv):
            continue
        if "--port" in argv:
            i = argv.index("--port")
            if i + 1 < len(argv) and argv[i + 1] != str(port):
                continue
        pids.append(int(name))
    return sorted(pids)


def apply_garg(chains=CHAINS, *, is_root: Optional[Callable[[], bool]] = None,
               find_pids: Callable[[], List[int]] = garg_backend_pids,
               kill: Callable[[int, int], None] = os.kill,
               listening: Callable[[], bool] = lambda: port_open(GARG_BACKEND_PORT),
               run: Callable[[List[str]], int] = lambda cmd: subprocess.call(cmd, timeout=600),
               sleep: Callable[[float], None] = time.sleep,
               stop_wait: float = 15.0, start_wait: float = 90.0) -> int:
    """Make an edited endpoint file take effect. 0 applied · 1 backend not back · 2 cannot."""
    garg = [c for c in chains if c[0] == "garg"]
    if not garg or read_env(garg[0][1]) is None:
        print("awnix endpoints: no garg chain on this box; /etc/awnix/endpoints.env is read by "
              "every awnix command on its next run, so there is nothing to restart")
        return 0
    root = (os.geteuid() == 0) if is_root is None and hasattr(os, "geteuid") else bool(
        is_root and is_root())
    if not root:
        print("awnix endpoints apply: run as root (sudo awnix endpoints apply)", file=sys.stderr)
        return 2
    pids = find_pids()
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            kill(pid, signal.SIGTERM)
    waited = 0.0
    while pids and waited < stop_wait and (find_pids() or listening()):
        sleep(0.5)
        waited += 0.5
    for pid in find_pids():
        with contextlib.suppress(ProcessLookupError):
            kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    print(f"stopped garg backend pid(s) {pids or 'none running'}; restarting {GARG_UNIT}")
    try:
        rc = run(["systemctl", "restart", GARG_UNIT])
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"awnix endpoints apply: cannot run systemctl ({exc})", file=sys.stderr)
        return 2
    if rc != 0:
        print(f"awnix endpoints apply: systemctl restart {GARG_UNIT} exited {rc}", file=sys.stderr)
    waited = 0.0
    while waited < start_wait:
        if listening():
            print(f"garg backend answering on :{GARG_BACKEND_PORT} with the new endpoints")
            return 0
        sleep(1.0)
        waited += 1.0
    print(f"awnix endpoints apply: garg backend is not answering on :{GARG_BACKEND_PORT} after "
          f"{int(start_wait)} s; see /var/log/gargbot/backend.log", file=sys.stderr)
    return 1


def main(argv: List[str], chains=CHAINS, prober=probe_one) -> int:
    if "--list-verbs" in argv:
        print("\n".join(VERBS))
        return 0
    if "--self-test" in argv:
        return self_test()
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    verb = args[0] if args else "show"
    if verb not in VERBS:
        print(f"awnix endpoints: unknown verb {verb!r} (one of: {', '.join(VERBS)})",
              file=sys.stderr)
        return 2
    if verb == "apply":
        return apply_garg(chains)
    rows = collect(chains)
    if rows is None:
        print("awnix endpoints: no endpoint env chain on this box "
              "(/usr/lib/awnix/endpoints.env or /usr/lib/gargbot/appliance.env)", file=sys.stderr)
        return 2
    code = 0
    if verb == "probe":
        for r in rows:
            r["reachable"] = prober(r["value"])
            if r["reachable"] == "unreachable":
                code = 1
    if any(r["internal"] and r["value"] for r in rows):
        code = 1
    print(json.dumps({"endpoints": rows}, indent=2) if as_json else render(rows))
    return code


def self_test() -> int:
    fails: List[str] = []

    def chk(cond: bool, name: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        def w(name: str, body: str) -> str:
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)
            return p
        v = w("vendor.env", "# c\nAWNIX_LINK_HOST=https://link.example\nAWNIX_PYPI=https://pypi.example/pypi\n"
                            "AWNIX_GITHUB_ORG=Example\nGARGBOT_DEPLOYMENT_MODE=standalone\n")
        a = w("admin.env", "AWNIX_PYPI=https://mirror.example/pypi\n")
        gv = w("gvendor.env", "GENESIS_URL=\nQDRANT_URL=http://127.0.0.1:6333\n"
                              # split literal: PRT004 scans this file
                              "OLD_URL=https://aitheros" "-genesis:8001\n")
        chains = (("awnix", v, a), ("garg", gv, os.path.join(td, "absent.env")))
        rows = collect(chains, environ={"AWNIX_LINK_HOST": "https://proc.example"})
        by = {r["var"]: r for r in rows or []}
        chk(by.get("AWNIX_LINK_HOST", {}).get("source") == "process-env",
            "process env beats both files")
        chk(by.get("AWNIX_PYPI", {}).get("source") == "admin-env", "admin file beats vendor file")
        chk(by.get("AWNIX_PYPI", {}).get("value") == "https://mirror.example/pypi",
            "admin value used")
        chk(by.get("AWNIX_GITHUB_ORG", {}).get("source") == "vendor-env", "vendor default kept")
        chk("GARGBOT_DEPLOYMENT_MODE" not in by, "a mode switch is not an endpoint")
        chk(by.get("OLD_URL", {}).get("internal") is True, "aitheros-* flagged internal")
        chk(by.get("QDRANT_URL", {}).get("internal") is False, "loopback is not internal")
        chk(probe_one("") == "off", "empty value probes as off")
        chk(probe_one("http://127.0.0.1:1", timeout=0.5) == "unreachable",
            "closed port is unreachable")
        absent = (("x", os.path.join(td, "nope"), os.path.join(td, "nope2")),)
        chk(collect(absent, environ={}) is None,
            "no chain present -> None (exit 2)")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["probe", "--json"], chains=chains,
                        prober=lambda val: "ok" if val else "off")
        data = json.loads(buf.getvalue())
        chk(code == 1, "an internal default that is set fails probe (exit 1)")
        chk(all("reachable" in r for r in data["endpoints"]), "probe adds reachable to every row")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc_none = main(["show"], chains=(("x", os.path.join(td, "nope"), ""),))
            rc_bogus = main(["bogus"], chains=chains)
        chk(rc_none == 2, "no chain -> exit 2")
        chk(rc_bogus == 2, "unknown verb -> exit 2")

        # apply: garg-firstboot skips a live :8900, so apply must stop the backend first.
        proc = os.path.join(td, "proc")
        for pid, argv in ((101, ["/usr/bin/python3.11", "-m", "uvicorn", "app.main:app",
                                 "--app-dir", "/opt/gargbot/backend", "--host", "0.0.0.0",
                                 "--port", "8900"]),
                          (102, ["/usr/bin/python3.11", "-m", "uvicorn", "app.main:app",
                                 "--port", "9999"]),
                          (103, ["/opt/qdrant/qdrant"])):
            os.makedirs(os.path.join(proc, str(pid)))
            with open(os.path.join(proc, str(pid), "cmdline"), "wb") as fh:
                fh.write(b"\0".join(a.encode() for a in argv) + b"\0")
        chk(garg_backend_pids(proc) == [101], "finds only the :8900 uvicorn backend")
        state = {"alive": {101}, "events": []}

        def fkill(pid: int, sig: int) -> None:
            state["events"].append(("kill", pid))
            state["alive"].discard(pid)

        def frun(cmd: List[str]) -> int:
            state["events"].append(("run", " ".join(cmd)))
            state["alive"].add(201)
            return 0
        silent = contextlib.redirect_stdout(io.StringIO())
        with silent, contextlib.redirect_stderr(io.StringIO()):
            rc = apply_garg(chains, is_root=lambda: True,
                            find_pids=lambda: sorted(state["alive"] & {101}),
                            kill=fkill, listening=lambda: bool(state["alive"]), run=frun,
                            sleep=lambda s: None)
        chk(rc == 0, "apply returns 0 when the backend answers again")
        chk(state["events"][:2] == [("kill", 101), ("run", "systemctl restart garg-firstboot")],
            "apply stops the running backend BEFORE restarting garg-firstboot")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = apply_garg(chains, is_root=lambda: True, find_pids=lambda: [], kill=fkill,
                            listening=lambda: False, run=lambda c: 0, sleep=lambda s: None,
                            start_wait=2.0)
        chk(rc == 1, "apply returns 1 when the backend does not come back")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc_user = apply_garg(chains, is_root=lambda: False)
            rc_awnix = apply_garg((("awnix", v, a),), is_root=lambda: True)
        chk(rc_user == 2, "apply as non-root -> exit 2")
        chk(rc_awnix == 0, "apply on an awnix-only box has nothing to restart -> 0")
        chk("awnix endpoints apply" in render([]) and "skips the running backend" in render([]),
            "the printed hint names apply, not a bare garg-firstboot restart")
    print(f"self-test: {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
