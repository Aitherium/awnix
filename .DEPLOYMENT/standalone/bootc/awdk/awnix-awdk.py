#!/usr/bin/python3.11
"""awnix awdk -- the local agent daemon on an air-gapped awnix / garg box.

Installed as /usr/libexec/awnix/awnix-awdk, so `awnix awdk <verb>` reaches it.
Stdlib only, Python 3.10-compatible.

Verbs:
  status        [--json]   config, env files, adk version (no network)
  health        [--json] [--marker] [--wait S]
                           llama.cpp /v1/models + daemon /health + listener scan
                           + in-process egress probe -> verdict ok|degraded|dark
  probe-egress  [--json]   python3.11 -m adk.compliance.egress_guard --probe --json
  run-example   --corpus DIR --out FILE.json
                           run the air-gapped local agent example
  --self-test              hermetic: stub llama + daemon on ephemeral loopback ports
  --list-verbs             one verb per line

Exit codes: 0 ok, 1 degraded/dark/violation, 2 could-not-judge.

health --json:
  {verdict, adk_version, llama:{url,ok,model}, daemon:{ok,bind,url},
   loopback_only, air_gap:{mode,guard,probe}, problems:[...]}

--marker prints the serial line the boot proof greps:
  awdk-local: <ok|degraded|dark> model=<id> bind=127.0.0.1:9001 airgap=<strict|audit|off>

Environment (tests and operators): AWNIX_AWDK_ENV_FILES (colon list, later wins),
AWNIX_AWDK_DAEMON_URL, AWNIX_AWDK_PROC_NET (dir with tcp/tcp6),
AWNIX_AWDK_PYTHON, AWNIX_AWDK_PROBE_CMD (JSON argv), AWNIX_AWDK_EXAMPLE.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VERBS = ("status", "health", "probe-egress", "run-example")
DEFAULT_ENV_FILES = ("/usr/lib/awdk/awdk.env", "/etc/awdk/awdk.env")
DEFAULT_LLAMA_URL = "http://127.0.0.1:8199/v1"
DEFAULT_DAEMON_URL = "http://127.0.0.1:9001"
DEFAULT_EXAMPLE = "/usr/lib/awdk/examples/airgap_local_agent.py"
EXIT_OK, EXIT_FAIL, EXIT_UNJUDGED = 0, 1, 2


# ---- config ----------------------------------------------------------------


def parse_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE lines (systemd EnvironmentFile subset). Missing file -> {}."""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_config(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    environ = dict(os.environ if environ is None else environ)
    files_raw = environ.get("AWNIX_AWDK_ENV_FILES")
    files = [f for f in files_raw.split(":") if f] if files_raw else list(DEFAULT_ENV_FILES)
    merged: Dict[str, str] = {}
    used: List[str] = []
    for f in files:
        vals = parse_env_file(f)
        if vals:
            used.append(f)
        merged.update(vals)
    # the process environment wins over files (systemd already applied them)
    for k in ("AITHER_LLM_BASE_URL", "AITHER_AIR_GAP_CONFIG", "AITHER_MODEL", "AITHER_OFFLINE"):
        if environ.get(k):
            merged[k] = environ[k]
    return {
        "env_files": used,
        "env": merged,
        "llama_url": (merged.get("AITHER_LLM_BASE_URL") or DEFAULT_LLAMA_URL).rstrip("/"),
        "daemon_url": (environ.get("AWNIX_AWDK_DAEMON_URL") or DEFAULT_DAEMON_URL).rstrip("/"),
        "air_gap_config": merged.get("AITHER_AIR_GAP_CONFIG", ""),
        "python": environ.get("AWNIX_AWDK_PYTHON") or "python3.11",
        "proc_net": environ.get("AWNIX_AWDK_PROC_NET") or "/proc/net",
        "probe_cmd": environ.get("AWNIX_AWDK_PROBE_CMD", ""),
        "example": environ.get("AWNIX_AWDK_EXAMPLE") or DEFAULT_EXAMPLE,
    }


# ---- probes ----------------------------------------------------------------


def http_json(url: str, timeout: float = 3.0) -> Tuple[bool, Any, str]:
    """(ok, parsed body, error). Loopback plain HTTP only; no proxies."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            body = r.read(1 << 20)
            return True, json.loads(body.decode("utf-8") or "null"), ""
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, None, str(getattr(e, "reason", e))


def probe_llama(url: str) -> Dict[str, Any]:
    ok, body, err = http_json(f"{url}/models")
    model = None
    if ok and isinstance(body, dict):
        data = body.get("data") or body.get("models") or []
        if data and isinstance(data[0], dict):
            model = data[0].get("id") or data[0].get("name") or data[0].get("model")
    out = {"url": url, "ok": bool(ok and model), "model": model}
    if err or (ok and not model):
        out["error"] = err or "no model listed"
    return out


def probe_daemon(url: str) -> Dict[str, Any]:
    ok, body, err = http_json(f"{url}/health")
    parsed = urllib.parse.urlsplit(url)
    out: Dict[str, Any] = {"url": url, "ok": bool(ok and isinstance(body, dict)),
                           "bind": f"{parsed.hostname}:{parsed.port or 80}"}
    if isinstance(body, dict):
        out["version"] = body.get("version")
        out["air_gap"] = body.get("air_gap")
    if err:
        out["error"] = err
    return out


def _hex_to_ip(h: str) -> str:
    """/proc/net/tcp{,6} address hex (host-endian 32-bit words) -> ip string."""
    raw = bytes.fromhex(h)
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
    return str(ipaddress.ip_address(b"".join(words)))


def listeners_on(port: int, proc_net: str) -> List[str]:
    """Local addresses LISTENing on TCP ``port`` (state 0A)."""
    found: List[str] = []
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc_net, name), encoding="ascii") as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 4 or cols[3] != "0A":
                continue
            addr, _, port_hex = cols[1].partition(":")
            if int(port_hex, 16) != port:
                continue
            try:
                found.append(_hex_to_ip(addr))
            except ValueError:
                found.append("?" + addr)
    return found


def _is_loopback(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_loopback


def probe_egress(cfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    if cfg["probe_cmd"]:
        argv = json.loads(cfg["probe_cmd"])
    else:
        argv = [cfg["python"], "-m", "adk.compliance.egress_guard", "--probe", "--json"]
    env = dict(os.environ)
    env.update({k: v for k, v in cfg["env"].items() if k.startswith("AITHER_")})
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return EXIT_UNJUDGED, {"verdict": "error", "detail": str(e)}
    try:
        body = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except ValueError:
        body = {"detail": p.stdout.strip()[-400:]}
    if not isinstance(body, dict):
        body = {"detail": str(body)}
    body.setdefault("verdict", {0: "blocked", 1: "egress-possible"}.get(p.returncode, "error"))
    if p.returncode not in (0, 1):
        body.setdefault("detail", (p.stderr or "").strip()[-400:])
        return EXIT_UNJUDGED, body
    return p.returncode, body


def adk_version(cfg: Dict[str, Any]) -> Optional[str]:
    try:
        p = subprocess.run([cfg["python"], "-c", "import adk;print(adk.__version__)"],
                           capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None


# ---- verbs -----------------------------------------------------------------


def health(cfg: Dict[str, Any], *, with_version: bool = True) -> Tuple[int, Dict[str, Any]]:
    problems: List[str] = []
    llama = probe_llama(cfg["llama_url"])
    daemon = probe_daemon(cfg["daemon_url"])
    port = urllib.parse.urlsplit(cfg["daemon_url"]).port or 9001
    listeners = listeners_on(port, cfg["proc_net"])
    loopback_only: Optional[bool] = (all(_is_loopback(a) for a in listeners)
                                     if listeners else None)
    probe_rc, probe = probe_egress(cfg)
    ag = daemon.get("air_gap") if isinstance(daemon.get("air_gap"), dict) else {}
    mode = ag.get("mode") or ("disabled" if not ag else "?")
    air_gap = {"mode": {"disabled": "off"}.get(mode, mode),
               "guard": bool(ag.get("guard_installed")),
               "probe": probe.get("verdict", "error")}
    if not daemon["ok"]:
        problems.append(f"daemon {cfg['daemon_url']} not answering: {daemon.get('error', '')}")
    if not llama["ok"]:
        problems.append(f"llama {cfg['llama_url']} not serving: {llama.get('error', '')}")
    if loopback_only is False:
        problems.append(f"port {port} listens off-loopback: {', '.join(listeners)}")
    if loopback_only is None and daemon["ok"]:
        problems.append(f"no listener found on port {port} in {cfg['proc_net']}")
    if probe_rc != EXIT_OK:
        problems.append(f"egress probe: {probe.get('verdict')} {probe.get('detail', '')}".strip())
    if daemon["ok"] and air_gap["mode"] != "strict":
        problems.append(f"daemon air gap mode is {air_gap['mode']}, not strict")
    # The probe runs in a SEPARATE interpreter, so it cannot see whether the
    # daemon's own process patched its sockets. Strict mode is config only;
    # install_egress_guard swallows per-library patch failures. Judge the
    # daemon's self-report too, or "strict" can be printed over an unguarded
    # process.
    if daemon["ok"] and not air_gap["guard"]:
        problems.append("daemon reports air_gap.guard_installed=false "
                        "(in-process egress guard not installed)")
    if not daemon["ok"]:
        verdict = "dark"
    elif problems:
        verdict = "degraded"
    else:
        verdict = "ok"
    report = {
        "verdict": verdict,
        "adk_version": adk_version(cfg) if with_version else daemon.get("version"),
        "llama": llama,
        # bind = what /proc/net MEASURED on the port, not the configured URL; a
        # wildcard listener must show up as 0.0.0.0 on the serial marker.
        "daemon": {"ok": daemon["ok"],
                   "bind": (",".join(f"{a}:{port}" for a in listeners)
                            if listeners else "none"),
                   "configured": daemon["bind"], "url": daemon["url"]},
        "listeners": listeners,
        "loopback_only": loopback_only,
        "air_gap": air_gap,
        "problems": problems,
    }
    if probe_rc == EXIT_UNJUDGED and not daemon["ok"] and not llama["ok"]:
        return EXIT_UNJUDGED, report
    return (EXIT_OK if verdict == "ok" else EXIT_FAIL), report


def marker(report: Dict[str, Any]) -> str:
    model = (report.get("llama") or {}).get("model") or "none"
    return (f"awdk-local: {report['verdict']} model={model} "
            f"bind={report['daemon']['bind']} airgap={report['air_gap']['mode']}")


def cmd_status(cfg: Dict[str, Any], as_json: bool) -> int:
    out = {"env_files": cfg["env_files"], "llama_url": cfg["llama_url"],
           "daemon_url": cfg["daemon_url"], "air_gap_config": cfg["air_gap_config"],
           "air_gap_config_present": bool(cfg["air_gap_config"])
           and os.path.exists(cfg["air_gap_config"]),
           "offline": cfg["env"].get("AITHER_OFFLINE", ""),
           "adk_version": adk_version(cfg)}
    if as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return EXIT_OK if out["adk_version"] else EXIT_UNJUDGED


def cmd_health(cfg: Dict[str, Any], as_json: bool, want_marker: bool, wait: float) -> int:
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        rc, rep = health(cfg)
        if rc == EXIT_OK or time.monotonic() >= deadline:
            break
        time.sleep(3)
    if as_json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    elif not want_marker:
        print(f"{rep['verdict']}: model={rep['llama'].get('model')} "
              f"daemon={rep['daemon']['bind']} loopback_only={rep['loopback_only']} "
              f"airgap={rep['air_gap']}")
        for p in rep["problems"]:
            print(f"  - {p}")
    if want_marker:
        print(marker(rep), flush=True)
    return rc


def cmd_probe(cfg: Dict[str, Any], as_json: bool) -> int:
    rc, body = probe_egress(cfg)
    print(json.dumps(body, sort_keys=True) if as_json
          else f"{body.get('verdict')}: {body.get('detail', '')}")
    return rc


def cmd_run_example(cfg: Dict[str, Any], corpus: str, out: str) -> int:
    if not os.path.isfile(cfg["example"]):
        print(f"could-not-judge: example not installed at {cfg['example']}", file=sys.stderr)
        return EXIT_UNJUDGED
    env = dict(os.environ)
    env.update({k: v for k, v in cfg["env"].items() if k.startswith("AITHER_")})
    argv = [cfg["python"], cfg["example"], "--corpus", corpus, "--out", out,
            "--base-url", cfg["llama_url"]]
    try:
        return subprocess.run(argv, env=env, timeout=3600).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"could-not-judge: {e}", file=sys.stderr)
        return EXIT_UNJUDGED


# ---- self-test -------------------------------------------------------------


def _stub_server(routes: Dict[str, Any]):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = routes.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # silence
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _proc_tcp_line(ip: str, port: int) -> str:
    raw = ipaddress.ip_address(ip).packed
    words = "".join(raw[i:i + 4][::-1].hex().upper() for i in range(0, len(raw), 4))
    return f"   0: {words}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000"


def write_fake_proc(d: str, listeners: List[Tuple[str, int]]) -> None:
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    v4 = [_proc_tcp_line(ip, p) for ip, p in listeners if ":" not in ip]
    v6 = [_proc_tcp_line(ip, p) for ip, p in listeners if ":" in ip]
    for name, rows in (("tcp", v4), ("tcp6", v6)):
        with open(os.path.join(d, name), "w", encoding="ascii") as fh:
            fh.write("\n".join([hdr] + rows) + "\n")


def self_test() -> int:
    failures: List[str] = []
    probe_ok = json.dumps([sys.executable, "-c",
                           "print('{\"verdict\": \"blocked\", \"detail\": \"stub\"}')"])
    probe_open = json.dumps([sys.executable, "-c",
                             "import sys;print('{\"verdict\": \"egress-possible\"}');sys.exit(1)"])
    strict = {"status": "healthy", "version": "0.0-test",
              "air_gap": {"enforced": True, "mode": "strict", "guard_installed": True,
                          "violations_total": 0}}
    llama = _stub_server({"/v1/models": {"data": [{"id": "bonsai-test"}]}})
    daemon = _stub_server({"/health": strict})
    dport = daemon.server_address[1]
    tmp = tempfile.mkdtemp(prefix="awnix-awdk-selftest-")
    envf = os.path.join(tmp, "awdk.env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write(f"# test\nAITHER_LLM_BASE_URL=http://127.0.0.1:{llama.server_address[1]}/v1\n"
                 "AITHER_OFFLINE=1\n")

    def cfg(**over: str) -> Dict[str, Any]:
        env = {"AWNIX_AWDK_ENV_FILES": envf, "AWNIX_AWDK_PROC_NET": tmp,
               "AWNIX_AWDK_DAEMON_URL": f"http://127.0.0.1:{dport}",
               "AWNIX_AWDK_PROBE_CMD": probe_ok, "AWNIX_AWDK_PYTHON": sys.executable}
        env.update(over)
        return load_config(env)

    def case(name: str, c: Dict[str, Any], listeners: List[Tuple[str, int]],
             want_rc: int, want_verdict: str) -> None:
        write_fake_proc(tmp, listeners)
        rc, rep = health(c, with_version=False)
        if rc != want_rc or rep["verdict"] != want_verdict:
            failures.append(f"{name}: rc={rc} verdict={rep['verdict']} "
                            f"(want {want_rc}/{want_verdict}) problems={rep['problems']}")

    try:
        case("all-good", cfg(), [("127.0.0.1", dport)], 0, "ok")
        case("v6-loopback", cfg(), [("::1", dport)], 0, "ok")
        case("wildcard-bind", cfg(), [("0.0.0.0", dport)], 1, "degraded")
        case("probe-open", cfg(AWNIX_AWDK_PROBE_CMD=probe_open),
             [("127.0.0.1", dport)], 1, "degraded")
        case("daemon-down", cfg(AWNIX_AWDK_DAEMON_URL="http://127.0.0.1:1"), [], 1, "dark")
        strict["air_gap"]["guard_installed"] = False
        case("strict-but-unguarded", cfg(), [("127.0.0.1", dport)], 1, "degraded")
        strict["air_gap"]["guard_installed"] = True
        write_fake_proc(tmp, [("0.0.0.0", dport)])
        _, rep = health(cfg(), with_version=False)
        if f"bind=0.0.0.0:{dport}" not in marker(rep):
            failures.append(f"wildcard listener not visible in marker: {marker(rep)!r}")
        bad_env = os.path.join(tmp, "bad.env")
        with open(bad_env, "w", encoding="utf-8") as fh:
            fh.write("AITHER_LLM_BASE_URL=http://127.0.0.1:1/v1\n")
        case("llama-down", cfg(AWNIX_AWDK_ENV_FILES=bad_env), [("127.0.0.1", dport)],
             1, "degraded")
        write_fake_proc(tmp, [("127.0.0.1", dport)])
        _, rep = health(cfg(), with_version=False)
        m = marker(rep)
        want = f"awdk-local: ok model=bonsai-test bind=127.0.0.1:{dport} airgap=strict"
        if m != want:
            failures.append(f"marker {m!r} != {want!r}")
        if _hex_to_ip("0100007F") != "127.0.0.1":
            failures.append("proc hex decode of 0100007F is not 127.0.0.1")
    finally:
        llama.shutdown()
        daemon.shutdown()
    for f in failures:
        print(f"SELF-TEST FAIL: {f}")
    print("SELF-TEST " + ("FAIL" if failures else "PASS"))
    return EXIT_FAIL if failures else EXIT_OK


# ---- main ------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list-verbs" in argv:
        print("\n".join(VERBS))
        return EXIT_OK
    if "--self-test" in argv:
        return self_test()
    ap = argparse.ArgumentParser(prog="awnix awdk", description=__doc__.split("\n\n")[0])
    ap.add_argument("verb", choices=VERBS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--marker", action="store_true")
    ap.add_argument("--wait", type=float, default=0.0)
    ap.add_argument("--corpus")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
        if args.verb == "status":
            return cmd_status(cfg, args.json)
        if args.verb == "health":
            return cmd_health(cfg, args.json, args.marker, args.wait)
        if args.verb == "probe-egress":
            return cmd_probe(cfg, args.json)
        if not (args.corpus and args.out):
            print("run-example needs --corpus DIR --out FILE.json", file=sys.stderr)
            return EXIT_UNJUDGED
        return cmd_run_example(cfg, args.corpus, args.out)
    except Exception as e:  # noqa: BLE001 - a CLI that cannot judge says so
        print(f"could-not-judge: {type(e).__name__}: {e} ({shlex.join(argv)})", file=sys.stderr)
        return EXIT_UNJUDGED


if __name__ == "__main__":
    sys.exit(main())
