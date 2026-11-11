#!/usr/bin/env python3
"""
dgx_node_agent — minimal, stdlib-only cluster node agent.

A lightweight companion to ClusterAgent for remote nodes (e.g. the DGX Spark)
whose images don't carry the full AitherOS lib. It heartbeats the node's GPU +
container inventory so the node shows up as a first-class, observable node.

No third-party deps — uses urllib + subprocess so it runs on any python3.

TWO SINKS, either or both:
  * fleet-pulse (pulse.aitherium.com POST /beat) — the live heartbeat surface. It is
    a Cloudflare Worker, so the node stays visible while the primary box is down.
  * the legacy gateway node registry (/nodes/register + /nodes/{id}/heartbeat).
    AitherGateway was retired 2026-10-01 and cluster.aitherium.com now serves Veil
    (D-1906), which answers every path with 307 -> /login. A redirect is therefore
    read as "this is not a registry": never followed (that would POST the PSK to a
    login page), logged once, and re-probed hourly instead of erroring every 30 s.

Env:
  AITHER_NODE_ID         node id (default: node-<hostname>)
  AITHER_NODE_ROLE       default "sovereign"
  AITHER_CLUSTER_GATEWAY legacy registry base URL (optional; unset = no registry)
  AITHER_CLUSTER_TOKEN   shared cluster PSK for that registry
  AITHER_NODE_HOST       advertised host (default: hostname)
  HEARTBEAT_SECONDS      default 30
  FLEET_PULSE_URL        the pulse board's base URL (default: PULSE_URL below)
  FLEET_PULSE_TOKEN_FILE default /etc/aither/fleet-pulse-token (or FLEET_PULSE_TOKEN)
  FLEET_PULSE_HOST       host key on the pulse board (default: AITHER_NODE_HOST)
  AITHER_NODE_PULSE      0 turns the pulse sink off. `awnix fleet join` writes 0: a
                         host that already beats pulse from its own script (the fleet
                         host) must not have this agent overwrite that richer record.
  REGISTRY_RETRY_SECONDS re-probe a redirecting registry this often (default 3600)

PLACEMENT (aither-placement/v1), same loop, off unless the node was joined:
  The converge step runs only when /etc/aither/placement exists or
  AITHER_PLACEMENT_URL is set; without them this file behaves exactly as before.
  It reads desired.json, verifies its HMAC with the node key, the node id and the
  generation (an older generation is a replay and is refused), then gives every
  listed unit a verdict: ok / would-start / would-stop / hold + reason.
    observe        changes nothing on the host: no systemctl verb that mutates, no
                   unit file, no absent.units. What it writes is its own: the
                   replay floor (the highest accepted generation) in state.json
                   and, in pull mode, the files it fetched (below).
    enforce-absent may stop AND MASK units in absent[] (and their
                   <unit>-ondemand.socket), write absent.units, and unmask a unit
                   it masked itself once a manifest lists it present again. Never
                   starts or installs anything.
    enforce        also installs / reloads / starts present[], one start per
                   cycle, behind a memory admission check and a health gate. A
                   start needs the unit's quadlet to carry exactly the manifest's
                   digest-pinned Image=.
  The effective mode is the LOWEST of AITHER_PLACEMENT_MODE, the manifest's mode
  and the local cap file. A unit in neither list is never touched. No unit file is
  ever deleted. A refused manifest, an unknown unit state or name, or a failed
  admission is HOLD: nothing falls back and nothing is rebalanced from here.

  Why absent means masked: a bare stop is undone by anything that Wants= the unit
  (a dependent restarting, the on-demand socket, the next boot), which gives two
  live copies of a moved service. The mask is the durable form, the same one the
  park plane uses. The agent records what IT masked in state.json and only ever
  unmasks those.

  PULL MODE (AITHER_PLACEMENT_URL set). Each cycle starts by fetching
  <url>/manifest and, when that manifest names a bundle this node does not hold,
  <url>/bundle. Both requests are signed like the report. NOTHING is written
  until all of this holds for the fetched bytes: the manifest's HMAC under the
  node key, the schema, this node's id, a generation at or above the highest
  one ever accepted here (and above the cached desired.json), and the bundle's
  sha256 equal to the manifest's bundle_sha256. Then, in every mode:
      bundle.tar and desired.json  replaced atomically, in that order;
      <dir>/staging/               the unit files the manifest's present[] needs
                                   (each unit's .container plus the .network /
                                   .volume files it names), unpacked for review.
                                   Nothing else in the tar is ever written out.
  Staging is never the quadlet directory (a staging path that is a link, or
  that resolves to it, is refused and reported as `pull.stage_error`). A unit
  file reaches AITHER_QUADLET_DIR only in enforce, one unit at a time, at the
  moment that unit is started, straight from the verified tar. A bundle is an
  UNCOMPRESSED tar; a compressed one is "not a readable tar".
  Generation 0 is the controller saying "nothing is stored for you" (a pending
  node, or an approved one with no upload yet). It is checked like any manifest
  and must be the empty observe manifest; it is then REPORTED and never written:
  it cannot replace a cached desired.json, lower the replay floor or empty
  staging. A node that holds generation N keeps converging on N.
  A fetch that fails (network, TLS, a non-200 answer, any check above) changes
  nothing: the cached desired.json stays in force and the report's `pull` block
  says what happened.

  HEALTH. A present unit with a health_url is healthy when a GET answers 2xx. One
  without (a non-HTTP probe) is reported healthy once systemd has shown it
  active, under the same activation (ActiveEnterTimestampMonotonic unchanged),
  for AITHER_PLACEMENT_STABLE_CHECKS consecutive cycles; until then its health
  is null, never true.

  EDGE. A manifest's edge.egress / edge.ingress entries say which names and ports
  must be routed between nodes once a service runs away from home. THIS AGENT DOES
  NOT APPLY THEM: it configures no name, no proxy and no firewall rule. Every
  report says so (`edge: {applied: false, egress: N, ingress: N}`, the counts being
  what the accepted manifest carries), and the placer refuses to open or advance a
  move off the home node while a node involved reports that.

  The report is POSTed to <url>/report as JSON with three headers the controller
  verifies (lib/orchestration/placement_store.py):
      X-Aither-Node       this node's id
      X-Aither-Timestamp  unix seconds
      X-Aither-Signature  hex HMAC-SHA256(node key, METHOD "\n" PATH "\n" TIMESTAMP
                          "\n" sha256_hex(body)) over the exact bytes sent
  It carries `units: {unit: {active, healthy, masked}}`, the shape the placer reads.

  AITHER_PLACEMENT_URL         controller base for this node, e.g.
                               https://<controller>/api/placement/nodes/<id>;
                               <url>/manifest and <url>/bundle are fetched,
                               the report goes to <url>/report
  AITHER_PLACEMENT_KEY_FILE    default /etc/aither/placement/node.key (0600)
  AITHER_PLACEMENT_MODE        observe | enforce-absent | enforce (default observe)
  AITHER_PLACEMENT_DIR         default /etc/aither/placement
  AITHER_PLACEMENT_CAP_FILE    default <dir>/mode.cap (one mode word; lowers the mode)
  AITHER_PLACEMENT_BUNDLE      default <dir>/bundle.tar (unit files; sha256 must
                               equal the manifest's bundle_sha256)
  AITHER_PLACEMENT_HEADROOM_MB memory that must stay free after a start. Unset
                               means no start is admitted.
  AITHER_PLACEMENT_INTERVAL    seconds between converge steps (default 120)
  AITHER_PLACEMENT_HEALTH_WAIT seconds to wait for a started unit (default 60)
  AITHER_PLACEMENT_STABLE_CHECKS
                               consecutive active checks that stand in for a
                               health URL (default 3, minimum 2)
  AITHER_PLACEMENT_CA_FILE     CA bundle for the controller, when it is not public
  AITHER_PLACEMENT_CONTROLLER_SHA256
                               SHA-256 of the controller's certificate. When set it
                               IS the trust decision (checked before anything is
                               sent) and the CA file is not consulted: a pinned leaf
                               issued by a private CA is not a trust anchor for
                               OpenSSL on Python < 3.13.
  AITHER_QUADLET_DIR           default /etc/containers/systemd
"""
import hashlib
import hmac
import http.client
import io
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request

NODE_ID = os.getenv("AITHER_NODE_ID", f"node-{platform.node()}")
ROLE = os.getenv("AITHER_NODE_ROLE", "sovereign")
GATEWAY = os.getenv("AITHER_CLUSTER_GATEWAY", "").rstrip("/")
TOKEN = os.getenv("AITHER_CLUSTER_TOKEN", "")
HOST = os.getenv("AITHER_NODE_HOST", socket.gethostname())
HEARTBEAT = int(os.getenv("HEARTBEAT_SECONDS", "30"))
PULSE_URL = os.getenv("FLEET_PULSE_URL", "https://pulse.aitherium.com").rstrip("/")
PULSE_TOKEN_FILE = os.getenv("FLEET_PULSE_TOKEN_FILE", "/etc/aither/fleet-pulse-token")
PULSE_HOST = (os.getenv("FLEET_PULSE_HOST") or HOST).lower()[:64]
REGISTRY_RETRY = int(os.getenv("REGISTRY_RETRY_SECONDS", "3600"))
PULSE_ENABLED = os.getenv("AITHER_NODE_PULSE", "1").strip().lower() not in (
    "0", "off", "false", "no")

PLACEMENT_DIR = os.getenv("AITHER_PLACEMENT_DIR", "/etc/aither/placement")
PLACEMENT_URL = os.getenv("AITHER_PLACEMENT_URL", "").rstrip("/")
PLACEMENT_KEY_FILE = os.getenv("AITHER_PLACEMENT_KEY_FILE", "")
PLACEMENT_MODE = os.getenv("AITHER_PLACEMENT_MODE", "observe")
PLACEMENT_CAP_FILE = os.getenv("AITHER_PLACEMENT_CAP_FILE", "")
PLACEMENT_BUNDLE = os.getenv("AITHER_PLACEMENT_BUNDLE", "")
PLACEMENT_HEADROOM_MB = os.getenv("AITHER_PLACEMENT_HEADROOM_MB", "")
PLACEMENT_INTERVAL = int(os.getenv("AITHER_PLACEMENT_INTERVAL", "120"))
PLACEMENT_HEALTH_WAIT = int(os.getenv("AITHER_PLACEMENT_HEALTH_WAIT", "60"))
PLACEMENT_STABLE_CHECKS = os.getenv("AITHER_PLACEMENT_STABLE_CHECKS", "")
PLACEMENT_CA_FILE = os.getenv("AITHER_PLACEMENT_CA_FILE", "")
PLACEMENT_CONTROLLER_SHA256 = os.getenv(
    "AITHER_PLACEMENT_CONTROLLER_SHA256", "").replace(":", "").strip().lower()
QUADLET_DIR = os.getenv("AITHER_QUADLET_DIR", "/etc/containers/systemd")

_CTX = ssl.create_default_context()  # public Cloudflare cert — verify normally
# Cloudflare bot-protection 403s the default python-urllib UA.
_UA = "Mozilla/5.0 (compatible; AitherClusterAgent/1.1; +https://aitherium.com)"


class RegistryRetiredError(Exception):
    """The registry host answered with a redirect (a login page, not a registry)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RegistryRetiredError(f"HTTP {code} -> {headers.get('Location', '?')}")


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_CTX))


def _request(url: str, payload: dict, headers: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA, **headers},
    )
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _post(path: str, payload: dict, timeout: float = 15.0, node_key: str = "") -> dict:
    headers = {"X-Cluster-Token": TOKEN}
    if node_key:
        # The registry authenticates heartbeats with the key /nodes/register returned.
        headers["Authorization"] = f"Bearer {node_key}"
    return _request(f"{GATEWAY}{path}", payload, headers, timeout)


def _pulse_token() -> str:
    tok = os.getenv("FLEET_PULSE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(PULSE_TOKEN_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _gpus() -> list:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    gpus.append({"name": parts[0], "vram_mb": int(float(parts[1]))})
                except ValueError:
                    gpus.append({"name": parts[0]})
        return gpus
    except Exception:
        return []


class Runner:
    """Every touch of the host goes through here, so tests swap in a fake."""

    def run(self, argv, timeout=20.0):
        """(returncode, stdout, stderr). 127 = no such binary, 124 = timed out."""
        try:
            out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
            return out.returncode, out.stdout, out.stderr
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[0]}: timed out after {timeout}s"
        except Exception as exc:
            return 1, "", str(exc)[:200]

    def which(self, name):
        return shutil.which(name)

    def meminfo(self):
        """/proc/meminfo as {field: kB}; {} when it cannot be read."""
        info = {}
        try:
            with open("/proc/meminfo") as fh:
                for ln in fh:
                    key, _, rest = ln.partition(":")
                    val = rest.split()
                    if val and val[0].isdigit():
                        info[key.strip()] = int(val[0])
        except Exception:
            return {}
        return info

    def host(self):
        return {"machine": platform.machine(), "cores": os.cpu_count() or 0}

    def http_ok(self, url, timeout=3.0):
        """True when a GET answers 2xx. Health URLs are the node's own services and
        most carry a private-CA certificate, so the certificate is not checked
        here; nothing secret is sent."""
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def now(self):
        return time.time()

    def _http(self, method, url, body, headers, limit, timeout, pin, ca_file):
        """One request to the controller: (status, body bytes). A redirect is
        returned as its 3xx status, never followed. A body over `limit` bytes is
        an error, not a truncated read.

        Trust: with a pin (AITHER_PLACEMENT_CONTROLLER_SHA256) the peer
        certificate's SHA-256 is compared BEFORE the request is written, and
        nothing else about the certificate is consulted. Without one the system
        store, or the CA file, verifies the chain and the host name."""
        pin = PLACEMENT_CONTROLLER_SHA256 if pin is None else pin
        ca_file = PLACEMENT_CA_FILE if ca_file is None else ca_file
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        if u.scheme == "https":
            if pin:
                if not re.match(r"^[0-9a-f]{64}$", pin):
                    raise ValueError("AITHER_PLACEMENT_CONTROLLER_SHA256 is not 64 hex digits")
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # the pin below is the trust decision
            else:
                ctx = ssl.create_default_context(cafile=ca_file or None)
            conn = http.client.HTTPSConnection(host, u.port or 443, timeout=timeout, context=ctx)
        elif u.scheme == "http":
            conn = http.client.HTTPConnection(host, u.port or 80, timeout=timeout)
        else:
            raise ValueError("controller URL is not http(s)")
        try:
            conn.connect()
            if u.scheme == "https" and pin:
                der = conn.sock.getpeercert(binary_form=True)
                seen = hashlib.sha256(der or b"").hexdigest()
                if not hmac.compare_digest(seen, pin):
                    raise ssl.SSLError("controller certificate does not match the pinned "
                                       "fingerprint; nothing was sent")
            send = {"User-Agent": _UA}
            if body is not None:
                send["Content-Type"] = "application/json"
            send.update(headers or {})
            conn.request(method, u.path or "/", body=body, headers=send)
            resp = conn.getresponse()
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"controller answer is over {limit} bytes")
            return resp.status, data
        finally:
            conn.close()

    def post(self, url, body, headers, timeout=15.0, pin=None, ca_file=None):
        """POST these exact bytes; returns the HTTP status (see _http for trust)."""
        resp = self._http("POST", url, body, headers, 65536, timeout, pin, ca_file)
        return resp[0]

    def get(self, url, headers, limit, timeout=30.0, pin=None, ca_file=None):
        """GET; returns (status, body bytes), the body capped at `limit` bytes."""
        return self._http("GET", url, None, headers, limit, timeout, pin, ca_file)

    def sleep(self, seconds):
        time.sleep(seconds)


_RUNNER = Runner()
_RUNTIMES = ("podman", "docker")


def _podman_rows(text):
    rows = []
    for c in json.loads(text or "[]") or []:
        names = c.get("Names") or []
        name = names[0] if isinstance(names, list) and names else str(names or "")
        if not name:
            continue
        labels = c.get("Labels") if isinstance(c.get("Labels"), dict) else {}
        rows.append({"name": name, "status": str(c.get("Status") or c.get("State") or ""),
                     "image": str(c.get("Image") or ""), "runtime": "podman",
                     # The owning unit comes from the label, never from scanning
                     # quadlet files: some containers are owned by plain units.
                     "unit": labels.get("PODMAN_SYSTEMD_UNIT") or None})
    return rows


def _docker_rows(text):
    rows = []
    for line in text.strip().splitlines():
        parts = line.split("|")
        if parts and parts[0]:
            rows.append({"name": parts[0],
                         "status": parts[1] if len(parts) > 1 else "",
                         "image": parts[2] if len(parts) > 2 else "",
                         "runtime": "docker", "unit": None})
    return rows


def inventory(runner=None):
    """(rows, runtimes): running containers from podman and/or docker, one `ps`
    per runtime that is installed. rows is None when no runtime answered."""
    runner = runner or _RUNNER
    rows, seen, answered, found = [], set(), False, []
    for rt in _RUNTIMES:
        if not runner.which(rt):
            continue
        found.append(rt)
        try:
            if rt == "podman":
                rc, out, _ = runner.run(["podman", "ps", "--format", "json"], timeout=20)
                got = _podman_rows(out) if rc == 0 else None
            else:
                rc, out, _ = runner.run(
                    ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"], timeout=8)
                got = _docker_rows(out) if rc == 0 else None
        except Exception:
            got = None
        if got is None:
            continue
        answered = True
        for row in got:  # `docker` may be the podman shim: one row per name
            if row["name"] not in seen:
                seen.add(row["name"])
                rows.append(row)
    return (rows if answered else None), found


def _containers(runner=None):
    """Running containers on this node (the observability payload); None if no
    container runtime answered."""
    rows, _ = inventory(runner)
    if rows is None:
        return None
    return [{"name": r["name"], "status": r["status"], "image": r["image"]} for r in rows]


def _capabilities() -> dict:
    caps = {"platform": platform.system().lower(), "machine": platform.machine(),
            "cpu_cores": os.cpu_count() or 0, "hostname": HOST}
    gpus = _gpus()
    if gpus:
        caps["gpus"] = gpus
        caps["gpu"] = gpus[0]["name"]
        caps["gpu_count"] = len(gpus)
        caps["vram_gb"] = round(sum(g.get("vram_mb", 0) for g in gpus) / 1024, 1)
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    caps["memory_gb"] = round(int(ln.split()[1]) / (1024 ** 2), 1)
                    break
    except Exception as exc:
        print(f"[dgx-agent] memory size unreadable ({exc.__class__.__name__}); "
              "capabilities go out without memory_gb", flush=True)
    return caps


def _booted_at():
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - up))
    except Exception:
        return None


def _load():
    try:
        with open("/proc/loadavg") as fh:
            return fh.read().split()[0]
    except Exception:
        return None


def pulse_body(containers) -> dict:
    """One host's view for fleet-pulse /beat. Host-level fields ONLY: `inference` and
    `platform` are global keys on the Worker, and sending them from here would
    overwrite the primary's records. A missing reading stays None, never 0."""
    body = {"host": PULSE_HOST, "booted_at": _booted_at(), "load": _load(),
            "running": None, "failed": None, "notable": []}
    if containers is not None:
        bad = [c for c in containers
               if "unhealthy" in c["status"] or c["status"].startswith("Restarting")]
        body["running"] = len(containers)
        body["failed"] = len(bad)
        body["notable"] = [f"{c['name']} : {c['status']}" for c in bad][:20]
    return body


def registry_cycle(node_key, containers):
    """One register/heartbeat round against the legacy registry.
    Returns (node_key, seconds until the next attempt)."""
    try:
        if not node_key:
            caps = _capabilities()
            res = _post("/nodes/register", {
                "name": NODE_ID, "host": HOST, "port": 8226,
                "capabilities": list(caps.keys()),
                "labels": {"role": ROLE, "tier": "sovereign", "location": "remote",
                           "gpu": caps.get("gpu", "")},
                "hardware": caps, "token": TOKEN,
            })
            node_key = res.get("api_key") or True
            print(f"[dgx-agent] registered as {res.get('node_id', NODE_ID)}", flush=True)

        _post(f"/nodes/{NODE_ID}/heartbeat", {
            "node_id": NODE_ID, "role": ROLE,
            "capabilities": _capabilities(),
            "services": [c["name"] for c in containers or []],
            "containers": containers or [],
            "token": TOKEN,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, node_key=node_key if isinstance(node_key, str) else "")
        return node_key, 0
    except RegistryRetiredError as exc:
        print(f"[dgx-agent] registry {GATEWAY} redirects ({exc}) — not a node registry; "
              f"re-probing in {REGISTRY_RETRY}s", flush=True)
        return None, REGISTRY_RETRY
    except Exception as exc:
        print(f"[dgx-agent] registry error: {str(exc)[:200]} — retrying", flush=True)
        return None, 0  # force re-register on next loop


# ───────────────────────────── placement ─────────────────────────────

MANIFEST_SCHEMA = "aither-placement/v1"
REPORT_SCHEMA = "aither-placement-report/v1"
MODES = ("observe", "enforce-absent", "enforce")  # weakest first
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:\\-]*\.service$")
_RUNNING = ("active", "reloading", "activating")
# Anything not definitively down counts as live when the placer asks "is the source
# stopped": a unit on its way down may still hold the port.
_LIVE = _RUNNING + ("deactivating",)
_UNIT_KINDS = (".service", ".socket")
_ONDEMAND_SOCKET = "-ondemand.socket"
_ONDEMAND_PROXY = "-ondemand.service"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_UNIT_FILE_BYTES = 256 * 1024
MAX_START_ATTEMPTS = 3  # per unit, per manifest generation
# The only things a bundle may hold, and so the only things ever written from one.
_BUNDLE_SUFFIXES = (".container", ".volume", ".network")
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:-]*\.(container|volume|network)$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_STABLE_CHECKS = 3


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign(obj: dict, key: bytes) -> str:
    """hex HMAC-SHA256 over the canonical JSON of obj without its `sig` field."""
    body = {k: v for k, v in obj.items() if k != "sig"}
    return hmac.new(key, canonical(body), hashlib.sha256).hexdigest()


def request_headers(key: bytes, node_id: str, method: str, url: str, body: bytes, ts: int) -> dict:
    """The three headers the controller checks on a node request. The signature
    covers the method, the URL path (no query), the timestamp and the body hash."""
    path = urllib.parse.urlsplit(url).path
    to_sign = "\n".join([method.upper(), path, str(int(ts)),
                         hashlib.sha256(body or b"").hexdigest()]).encode("utf-8")
    return {"X-Aither-Node": node_id, "X-Aither-Timestamp": str(int(ts)),
            "X-Aither-Signature": hmac.new(key, to_sign, hashlib.sha256).hexdigest()}


def manifest_mode(man: dict) -> str:
    """The manifest's own mode cap. A missing, null or non-string mode is observe:
    None must only ever mean "the cap FILE is absent" to effective_mode()."""
    mode = man.get("mode")
    return mode if isinstance(mode, str) else "observe"


def quadlet_image(text) -> str:
    """The Image= value of a quadlet (.container) file, "" when it has none."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("Image="):
            return line[len("Image="):].strip()
    return ""


def effective_mode(*values) -> str:
    """The weakest of the given modes. None means "this source is absent" and is
    skipped; anything that is not a known mode word counts as observe."""
    rank = len(MODES) - 1
    for v in values:
        if v is None:
            continue
        v = str(v).strip().lower()
        rank = min(rank, MODES.index(v) if v in MODES else 0)
    return MODES[rank]


def _arch(machine: str) -> str:
    m = (machine or "").lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(tmp, mode)
    except OSError as exc:
        print(f"[dgx-agent] could not set mode {mode:o} on {tmp} ({exc.__class__.__name__})",
              flush=True)
    os.replace(tmp, path)


def _read_bytes(path: str, limit: int):
    """File content, or None when it is missing, unreadable or over the limit."""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def bundle_files(raw: bytes):
    """({name: bytes}, None) for a unit bundle, (None, reason) when it is not a
    readable tar. Only flat regular files named *.container / *.volume /
    *.network are returned; anything else in the tar is ignored, never written.
    The tar must be UNCOMPRESSED (what the controller stores and serves): a
    compressed one could expand far past the byte cap its file was held to."""
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for member in tar:
                name = member.name[2:] if member.name.startswith("./") else member.name
                if (not member.isfile() or not _BUNDLE_NAME_RE.match(name)
                        or member.size > _MAX_UNIT_FILE_BYTES):
                    continue
                fh = tar.extractfile(member)
                if fh is not None:
                    files[name] = fh.read()
    except (tarfile.TarError, OSError, EOFError):
        return None, "bundle is not a readable tar"
    return files, None


def quadlet_refs(text) -> list:
    """The sibling .network / .volume quadlets a .container file names
    (`Network=x.network`, `Volume=x.volume:/path`)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    refs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("Network="):
            ref = line[len("Network="):].split(":")[0].strip()
        elif line.startswith("Volume="):
            ref = line[len("Volume="):].split(":")[0].strip()
        else:
            continue
        if ref.endswith((".network", ".volume")) and _BUNDLE_NAME_RE.match(ref):
            refs.append(ref)
    return refs


def unit_bundle_files(unit: str, files: dict) -> dict:
    """The bundle files ONE unit needs: its .container and what that names."""
    stem = unit[:-len(".service")]
    body = files.get(f"{stem}.container")
    if body is None:
        return {}
    out = {f"{stem}.container": body}
    for ref in quadlet_refs(body):
        if ref in files:
            out[ref] = files[ref]
    return out


def _masked(state: dict) -> list:
    """The units this agent masked, as recorded in state.json."""
    got = state.get("masked")
    return [u for u in got if isinstance(u, str)] if isinstance(got, list) else []


class PlacementAgent:
    """One node's converge step. Constructed once; cycle() is called from the loop."""

    def __init__(self, runner=None, root=None, node_id=None, mode=None, url=None,
                 key_file=None, cap_file=None, bundle=None, quadlet_dir=None,
                 headroom_mb=None, health_wait=None, log=None, stable_checks=None):
        self.runner = runner or _RUNNER
        self.root = root or PLACEMENT_DIR
        self.node_id = node_id or NODE_ID
        self.env_mode = PLACEMENT_MODE if mode is None else mode
        self.url = (PLACEMENT_URL if url is None else url).rstrip("/")
        self.key_file = key_file or PLACEMENT_KEY_FILE or os.path.join(self.root, "node.key")
        self.cap_file = cap_file or PLACEMENT_CAP_FILE or os.path.join(self.root, "mode.cap")
        self.bundle = bundle or PLACEMENT_BUNDLE or os.path.join(self.root, "bundle.tar")
        self.quadlet_dir = quadlet_dir or QUADLET_DIR
        self.desired = os.path.join(self.root, "desired.json")
        self.absent_units = os.path.join(self.root, "absent.units")
        self.state_file = os.path.join(self.root, "state.json")
        self.staging = os.path.join(self.root, "staging")
        if stable_checks is None:
            raw = PLACEMENT_STABLE_CHECKS.strip()
            stable_checks = int(raw) if raw.isdigit() else DEFAULT_STABLE_CHECKS
        # One sample proves nothing about a unit that restarts between samples.
        self.stable_checks = max(2, int(stable_checks))
        self._stable = {}  # unit -> (ActiveEnterTimestampMonotonic, consecutive checks)
        self._pull_line = None
        if headroom_mb is None:
            raw = PLACEMENT_HEADROOM_MB.strip()
            headroom_mb = int(raw) if raw.isdigit() else None
        self.headroom_mb = headroom_mb
        self.health_wait = PLACEMENT_HEALTH_WAIT if health_wait is None else health_wait
        self.log = log or (lambda msg: print(f"[dgx-agent] {msg}", flush=True))
        self._last_line = None
        self._report_ok = None

    # ── inputs ──────────────────────────────────────────────────────────

    def _key(self):
        """(key bytes, None) or (None, reason). The value is never logged."""
        try:
            st = os.stat(self.key_file)
        except OSError:
            return None, "no node key"
        if os.name == "posix" and st.st_mode & 0o077:
            return None, "node key is readable by group or others (want 0600)"
        raw = _read_bytes(self.key_file, 4096)
        key = (raw or b"").strip()
        if not key:
            return None, "node key is empty or unreadable"
        return key, None

    def _state(self) -> dict:
        raw = _read_bytes(self.state_file, _MAX_MANIFEST_BYTES)
        if raw is None:
            # Present but unreadable must not reopen the replay window.
            return {"corrupt": True} if os.path.lexists(self.state_file) else {}
        try:
            st = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"corrupt": True}
        return st if isinstance(st, dict) else {"corrupt": True}

    def _cap(self):
        """The cap file's mode word; None when there is no cap file."""
        if not os.path.lexists(self.cap_file):
            return None
        raw = _read_bytes(self.cap_file, 256)
        if raw is None:
            return "observe"
        words = raw.decode("utf-8", "replace").split()
        return words[0] if words else "observe"

    def _parse_signed(self, raw, key):
        """(manifest, expected sig, None) for bytes that are a manifest signed for
        THIS node, else (None, None, reason). The signature is checked before any
        field is believed; the generation is not judged here."""
        try:
            man = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None, None, "manifest is not JSON"
        if not isinstance(man, dict):
            return None, None, "manifest is not an object"
        sig = man.get("sig")
        try:
            want = sign(man, key)
        except (TypeError, ValueError):
            return None, None, "manifest cannot be canonicalised"
        if not isinstance(sig, str) or not hmac.compare_digest(sig.lower().encode(), want.encode()):
            return None, None, "bad signature"
        if man.get("schema") != MANIFEST_SCHEMA:
            return None, None, f"unknown schema {str(man.get('schema'))[:40]!r}"
        if man.get("node") != self.node_id:
            return None, None, (f"manifest is for node {str(man.get('node'))[:64]!r}, "
                                f"not {self.node_id!r}")
        gen = man.get("generation")
        if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
            return None, None, "generation is not a non-negative integer"
        problem = self._shape_problem(man)
        if problem:
            return None, None, problem
        return man, want, None

    @staticmethod
    def _replay_problem(gen, want, state, extra=None):
        """Why generation `gen` (signature `want`) may not be accepted, or None.

        The replay floor is the HIGHEST generation this node ever accepted, in any
        mode. An observe manifest advances it too: otherwise freezing a node with
        generation N+1 (observe) would leave generation N (enforce) replayable.
        `extra` is one more (generation, sig, word) floor: the cached desired.json
        when a fetched manifest is judged."""
        if state.get("corrupt"):
            return "state.json is unreadable: cannot rule out a replay"
        floor, floor_sig, word = None, None, "applied"
        floors = [(state.get("generation"), state.get("sig"), "applied"),
                  (state.get("accepted_generation"), state.get("accepted_sig"), "accepted")]
        if extra is not None:
            floors.append(extra)
        for seen, seen_sig, label in floors:
            if isinstance(seen, int) and not isinstance(seen, bool) and (
                    floor is None or seen > floor):
                floor, floor_sig, word = seen, seen_sig, label
        if floor is not None:
            if gen < floor:
                return f"replay: generation {gen} is older than {word} {floor}"
            # The accepted manifest stays on disk and is re-converged every cycle;
            # a DIFFERENT manifest must carry a higher generation.
            if gen == floor and floor_sig != want:
                return f"replay: generation {gen} was already {word} with other content"
        return None

    def load_manifest(self, key, state):
        """(manifest, None) when desired.json may be acted on, else (None, reason)."""
        raw = _read_bytes(self.desired, _MAX_MANIFEST_BYTES)
        if raw is None:
            return None, ("manifest unreadable" if os.path.lexists(self.desired)
                          else "no manifest")
        man, want, reason = self._parse_signed(raw, key)
        if man is None:
            return None, reason
        if man["generation"] < 1:
            # Generation 0 is the controller's "nothing stored" answer. It is
            # never written here by a pull; a copy placed by hand is not a manifest.
            return None, "generation is not a positive integer"
        problem = self._replay_problem(man["generation"], want, state)
        if problem:
            return None, problem
        return man, None

    # ── pull mode ───────────────────────────────────────────────────────

    def _fetch(self, what, key, limit, etag=None):
        """Signed GET of <url>/<what>: (status, body)."""
        url = f"{self.url}/{what}"
        headers = request_headers(key, self.node_id, "GET", url, b"", int(self.runner.now()))
        if etag:
            headers["If-None-Match"] = etag
        status, body = self.runner.get(url, headers, limit)
        return int(status), body or b""

    def _bundle_for(self, man, key):
        """(tar bytes | b"" when the manifest names none, fetched?, problem).
        The digest is checked before the bytes are returned to anything."""
        want = man.get("bundle_sha256")
        if not want:
            return b"", False, None
        if not isinstance(want, str) or not _HEX64_RE.match(want):
            return None, False, "manifest bundle_sha256 is not a sha256"
        local = _read_bytes(self.bundle, _MAX_BUNDLE_BYTES)
        if local is not None and hashlib.sha256(local).hexdigest() == want:
            return local, False, None
        try:
            status, data = self._fetch("bundle", key, _MAX_BUNDLE_BYTES)
        except Exception as exc:
            return None, False, f"bundle fetch failed: {exc.__class__.__name__}: {str(exc)[:120]}"
        if status != 200:
            return None, False, f"controller answered HTTP {status} for the bundle"
        if hashlib.sha256(data).hexdigest() != want:
            return None, False, "fetched bundle sha256 does not match the manifest"
        _, err = bundle_files(data)
        if err:
            return None, False, err
        return data, True, None

    def _stage(self, man, raw_bundle):
        """Unpack what present[] needs from the verified bundle into <dir>/staging
        and drop staged unit files it no longer needs: (staged names, None), or
        (None, why) when staging could not be written. Never touches the quadlet
        directory: a staging path that is a link, or that IS the quadlet
        directory, is refused instead of written through. Staging is a review
        copy; what a start installs comes from the verified tar, so a staging
        fault holds nothing back and is only reported."""
        files, _ = bundle_files(raw_bundle) if raw_bundle else ({}, None)
        wanted = {}
        for ent in man.get("present", []):
            wanted.update(unit_bundle_files(ent["unit"], files or {}))
        try:
            if os.path.islink(self.staging):
                return None, "staging is a symbolic link; nothing was unpacked"
            if os.path.realpath(self.staging) == os.path.realpath(self.quadlet_dir):
                return None, "staging is the quadlet directory; nothing was unpacked"
            if wanted or os.path.isdir(self.staging):
                os.makedirs(self.staging, mode=0o700, exist_ok=True)
                for name, data in sorted(wanted.items()):
                    path = os.path.join(self.staging, name)
                    if os.path.islink(path):
                        os.remove(path)  # never write through a link someone left here
                    if _read_bytes(path, _MAX_UNIT_FILE_BYTES) != data:
                        _write_atomic(path, data, 0o644)
                for name in os.listdir(self.staging):
                    if name.endswith(_BUNDLE_SUFFIXES) and name not in wanted:
                        os.remove(os.path.join(self.staging, name))
        except OSError as exc:
            return None, f"staging could not be written ({exc.__class__.__name__})"
        return sorted(wanted), None

    def pull(self, key, state) -> dict:
        """Fetch this node's manifest (and bundle) from the controller and, only
        when every check passes, replace the cached copies. Returns the `pull`
        block of the report. A failure of any kind writes nothing."""
        out = {"ok": False, "status": "", "reason": None, "generation": None,
               "changed": False, "staged": None, "stage_error": None}

        def done(status, reason=None, ok=False, **more):
            out.update({"ok": ok, "status": status, "reason": reason})
            out.update(more)
            return out

        def staged(man, data):
            names, problem = self._stage(man, data)
            return {"staged": names, "stage_error": problem}

        cached = _read_bytes(self.desired, _MAX_MANIFEST_BYTES)
        etag = '"%s"' % hashlib.sha256(cached).hexdigest() if cached else None
        try:
            status, body = self._fetch("manifest", key, _MAX_MANIFEST_BYTES, etag)
        except Exception as exc:
            return done("unreachable", f"manifest fetch failed: {exc.__class__.__name__}: "
                                       f"{str(exc)[:120]}; cached manifest kept")
        cached_man, cached_sig = None, None
        if cached:
            cached_man, cached_sig, _ = self._parse_signed(cached, key)
        if status == 304 or (status == 200 and cached and body == cached):
            if cached_man is None:
                return done("refused", "controller says unchanged but the cached manifest "
                                       "does not verify; nothing written")
            if cached_man["generation"] < 1 or self._replay_problem(
                    cached_man["generation"], cached_sig, state):
                # load_manifest refuses it below and says why; fetch nothing for it.
                return done("unchanged", ok=True, generation=cached_man["generation"])
            # Same manifest; make sure the bundle it names is here and staged too.
            data, fetched, problem = self._bundle_for(cached_man, key)
            if problem:
                return done("refused", f"{problem}; cached manifest kept",
                            generation=cached_man["generation"])
            if fetched:
                _write_atomic(self.bundle, data, 0o600)
            return done("unchanged", ok=True, generation=cached_man["generation"],
                        changed=fetched, **staged(cached_man, data))
        if status != 200:
            return done("refused", f"controller answered HTTP {status} for the manifest; "
                                   "cached manifest kept")
        man, want, reason = self._parse_signed(body, key)
        if man is None:
            return done("refused", f"fetched manifest refused: {reason}; nothing written")
        gen = man["generation"]
        held = cached_man["generation"] if cached_man else None
        if gen == 0:
            # "Nothing is stored for you." Recognised, reported, never written:
            # it cannot replace a manifest this node holds or lower its floor.
            if (man.get("present") or man.get("absent") or man.get("on_demand")
                    or manifest_mode(man) != "observe" or man.get("bundle_sha256")):
                return done("refused", "fetched manifest refused: generation 0 must be the "
                                       "empty observe manifest; nothing written")
            return done("controller-has-no-manifest", ok=True, generation=0, reason=(
                "the controller holds no manifest for this node"
                + (f"; this node keeps generation {held}" if held else "")))
        extra = (held, cached_sig, "cached") if cached_man else None
        problem = self._replay_problem(gen, want, state, extra)
        if problem:
            return done("refused", f"fetched manifest refused: {problem}; nothing written",
                        generation=gen)
        data, fetched, problem = self._bundle_for(man, key)
        if problem:
            return done("refused", f"{problem}; nothing written", generation=gen)
        try:
            os.makedirs(self.root, exist_ok=True)
            if fetched:
                _write_atomic(self.bundle, data, 0o600)
            _write_atomic(self.desired, body, 0o644)
        except OSError as exc:
            # desired.json is replaced last and atomically, so it is still the old
            # one. If bundle.tar was already swapped, the old manifest's units
            # that still need installing HOLD (digest mismatch) until the next
            # cycle retries this manifest.
            return done("refused", f"the fetched manifest could not be written "
                                   f"({exc.__class__.__name__}); cached manifest kept",
                        generation=gen)
        return done("updated", ok=True, generation=gen, changed=True, **staged(man, data))

    @staticmethod
    def _shape_problem(man: dict):
        names = {}
        for lst in ("present", "absent"):
            entries = man.get(lst, [])
            if not isinstance(entries, list):
                return f"{lst} is not a list"
            for ent in entries:
                unit = ent.get("unit") if isinstance(ent, dict) else None
                if not isinstance(unit, str) or not _UNIT_RE.match(unit):
                    return f"{lst} has an invalid unit name"
                if unit in names:
                    return f"unit {unit} is listed twice ({names[unit]} and {lst})"
                names[unit] = lst
        return None

    def unit_states(self):
        """{unit: (LOAD, ACTIVE)} for services and sockets from ONE systemctl call,
        or None when it failed. A unit that is not listed is not in memory, which
        means it is not running; whether it EXISTS is a separate question that
        _unit() answers (systemd drops an idle, unreferenced unit from this list)."""
        rc, out, _ = self.runner.run(
            ["systemctl", "list-units", "--type=service,socket", "--all", "--no-legend",
             "--plain", "--no-pager"], timeout=30)
        if rc != 0:
            return None
        states = {}
        for line in out.splitlines():
            cols = line.replace("\u25cf", " ").replace("*", " ").split()
            if len(cols) >= 3 and cols[0].endswith(_UNIT_KINDS):
                states[cols[0]] = (cols[1], cols[2])
        return states

    def _unit(self, unit: str, states: dict):
        """(LOAD, ACTIVE) of one unit. A unit systemd does not hold in memory is
        asked for by name: LOAD is then loaded / masked / not-found, or None when
        systemd did not answer. Read-only."""
        if unit in states:
            return states[unit]
        rc, out, _ = self.runner.run(
            ["systemctl", "show", "-p", "LoadState", "--value", unit], timeout=20)
        load = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else None
        return load, "inactive"

    # ── verdicts and actions ────────────────────────────────────────────

    def converge(self, man: dict, mode: str, states, state: dict) -> list:
        rank = MODES.index(mode)
        verdicts = []
        ours = _masked(state)
        before = (list(ours), dict(state.get("starts") or {}))

        def verdict(ent, lst, st, word, reason="", action="none", healthy=None, masked=None,
                    known=True):
            verdicts.append({"unit": ent["unit"], "service": ent.get("service"), "list": lst,
                             "state": st, "verdict": word, "reason": reason,
                             "action": action, "healthy": healthy, "masked": masked,
                             "known": known})

        # absent first: the source of a move is stopped before anything new starts.
        for ent in man.get("absent", []):
            unit = ent["unit"]
            if states is None:
                verdict(ent, "absent", None, "hold",
                        "unit state unknown (systemctl did not answer)", known=False)
                continue
            load, st = self._unit(unit, states)
            if load is None or load == "not-found":
                # "Not running" is not evidence here. A unit systemd has never heard
                # of means this node and the manifest disagree on the NAME, and the
                # real unit may be running under another one.
                verdict(ent, "absent", None, "hold",
                        "unknown unit: systemd has no unit by this name on this node"
                        if load == "not-found" else
                        "unit load state unknown (systemctl did not answer)", known=False)
                continue
            masked = load == "masked"
            if rank < 1:
                if st in _RUNNING:
                    verdict(ent, "absent", st, "would-stop", f"mode {mode} stops nothing",
                            masked=masked)
                elif masked:
                    verdict(ent, "absent", st, "ok", masked=True)
                else:
                    verdict(ent, "absent", st, "ok", f"stopped, not masked (mode {mode} "
                            "masks nothing)", masked=False)
                continue
            problems, actions = [], []
            # The on-demand socket first: while it listens, the first client byte
            # starts the container again.
            stem = unit[:-len(".service")]
            sock, proxy = stem + _ONDEMAND_SOCKET, stem + _ONDEMAND_PROXY
            sload, sst = self._unit(sock, states)
            if sload in ("loaded", "masked"):
                if sst in _LIVE:
                    rc, _, err = self.runner.run(["systemctl", "stop", sock], timeout=60)
                    if rc != 0:
                        problems.append(f"stop {sock} failed: {err.strip()[:120]}")
                if sload != "masked" and not problems:
                    rc, _, err = self.runner.run(["systemctl", "mask", sock], timeout=60)
                    if rc == 0:
                        ours.append(sock)
                    else:
                        problems.append(f"mask {sock} failed: {err.strip()[:120]}")
                if proxy in states and states[proxy][1] in _LIVE:
                    self.runner.run(["systemctl", "stop", proxy], timeout=60)
            if st in _LIVE:
                rc, _, err = self.runner.run(["systemctl", "stop", unit], timeout=180)
                if rc == 0:
                    st = "inactive"
                    actions.append("stopped")
                else:
                    problems.append(f"stop failed: {err.strip()[:160]}")
            if st not in _LIVE and not masked:
                rc, _, err = self.runner.run(["systemctl", "mask", unit], timeout=60)
                if rc == 0:
                    masked = True
                    ours.append(unit)
                    actions.append("masked")
                else:
                    problems.append(f"stopped, but mask failed: {err.strip()[:140]}")
            if problems:
                verdict(ent, "absent", st, "hold", "; ".join(problems),
                        "+".join(actions) or "none", masked=masked)
            else:
                verdict(ent, "absent", st, "ok", " and ".join(actions),
                        "+".join(actions) or "none", masked=masked)
        if rank >= 1:
            self._write_absent_units(man)

        attempted = False
        starts = state.get("starts") if isinstance(state.get("starts"), dict) else {}
        for ent in man.get("present", []):
            unit = ent["unit"]
            if states is None:
                verdict(ent, "present", None, "hold",
                        "unit state unknown (systemctl did not answer)", known=False)
                continue
            load, st = self._unit(unit, states) if unit in states or unit in ours else (
                None, "not-loaded")
            if load == "masked" or unit in ours:
                if unit not in ours:
                    verdict(ent, "present", st, "hold", "masked on this node by something "
                            "other than this agent; left alone", masked=True)
                    continue
                if rank < 1:
                    verdict(ent, "present", st, "would-start",
                            f"masked by this agent; mode {mode} unmasks nothing", masked=True)
                    continue
                # Listed present again: undo exactly the mask this agent placed.
                mine = [unit] + [u for u in ours
                                 if u == unit[:-len(".service")] + _ONDEMAND_SOCKET]
                rc, _, err = self.runner.run(["systemctl", "unmask"] + mine, timeout=60)
                if rc != 0:
                    verdict(ent, "present", st, "hold", f"unmask failed: {err.strip()[:160]}",
                            masked=True)
                    continue
                ours[:] = [u for u in ours if u not in mine]
                st = "inactive"
            if st != "active":
                self._stable.pop(unit, None)
            if st == "active":
                url = ent.get("health_url")
                note = ""
                if url:
                    healthy = self.runner.http_ok(url)
                else:
                    healthy, note = self._systemd_stable(unit)
                if healthy is False:
                    verdict(ent, "present", st, "hold", "active but its health check fails",
                            healthy=False, masked=False)
                else:
                    # The attempt cap counts consecutive failures, not lifetime
                    # starts: a node that reboots must be able to start it again.
                    starts.pop(unit, None)
                    verdict(ent, "present", st, "ok", note, healthy=healthy, masked=False)
            elif st in ("activating", "reloading", "deactivating"):
                verdict(ent, "present", st, "hold", f"unit is {st}", masked=False)
            elif rank < 2:
                verdict(ent, "present", st, "would-start", f"mode {mode} starts nothing",
                        masked=False)
            elif attempted:
                verdict(ent, "present", st, "would-start", "queued: one start per cycle",
                        masked=False)
            else:
                word, reason, action, healthy, tried = self._start(ent, man, state)
                attempted = attempted or tried
                verdict(ent, "present", "active" if word == "ok" or action == "started" else st,
                        word, reason, action, healthy, masked=False)
        if rank >= 1:
            state["masked"] = sorted(set(ours))
            if (state["masked"], dict(state.get("starts") or {})) != (sorted(set(before[0])),
                                                                      before[1]):
                self._save_state(state)
        return verdicts

    def _systemd_stable(self, unit: str):
        """Health of an active unit that has no health_url: (True, note) once
        systemd has shown it active under the SAME activation for
        `stable_checks` consecutive cycles, else (None, note). Never False: a
        unit that is not active does not reach here. Read-only."""
        rc, out, _ = self.runner.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", unit],
            timeout=20)
        since = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
        if not since.isdigit() or since == "0":
            self._stable.pop(unit, None)
            return None, "no health_url, and systemd did not say since when it is active"
        seen = self._stable.get(unit)
        count = seen[1] + 1 if seen and seen[0] == since else 1
        self._stable[unit] = (since, count)
        if count >= self.stable_checks:
            return True, (f"no health_url: active under one activation for {count} "
                          "consecutive checks")
        return None, (f"no health_url: active for {count} of {self.stable_checks} "
                      "consecutive checks")

    def _write_absent_units(self, man: dict) -> None:
        text = "".join(f"{ent['unit']}\n" for ent in man.get("absent", []))
        current = _read_bytes(self.absent_units, _MAX_MANIFEST_BYTES)
        if current is None or current.decode("utf-8", "replace") != text:
            _write_atomic(self.absent_units, text.encode("ascii"), 0o644)

    def _save_state(self, state: dict) -> None:
        _write_atomic(self.state_file, json.dumps(state, sort_keys=True, indent=1).encode(), 0o600)

    def _bundle_files(self, man: dict):
        """({name: bytes}, None) from the verified bundle, ({}, None) when the
        manifest names no bundle, (None, reason) when it names one that fails."""
        want = man.get("bundle_sha256")
        if not want:
            return {}, None
        raw = _read_bytes(self.bundle, _MAX_BUNDLE_BYTES)
        if raw is None:
            return None, "bundle is missing or too large"
        if not isinstance(want, str) or hashlib.sha256(raw).hexdigest() != want.lower():
            return None, "bundle sha256 does not match the manifest"
        return bundle_files(raw)

    def _install(self, unit: str, man: dict, image: str):
        """Write this unit's quadlet (and any network file it needs that is not
        there yet) from the verified bundle. (changed, error). A bundle quadlet
        whose Image= is not the manifest's image is refused before it is written."""
        files, err = self._bundle_files(man)
        if files is None:
            return False, err
        stem = unit[:-len(".service")]
        # Only this unit's own files: its quadlet and the siblings it names.
        files = unit_bundle_files(unit, files)
        body = files.get(f"{stem}.container")
        if body is None:
            return False, None
        if not self.runner.which("podman"):
            return False, "bundle carries a quadlet but this node has no podman"
        if quadlet_image(body) != image:
            return False, ("the bundle's quadlet has Image=%s, not the manifest's image; "
                           "not installed" % (quadlet_image(body)[:120] or "(none)"))
        changed = False
        target = os.path.join(self.quadlet_dir, f"{stem}.container")
        if _read_bytes(target, _MAX_UNIT_FILE_BYTES) != body:
            _write_atomic(target, body, 0o644)
            changed = True
        for name, data in sorted(files.items()):
            path = os.path.join(self.quadlet_dir, name)
            if name.endswith((".network", ".volume")) and not os.path.lexists(path):
                _write_atomic(path, data, 0o644)  # never overwrites an existing one
                changed = True
        return changed, None

    def _start(self, ent: dict, man: dict, state: dict):
        """Admission -> install -> reload -> start -> health gate.
        Returns (verdict, reason, action, healthy, systemctl_start_was_called)."""
        unit = ent["unit"]
        starts = state.setdefault("starts", {})
        if starts.get(unit, 0) >= MAX_START_ATTEMPTS:
            return ("hold", f"start already tried {MAX_START_ATTEMPTS} times for this generation",
                    "none", None, False)
        mem_mb = ent.get("mem_mb")
        if isinstance(mem_mb, bool) or not isinstance(mem_mb, int) or mem_mb <= 0:
            return "hold", "admission: no measured mem_mb", "none", None, False
        if self.headroom_mb is None:
            return ("hold", "admission: no headroom declared (AITHER_PLACEMENT_HEADROOM_MB)",
                    "none", None, False)
        avail_kb = self.runner.meminfo().get("MemAvailable")
        if not isinstance(avail_kb, int):
            return "hold", "admission: MemAvailable could not be read", "none", None, False
        avail_mb = avail_kb // 1024
        if avail_mb - mem_mb < self.headroom_mb:
            return ("hold", f"admission: MemAvailable {avail_mb} MB - mem_mb {mem_mb} MB "
                            f"is under headroom {self.headroom_mb} MB", "none", None, False)
        if "@sha256:" not in str(ent.get("image") or ""):
            return "hold", "image is not digest-pinned", "none", None, False
        image = str(ent.get("image") or "")
        try:
            changed, err = self._install(unit, man, image)
        except OSError as exc:
            return "hold", f"install failed: {str(exc)[:160]}", "none", None, False
        if err:
            return "hold", err, "none", None, False
        # What runs is the Image= of the quadlet on disk, not the manifest field.
        # No quadlet, or another image, and the signed digest pin proves nothing.
        quadlet = os.path.join(self.quadlet_dir, unit[:-len(".service")] + ".container")
        on_disk = _read_bytes(quadlet, _MAX_UNIT_FILE_BYTES)
        if on_disk is None:
            return ("hold", "no quadlet for this unit in %s: cannot prove which image it "
                            "would run" % self.quadlet_dir, "none", None, False)
        if quadlet_image(on_disk) != image:
            return ("hold", "the unit's Image=%s is not the manifest's digest-pinned image"
                    % (quadlet_image(on_disk)[:120] or "(none)"), "none", None, False)
        if changed:
            rc, _, serr = self.runner.run(["systemctl", "daemon-reload"], timeout=120)
            if rc != 0:
                return ("hold", f"daemon-reload failed: {serr.strip()[:160]}", "installed",
                        None, False)
        starts[unit] = starts.get(unit, 0) + 1
        self._save_state(state)
        rc, _, serr = self.runner.run(["systemctl", "start", unit], timeout=300)
        if rc != 0:
            return "hold", f"start failed: {serr.strip()[:160]}", "none", None, True
        url = ent.get("health_url")
        if not url:
            return "ok", "started (no health_url to gate on)", "started", None, True
        waited = 0
        while True:
            if self.runner.http_ok(url):
                return "ok", "started and healthy", "started", True, True
            if waited >= self.health_wait:
                break
            self.runner.sleep(5)
            waited += 5
        return ("hold", f"started but not healthy after {self.health_wait}s "
                        "(left running, reported)", "started", False, True)

    # ── one cycle ───────────────────────────────────────────────────────

    def cycle(self, rows=None, runtimes=None) -> dict:
        """Verify, give verdicts, act as far as the effective mode allows, report."""
        if runtimes is None:
            rows, runtimes = inventory(self.runner)
        key, key_problem = self._key()
        man, reason, verdicts, mode = None, key_problem, [], "observe"
        state = self._state()
        pulled = None
        if self.url and key is not None:
            try:
                pulled = self.pull(key, state)
            except Exception as exc:  # a fetch fault must never stop the converge
                pulled = {"ok": False, "status": "error", "generation": None,
                          "changed": False, "staged": None, "stage_error": None,
                          "reason": f"pull failed: {exc.__class__.__name__}: {str(exc)[:120]}; "
                                    "cached manifest kept"}
            line = "pull: " + pulled["status"] + (
                f" ({pulled['reason']})" if pulled.get("reason") else "")
            if line != self._pull_line:  # transitions, not every cycle
                self.log(line)
                self._pull_line = line
        if key is not None:
            man, reason = self.load_manifest(key, state)
            if (man is None and reason == "no manifest" and pulled
                    and pulled["status"] == "controller-has-no-manifest"):
                reason = "no manifest: the controller holds none for this node yet"
        if man is not None:
            mode = effective_mode(self.env_mode, manifest_mode(man), self._cap())
            gen, sig = man["generation"], man["sig"].lower()
            dirty = False
            if state.get("accepted_generation") != gen or state.get("accepted_sig") != sig:
                # Every mode, observe included: this is the replay floor.
                state.update({"node": self.node_id, "accepted_generation": gen,
                              "accepted_sig": sig})
                dirty = True
            if mode != "observe" and state.get("generation") != gen:
                state = {"node": self.node_id, "generation": gen, "sig": sig,
                         "accepted_generation": gen, "accepted_sig": sig, "starts": {},
                         # What this agent masked outlives a generation: only a
                         # manifest that lists the unit present again undoes it.
                         "masked": _masked(state),
                         "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                dirty = True
            try:
                if dirty:
                    self._save_state(state)
            except OSError as exc:
                # A floor that cannot be recorded is a replay window: hold.
                man, mode = None, "observe"
                reason = f"state.json cannot be written ({exc.__class__.__name__})"
        if man is not None:
            verdicts = self.converge(man, mode, self.unit_states(), state)

        counts = {}
        for v in verdicts:
            counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        if man is not None:
            line = (f"converge: {mode} gen={man['generation']} "
                    + " ".join(f"{k}={counts[k]}" for k in sorted(counts))).rstrip()
        else:
            line = f"converge: hold ({reason})"
        if line != self._last_line:  # transitions, not every cycle
            self.log(line)
            self._last_line = line

        mem = self.runner.meminfo()
        host = self.runner.host()
        versions = {}
        for rt in runtimes or []:
            rc, out, _ = self.runner.run([rt, "--version"], timeout=8)
            versions[rt] = out.strip().splitlines()[0][:80] if rc == 0 and out.strip() else None
        primary = (runtimes or ["none"])[0]
        edge = man.get("edge") if man is not None and isinstance(man.get("edge"), dict) else {}
        report = {
            "schema": REPORT_SCHEMA, "node": self.node_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "manifest": {"accepted": man is not None, "reason": reason,
                         "generation": man["generation"] if man else None,
                         "applied_generation": state.get("generation"),
                         "accepted_generation": state.get("accepted_generation")},
            # Pull mode only: what the fetch did this cycle. ok=False means the
            # cached desired.json is what was converged on.
            "pull": pulled,
            # This agent applies NO edge entry: it sets up no name and opens no port.
            # Said in every report, with how many entries the accepted manifest
            # carries, so the placer never treats a name as routed here.
            "edge": {"applied": False,
                     "egress": len(edge["egress"]) if isinstance(edge.get("egress"), list) else 0,
                     "ingress": len(edge["ingress"])
                     if isinstance(edge.get("ingress"), list) else 0},
            "verdicts": verdicts,
            # What the placer reads. Only units whose state is KNOWN are listed: an
            # unknown unit must never look like a stopped one.
            "units": {v["unit"]: {"active": v["state"] in _LIVE, "healthy": v["healthy"],
                                  "masked": v["masked"]}
                      for v in verdicts if v["known"] and v["state"] is not None},
            "inventory": rows,  # None = no runtime answered; never an empty list
            "mem_total_mb": (mem["MemTotal"] // 1024
                             if isinstance(mem.get("MemTotal"), int) else None),
            "mem_available_mb": (mem["MemAvailable"] // 1024
                                 if isinstance(mem.get("MemAvailable"), int) else None),
            "cores": host.get("cores"), "arch": _arch(host.get("machine", "")),
            "runtime": primary, "runtime_version": versions.get(primary),
            "runtimes": versions,
        }
        if self.url and key is not None:
            report["sig"] = sign(report, key)
            try:
                url = f"{self.url}/report"
                body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
                status = self.runner.post(
                    url, body, request_headers(key, self.node_id, "POST", url, body,
                                               int(self.runner.now())))
                if not 200 <= int(status) < 300:
                    raise RuntimeError(f"controller answered HTTP {status}")
                if self._report_ok is not True:
                    self.log("placement report ok")
                self._report_ok = True
            except Exception as exc:
                if self._report_ok is not False:
                    self.log(f"placement report error: {str(exc)[:200]}")
                self._report_ok = False
        return report


def placement_configured() -> bool:
    return bool(PLACEMENT_URL) or os.path.isdir(PLACEMENT_DIR)


def main() -> None:
    pulse_token = _pulse_token() if PULSE_ENABLED else ""
    registry = bool(GATEWAY and TOKEN)
    placement = PlacementAgent() if placement_configured() else None
    if not pulse_token and not registry and placement is None:
        raise SystemExit("no sink: set FLEET_PULSE_TOKEN(_FILE) and/or "
                         "AITHER_CLUSTER_GATEWAY + AITHER_CLUSTER_TOKEN")

    print(f"[dgx-agent] starting — node={NODE_ID} pulse={'on' if pulse_token else 'off'} "
          f"registry={GATEWAY if registry else 'off'} "
          f"placement={PLACEMENT_MODE if placement else 'off'}", flush=True)

    node_key = None
    registry_next = 0.0
    placement_next = 0.0
    pulse_ok = None
    while True:
        rows, runtimes = inventory()
        containers = (None if rows is None else
                      [{"name": r["name"], "status": r["status"], "image": r["image"]}
                       for r in rows])
        if pulse_token:
            try:
                _request(f"{PULSE_URL}/beat", pulse_body(containers),
                         {"Authorization": f"Bearer {pulse_token}"})
                if pulse_ok is not True:  # log transitions, not every beat
                    print(f"[dgx-agent] pulse beat ok — host={PULSE_HOST}", flush=True)
                pulse_ok = True
            except Exception as exc:
                print(f"[dgx-agent] pulse error: {str(exc)[:200]}", flush=True)
                pulse_ok = False
        if registry and time.monotonic() >= registry_next:
            node_key, wait = registry_cycle(node_key, containers)
            registry_next = time.monotonic() + wait
        if placement is not None and time.monotonic() >= placement_next:
            try:
                placement.cycle(rows, runtimes)
            except Exception as exc:  # a converge fault must never stop the beat
                print(f"[dgx-agent] converge error: {str(exc)[:200]}", flush=True)
            placement_next = time.monotonic() + PLACEMENT_INTERVAL
        time.sleep(HEARTBEAT)


if __name__ == "__main__":
    main()
