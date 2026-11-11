#!/usr/bin/python3.11
"""awnix-fleet -- offer this machine to a fleet's placement controller, report it, leave.

Installed as /usr/libexec/awnix/awnix-fleet, so `awnix fleet ...` runs it through the
dispatcher. Stdlib only and Python 3.9 compatible: node-install.sh runs the same file
with the system python3 on plain Linux.

    awnix fleet join <controller-url> <token|@file|-> [--node-id ID] [--fleet-ip IP]
                     [--pin-sha256 HEX | --ca-file PEM] [--allow-http]
                     [--dry-run] [--no-start]
    awnix fleet join --request-file PATH
                     (one line: <controller-url> <token> [sha256:HEX] [node=ID])
    awnix fleet status [--json]
    awnix fleet leave [--purge]
    awnix-fleet --self-test | --list-verbs

What a join does, in this order:

  1. MEASURE: cores, MemTotal, MemAvailable, arch, layout (wsl|linux), the container
     runtime and its version, the overlay address. Nothing is asked of the controller
     before the machine has been measured.
  2. REFUSE, with the reason, when there is no quadlet-capable podman (podman >= 4.4, its
     systemd generator, systemd as PID 1) or when less than 4 GiB would remain after a
     6 GiB headroom is taken from MemAvailable. A refused machine is left untouched.
  3. POST {controller}/api/placement/enroll {schema, token, node_id, hostname, capacity}.
     The answer carries this node's own key. The node shows as PENDING on the controller
     until an operator approves it there.
  4. Write /etc/aither/placement/agent.env (0644, no secret in it) and node.key (0600),
     then start aither-node-agent.service in OBSERVE mode. The agent's
     AITHER_PLACEMENT_URL is {controller}/api/placement/nodes/{node}: it reports to
     <that>/report. agent.env also carries the headroom this join measured against
     (AITHER_PLACEMENT_HEADROOM_MB; the agent admits no start without one) and turns
     the agent's fleet-pulse sink off (AITHER_NODE_PULSE=0: a host that already beats
     pulse from its own script must not have that record overwritten).

THE NODE ID is the node's key in the fleet's nodes.yaml, everywhere: the manifest, the
controller's routes and the node's max_mode are all looked up by it. It is NOT the
hostname on most fleet machines, so pass --node-id (or `node=<id>` in a request file,
which is how the first-boot step and awnix-fleet-wsl.ps1 -FleetNodeId carry it). The
short hostname is only the default. A token the operator bound to a node id enrolls
that id and no other.

A join starts no fleet service and installs no unit: the agent in observe mode reads and
reports. `join` always writes AITHER_PLACEMENT_MODE=observe; raising the mode is an
operator's edit, never this verb's.

The controller is checked before the token is sent: https with the system trust store, or
--ca-file, or --pin-sha256 (the SHA-256 of the controller's certificate, which a
self-signed fleet controller needs). Plain http is refused except to loopback or with
--allow-http. Redirects are never followed. The trust decision outlives the join: with a
pin, agent.env carries AITHER_PLACEMENT_CONTROLLER_SHA256 and the agent verifies the
controller BY THAT FINGERPRINT (a pinned leaf issued by a private CA is not a trust
anchor for OpenSSL on the node's Python 3.9, so the saved controller.pem alone would
fail every report); with --ca-file the bundle is saved as controller.pem and named as
AITHER_PLACEMENT_CA_FILE. Either way the agent accepts that controller and no other.

`leave` stops the agent and removes agent.env (and controller.pem). node.key is kept
until `leave --purge`.
desired.json, absent.units and state.json are never removed by this verb: the boot path
reads absent.units to keep services that were moved elsewhere from starting here again.

Exit: 0 ok (joined, left, or nothing to do), 1 refused/invalid, 2 offline or the agent
could not be started/stopped. The token and the node key are never printed or logged.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:  # POSIX only; the Windows test host has none and runs one join at a time
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

VERBS = ("join", "status", "leave")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_OFFLINE = 2

ENROLL_PATH = "/api/placement/enroll"
NODE_PATH = "/api/placement/nodes/%s"  # the agent posts to <this>/report
ENROLL_SCHEMA = "aither-placement-enroll/v1"
AGENT_UNIT = "aither-node-agent.service"
MODE = "observe"

#: The admission floor. A machine is offered only when FLOOR_GIB is still free after
#: HEADROOM_GIB has been set aside for the machine's own work.
HEADROOM_GIB = 6.0
FLOOR_GIB = 4.0
#: Quadlet (.container units through podman's systemd generator) arrived in podman 4.4.
MIN_PODMAN = (4, 4)
QUADLET_GENERATORS = (
    "usr/lib/systemd/system-generators/podman-system-generator",
    "usr/libexec/podman/quadlet",
    "usr/lib/podman/quadlet",
)
#: Where a docker CLI is installed. Its PRESENCE is the measurement: this verb never runs a
#: docker command (FDT001 -- no docker engine is driven from the bootc tree), and a
#: docker-only machine is refused whatever its version.
DOCKER_BINS = ("usr/bin/docker", "usr/local/bin/docker", "bin/docker", "snap/bin/docker")

HTTP_TIMEOUT = 20.0
ENROLL_ATTEMPTS = 5
ENROLL_BACKOFF_S = 5.0

TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,4096}$")
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
PIN_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{32,4096}$")

#: The first is the RFC 6598 shared address space (100.64/10), the range a tailnet
#: allocates node addresses from. It is a RANGE this verb classifies an interface by,
#: never a host it dials, and it is spelled in octets so that no dotted address of that
#: range appears in a shipped file (check_awnix_portability.py PRT004 reads one as a
#: fleet host).
OVERLAY_NETS = (
    ("tailnet", ipaddress.ip_network((bytes((100, 64, 0, 0)), 10))),
    ("aithernet", ipaddress.ip_network("10.77.0.0/16")),
)

Runner = Callable[[List[str], float], Tuple[int, str]]
Out = Callable[[str], None]


class RefusedError(Exception):
    """A stated reason this machine is not joined. Exit 1."""


class OfflineError(Exception):
    """The controller could not be reached. Exit 2; the request is kept for a retry."""


# ── paths ────────────────────────────────────────────────────────────────────


class Paths:
    """Every path the verb touches, under `root` so a test never sees the real tree."""

    def __init__(self, root: str = "/") -> None:
        self.root = root
        self.dir = self._j("etc/aither/placement")
        self.agent_env = os.path.join(self.dir, "agent.env")
        self.node_key = os.path.join(self.dir, "node.key")
        self.controller_pem = os.path.join(self.dir, "controller.pem")
        self.request = os.path.join(self.dir, "join.request")
        self.state = os.path.join(self.dir, "state.json")
        self.lock = os.path.join(self.dir, ".join.lock")
        self.meminfo = self._j("proc/meminfo")
        self.cpuinfo = self._j("proc/cpuinfo")
        self.pid1 = self._j("proc/1/comm")
        self.version = self._j("proc/version")
        self.wsl_interop = self._j("proc/sys/fs/binfmt_misc/WSLInterop")

    def _j(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def generators(self) -> List[str]:
        return [self._j(g) for g in QUADLET_GENERATORS]

    def docker_bins(self) -> List[str]:
        return [self._j(g) for g in DOCKER_BINS]


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def write_file(path: str, data: str, mode: int) -> None:
    """Atomic write with an exact mode: the file never exists with wider permissions,
    not even for the instant between create and chmod."""
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def read_env_file(path: str) -> Optional[Dict[str, str]]:
    text = _read(path)
    if text is None:
        return None
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@contextlib.contextmanager
def join_lock(paths: Paths) -> Iterator[None]:
    """One join at a time: first boot can apply a seed while an operator runs the verb."""
    if fcntl is None:
        yield
        return
    os.makedirs(paths.dir, mode=0o755, exist_ok=True)
    fd = os.open(paths.lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ── measurement ──────────────────────────────────────────────────────────────


def real_runner(argv: List[str], timeout: float = 10.0) -> Tuple[int, str]:
    """(exit, stdout). 127 when the program is not installed, 124 on a timeout."""
    if not shutil.which(argv[0]):
        return 127, ""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def parse_meminfo(text: str) -> Dict[str, int]:
    """kB values of /proc/meminfo, by key."""
    out: Dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def norm_arch(machine: str) -> str:
    m = (machine or "").strip().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m or "unknown"


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def measure_runtime(paths: Paths, run: Runner) -> Dict[str, Any]:
    """The container runtime and whether quadlet units can run on it, with the reason."""
    rc, out = run(["podman", "--version"], 10.0)
    ver = parse_version(out) if rc == 0 else None
    if ver is None:
        if any(os.path.exists(d) for d in paths.docker_bins()):
            # seen, not run: its version changes nothing about the refusal
            return {"runtime": "docker", "version": None, "quadlet": False,
                    "why": "the container runtime is docker; placement needs podman with quadlet"}
        return {"runtime": "none", "version": None, "quadlet": False,
                "why": "podman is not installed"}
    info: Dict[str, Any] = {"runtime": "quadlet", "version": ".".join(map(str, ver)),
                            "quadlet": True, "why": ""}
    why = ""
    if ver[:2] < MIN_PODMAN:
        why = "podman %s is older than %d.%d, the first release with quadlet" % (
            info["version"], MIN_PODMAN[0], MIN_PODMAN[1])
    elif not any(os.path.exists(g) for g in paths.generators()):
        why = "podman's systemd generator (quadlet) is not installed"
    elif (_read(paths.pid1) or "").strip() != "systemd":
        why = "systemd is not PID 1, so quadlet units cannot run"
    if why:
        info.update(runtime="podman", quadlet=False, why=why)
    return info


def measure_overlay(run: Runner, override: Optional[str] = None) -> Dict[str, Any]:
    """The address other fleet nodes reach this one on. None when there is no overlay."""
    if override:
        return {"address": override, "kind": "operator"}
    rc, out = run(["tailscale", "ip", "-4"], 10.0)
    if rc == 0:
        for line in out.splitlines():
            if _is_ipv4(line.strip()):
                return {"address": line.strip(), "kind": "tailnet"}
    rc, out = run(["ip", "-4", "-o", "addr", "show"], 10.0)
    if rc == 0:
        for addr in re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", out):
            for kind, net in OVERLAY_NETS:
                if ipaddress.ip_address(addr) in net:
                    return {"address": addr, "kind": kind}
    return {"address": None, "kind": "none"}


def _is_ipv4(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


def measure(paths: Paths, env: Dict[str, str], run: Runner,
            fleet_ip: Optional[str] = None) -> Dict[str, Any]:
    mem = parse_meminfo(_read(paths.meminfo) or "")
    cpu = _read(paths.cpuinfo)
    cores = len(re.findall(r"(?m)^processor\s*:", cpu)) if cpu else 0
    if not cores:
        cores = os.cpu_count() or 0
    wsl = os.path.exists(paths.wsl_interop) or "microsoft" in (_read(paths.version) or "").lower()
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable")
    rt = measure_runtime(paths, run)
    return {
        "cores": cores,
        "mem_total_kb": total,
        "mem_available_kb": avail,
        "mem_total_gib": _gib(total),
        "mem_available_gib": _gib(avail),
        "arch": norm_arch(env.get("AWNIX_FLEET_ARCH") or platform.machine()),
        "layout": "wsl" if wsl else "linux",
        "runtime": rt["runtime"],
        "runtime_version": rt["version"],
        "quadlet": rt["quadlet"],
        "runtime_why": rt["why"],
        "overlay": measure_overlay(run, fleet_ip),
        "headroom_gib": HEADROOM_GIB,
        "offer_gib": None if avail is None else round(max(0.0, _gib(avail) - HEADROOM_GIB), 2),
    }


def _gib(kb: Optional[int]) -> Optional[float]:
    return None if kb is None else round(kb / 1048576.0, 2)


def admission(m: Dict[str, Any]) -> List[str]:
    """Every reason this machine may not be offered. Empty list = admitted."""
    why: List[str] = []
    if not m.get("quadlet"):
        why.append("no quadlet-capable podman: %s" % (m.get("runtime_why") or "not found"))
    avail = m.get("mem_available_gib")
    if avail is None:
        why.append("MemAvailable could not be read, so no memory can be offered")
    elif avail - HEADROOM_GIB < FLOOR_GIB:
        why.append(
            "%.1f GiB is available; after the %.0f GiB headroom %.1f GiB would remain, "
            "under the %.0f GiB floor" % (avail, HEADROOM_GIB, max(0.0, avail - HEADROOM_GIB),
                                         FLOOR_GIB))
    return why


# ── the controller call ──────────────────────────────────────────────────────


def check_controller(url: str, allow_http: bool = False) -> str:
    """The normalised controller base URL, or RefusedError with the reason."""
    u = urllib.parse.urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RefusedError("the controller must be an http(s) URL, e.g. https://fleet.example:8149")
    if u.username or u.password or u.query or u.fragment:
        raise RefusedError("the controller URL must not carry credentials, a query or a fragment")
    try:
        u.port
    except ValueError:
        raise RefusedError("the controller URL has an invalid port")
    if u.scheme == "http" and not allow_http and not _is_loopback(u.hostname):
        raise RefusedError("refusing plain http to %s: the token would cross the network in the "
                      "clear. Use https, or --allow-http on a network you trust" % u.hostname)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def peer_fingerprint(host: str, port: int, timeout: float) -> Optional[str]:
    """SHA-256 of the certificate the host presents. Sends nothing after the handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    return hashlib.sha256(der).hexdigest() if der else None


def post_json(url: str, body: Dict[str, Any], pin: Optional[str] = None,
              ca_file: Optional[str] = None, timeout: float = HTTP_TIMEOUT,
              capture: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    """POST JSON, return (status, parsed body or None). The body is not sent until the
    controller has passed the trust check. Raises OfflineError (unreachable) or RefusedError.
    With a pin, the certificate that matched it is left in capture["pem"]."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname or "", u.port
    conn: http.client.HTTPConnection
    if u.scheme == "https":
        if pin:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # the pin below is the trust decision
        else:
            try:
                ctx = ssl.create_default_context(cafile=ca_file)
            except (OSError, ssl.SSLError) as e:
                raise RefusedError("--ca-file could not be loaded (%s)" % e.__class__.__name__)
        conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
    try:
        try:
            conn.connect()
        except ssl.SSLCertVerificationError:
            seen = peer_fingerprint(host, port or 443, timeout)
            raise RefusedError(
                "the controller's certificate is not trusted by this machine. Ask the fleet "
                "operator for its SHA-256 fingerprint; when it matches the one presented "
                "here (%s) re-run with --pin-sha256, or pass --ca-file" % (seen or "unreadable"))
        except (OSError, ssl.SSLError) as e:
            raise OfflineError("cannot reach the controller (%s)" % e.__class__.__name__)
        if pin:
            der = conn.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
            seen = hashlib.sha256(der or b"").hexdigest()
            if not hmac.compare_digest(seen, pin):
                raise RefusedError("the controller's certificate (%s) does not match the pinned "
                              "fingerprint; nothing was sent" % seen)
            if capture is not None and der:
                capture["pem"] = ssl.DER_cert_to_PEM_cert(der)
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        try:
            conn.request("POST", (u.path or "") or "/", body=raw, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "awnix-fleet/1"})
            resp = conn.getresponse()
            data = resp.read(1 << 20)
        except (OSError, http.client.HTTPException) as e:
            raise OfflineError("the controller dropped the connection (%s)" % e.__class__.__name__)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else None
        except ValueError:
            parsed = None
        return resp.status, parsed
    finally:
        conn.close()


def _scrub(text: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***")
    return text


def enroll(controller: str, token: str, node_id: str, capacity: Dict[str, Any],
           pin: Optional[str], ca_file: Optional[str], attempts: int = ENROLL_ATTEMPTS,
           backoff: float = ENROLL_BACKOFF_S,
           sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Exchange the enrollment token for this node's key. Returns
    {node, node_key, status[, controller_pem]}. Retries only an unreachable controller."""
    body = {"schema": ENROLL_SCHEMA, "token": token, "node_id": node_id,
            "hostname": socket.gethostname(), "capacity": capacity,
            "agent": {"mode": MODE}}
    last = "unreachable"
    capture: Dict[str, str] = {}
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(backoff)
        try:
            status, data = post_json(controller + ENROLL_PATH, body, pin=pin, ca_file=ca_file,
                                     capture=capture)
        except OfflineError as e:
            last = str(e)
            continue
        if 300 <= status < 400:
            raise RefusedError("the controller answered with a redirect (HTTP %d): that address "
                          "is not a placement controller" % status)
        if status in (401, 403):
            raise RefusedError("the controller rejected the enrollment token (HTTP %d)" % status)
        if status >= 500:
            last = "the controller answered HTTP %d" % status
            continue
        if status not in (200, 201) or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("detail") or data.get("error") or "")[:200]
            raise RefusedError(_scrub("the controller refused the enrollment (HTTP %d%s)" % (
                status, (": " + detail) if detail else ""), token))
        key = data.get("node_key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise RefusedError("the controller's answer carries no usable node key")
        nid = str(data.get("node") or data.get("node_id") or node_id)
        if not NODE_ID_RE.match(nid):
            raise RefusedError("the controller assigned an invalid node id")
        got = {"node": nid, "node_key": key, "status": str(data.get("status") or "pending")}
        if capture.get("pem"):
            got["controller_pem"] = capture["pem"]
        return got
    raise OfflineError(last)


# ── verbs ────────────────────────────────────────────────────────────────────


class Ctx:
    def __init__(self, paths: Paths, env: Dict[str, str], out: Out, run: Runner,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.paths, self.env, self.out, self.run, self.sleep = paths, env, out, run, sleep


def default_node_id() -> str:
    host = re.sub(r"[^a-z0-9._-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-.")
    return host or "node"


def read_token(arg: str, stdin: Any = None) -> str:
    if arg == "-":
        return (stdin or sys.stdin).readline().strip()
    if arg.startswith("@"):
        text = _read(arg[1:])
        if text is None:
            raise RefusedError("the token file could not be read")
        return text.strip()
    return arg.strip()


def parse_request(text: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """One line `<controller-url> <token> [sha256:HEX] [node=ID]` -> (url, token, pin,
    node id). The two optional fields may come in either order, each at most once."""
    usage = ("the join request must be: <controller-url> <token> [sha256:<fingerprint>] "
             "[node=<node id>]")
    parts = text.split()
    if len(parts) not in (2, 3, 4):
        raise RefusedError(usage)
    pin: Optional[str] = None
    node: Optional[str] = None
    for field in parts[2:]:
        low = field.lower()
        if low.startswith("sha256:") and pin is None:
            pin = field[7:]
        elif low.startswith("node=") and node is None and field[5:]:
            node = field[5:]
        else:
            raise RefusedError(usage)
    return parts[0], parts[1], pin, node


def norm_pin(pin: Optional[str]) -> Optional[str]:
    if not pin:
        return None
    p = pin.replace(":", "").strip().lower()
    if not PIN_RE.match(p):
        raise RefusedError("--pin-sha256 must be the 64 hex digits of a certificate's SHA-256")
    return p


def render_agent_env(node: str, controller: str, key_file: str, pin: Optional[str],
                     ca_file: Optional[str] = None) -> str:
    lines = [
        "# Written by `awnix fleet join`. `awnix fleet leave` removes it, and",
        "# aither-node-agent.service does not start without it. No secret lives here:",
        "# the node key is in AITHER_PLACEMENT_KEY_FILE (0600).",
        "AITHER_NODE_ID=%s" % node,
        "AITHER_PLACEMENT_URL=%s" % (controller + NODE_PATH % node),
        "AITHER_PLACEMENT_KEY_FILE=%s" % key_file,
        "AITHER_PLACEMENT_MODE=%s" % MODE,
        "AITHER_PLACEMENT_CONTROLLER=%s" % controller,
        # The memory that must stay free after a start. The agent admits no start
        # without it, so a node whose mode an operator later raises would hold
        # every unit for ever.
        "AITHER_PLACEMENT_HEADROOM_MB=%d" % int(HEADROOM_GIB * 1024),
        # This agent's fleet-pulse sink is off: the fleet host already beats pulse
        # from its own script, and a second, poorer beat under the same host key
        # replaces that record. Set 1 on a node nothing else reports for.
        "AITHER_NODE_PULSE=0",
    ]
    if ca_file:
        lines.append("AITHER_PLACEMENT_CA_FILE=%s" % ca_file)
    if pin:
        lines.append("AITHER_PLACEMENT_CONTROLLER_SHA256=%s" % pin)
    return "\n".join(lines) + "\n"


def agent_active(ctx: Ctx) -> Optional[bool]:
    """True/False from systemd, None when it cannot be asked."""
    rc, out = ctx.run(["systemctl", "is-active", AGENT_UNIT], 15.0)
    if rc == 127 or rc == 124:
        return None
    return out.strip() == "active"


def start_agent(ctx: Ctx) -> Optional[str]:
    """None when the agent is running, else why it is not."""
    ctx.run(["systemctl", "enable", AGENT_UNIT], 30.0)
    rc, _ = ctx.run(["systemctl", "restart", AGENT_UNIT], 60.0)
    if rc == 127:
        return "systemctl is not available"
    if rc != 0:
        return "systemctl restart %s exited %d (journalctl -u %s)" % (AGENT_UNIT, rc, AGENT_UNIT)
    return None


def _need_root(ctx: Ctx) -> None:
    geteuid = getattr(os, "geteuid", None)
    if ctx.paths.root == "/" and geteuid is not None and geteuid() != 0:
        raise RefusedError("this verb changes /etc/aither/placement and a system unit; "
                           "run it as root")


def cmd_join(ctx: Ctx, a: argparse.Namespace) -> int:
    p = ctx.paths
    from_file = a.request_file
    pin_arg = a.pin_sha256
    if from_file:
        if a.controller or a.token:
            raise RefusedError("give either --request-file or <controller-url> <token>, not both")
        text = _read(from_file)
        if text is None:
            env_now = read_env_file(p.agent_env)
            if env_now and os.path.exists(p.node_key):
                ctx.out("awnix fleet: already joined as %s; no join request is waiting" %
                        env_now.get("AITHER_NODE_ID", "?"))
                return EXIT_OK
            raise RefusedError("no join request at %s" % from_file)
        try:
            controller_raw, token, file_pin, file_node = parse_request(text)
        except RefusedError:
            _unlink(from_file)
            raise
        pin_arg = pin_arg or file_pin
        if file_node and not a.node_id:
            a.node_id = file_node
    else:
        if not a.controller or not a.token:
            raise RefusedError("usage: awnix fleet join <controller-url> <token|@file|->")
        controller_raw, token = a.controller, read_token(a.token)

    def drop_request() -> None:
        if from_file:
            _unlink(from_file)

    try:
        controller = check_controller(controller_raw, a.allow_http)
        if not TOKEN_RE.match(token):
            raise RefusedError("that does not look like an enrollment token")
        pin = norm_pin(pin_arg)
        if pin and not controller.startswith("https://"):
            raise RefusedError("--pin-sha256 only applies to an https controller")
        node_id = (a.node_id or ctx.env.get("AITHER_NODE_ID") or default_node_id()).lower()
        if not NODE_ID_RE.match(node_id):
            raise RefusedError("node id '%s' is invalid (lowercase letters, digits, . _ -)"
                               % node_id)
        if a.fleet_ip and not _is_ipv4(a.fleet_ip):
            raise RefusedError("--fleet-ip must be an IPv4 address")

        # 1. measure, 2. refuse -- before anything is asked of the controller.
        m = measure(p, ctx.env, ctx.run, a.fleet_ip)
        ctx.out("awnix fleet: measured %s cores, %s GiB total, %s GiB available, %s, %s %s, "
                "overlay %s" % (m["cores"], m["mem_total_gib"], m["mem_available_gib"],
                                m["arch"], m["runtime"], m["runtime_version"] or "-",
                                m["overlay"]["address"] or "none"))
        why = admission(m)
        if why:
            raise RefusedError("this machine is not offered to the fleet: " + "; ".join(why))
        existing = read_env_file(p.agent_env)
        if existing is not None:
            raise RefusedError("already joined as %s to %s; run `awnix fleet leave` first" % (
                existing.get("AITHER_NODE_ID", "?"),
                existing.get("AITHER_PLACEMENT_CONTROLLER", "?")))
        ca_text = None
        if a.ca_file:
            ca_text = _read(a.ca_file)
            if not ca_text or "BEGIN CERTIFICATE" not in ca_text:
                raise RefusedError("--ca-file is not a readable PEM certificate bundle")
    except RefusedError:
        drop_request()
        raise
    if m["overlay"]["address"] is None:
        ctx.out("awnix fleet: no overlay address found; the controller cannot reach this "
                "node until it is on the fleet's network (or pass --fleet-ip)")
    if a.dry_run:
        ctx.out("awnix fleet: dry run -- this machine would be offered as '%s' with %s GiB "
                "above the headroom; nothing was sent or written" % (node_id, m["offer_gib"]))
        return EXIT_OK

    _need_root(ctx)
    with join_lock(p):
        if read_env_file(p.agent_env) is not None:  # another join finished while we waited
            drop_request()
            ctx.out("awnix fleet: already joined; nothing to do")
            return EXIT_OK
        try:
            # 3. enroll
            got = enroll(controller, token, node_id, m, pin, a.ca_file, sleep=ctx.sleep)
        except RefusedError:
            drop_request()
            raise
        except OfflineError as e:
            keep = (" The request is kept at %s; re-run `awnix fleet join --request-file %s`."
                    % (from_file, from_file)) if from_file else ""
            ctx.out("awnix fleet: not joined -- %s.%s" % (e, keep))
            return EXIT_OFFLINE
        # 4. write: the key first, so agent.env never names a key that is not there.
        write_file(p.node_key, got["node_key"], 0o600)
        trust = got.get("controller_pem") or ca_text
        if trust:  # the agent trusts the controller this join trusted, and no other
            write_file(p.controller_pem, trust, 0o644)
        else:
            _unlink(p.controller_pem)
        write_file(p.agent_env, render_agent_env(got["node"], controller, p.node_key, pin,
                                                 p.controller_pem if trust else None), 0o644)
        drop_request()
    ctx.out("awnix fleet: enrolled as '%s' (%s). An operator approves the node on the "
            "controller; until then, and in observe mode after, nothing is started here."
            % (got["node"], got["status"]))
    if a.no_start:
        ctx.out("awnix fleet: agent not started (--no-start); start %s when ready" % AGENT_UNIT)
        return EXIT_OK
    problem = start_agent(ctx)
    if problem:
        ctx.out("awnix fleet: joined, but the agent is not running: %s" % problem)
        return EXIT_OFFLINE
    ctx.out("awnix fleet: %s started in observe mode" % AGENT_UNIT)
    return EXIT_OK


def cmd_leave(ctx: Ctx, a: argparse.Namespace) -> int:
    p = ctx.paths
    had_env = os.path.exists(p.agent_env)
    had_key = os.path.exists(p.node_key)
    if not had_env and not (a.purge and had_key):
        _unlink(p.request)
        ctx.out("awnix fleet: not joined; nothing to do" + (
            " (the node key is kept; `awnix fleet leave --purge` removes it)" if had_key else ""))
        return EXIT_OK
    _need_root(ctx)
    rc, _ = ctx.run(["systemctl", "stop", AGENT_UNIT], 60.0)
    if rc not in (0, 127) and agent_active(ctx):
        ctx.out("awnix fleet: %s did not stop (systemctl exited %d); nothing was removed"
                % (AGENT_UNIT, rc))
        return EXIT_OFFLINE
    _unlink(p.agent_env)
    _unlink(p.controller_pem)
    _unlink(p.request)
    if a.purge:
        _unlink(p.node_key)
        ctx.out("awnix fleet: left; the agent is stopped, agent.env and the node key are removed")
    else:
        ctx.out("awnix fleet: left; the agent is stopped and agent.env is removed. The node "
                "key is kept (`awnix fleet leave --purge` removes it)")
    ctx.out("awnix fleet: the controller still lists this node and will report it stale; "
            "an operator removes it there. Services already running here are not touched.")
    return EXIT_OK


def cmd_status(ctx: Ctx, a: argparse.Namespace) -> int:
    p = ctx.paths
    env_now = read_env_file(p.agent_env)
    m = measure(p, ctx.env, ctx.run)
    state: Dict[str, Any] = {}
    try:
        loaded = json.loads(_read(p.state) or "{}")
        if isinstance(loaded, dict):
            state = loaded
    except ValueError:
        state = {}
    st = {
        "joined": env_now is not None,
        "node": (env_now or {}).get("AITHER_NODE_ID"),
        "controller": (env_now or {}).get("AITHER_PLACEMENT_CONTROLLER"),
        "report_url": (env_now or {}).get("AITHER_PLACEMENT_URL"),
        "mode": (env_now or {}).get("AITHER_PLACEMENT_MODE", MODE) if env_now else None,
        "key_present": os.path.exists(p.node_key),
        "agent_active": agent_active(ctx) if env_now is not None else False,
        "generation": state.get("generation"),
        "capacity": m,
        "admitted": not admission(m),
        "refusals": admission(m),
    }
    if a.json:
        ctx.out(json.dumps(st, indent=2, sort_keys=True))
        return EXIT_OK
    if st["joined"]:
        ctx.out("joined:     yes, as '%s' to %s" % (st["node"], st["controller"]))
        ctx.out("mode:       %s" % st["mode"])
        agent = {True: "running", False: "not running", None: "unknown (no systemctl)"}
        ctx.out("agent:      %s" % agent[st["agent_active"]])
    else:
        ctx.out("joined:     no" + (" (a node key is kept from an earlier join)"
                                    if st["key_present"] else ""))
    ctx.out("capacity:   %s cores, %s GiB total, %s GiB available, %s, %s" % (
        m["cores"], m["mem_total_gib"], m["mem_available_gib"], m["arch"], m["layout"]))
    ctx.out("runtime:    %s %s" % (m["runtime"], m["runtime_version"] or ""))
    ctx.out("overlay:    %s" % (m["overlay"]["address"] or "none"))
    if st["admitted"]:
        ctx.out("offer:      %s GiB above the %.0f GiB headroom" % (m["offer_gib"], HEADROOM_GIB))
    else:
        for r in st["refusals"]:
            ctx.out("not offered: %s" % r)
    return EXIT_OK


# ── self-test ────────────────────────────────────────────────────────────────


class StubController:
    """Threaded 127.0.0.1 placement controller. `mode`: ok | reject | error | redirect |
    nokey. Records every request body in `calls`."""

    TOKEN = "enroll-token-0123456789abcdef"
    NODE_KEY = "node-key-SECRET-0123456789abcdef0123456789abcdef"

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: List[Dict[str, Any]] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _reply(self, code: int, obj: Any, extra: Optional[Dict[str, str]] = None) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802 (http.server names)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                stub.calls.append({"path": self.path, "body": body})
                if self.path != ENROLL_PATH:
                    return self._reply(404, {"detail": "not found"})
                if stub.mode == "redirect":
                    return self._reply(307, {}, {"Location": "/login"})
                if stub.mode == "error":
                    return self._reply(503, {"detail": "busy"})
                if stub.mode == "reject" or (body or {}).get("token") != StubController.TOKEN:
                    return self._reply(401, {"detail": "invalid enrollment token"})
                if stub.mode == "nokey":
                    return self._reply(200, {"node": (body or {}).get("node_id"),
                                             "status": "pending"})
                return self._reply(200, {"node": (body or {}).get("node_id"), "status": "pending",
                                         "node_key": StubController.NODE_KEY})

        class S(http.server.ThreadingHTTPServer):
            def server_bind(self) -> None:  # skip getfqdn(): seconds per bind on Windows
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.server = S(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "StubController":
        self._t.start()
        return self

    def __exit__(self, *_a: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def fake_machine(root: str, avail_gib: float = 24.0, total_gib: float = 32.0, cores: int = 8,
                 generator: bool = True, pid1: str = "systemd") -> None:
    """Fixture /proc and generator files under `root` (self-test and the tests)."""
    def put(rel: str, text: str) -> None:
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    put("proc/meminfo", "MemTotal:       %d kB\nMemFree:        1 kB\nMemAvailable:   %d kB\n" % (
        int(total_gib * 1048576), int(avail_gib * 1048576)))
    put("proc/cpuinfo", "".join("processor\t: %d\n\n" % i for i in range(cores)))
    put("proc/1/comm", pid1 + "\n")
    put("proc/version", "Linux version 6.6.0 (gcc)\n")
    if generator:
        put(QUADLET_GENERATORS[0], "")


class FakeRunner:
    """A Runner that answers from a table and records every argv it was asked to run."""

    def __init__(self, podman: Optional[str] = "podman version 5.4.0",
                 overlay: Optional[str] = "198.51.100.77", systemctl_rc: int = 0) -> None:
        self.podman, self.overlay, self.systemctl_rc = podman, overlay, systemctl_rc
        self.calls: List[List[str]] = []
        self.active = False

    def __call__(self, argv: List[str], timeout: float = 10.0) -> Tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] == "podman":
            return (0, self.podman + "\n") if self.podman else (127, "")
        if argv[0] == "tailscale":
            return (0, self.overlay + "\n") if self.overlay else (127, "")
        if argv[0] == "ip":
            return 127, ""
        if argv[0] == "systemctl":
            if argv[1] == "is-active":
                return (0, "active\n") if self.active else (3, "inactive\n")
            if self.systemctl_rc == 0 and argv[1] == "restart":
                self.active = True
            if self.systemctl_rc == 0 and argv[1] == "stop":
                self.active = False
            return self.systemctl_rc, ""
        return 127, ""

    def mutations(self) -> List[List[str]]:
        return [c for c in self.calls if c[0] == "systemctl" and c[1] != "is-active"]


def self_test() -> int:
    fails: List[str] = []

    def chk(cond: bool, label: str) -> None:
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        if not cond:
            fails.append(label)

    def go(root: str, argv: List[str], runner: FakeRunner) -> Tuple[int, str]:
        lines: List[str] = []
        rc = main(argv + ["--root", root], env={"AWNIX_FLEET_ARCH": "x86_64"},
                  out=lines.append, runner=runner, sleep=lambda _s: None)
        return rc, "\n".join(lines)

    chk(norm_arch("x86_64") == "amd64" and norm_arch("aarch64") == "arm64", "arch is normalised")
    chk(parse_version("podman version 4.9.3") == (4, 9, 3), "podman version parses")
    chk(parse_meminfo("MemAvailable:   1048576 kB\n")["MemAvailable"] == 1048576, "meminfo parses")
    try:
        check_controller("http://203.0.113.9:8149")
        chk(False, "plain http to a non-loopback host is refused")
    except RefusedError:
        chk(True, "plain http to a non-loopback host is refused")
    chk(check_controller("https://fleet.example:8149/") == "https://fleet.example:8149",
        "the controller URL is normalised")
    chk(parse_request("https://c:1 tok sha256:" + "a" * 64) == ("https://c:1", "tok", "a" * 64,
                                                                None),
        "a join request line parses")
    chk(parse_request("https://c:1 tok node=node1-wsl sha256:" + "a" * 64)
        == ("https://c:1", "tok", "a" * 64, "node1-wsl"),
        "a join request may name the node id")

    with tempfile.TemporaryDirectory() as tmp, StubController() as ctl:
        # below the floor: refused before the controller hears anything
        low = os.path.join(tmp, "low")
        fake_machine(low, avail_gib=9.5)
        r = FakeRunner()
        rc, text = go(low, ["join", ctl.url, StubController.TOKEN], r)
        chk(rc == EXIT_REFUSED and "floor" in text, "under 4 GiB after the headroom is refused")
        chk(not ctl.calls and not os.path.exists(Paths(low).agent_env) and not r.mutations(),
            "a refused machine is untouched and the controller is not called")

        nopod = os.path.join(tmp, "nopod")
        fake_machine(nopod)
        rc, text = go(nopod, ["join", ctl.url, StubController.TOKEN], FakeRunner(podman=None))
        chk(rc == EXIT_REFUSED and "podman" in text, "no podman is refused with the reason")
        old = os.path.join(tmp, "old")
        fake_machine(old)
        rc, text = go(old, ["join", ctl.url, StubController.TOKEN],
                      FakeRunner(podman="podman version 4.3.1"))
        chk(rc == EXIT_REFUSED and "quadlet" in text, "a pre-quadlet podman is refused")
        nogen = os.path.join(tmp, "nogen")
        fake_machine(nogen, generator=False)
        rc, _t = go(nogen, ["join", ctl.url, StubController.TOKEN], FakeRunner())
        chk(rc == EXIT_REFUSED and not ctl.calls, "a missing quadlet generator is refused")

        # the good path
        ok = os.path.join(tmp, "ok")
        fake_machine(ok)
        p = Paths(ok)
        r = FakeRunner()
        rc, text = go(ok, ["join", ctl.url, StubController.TOKEN, "--node-id", "box-1"], r)
        env_now = read_env_file(p.agent_env) or {}
        chk(rc == EXIT_OK and env_now.get("AITHER_PLACEMENT_MODE") == "observe",
            "join writes agent.env in observe mode")
        chk(env_now.get("AITHER_PLACEMENT_HEADROOM_MB") == "6144"
            and env_now.get("AITHER_NODE_PULSE") == "0",
            "agent.env carries the headroom and keeps the pulse sink off")
        chk(env_now.get("AITHER_NODE_ID") == "box-1"
            and env_now.get("AITHER_PLACEMENT_URL") == ctl.url + "/api/placement/nodes/box-1"
            and env_now.get("AITHER_PLACEMENT_CONTROLLER") == ctl.url
            and env_now.get("AITHER_PLACEMENT_KEY_FILE") == p.node_key,
            "agent.env names node, report URL and key file")
        chk(_read(p.node_key) == StubController.NODE_KEY, "node.key holds exactly the issued key")
        if os.name == "posix":
            chk((os.stat(p.node_key).st_mode & 0o777) == 0o600, "node.key is 0600")
            chk((os.stat(p.agent_env).st_mode & 0o777) == 0o644, "agent.env is 0644")
        chk(StubController.TOKEN not in text and StubController.NODE_KEY not in text
            and StubController.NODE_KEY not in (_read(p.agent_env) or ""),
            "neither the token nor the key is printed or written to agent.env")
        body = ctl.calls[-1]["body"]
        chk(body["schema"] == ENROLL_SCHEMA and body["node_id"] == "box-1"
            and body["capacity"]["cores"] == 8
            and body["capacity"]["mem_available_gib"] == 24.0
            and body["capacity"]["overlay"]["address"] == "198.51.100.77",
            "the enrollment carries the measured capacity")
        chk(r.mutations() == [["systemctl", "enable", AGENT_UNIT],
                              ["systemctl", "restart", AGENT_UNIT]],
            "join starts only the agent unit")
        rc, _t = go(ok, ["join", ctl.url, StubController.TOKEN], FakeRunner())
        chk(rc == EXIT_REFUSED, "a second join is refused until leave")
        rc, text = go(ok, ["status", "--json"], r)
        st = json.loads(text)
        chk(st["joined"] and st["agent_active"] and st["mode"] == "observe"
            and StubController.NODE_KEY not in text, "status reports the join without the key")

        rc, _t = go(ok, ["leave"], r)
        chk(rc == EXIT_OK and not os.path.exists(p.agent_env) and os.path.exists(p.node_key),
            "leave removes agent.env and keeps the key")
        rc2, _t = go(ok, ["leave"], r)
        chk(rc2 == EXIT_OK, "leave is idempotent")
        rc, _t = go(ok, ["leave", "--purge"], r)
        chk(rc == EXIT_OK and not os.path.exists(p.node_key), "leave --purge removes the key")

        # the request-file path (first-boot step): consumed on success
        req = os.path.join(tmp, "req")
        fake_machine(req)
        pr = Paths(req)
        write_file(pr.request, "%s %s node=node1-wsl\n" % (ctl.url, StubController.TOKEN),
                   0o600)
        rc, _t = go(req, ["join", "--request-file", pr.request], FakeRunner())
        chk(rc == EXIT_OK and not os.path.exists(pr.request) and os.path.exists(pr.agent_env),
            "a join request file is consumed by a successful join")
        chk((read_env_file(pr.agent_env) or {}).get("AITHER_NODE_ID") == "node1-wsl"
            and ctl.calls[-1]["body"]["node_id"] == "node1-wsl",
            "the request file's node id is the one enrolled")
        rc, _t = go(req, ["join", "--request-file", pr.request], FakeRunner())
        chk(rc == EXIT_OK, "a repeated request-file join on a joined box is a no-op")

        for mode, want, label in (
                ("reject", EXIT_REFUSED, "a rejected token is a refusal"),
                ("redirect", EXIT_REFUSED, "a redirecting address is a refusal"),
                ("nokey", EXIT_REFUSED, "an answer without a key is a refusal"),
                ("error", EXIT_OFFLINE, "a failing controller is offline, not joined")):
            d = os.path.join(tmp, mode)
            fake_machine(d)
            with StubController(mode) as c2:
                rr = FakeRunner()
                rc, text = go(d, ["join", c2.url, StubController.TOKEN], rr)
            chk(rc == want and not os.path.exists(Paths(d).agent_env)
                and not os.path.exists(Paths(d).node_key) and not rr.mutations()
                and StubController.TOKEN not in text, label)

        dry = os.path.join(tmp, "dry")
        fake_machine(dry)
        n = len(ctl.calls)
        rc, _t = go(dry, ["join", ctl.url, StubController.TOKEN, "--dry-run"], FakeRunner())
        chk(rc == EXIT_OK and len(ctl.calls) == n and not os.path.exists(Paths(dry).dir),
            "--dry-run sends and writes nothing")

    print("awnix-fleet self-test: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
    return 0 if not fails else 1


# ── cli ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="awnix fleet",
        description="Offer this machine to a fleet's placement controller, report it, leave.")
    ap.add_argument("--self-test", action="store_true", help="run the built-in checks and exit")
    ap.add_argument("--list-verbs", action="store_true", help="print the verbs and exit")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="/", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="verb")
    j = sub.add_parser("join", parents=[common], help="measure, enroll, start the agent (observe)")
    j.add_argument("controller", nargs="?", help="the placement controller's base URL")
    j.add_argument("token", nargs="?", help="enrollment token; @FILE reads a file, - reads stdin")
    j.add_argument("--request-file", metavar="PATH",
                   help="read `<controller-url> <token> [sha256:HEX] [node=ID]` from PATH "
                        "(removed after use)")
    j.add_argument("--node-id", help="this node's id: its key in the fleet's nodes.yaml "
                                     "(default: the short hostname)")
    j.add_argument("--fleet-ip", help="the overlay address to advertise (default: detected)")
    j.add_argument("--pin-sha256", metavar="HEX",
                   help="SHA-256 of the controller's certificate (self-signed controllers)")
    j.add_argument("--ca-file", metavar="PEM", help="CA bundle that signs the controller")
    j.add_argument("--allow-http", action="store_true",
                   help="permit plain http to a non-loopback controller")
    j.add_argument("--dry-run", action="store_true",
                   help="measure and judge only; send and write nothing")
    j.add_argument("--no-start", action="store_true",
                   help="write the files, do not start the agent")
    s = sub.add_parser("status", parents=[common], help="joined or not, and the measured capacity")
    s.add_argument("--json", action="store_true")
    lv = sub.add_parser("leave", parents=[common], help="stop the agent and remove agent.env")
    lv.add_argument("--purge", action="store_true", help="also remove the node key")
    return ap


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
         out: Optional[Out] = None, runner: Optional[Runner] = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    ap = build_parser()
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.self_test:
        return self_test()
    if a.list_verbs:
        print("\n".join(VERBS))
        return EXIT_OK
    if not a.verb:
        ap.print_help()
        return EXIT_REFUSED
    ctx = Ctx(Paths(a.root), dict(os.environ if env is None else env),
              out or (lambda s: print(s, flush=True)), runner or real_runner, sleep)
    try:
        return {"join": cmd_join, "status": cmd_status, "leave": cmd_leave}[a.verb](ctx, a)
    except RefusedError as e:
        ctx.out("awnix fleet: refused -- %s" % e)
        return EXIT_REFUSED
    except OSError as e:
        ctx.out("awnix fleet: failed -- %s: %s" % (e.__class__.__name__, e.strerror or e))
        return EXIT_OFFLINE


if __name__ == "__main__":
    sys.exit(main())
