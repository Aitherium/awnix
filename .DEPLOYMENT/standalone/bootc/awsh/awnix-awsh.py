#!/usr/bin/python3.11
"""awnix-awsh -- awsh on awnix: offline doctor, first-run, status and config layers.

Installed as /usr/libexec/awnix/awnix-awsh and reached as `awnix awsh <verb>`.
Standard library only; runs on 3.9+ (the image runs it with python3.11). It works whatever awsh version the image carries,
because it reads the same config layers awsh 1.19 reads and asks the same questions.

Verbs
  doctor [--json]          is awsh usable offline on this box? exit 0 pass, 1 fail,
                           2 could not judge (never 0 on a check it could not run)
  status [--json]          config summary, token presence, user unit states
  first-run [--quiet] [--json]
                           create ~/.aither (0700), mint ~/.aither/harness_token (0600)
                           if absent, write ~/.aither/awsh-agent.env; idempotent;
                           never writes a cloud key
  config show [--json]     every layer and each value's source
  config get KEY           one merged value
  --self-test              hermetic: temp layers + a loopback stub model server
  --list-verbs             one verb per line

Config precedence, lowest to highest:
  /usr/lib/awsh/shell.yaml < /usr/lib/awsh/shell.d/*.yaml (lexical)
  < /etc/awsh/shell.yaml < ~/.aither/shell.yaml < environment
Flat `key: value` lines; CRLF tolerated. `offline: true` or AITHER_OFFLINE=1 = no cloud.

Test seams: AWNIX_AWSH_ROOT prefixes /usr/lib and /etc; AWNIX_AWSH_PROC_NET names the
directory holding tcp/tcp6 (default /proc/net); HOME locates ~/.aither.
"""

from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

VERBS = ("doctor", "status", "first-run", "config")

DEFAULT_LLM_URL = "http://127.0.0.1:8199/v1"
DEFAULT_HARNESS_URL = "http://127.0.0.1:8362"
DEFAULT_AGENT_URL = "http://127.0.0.1:9001"

#: Environment variable -> config key. Environment is the top layer.
ENV_KEYS: Tuple[Tuple[str, str], ...] = (
    ("AITHER_OFFLINE", "offline"),
    ("AITHER_INFERENCE_MODE", "inference_mode"),
    ("AITHER_LLM_URL", "llm_url"),
    ("AITHER_API_URL", "api_url"),
    ("AITHER_GENESIS_URL", "genesis_url"),
    ("AITHER_GATEWAY_URL", "gateway_url"),
    ("AITHER_MCP_URL", "mcp_url"),
    ("AITHER_IDENTITY_URL", "identity_url"),
    ("AITHER_HARNESS_URL", "harness_url"),
)

#: Keys whose value is a URL awsh (or the agent it starts) would dial.
URL_KEY = re.compile(r"(_url|^gateway_url)$")

_LINE = re.compile(r"^(\w+):\s*(.+)$")
TRUE = ("1", "true", "yes", "on")


class ConfigUnreadableError(Exception):
    """A layer exists and could not be read: the doctor cannot judge (exit 2)."""


# ---------------------------------------------------------------------------
# config layers
# ---------------------------------------------------------------------------

def parse_flat(text: str) -> Dict[str, str]:
    """Flat `key: value` lines, the same grammar awsh's config.ts accepts."""
    out: Dict[str, str] = {}
    for line in re.split(r"\r?\n", text):
        m = _LINE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        value = re.sub(r"^[\"']|[\"']$", "", value)
        out[m.group(1)] = value
    return out


def _root() -> Path:
    return Path(os.environ.get("AWNIX_AWSH_ROOT", "") or "/")


def _home() -> Path:
    return Path(os.environ.get("HOME", "") or str(Path.home()))


def layer_paths(root: Optional[Path] = None, home: Optional[Path] = None) -> List[Path]:
    root = _root() if root is None else root
    home = _home() if home is None else home
    paths = [root / "usr/lib/awsh/shell.yaml"]
    drop = root / "usr/lib/awsh/shell.d"
    try:
        paths.extend(sorted(p for p in drop.glob("*.yaml") if p.is_file()))
    except OSError as exc:
        raise ConfigUnreadableError("%s: %s" % (drop, exc)) from exc
    paths.append(root / "etc/awsh/shell.yaml")
    paths.append(home / ".aither/shell.yaml")
    return paths


def load_layers(root: Optional[Path] = None, home: Optional[Path] = None,
                env: Optional[Dict[str, str]] = None) -> dict:
    """Merge every layer. Returns {values, sources, layers}."""
    env = dict(os.environ) if env is None else env
    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    layers = []
    for path in layer_paths(root, home):
        if not path.exists():
            layers.append({"path": str(path), "present": False, "keys": []})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigUnreadableError("%s: %s" % (path, exc)) from exc
        parsed = parse_flat(text)
        layers.append({"path": str(path), "present": True, "keys": sorted(parsed)})
        for key, value in parsed.items():
            values[key] = value
            sources[key] = str(path)
    env_keys = []
    for name, key in ENV_KEYS:
        value = (env.get(name) or "").strip()
        if value:
            values[key] = value
            sources[key] = "env:" + name
            env_keys.append(key)
    layers.append({"path": "environment", "present": bool(env_keys), "keys": sorted(env_keys)})
    return {"values": values, "sources": sources, "layers": layers}


def is_offline(values: Dict[str, str]) -> bool:
    return (values.get("offline") or "").strip().lower() in TRUE


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    try:
        return is_loopback_host(urllib.parse.urlsplit(url).hostname or "")
    except ValueError:
        return False


def url_port(url: str, default: int) -> int:
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.port:
            return int(parts.port)
        return {"http": 80, "https": 443}.get(parts.scheme, default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# probes (injectable so the doctor itself is pure)
# ---------------------------------------------------------------------------

def http_get_json(url: str, timeout: float = 3.0) -> Tuple[Optional[int], object]:
    """GET a LOOPBACK url. Refuses anything else: the doctor never dials off-box."""
    if not is_loopback_url(url):
        raise PermissionError("refusing to dial a non-loopback url: %s" % url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(1 << 20)
            try:
                return resp.status, json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, None


def _decode_v4(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    return socket.inet_ntop(socket.AF_INET, raw[::-1])


def _decode_v6(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, words)


def proc_listeners(proc_net: Optional[Path] = None) -> Optional[List[Tuple[str, int]]]:
    """TCP LISTEN sockets as (address, port). None when /proc/net is not readable."""
    proc_net = Path(os.environ.get("AWNIX_AWSH_PROC_NET", "") or "/proc/net") \
        if proc_net is None else proc_net
    found: List[Tuple[str, int]] = []
    seen_any = False
    for name, decode in (("tcp", _decode_v4), ("tcp6", _decode_v6)):
        path = proc_net / name
        try:
            lines = path.read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        seen_any = True
        for line in lines:
            cols = line.split()
            if len(cols) < 4 or cols[3] != "0A":
                continue
            addr, _, port = cols[1].partition(":")
            try:
                found.append((decode(addr), int(port, 16)))
            except (ValueError, OSError):
                continue
    return found if seen_any else None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _check(checks: list, cid: str, ok: Optional[bool], detail: str,
           required: bool = True) -> None:
    checks.append({"id": cid, "ok": ok, "detail": detail, "required": required})


def _bind_of(listeners: Optional[List[Tuple[str, int]]], port: int) -> Optional[List[str]]:
    if listeners is None:
        return None
    return sorted({"%s:%d" % (a if ":" not in a else "[%s]" % a, p)
                   for a, p in listeners if p == port})


def run_doctor(cfg: dict,
               get_json: Callable[[str], Tuple[Optional[int], object]] = http_get_json,
               listeners: Optional[List[Tuple[str, int]]] = None,
               home: Optional[Path] = None,
               check_mode: bool = True) -> dict:
    """Pure verdict over the merged config and injected probes."""
    home = _home() if home is None else home
    values = cfg["values"]
    checks: list = []
    egress: List[str] = []
    offline = is_offline(values)
    _check(checks, "offline", offline,
           "offline mode is on (no cloud rung)" if offline else
           "offline is not set: awsh may fall back to a cloud gateway "
           "(set `offline: true` in /etc/awsh/shell.yaml)")

    bad = []
    for key in sorted(values):
        if URL_KEY.search(key) and values[key] and not is_loopback_url(values[key]):
            bad.append("%s=%s (%s)" % (key, values[key], cfg["sources"].get(key, "?")))
            egress.append(values[key])
    _check(checks, "loopback-urls", not bad,
           "every configured url is loopback" if not bad else
           "non-loopback url(s) configured: " + "; ".join(bad))

    llm_url = (values.get("llm_url") or DEFAULT_LLM_URL).rstrip("/")
    model = None
    if not is_loopback_url(llm_url):
        _check(checks, "local-llm", False, "llm_url %s is not loopback; not dialed" % llm_url)
    else:
        status, body = get_json(llm_url + "/models")
        if status is None:
            _check(checks, "local-llm", False, "nothing answers %s/models" % llm_url)
        elif status != 200:
            _check(checks, "local-llm", False, "%s/models answered HTTP %s" % (llm_url, status))
        else:
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, list) and data and isinstance(data[0], dict) \
                    and data[0].get("id"):
                model = str(data[0]["id"])
                _check(checks, "local-llm", True, "%s serves model %s" % (llm_url, model))
            else:
                _check(checks, "local-llm", False, "%s/models lists no model" % llm_url)

    harness_url = (values.get("harness_url") or DEFAULT_HARNESS_URL).rstrip("/")
    hport = url_port(harness_url, 8362)
    binds = _bind_of(listeners, hport)
    harness_bind = None
    if binds is None:
        _check(checks, "harness-bind", None,
               "cannot read the listener table; bind of :%d not judged" % hport)
    elif not binds:
        _check(checks, "harness-bind", False,
               "nothing listens on :%d (systemctl --user start awsh-harness)" % hport)
    else:
        harness_bind = binds[0] if len(binds) == 1 else ",".join(binds)
        loop = all(is_loopback_host(b.rsplit(":", 1)[0]) for b in binds)
        _check(checks, "harness-bind", loop,
               "harness bound %s" % harness_bind if loop else
               "harness bound %s: not loopback-only (zero open ports)" % harness_bind)

    if is_loopback_url(harness_url):
        status, _ = get_json(harness_url + "/health")
        _check(checks, "harness-health", status == 200,
               "%s/health -> %s" % (harness_url, status if status is not None else "no answer"))
    else:
        _check(checks, "harness-health", False, "harness_url %s is not loopback" % harness_url)

    agent_binds = _bind_of(listeners, url_port(DEFAULT_AGENT_URL, 9001))
    agent_bind = ",".join(agent_binds) if agent_binds else None
    if agent_binds:
        loop = all(is_loopback_host(b.rsplit(":", 1)[0]) for b in agent_binds)
        _check(checks, "agent-bind", loop,
               "agent bound %s" % agent_bind if loop else
               "agent bound %s: not loopback-only" % agent_bind)
    else:
        _check(checks, "agent-bind", None,
               "agent daemon :9001 not listening (advisory)", required=False)

    token = home / ".aither" / "harness_token"
    if not token.is_file():
        _check(checks, "harness-token", False, "%s missing (awnix awsh first-run)" % token)
    elif check_mode and os.name == "posix" and stat.S_IMODE(token.stat().st_mode) & 0o077:
        _check(checks, "harness-token", False,
               "%s is mode %o; must be 0600" % (token, stat.S_IMODE(token.stat().st_mode)))
    else:
        _check(checks, "harness-token", True, "%s present" % token)

    required = [c for c in checks if c["required"]]
    if any(c["ok"] is False for c in required):
        verdict = "fail"
    elif any(c["ok"] is None for c in required):
        verdict = "unknown"
    else:
        verdict = "pass"
    return {
        "schema": 1,
        "verdict": verdict,
        "offline": offline,
        "checks": checks,
        "nonloopback_urls": egress,
        "llm_url": llm_url,
        "model": model,
        "harness_bind": harness_bind,
        "agent_bind": agent_bind,
        "layers": cfg["layers"],
    }


EXIT = {"pass": 0, "fail": 1, "unknown": 2}


# ---------------------------------------------------------------------------
# first-run
# ---------------------------------------------------------------------------

def _write_private(path: Path, text: str, exclusive: bool) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if os.name == "posix":
        os.chmod(str(path), 0o600)
    return True


def first_run(cfg: dict, home: Optional[Path] = None) -> dict:
    home = _home() if home is None else home
    adir = home / ".aither"
    created = not adir.exists()
    adir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(str(adir), 0o700)
    token = adir / "harness_token"
    minted = False
    if not token.exists():
        minted = _write_private(token, secrets.token_urlsafe(32) + "\n", exclusive=True)
    llm_url = (cfg["values"].get("llm_url") or DEFAULT_LLM_URL).rstrip("/")
    offline = is_offline(cfg["values"])
    env_lines = ["# written by awnix awsh first-run from the awsh config layers; do not edit"]
    refused = None
    if offline and not is_loopback_url(llm_url):
        # Offline and the configured model is off-box: never hand it to the agent daemon.
        # Leaving AITHER_LLM_BASE_URL out keeps the unit's own loopback value in force.
        refused = llm_url
        env_lines.append("# refused non-loopback llm_url %s (offline)" % llm_url)
        llm_url = None
    else:
        env_lines += ["AITHER_LLM_BASE_URL=%s" % llm_url,
                      "AITHER_LOCAL_LLM_URL=%s" % re.sub(r"/v1$", "", llm_url)]
    if offline:
        env_lines.append("AITHER_OFFLINE=1")
    env_file = adir / "awsh-agent.env"
    new_text = "\n".join(env_lines) + "\n"
    old_text = env_file.read_text(encoding="utf-8") if env_file.is_file() else None
    wrote_env = old_text != new_text
    if wrote_env:
        _write_private(env_file, new_text, exclusive=False)
    return {"dir": str(adir), "created_dir": created, "minted_token": minted,
            "token": str(token), "agent_env": str(env_file), "wrote_env": wrote_env,
            "llm_url": llm_url, "refused_llm_url": refused}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def unit_state(unit: str) -> str:
    exe = shutil.which("systemctl")
    if not exe:
        return "unknown"
    try:
        res = subprocess.run([exe, "--user", "is-active", unit], capture_output=True,
                             text=True, encoding="utf-8", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (res.stdout or "").strip() or "unknown"


def status(cfg: dict, home: Optional[Path] = None) -> dict:
    home = _home() if home is None else home
    v = cfg["values"]
    return {
        "schema": 1,
        "offline": is_offline(v),
        "llm_url": v.get("llm_url") or DEFAULT_LLM_URL,
        "harness_url": v.get("harness_url") or DEFAULT_HARNESS_URL,
        "api_url": v.get("api_url") or DEFAULT_AGENT_URL,
        "token_present": (home / ".aither" / "harness_token").is_file(),
        "units": {u: unit_state(u) for u in ("awsh-harness.service", "awsh-agent.service")},
    }


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

class _Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.endswith("/v1/models"):
            body = json.dumps({"data": [{"id": "stub-model"}]}).encode()
        elif self.path.endswith("/health"):
            body = b'{"status":"ok"}'
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def self_test() -> int:
    failures: List[str] = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    tmp = Path(tempfile.mkdtemp(prefix="awnix-awsh-selftest-"))
    try:
        root, home = tmp / "root", tmp / "home"
        (root / "usr/lib/awsh/shell.d").mkdir(parents=True)
        (root / "etc/awsh").mkdir(parents=True)
        (home / ".aither").mkdir(parents=True)
        (root / "usr/lib/awsh/shell.yaml").write_bytes(
            ("offline: true\r\nllm_url: http://127.0.0.1:1/v1\r\n"
             "harness_url: http://127.0.0.1:%d\r\n" % port).encode())
        (root / "usr/lib/awsh/shell.d/50-x.yaml").write_text(
            "llm_url: http://127.0.0.1:%d/v1\n" % port, encoding="utf-8")
        env: Dict[str, str] = {}

        cfg = load_layers(root, home, env)
        if cfg["values"].get("llm_url") != "http://127.0.0.1:%d/v1" % port:
            failures.append("shell.d did not override the vendor layer: %r" % cfg["values"])
        if cfg["sources"].get("offline", "").endswith("shell.yaml") is False:
            failures.append("CRLF vendor layer not parsed: %r" % cfg["sources"])

        fr = first_run(cfg, home)
        fr2 = first_run(cfg, home)
        if not fr["minted_token"] or fr2["minted_token"] or fr2["wrote_env"]:
            failures.append("first-run not idempotent: %r / %r" % (fr, fr2))
        if os.name == "posix":
            mode = stat.S_IMODE((home / ".aither/harness_token").stat().st_mode)
            if mode != 0o600:
                failures.append("token mode %o != 600" % mode)

        good = run_doctor(cfg, listeners=[("127.0.0.1", port)], home=home)
        if good["verdict"] != "pass" or good["nonloopback_urls"] or good["model"] != "stub-model":
            failures.append("pass case: %s" % json.dumps(good))

        lan = run_doctor(cfg, listeners=[("0.0.0.0", port)], home=home)  # noqa: S104
        if lan["verdict"] != "fail":
            failures.append("0.0.0.0 harness bind not failed: %s" % lan["verdict"])

        (root / "etc/awsh/shell.yaml").write_text(
            "offline: false\nmcp_url: https://gateway.example.com/mcp\n", encoding="utf-8")
        cloud = run_doctor(load_layers(root, home, env),
                           listeners=[("127.0.0.1", port)], home=home)
        if cloud["verdict"] != "fail" or not cloud["nonloopback_urls"]:
            failures.append("cloud url + offline:false not failed: %s" % json.dumps(cloud))
        (root / "etc/awsh/shell.yaml").unlink()

        off_box = load_layers(root, home, {"AITHER_LLM_URL": "http://203.0.113.9:8000/v1"})
        fr3 = first_run(off_box, home)
        agent_env = (home / ".aither/awsh-agent.env").read_text(encoding="utf-8")
        if fr3["refused_llm_url"] != "http://203.0.113.9:8000/v1"                 or "AITHER_LLM_BASE_URL=" in agent_env:
            failures.append("offline first-run passed a non-loopback llm_url: %r" % fr3)
        first_run(cfg, home)  # restore the loopback env file

        unknown = run_doctor(cfg, listeners=None, home=home)
        if unknown["verdict"] != "unknown":
            failures.append("unreadable listener table not 'unknown': %s" % unknown["verdict"])

        down = run_doctor(load_layers(root, home, {"AITHER_LLM_URL": "http://127.0.0.1:1/v1"}),
                          listeners=[("127.0.0.1", port)], home=home)
        if down["verdict"] != "fail":
            failures.append("dead model server not failed: %s" % down["verdict"])

        refused = False
        try:
            http_get_json("http://203.0.113.1/v1/models")
        except PermissionError:
            refused = True
        if not refused:
            failures.append("http_get_json dialed a non-loopback url")

        if _decode_v4("0100007F") != "127.0.0.1":
            failures.append("tcp v4 decode wrong")
        if _decode_v6("00000000000000000000000001000000") != "::1":
            failures.append("tcp v6 decode wrong")
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    for f in failures:
        print("SELF-TEST FAIL: %s" % f, file=sys.stderr)
    print("awnix-awsh self-test: %s" % ("FAIL" if failures else "ok"))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_doctor(rep: dict) -> None:
    mark = {True: "ok  ", False: "FAIL", None: "??  "}
    for c in rep["checks"]:
        note = "" if c["required"] else " (advisory)"
        print("  [%s] %-15s %s%s" % (mark[c["ok"]], c["id"], c["detail"], note))
    print("  verdict: %s" % rep["verdict"].upper())


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix awsh", description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    sub = ap.add_subparsers(dest="verb")
    d = sub.add_parser("doctor")
    d.add_argument("--json", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("first-run")
    f.add_argument("--json", action="store_true")
    f.add_argument("--quiet", action="store_true")
    c = sub.add_parser("config")
    csub = c.add_subparsers(dest="config_verb")
    cs = csub.add_parser("show")
    cs.add_argument("--json", action="store_true")
    cg = csub.add_parser("get")
    cg.add_argument("key")
    args = ap.parse_args(argv)

    if args.list_verbs:
        print("\n".join(VERBS))
        return 0
    if args.self_test:
        return self_test()
    if not args.verb:
        ap.print_help()
        return 2

    try:
        cfg = load_layers()
    except ConfigUnreadableError as exc:
        if args.verb == "doctor" and getattr(args, "json", False):
            print(json.dumps({"schema": 1, "verdict": "unknown", "checks": [
                {"id": "config", "ok": None, "detail": str(exc), "required": True}],
                "nonloopback_urls": [], "llm_url": None, "model": None,
                "harness_bind": None}))
        else:
            print("awnix awsh: config unreadable: %s" % exc, file=sys.stderr)
        return 2

    if args.verb == "doctor":
        try:
            rep = run_doctor(cfg, listeners=proc_listeners())
        except Exception as exc:  # a probe crash is could-not-judge, never a pass
            rep = {"schema": 1, "verdict": "unknown", "checks": [
                {"id": "doctor", "ok": None, "detail": "probe crashed: %r" % exc,
                 "required": True}], "nonloopback_urls": [], "llm_url": None,
                   "model": None, "harness_bind": None}
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            _print_doctor(rep)
        return EXIT[rep["verdict"]]
    if args.verb == "status":
        rep = status(cfg)
        if args.json:
            print(json.dumps(rep, indent=2))
        else:
            for k, v in rep.items():
                print("  %-13s %s" % (k, v))
        return 0
    if args.verb == "first-run":
        try:
            rep = first_run(cfg)
        except OSError as exc:
            print("awnix awsh first-run: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rep, indent=2))
        elif not args.quiet:
            print("  ~/.aither ready; token %s; agent env %s" % (
                "minted" if rep["minted_token"] else "kept",
                "written" if rep["wrote_env"] else "unchanged"))
        if rep["refused_llm_url"]:
            print("awnix awsh first-run: offline, refused non-loopback llm_url %s; the agent "
                  "keeps its loopback model url" % rep["refused_llm_url"], file=sys.stderr)
            return 1
        return 0
    if args.verb == "config":
        if args.config_verb == "get":
            value = cfg["values"].get(args.key)
            if value is None:
                return 1
            print(value)
            return 0
        rep = {"layers": cfg["layers"],
               "values": {k: {"value": v, "source": cfg["sources"][k]}
                          for k, v in sorted(cfg["values"].items())}}
        if getattr(args, "json", False):
            print(json.dumps(rep, indent=2))
        else:
            for layer in cfg["layers"]:
                print("  %s %s" % ("+" if layer["present"] else "-", layer["path"]))
            for k, v in rep["values"].items():
                print("  %-15s %-40s %s" % (k, v["value"], v["source"]))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
