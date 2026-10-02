#!/usr/bin/python3.11
"""awnix-mesh -- join this box to a tenant's AitherMesh, report it, leave it.

Installed as /usr/libexec/awnix/awnix-mesh, so `awnix mesh ...` runs it through the
dispatcher. Stdlib only and Python 3.10 compatible, like every awnix CLI.

    awnix mesh join <token|@file|-> [--portal URL] [--node-name N] [--role worker|edge]
                                    [--cafile PEM] [--dry-run]
    awnix mesh status [--json]
    awnix mesh leave
    awnix mesh retry [--quiet]
    awnix mesh capabilities --json
    awnix-mesh --self-test | --list-verbs

It reuses the tenant join path the portal's "Connect a device" flow already serves:

  1. POST {portal}/v1/workspace/api-keys/enrollment-token/exchange {enrollment_token}
     -> a 30-day capability token (endpoint:mesh), kept in /etc/awnix/mesh/credential.
  2. POST {portal}/aithernet/nodes/join {hostname, wg_public_key, external_ip: null, ...}
     -> this node's overlay IP and the tenant's peers (the server filters to the tenant
     named by the token, never by the payload).
  3. POST {portal}/compute/nodes/register -> GPU/CPU/arch capabilities in the fabric.

The overlay is a NetworkManager WireGuard keyfile, so wireguard-tools is not needed. The
node is CLIENT-ONLY: no listen-port is written (NM picks an ephemeral UDP source port),
it has no endpoint of its own, and the connection sits in its own firewalld zone
`awnix-mesh` (target DROP). That is deliberately NOT the host zone `awnix`, which the
zero-ports profile makes the default and an admin may open LAN ports in. Nothing inbound
is opened; the peers learn its address from its own keepalives.

Offline first: a join with no network records /var/lib/awnix/mesh/pending.json (0600)
and exits 2. awnix-mesh.path fires on that file, and awnix-mesh.timer retries, so the join
finishes when the network appears. Under the air-gap profile the join is refused (exit 1)
unless the portal resolves inside the air-gap allowed_subnets -- a private enclave
conductor, never the public control plane. When the air gap comes on (or the node is
revoked) AFTER a join, the next retry tears the overlay down: the NM keyfile is removed
and the connection taken down, so no keepalive leaves the box. Under the air gap no
https_proxy from the environment is used either.

Exit: 0 joined (or nothing to do), 1 refused/invalid, 2 offline/pending/could-not-judge.
The status file never contains a token; stdout never prints one.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import http.server
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

VERBS = ("join", "status", "leave", "retry", "capabilities")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PENDING = 2

# The one vendor default. Every other source of the portal is the endpoints chain
# (contract endpoints-env): /usr/lib/awnix/endpoints.env < /etc/awnix/endpoints.env <
# process env, key AWNIX_MESH_PORTAL. An EMPTY value there switches the mesh off.
PORTAL_KEY = "AWNIX_MESH_PORTAL"
DEFAULT_PORTAL = "https://api.aitherium.com"
VENDOR_ENDPOINTS = "usr/lib/awnix/endpoints.env"
ADMIN_ENDPOINTS = "etc/awnix/endpoints.env"

EXCHANGE_PATH = "/v1/workspace/api-keys/enrollment-token/exchange"
RENEW_PATH = "/v1/workspace/api-keys/endpoint-token/renew"
JOIN_PATH = "/aithernet/nodes/join"
REGISTER_PATH = "/compute/nodes/register"
NODE_PATH = "/compute/nodes/{node_id}"
HEARTBEAT_PATH = "/compute/nodes/{node_id}/heartbeat"

IFACE = "aithernet0"
ZONE = "awnix-mesh"
KEEPALIVE = 25
OVERLAY_NET = ipaddress.ip_network("10.77.0.0/16")
RENEW_WITHIN_S = 7 * 86400
HTTP_TIMEOUT = 20.0

TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{16,4096}$")
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ROLES = ("worker", "edge")

# Every key the status file may hold. None of them is a credential; AMS003 reads this
# tuple and fails if a token/secret/key-shaped name is ever added.
STATUS_KEYS = (
    "schema", "state", "node_id", "compute_node_id", "overlay_ip", "overlay_cidr",
    "portal", "tenant_id", "iface", "peers", "registered", "capabilities", "node_type",
    "expires_at", "heartbeat_at", "checked_at", "detail",
)
STATES = ("unjoined", "pending", "joined", "refused", "offline", "left", "error")
STATE_EXIT = {
    "unjoined": EXIT_OK, "left": EXIT_OK, "joined": EXIT_OK,
    "refused": EXIT_REFUSED, "error": EXIT_REFUSED,
    "pending": EXIT_PENDING, "offline": EXIT_PENDING,
}


# ── paths ───────────────────────────────────────────────────────────────────


class Paths:
    """Every file this CLI touches, under a root ('/' on a box, a tempdir in tests)."""

    def __init__(self, root: str = "/") -> None:
        self.root = root

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    @property
    def live(self) -> bool:
        return os.path.abspath(self.root) == os.path.abspath(os.sep)

    @property
    def state_dir(self) -> str:
        return self.p("var/lib/awnix/mesh")

    @property
    def etc_dir(self) -> str:
        return self.p("etc/awnix/mesh")

    @property
    def status(self) -> str:
        return os.path.join(self.state_dir, "status.json")

    @property
    def pending(self) -> str:
        return os.path.join(self.state_dir, "pending.json")

    @property
    def credential(self) -> str:
        return os.path.join(self.etc_dir, "credential")

    @property
    def wg_key(self) -> str:
        return os.path.join(self.etc_dir, "wg.key")

    @property
    def keyfile(self) -> str:
        return self.p(f"etc/NetworkManager/system-connections/{IFACE}.nmconnection")

    @property
    def release_env(self) -> str:
        return self.p("usr/lib/awnix/release.env")

    @property
    def profile_marker(self) -> str:
        return self.p("usr/lib/awnix/profile")

    @property
    def air_gap_yaml(self) -> str:
        return self.p("etc/aither/air_gap.yaml")


# ── small file helpers ──────────────────────────────────────────────────────


def _write_private(path: str, data: str) -> None:
    """Atomic 0600 write. The file never exists world-readable, not even mid-write."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def read_env_file(path: str) -> Optional[Dict[str, str]]:
    """Plain KEY=VALUE, no expansion (contract endpoints-env). None when absent."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── status ──────────────────────────────────────────────────────────────────


def load_status(paths: Paths) -> Dict[str, Any]:
    st = _read_json(paths.status) or {}
    base = {k: None for k in STATUS_KEYS}
    base.update({"schema": 1, "state": "unjoined", "iface": IFACE, "peers": [],
                 "registered": False})
    for k in STATUS_KEYS:
        if k in st:
            base[k] = st[k]
    if base["state"] not in STATES:
        base["state"] = "error"
    return base


def save_status(paths: Paths, st: Dict[str, Any]) -> Dict[str, Any]:
    clean = {k: st.get(k) for k in STATUS_KEYS}
    clean["schema"] = 1
    clean["checked_at"] = now_iso()
    _write_private(paths.status, json.dumps(clean, indent=2, sort_keys=True) + "\n")
    try:  # status is readable by the console; it holds nothing secret (AMS003).
        os.chmod(paths.status, 0o644)
    except OSError:
        pass
    return clean


# ── WireGuard keys: RFC 7748 X25519, pure stdlib ────────────────────────────

_P = 2 ** 255 - 19
_A24 = 121665


def _clamp(k: bytes) -> bytes:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return bytes(b)


def x25519(scalar: bytes, u: bytes) -> bytes:
    """Montgomery ladder, RFC 7748 section 5. Same math as `wg pubkey`."""
    k = int.from_bytes(_clamp(scalar), "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def wg_public(private_b64: str) -> str:
    priv = base64.b64decode(private_b64)
    if len(priv) != 32:
        raise ValueError("wg private key is not 32 bytes")
    return base64.b64encode(x25519(priv, (9).to_bytes(32, "little"))).decode()


def ensure_wg_key(paths: Paths) -> Tuple[str, str]:
    """(private_b64, public_b64); generated once, 0600, persisted in /etc across upgrades."""
    try:
        with open(paths.wg_key, encoding="utf-8") as fh:
            priv = fh.read().strip()
        return priv, wg_public(priv)
    except (OSError, ValueError):
        pass
    priv = base64.b64encode(_clamp(secrets.token_bytes(32))).decode()
    _write_private(paths.wg_key, priv + "\n")
    return priv, wg_public(priv)


# ── endpoints + air gap ─────────────────────────────────────────────────────


def resolve_portal(paths: Paths, env: Dict[str, str], flag: Optional[str]) -> Tuple[str, str]:
    """(portal, source). An explicit empty value anywhere in the chain means 'off'."""
    if flag:
        return flag.rstrip("/"), "flag"
    value, source = None, "default"
    for rel, src in ((VENDOR_ENDPOINTS, "vendor-env"), (ADMIN_ENDPOINTS, "admin-env")):
        data = read_env_file(paths.p(rel))
        if data is not None and PORTAL_KEY in data:
            value, source = data[PORTAL_KEY], src
    if PORTAL_KEY in env:
        value, source = env[PORTAL_KEY], "process-env"
    if value is None:
        return DEFAULT_PORTAL, "default"
    return value.strip().rstrip("/"), source


def _yaml_lite_air_gap(text: str) -> Tuple[Optional[bool], List[str]]:
    enabled: Optional[bool] = None
    subnets: List[str] = []
    in_subnets = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_subnets = s.startswith("allowed_subnets:")
            if s.startswith("enabled:"):
                enabled = s.split(":", 1)[1].strip().lower() in ("true", "yes", "1", "on")
            if in_subnets and "[" in s:
                inner = s.split("[", 1)[1].rsplit("]", 1)[0]
                subnets += [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
                in_subnets = False
            continue
        if in_subnets and s.startswith("-"):
            subnets.append(s[1:].strip().strip("\"'"))
    return enabled, subnets


def air_gap_policy(paths: Paths, env: Dict[str, str]) -> Dict[str, Any]:
    """{enabled, subnets, source}. Loopback is always allowed; nothing else is implied."""
    cfg = env.get("AITHER_AIR_GAP_CONFIG") or paths.air_gap_yaml
    enabled: Optional[bool] = None
    subnets: List[str] = []
    source = "none"
    try:
        with open(cfg, encoding="utf-8") as fh:
            enabled, subnets = _yaml_lite_air_gap(fh.read())
        source = cfg
    except OSError:
        pass
    mode = env.get("AITHER_AIR_GAP", "").strip().lower()
    if mode in ("strict", "audit", "1", "true", "on"):
        enabled, source = True, "AITHER_AIR_GAP"
    elif mode in ("0", "false", "off") and enabled:
        enabled, source = False, "AITHER_AIR_GAP"
    # The image profile. ANY of these saying airgap wins and the environment cannot
    # switch it off (fail closed, the same sources garg-tls-proxy reads):
    #   /usr/lib/awnix/profile   written by `awnix-zero-ports apply --profile airgap`
    #   awnix.profile=airgap     on the kernel command line
    #   AWNIX_PROFILE=airgap     in the environment
    if env.get("AWNIX_PROFILE", "").strip().lower() == "airgap":
        enabled, source = True, "AWNIX_PROFILE"
    try:
        with open(paths.p("proc/cmdline"), encoding="utf-8") as fh:
            if "awnix.profile=airgap" in fh.read().split():
                enabled, source = True, "/proc/cmdline"
    except OSError:
        pass
    try:
        with open(paths.profile_marker, encoding="utf-8") as fh:
            if "airgap" in fh.read():
                enabled, source = True, paths.profile_marker
    except FileNotFoundError:
        pass  # no image profile: not an air-gap build
    except (OSError, UnicodeDecodeError):
        # The marker exists but cannot be read: fail closed, never open.
        enabled, source = True, f"{paths.profile_marker} (unreadable)"
    return {"enabled": bool(enabled), "subnets": subnets, "source": source}


Resolver = Callable[[str], List[str]]


def _default_resolver(host: str) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos})


def host_allowed(host: str, subnets: List[str], resolver: Resolver = _default_resolver) -> bool:
    """Loopback always; an IP literal must sit in a subnet; a name must resolve ONLY there."""
    if not host:
        return False
    nets = []
    for s in subnets:
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    try:
        addrs = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        if host == "localhost":
            return True
        resolved = resolver(host)
        if not resolved:
            return False
        try:
            addrs = [ipaddress.ip_address(a.split("%", 1)[0]) for a in resolved]
        except ValueError:
            return False
    for a in addrs:
        if a.is_loopback:
            continue
        if not any(a.version == n.version and a in n for n in nets):
            return False
    return True


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_portal(portal: str, paths: Paths, env: Dict[str, str],
                 resolver: Resolver = _default_resolver) -> Optional[str]:
    """None when the portal may be dialled, else the refusal reason (exit 1)."""
    if not portal:
        return (f"the mesh is switched off: {PORTAL_KEY} is empty in the endpoints chain. "
                "Pass --portal for a private enclave.")
    u = urllib.parse.urlsplit(portal)
    host = u.hostname or ""
    if u.scheme not in ("https", "http") or not host:
        return f"portal {portal!r} is not an http(s) URL"
    if u.scheme == "http" and not _is_loopback_host(host):
        return "plain http is refused for a non-loopback portal; the credential would travel in clear"
    ag = air_gap_policy(paths, env)
    if ag["enabled"] and not host_allowed(host, ag["subnets"], resolver):
        return (f"air gap is enabled ({ag['source']}): portal host {host!r} does not resolve "
                "inside allowed_subnets. Only a private enclave conductor may be joined.")
    return None


# ── HTTP ────────────────────────────────────────────────────────────────────


class OfflineError(Exception):
    """The portal could not be reached at all (DNS, refused, timeout, TLS)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # a redirected POST would drop the body or leak the bearer


def proxies_for(paths: Paths, env: Dict[str, str]) -> Optional[Dict[str, str]]:
    """None = proxies from the environment; {} = none. Under the air gap no proxy is
    used: check_portal judged the PORTAL host, and a proxy would carry the traffic to a
    host it never judged."""
    return {} if air_gap_policy(paths, env)["enabled"] else None


def http_json(method: str, url: str, body: Optional[dict] = None, bearer: Optional[str] = None,
              cafile: Optional[str] = None, timeout: float = HTTP_TIMEOUT,
              proxies: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "awnix-mesh/1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    # Built by hand rather than build_opener(): that one always loads a default TLS
    # context even for http://, and on a plain-http loopback enclave it is dead weight.
    # Proxies come from the environment (https_proxy/no_proxy) unless the caller passes
    # {} -- which it does under the air gap (proxies_for).
    opener = urllib.request.OpenerDirector()
    px = urllib.request.getproxies_environment() if proxies is None else proxies
    handlers: List[Any] = [urllib.request.ProxyHandler(px),
                           urllib.request.UnknownHandler(), urllib.request.HTTPHandler(),
                           urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
                           urllib.request.HTTPErrorProcessor()]
    if url.startswith("https:"):
        ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    for h in handlers:
        opener.add_handler(h)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        raise OfflineError(str(getattr(e, "reason", e))) from e
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        parsed = None
    return status, parsed


def _detail(status: int, body: Any) -> str:
    d = body.get("detail") if isinstance(body, dict) else None
    return f"HTTP {status}" + (f": {str(d)[:200]}" if d else "")


# ── capabilities ────────────────────────────────────────────────────────────

_PCI_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}


def _run(argv: List[str], timeout: float = 10.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def capabilities(paths: Paths, env: Dict[str, str]) -> Dict[str, Any]:
    arch = env.get("AWNIX_MESH_ARCH") or platform.machine() or "unknown"
    cpu_model, cpu_count = "", 0
    try:
        with open(paths.p("proc/cpuinfo"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                if k == "processor":
                    cpu_count += 1
                elif k in ("model name", "Model", "Hardware") and not cpu_model and v:
                    cpu_model = v
                elif k == "CPU part" and not cpu_model and v:
                    cpu_model = f"arm cpu part {v}"
    except OSError:
        pass
    cpu_count = cpu_count or (os.cpu_count() or 0)
    mem_gb = 0.0
    try:
        with open(paths.p("proc/meminfo"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_gb = round(int(line.split()[1]) / 1048576, 1)
                    break
    except (OSError, ValueError, IndexError):
        pass

    gpus: List[Dict[str, Any]] = []
    smi = env.get("AWNIX_MESH_NVIDIA_SMI")
    smi_argv = shlex.split(smi) if smi else ([shutil.which("nvidia-smi")] if paths.live and shutil.which("nvidia-smi") else [])
    if smi_argv:
        rc, out = _run(smi_argv + ["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if rc == 0:
            for line in out.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if not parts or not parts[0]:
                    continue
                vram = None
                if len(parts) > 1:
                    try:
                        vram = round(float(parts[1]) / 1024, 1)
                    except ValueError:
                        vram = None  # "[N/A]" on unified-memory parts (GB10)
                gpus.append({"vendor": "nvidia", "model": parts[0], "vram_gb": vram})
    if not gpus:
        drm = paths.p("sys/class/drm")
        try:
            cards = sorted(c for c in os.listdir(drm) if re.fullmatch(r"card\d+", c))
        except OSError:
            cards = []
        for c in cards:
            try:
                with open(os.path.join(drm, c, "device", "vendor"), encoding="utf-8") as fh:
                    vendor = _PCI_VENDORS.get(fh.read().strip().lower())
            except OSError:
                continue
            if vendor:
                gpus.append({"vendor": vendor, "model": f"{vendor} {c}", "vram_gb": None})
    accel: List[str] = []
    if os.path.exists(paths.p("dev/accel")):
        accel.append("npu")
    return {
        "arch": arch, "cpu_model": cpu_model or platform.processor() or "unknown",
        "cpu_count": cpu_count, "mem_gb": mem_gb, "gpus": gpus, "accelerators": accel,
        "node_type": "gpu_node" if gpus else "agent",
    }


def capability_tags(caps: Dict[str, Any]) -> List[str]:
    tags = [f"arch:{caps['arch']}", f"cpu:{caps['cpu_count']}"]
    tags += sorted({f"gpu:{g['vendor']}" for g in caps["gpus"]})
    tags += [f"accel:{a}" for a in caps["accelerators"]]
    return tags


def variant(paths: Paths) -> str:
    return (read_env_file(paths.release_env) or {}).get("AWNIX_VARIANT", "unknown")


# ── NetworkManager keyfile ──────────────────────────────────────────────────

# No listen port key anywhere: a client-only peer. AMS005 asserts this template.
KEYFILE_TEMPLATE = """\
# Written by awnix-mesh. Client-only AitherMesh overlay: nothing listens, zone={zone}.
[connection]
id={iface}
uuid={uuid}
type=wireguard
interface-name={iface}
zone={zone}
autoconnect=true

[wireguard]
private-key={private_key}

{peers}
[ipv4]
method=manual
address1={address}
never-default=true

[ipv6]
method=disabled
"""

PEER_TEMPLATE = """\
[wireguard-peer.{public_key}]
endpoint={endpoint}
allowed-ips={allowed_ips}
persistent-keepalive={keepalive}

"""

_B64_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


def _overlay_24(value: Any) -> Optional[str]:
    """A peer's advertised /24, only when it is a /24 inside 10.77.0.0/16."""
    try:
        net = ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return None
    if net.version != 4 or net.prefixlen != 24 or not net.subnet_of(OVERLAY_NET):  # type: ignore[arg-type]
        return None
    return str(net)


def plan_peers(own_cidr: str, peers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Peers with an endpoint are dialled directly with their own /24. Peers with no
    endpoint are reached through the first endpoint peer (the hub). Nothing wider than a
    /24 inside 10.77.0.0/16 is ever routed, never a default route."""
    direct: List[Dict[str, Any]] = []
    routed: List[str] = []
    for p in peers:
        key = str(p.get("public_key") or "")
        cidr = _overlay_24(p.get("allowed_ips"))
        if not _B64_KEY.match(key) or not cidr or cidr == own_cidr:
            continue
        ep = p.get("endpoint")
        if ep and re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+:\d{1,5}", str(ep)):
            direct.append({"public_key": key, "endpoint": str(ep), "cidrs": [cidr],
                           "node_id": p.get("node_id"), "hostname": p.get("hostname")})
        else:
            routed.append(cidr)
    if direct:
        for c in routed:
            if c not in direct[0]["cidrs"]:
                direct[0]["cidrs"].append(c)
    return direct


def render_keyfile(private_key: str, overlay_ip: str, own_cidr: str,
                   direct: List[Dict[str, Any]], conn_uuid: str) -> str:
    peers = "".join(PEER_TEMPLATE.format(public_key=d["public_key"], endpoint=d["endpoint"],
                                         allowed_ips=";".join(d["cidrs"]) + ";",
                                         keepalive=KEEPALIVE) for d in direct)
    prefix = ipaddress.ip_network(own_cidr).prefixlen
    return KEYFILE_TEMPLATE.format(zone=ZONE, iface=IFACE, uuid=conn_uuid,
                                   private_key=private_key, peers=peers,
                                   address=f"{overlay_ip}/{prefix}")


def _nmcli(paths: Paths, env: Dict[str, str]) -> Optional[List[str]]:
    override = env.get("AWNIX_MESH_NMCLI")
    if override:
        return shlex.split(override)
    if not paths.live:
        return None  # tests never touch the host's NetworkManager
    found = shutil.which("nmcli")
    return [found] if found else None


def nm_apply(paths: Paths, env: Dict[str, str], up: bool) -> str:
    nm = _nmcli(paths, env)
    if not nm:
        return "nmcli not run (not a live root, or NetworkManager absent)"
    if up:
        _run(nm + ["connection", "reload"])
        rc, _ = _run(nm + ["connection", "up", IFACE], timeout=45)
        return "overlay up" if rc == 0 else f"nmcli connection up rc={rc}; NM autoconnect will retry"
    _run(nm + ["connection", "down", IFACE])
    rc, _ = _run(nm + ["connection", "reload"])
    return "overlay down" if rc == 0 else f"nmcli reload rc={rc}"


def iface_present(paths: Paths) -> bool:
    return os.path.isdir(paths.p(f"sys/class/net/{IFACE}"))


class StateLock:
    """flock on /var/lib/awnix/mesh/.lock. An interactive join and the path-triggered
    service must never both spend the single-use enroll token; the second waits, then
    sees the first one's result. No fcntl (a Windows dev box) means no lock."""

    def __init__(self, paths: Paths) -> None:
        self.path = os.path.join(paths.state_dir, ".lock")
        self.fd: Optional[int] = None

    def __enter__(self) -> "StateLock":
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl
        except ImportError:
            return self
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_a: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing the fd releases the flock
            self.fd = None


# ── the join state machine ──────────────────────────────────────────────────


class Ctx:
    def __init__(self, paths: Paths, env: Dict[str, str], out: Callable[[str], None],
                 resolver: Resolver = _default_resolver) -> None:
        self.paths, self.env, self.out, self.resolver = paths, env, out, resolver


def _set(ctx: Ctx, **kw: Any) -> Dict[str, Any]:
    st = load_status(ctx.paths)
    st.update(kw)
    return save_status(ctx.paths, st)


def _teardown_overlay(ctx: Ctx) -> str:
    """Remove the NM keyfile and take aithernet0 down. A refused node must not keep a
    tunnel that autoconnects and sends keepalives to the hub (air gap = egress 0)."""
    if not _unlink(ctx.paths.keyfile):
        return ""
    return nm_apply(ctx.paths, ctx.env, up=False)


def _refuse(ctx: Ctx, detail: str, drop_pending: bool = True, drop_cred: bool = False,
            teardown: bool = True) -> int:
    if drop_pending:
        _unlink(ctx.paths.pending)
    if drop_cred:
        _unlink(ctx.paths.credential)
    extra: Dict[str, Any] = {}
    if teardown:
        note = _teardown_overlay(ctx)
        if note:
            detail = f"{detail} (overlay removed: {note})"
            extra = {"peers": [], "overlay_ip": None, "overlay_cidr": None}
    _set(ctx, state="refused", detail=detail, **extra)
    ctx.out(f"awnix-mesh: refused: {detail}")
    return EXIT_REFUSED


def _pend(ctx: Ctx, pend: Dict[str, Any], detail: str) -> int:
    # pending.json is rewritten ONLY on progress (a stage change). A retry that makes
    # no progress touches status.json alone; otherwise PathChanged= on pending.json
    # would re-fire awnix-mesh.service in a tight loop for as long as the box is offline.
    _set(ctx, state="pending", portal=pend.get("portal"), detail=detail)
    ctx.out(f"awnix-mesh: pending: {detail} (will retry when the network is up)")
    return EXIT_PENDING


def advance(ctx: Ctx, pend: Dict[str, Any]) -> int:
    """Run the pending join forward as far as the network allows."""
    paths, env = ctx.paths, ctx.env
    portal = str(pend.get("portal") or "")
    cafile = pend.get("cafile") or env.get("AWNIX_MESH_CAFILE") or None
    why = check_portal(portal, paths, env, ctx.resolver)
    if why:
        return _refuse(ctx, why)
    px = proxies_for(paths, env)

    if pend.get("stage") == "exchange":
        tok = str(pend.get("token") or "")
        try:
            status, body = http_json("POST", portal + EXCHANGE_PATH, {"enrollment_token": tok},
                                     cafile=cafile, proxies=px)
        except OfflineError as e:
            return _pend(ctx, pend, f"offline: {e}")
        if status in (401, 403, 400, 404, 410):
            return _refuse(ctx, f"enroll token rejected ({_detail(status, body)}); "
                                "mint a fresh one from the portal's Connect a device", drop_cred=True)
        if status != 200 or not isinstance(body, dict) or not body.get("token"):
            return _pend(ctx, pend, f"exchange could not complete ({_detail(status, body)})")
        cred = {"token": body["token"], "renewal_secret": body.get("renewal_secret"),
                "expires_at": body.get("expires_at"), "node_id": body.get("node_id"),
                "tenant_id": body.get("tenant_id"), "portal": portal,
                "node_name": pend.get("node_name"), "role": pend.get("role")}
        _write_private(paths.credential, json.dumps(cred, indent=2) + "\n")
        pend.pop("token", None)  # single-use: never kept once exchanged
        pend["stage"] = "join"
        _write_private(paths.pending, json.dumps(pend, indent=2) + "\n")
        _set(ctx, state="pending", node_id=body.get("node_id"), tenant_id=body.get("tenant_id"),
             expires_at=body.get("expires_at"), portal=portal, detail="credential issued; joining overlay")

    cred = _read_json(paths.credential) or {}
    bearer = str(cred.get("token") or "")
    if not bearer:
        return _refuse(ctx, "no credential on this box; run `awnix mesh join <token>` again")

    if pend.get("stage") == "join":
        caps = capabilities(paths, env)
        _priv, pub = ensure_wg_key(paths)
        payload = {"hostname": pend.get("node_name"), "wg_public_key": pub, "external_ip": None,
                   "services": [], "metadata": {"role": pend.get("role"), "arch": caps["arch"],
                                                "awnix_variant": variant(paths)}}
        try:
            status, body = http_json("POST", portal + JOIN_PATH, payload, bearer=bearer, cafile=cafile,
                                     proxies=px)
        except OfflineError as e:
            return _pend(ctx, pend, f"offline: {e}")
        if status in (401, 403):
            return _refuse(ctx, f"the mesh rejected the credential ({_detail(status, body)})",
                           drop_cred=True)
        ip = body.get("aithernet_ip") if isinstance(body, dict) else None
        try:
            addr = ipaddress.ip_address(str(ip))
            ok_ip = addr.version == 4 and addr in OVERLAY_NET
        except ValueError:
            ok_ip = False
        if status != 200 or not ok_ip:
            return _pend(ctx, pend, f"overlay join could not complete ({_detail(status, body)})")
        own_cidr = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        peers = body.get("peers") if isinstance(body.get("peers"), list) else []
        direct = plan_peers(own_cidr, [p for p in peers if isinstance(p, dict)])
        if not direct:
            return _pend(ctx, pend, "the mesh returned no reachable hub peer yet")
        old = (_read_json(paths.status) or {})
        conn_uuid = str(pend.get("conn_uuid") or uuid.uuid4())
        pend["conn_uuid"] = conn_uuid
        _write_private(paths.keyfile, render_keyfile(_priv, str(ip), own_cidr, direct, conn_uuid))
        note = nm_apply(paths, env, up=True)
        pend["stage"] = "register"
        pend["overlay_note"] = note
        _write_private(paths.pending, json.dumps(pend, indent=2) + "\n")
        _set(ctx, state="joined", node_id=body.get("node_id") or old.get("node_id"),
             overlay_ip=str(ip), overlay_cidr=own_cidr, iface=IFACE, capabilities=caps,
             node_type=caps["node_type"], registered=False,
             peers=[{"node_id": d["node_id"], "hostname": d["hostname"], "endpoint": d["endpoint"],
                     "allowed_ips": d["cidrs"], "hub": i == 0} for i, d in enumerate(direct)],
             detail=f"overlay configured; {note}; registering capabilities")

    if pend.get("stage") == "register":
        st = load_status(paths)
        caps = st.get("capabilities") or capabilities(paths, env)
        body_in = {"name": pend.get("node_name"), "address": st.get("overlay_ip") or "", "port": 0,
                   "node_type": caps["node_type"],
                   "location": "edge" if pend.get("role") == "edge" else "workstation",
                   "capabilities": capability_tags(caps),
                   "metadata": {**caps, "awnix_variant": variant(paths), "role": pend.get("role"),
                                "overlay_ip": st.get("overlay_ip")}}
        try:
            status, body = http_json("POST", portal + REGISTER_PATH, body_in, bearer=bearer, cafile=cafile,
                                     proxies=px)
        except OfflineError as e:
            _set(ctx, detail=f"overlay configured; capability registration pending (offline: {e})")
            ctx.out("awnix-mesh: joined; capability registration pending (offline)")
            return EXIT_PENDING
        if status in (401, 403):
            return _refuse(ctx, f"compute registration refused ({_detail(status, body)})", drop_cred=False)
        if status != 200 or not isinstance(body, dict):
            _set(ctx, detail=f"overlay configured; registration failed ({_detail(status, body)}), will retry")
            ctx.out(f"awnix-mesh: joined; registration failed ({_detail(status, body)})")
            return EXIT_PENDING
        _unlink(paths.pending)
        st = _set(ctx, state="joined", registered=True, compute_node_id=body.get("node_id"),
                  heartbeat_at=now_iso(),
                  detail=f"joined; capabilities registered; {pend.get('overlay_note') or 'overlay configured'}")
        ctx.out(f"awnix-mesh: joined {st.get('overlay_ip')} ({st.get('overlay_cidr')}), "
                f"{len(st.get('peers') or [])} peer(s), node_type={caps['node_type']}")
        return EXIT_OK
    return _refuse(ctx, f"pending join has an unknown stage {pend.get('stage')!r}")


def read_token(arg: str, stdin=None) -> str:
    if arg == "-":
        return (stdin or sys.stdin).read().strip()
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as fh:
            return fh.read().strip()
    return arg.strip()


def cmd_join(ctx: Ctx, args: argparse.Namespace) -> int:
    try:
        token = read_token(args.token)
    except OSError as e:
        ctx.out(f"awnix-mesh: cannot read the token: {e.strerror}")
        return EXIT_REFUSED
    if not TOKEN_RE.match(token):
        ctx.out("awnix-mesh: that is not an enroll token (expected 16-4096 of A-Z a-z 0-9 . _ -)")
        return EXIT_REFUSED
    node_name = args.node_name or socket.gethostname().split(".", 1)[0]
    if not NODE_NAME_RE.match(node_name):
        ctx.out(f"awnix-mesh: invalid node name {node_name!r}")
        return EXIT_REFUSED
    portal, source = resolve_portal(ctx.paths, ctx.env, args.portal)
    why = check_portal(portal, ctx.paths, ctx.env, ctx.resolver)
    if args.dry_run:
        caps = capabilities(ctx.paths, ctx.env)
        plan = {"portal": portal, "portal_source": source, "node_name": node_name,
                "role": args.role, "refused": why, "capabilities": caps,
                "would_write": [ctx.paths.credential, ctx.paths.wg_key, ctx.paths.keyfile,
                                ctx.paths.status], "iface": IFACE, "zone": ZONE,
                "listen_port": None}
        ctx.out(json.dumps(plan, indent=2))
        return EXIT_REFUSED if why else EXIT_OK
    if why:
        # About the portal named for THIS join; an existing overlay is judged by retry.
        return _refuse(ctx, why, teardown=False)
    st = load_status(ctx.paths)
    if st["state"] == "joined" and os.path.exists(ctx.paths.credential):
        ctx.out("awnix-mesh: already joined; run `awnix mesh leave` first to re-enroll")
        return EXIT_REFUSED
    pend = {"stage": "exchange", "token": token, "portal": portal, "node_name": node_name,
            "role": args.role, "cafile": args.cafile, "created": now_iso(), "attempts": 0}
    _write_private(ctx.paths.pending, json.dumps(pend, indent=2) + "\n")
    return advance(ctx, pend)


def heartbeat(ctx: Ctx) -> int:
    paths, env = ctx.paths, ctx.env
    st = load_status(paths)
    cred = _read_json(paths.credential) or {}
    portal = str(cred.get("portal") or st.get("portal") or "")
    why = check_portal(portal, paths, env, ctx.resolver)
    if why:
        return _refuse(ctx, why, drop_pending=False)
    bearer = str(cred.get("token") or "")
    cafile = env.get("AWNIX_MESH_CAFILE") or None
    px = proxies_for(paths, env)
    exp = cred.get("expires_at")
    try:
        exp_s = float(exp) if isinstance(exp, (int, float)) else float(calendar.timegm(
            time.strptime(str(exp)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        exp_s = None
    try:
        if exp_s is not None and exp_s - time.time() < RENEW_WITHIN_S and cred.get("renewal_secret"):
            status, body = http_json("POST", portal + RENEW_PATH,
                                     {"node_id": cred.get("node_id"),
                                      "renewal_secret": cred.get("renewal_secret")},
                                     bearer=bearer, cafile=cafile, proxies=px)
            if status == 200 and isinstance(body, dict) and body.get("token"):
                cred.update(token=body["token"], expires_at=body.get("expires_at"),
                            renewal_secret=body.get("renewal_secret") or cred.get("renewal_secret"))
                _write_private(paths.credential, json.dumps(cred, indent=2) + "\n")
                bearer = cred["token"]
                st["expires_at"] = cred.get("expires_at")
        node = st.get("compute_node_id")
        if not node:
            return EXIT_OK
        status, body = http_json("POST", portal + HEARTBEAT_PATH.format(node_id=urllib.parse.quote(str(node), safe="")),
                                 {"status": "online", "metadata": {"overlay_ip": st.get("overlay_ip"),
                                                                   "iface_up": iface_present(paths)}},
                                 bearer=bearer, cafile=cafile, proxies=px)
    except OfflineError as e:
        _set(ctx, state="offline", detail=f"heartbeat could not reach the portal: {e}")
        return EXIT_PENDING
    if status in (401, 403):
        return _refuse(ctx, f"heartbeat refused ({_detail(status, body)}); the node was revoked",
                       drop_pending=False, drop_cred=True)
    if status == 404:
        _set(ctx, state="joined", registered=False, detail="the fabric forgot this node; re-registering")
        pend = {"stage": "register", "portal": portal,
                "node_name": cred.get("node_name") or socket.gethostname().split(".", 1)[0],
                "role": cred.get("role") or "worker", "created": now_iso(), "attempts": 0}
        _write_private(paths.pending, json.dumps(pend, indent=2) + "\n")
        return advance(ctx, pend)
    if status != 200:
        _set(ctx, state="offline", detail=f"heartbeat failed ({_detail(status, body)})")
        return EXIT_PENDING
    _set(ctx, state="joined", heartbeat_at=now_iso(), expires_at=st.get("expires_at"),
         detail="joined; heartbeat ok")
    return EXIT_OK


def cmd_retry(ctx: Ctx, args: argparse.Namespace) -> int:
    pend = _read_json(ctx.paths.pending)
    if pend:
        return advance(ctx, pend)
    st = load_status(ctx.paths)
    if st["state"] in ("joined", "offline") and os.path.exists(ctx.paths.credential):
        return heartbeat(ctx)
    if st["state"] == "refused" and os.path.exists(ctx.paths.keyfile):
        # A refused node still holding a tunnel (an older build, or a crash mid-refuse).
        return _refuse(ctx, str(st.get("detail") or "refused"), drop_pending=False)
    if not args.quiet:
        ctx.out(f"awnix-mesh: nothing to retry (state={st['state']})")
    return EXIT_OK


def cmd_leave(ctx: Ctx, args: argparse.Namespace) -> int:
    paths = ctx.paths
    st = load_status(paths)
    cred = _read_json(paths.credential) or {}
    notes: List[str] = []
    node = st.get("compute_node_id")
    portal = str(cred.get("portal") or st.get("portal") or "")
    if node and cred.get("token") and portal and not check_portal(portal, paths, ctx.env, ctx.resolver):
        try:
            status, body = http_json("DELETE", portal + NODE_PATH.format(node_id=urllib.parse.quote(str(node), safe="")),
                                     bearer=str(cred["token"]),
                                     cafile=ctx.env.get("AWNIX_MESH_CAFILE") or None,
                                     proxies=proxies_for(paths, ctx.env))
            notes.append("portal told" if status in (200, 204, 404) else f"portal answered {_detail(status, body)}")
        except OfflineError as e:
            notes.append(f"portal not told (offline: {e}); an admin can remove the node in the portal")
    if os.path.exists(paths.keyfile):
        notes.append(nm_apply(paths, ctx.env, up=False) if _unlink(paths.keyfile) else "")
    for f in (paths.credential, paths.wg_key, paths.pending):
        _unlink(f)
    _set(ctx, state="left", compute_node_id=None, overlay_ip=None, overlay_cidr=None, peers=[],
         registered=False, tenant_id=None, expires_at=None, heartbeat_at=None,
         detail="; ".join(n for n in notes if n) or "left; nothing was joined")
    ctx.out("awnix-mesh: left the mesh; overlay keyfile, credential and wg key removed")
    return EXIT_OK


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> int:
    st = load_status(ctx.paths)
    st["iface_up"] = iface_present(ctx.paths)
    st["pending"] = os.path.exists(ctx.paths.pending)
    if args.json:
        ctx.out(json.dumps(st, indent=2, sort_keys=True))
    else:
        ctx.out(f"state:      {st['state']}")
        for k in ("overlay_ip", "overlay_cidr", "portal", "node_type", "registered", "heartbeat_at", "detail"):
            if st.get(k) not in (None, ""):
                ctx.out(f"{k + ':':<11} {st[k]}")
        ctx.out(f"peers:      {len(st.get('peers') or [])}")
    return STATE_EXIT.get(st["state"], EXIT_REFUSED)


def cmd_capabilities(ctx: Ctx, args: argparse.Namespace) -> int:
    ctx.out(json.dumps(capabilities(ctx.paths, ctx.env), indent=2, sort_keys=True))
    return EXIT_OK


# ── a stub portal (self-test and the pytest suite share it) ─────────────────


class StubPortal:
    """Threaded 127.0.0.1 server speaking the four portal calls. `mode` steers it:
    ok | exchange-401 | join-401 | register-500 | no-hub. Records every request
    (path, headers, body) in `calls`."""

    ENROLL_OK = "enroll-token-0123456789abcdef"

    def __init__(self, mode: str = "ok", port: int = 0) -> None:
        self.mode = mode
        self.calls: List[Dict[str, Any]] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def _reply(self, code: int, obj: Any) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                stub.calls.append({"method": self.command, "path": self.path,
                                   "auth": self.headers.get("Authorization"), "body": body})
                m, p = stub.mode, self.path
                if p == EXCHANGE_PATH:
                    if m == "exchange-401" or (body or {}).get("enrollment_token") != StubPortal.ENROLL_OK:
                        return self._reply(401, {"detail": "invalid enroll token"})
                    return self._reply(200, {"token": "cap-token-SECRET-xyz", "renewal_secret": "renew-SECRET",
                                             "expires_at": "2099-01-01T00:00:00", "node_id": "node-a"})
                if self.headers.get("Authorization") != "Bearer cap-token-SECRET-xyz":
                    return self._reply(401, {"detail": "bad bearer"})
                if p == JOIN_PATH:
                    if m == "join-401":
                        return self._reply(401, {"detail": "no"})
                    hub_ep = None if m == "no-hub" else "203.0.113.10:51820"
                    return self._reply(200, {"success": True, "node_id": "n1", "aithernet_ip": "10.77.42.1",
                                             "peers": [{"node_id": "hub", "hostname": "hub",
                                                        "public_key": wg_public(base64.b64encode(b"\x01" * 32).decode()),
                                                        "endpoint": hub_ep, "allowed_ips": "10.77.7.0/24"},
                                                       {"node_id": "spoke", "hostname": "spoke",
                                                        "public_key": wg_public(base64.b64encode(b"\x02" * 32).decode()),
                                                        "endpoint": None, "allowed_ips": "10.77.9.0/24"},
                                                       {"node_id": "evil", "hostname": "evil",
                                                        "public_key": wg_public(base64.b64encode(b"\x03" * 32).decode()),
                                                        "endpoint": "198.51.100.1:51820", "allowed_ips": "0.0.0.0/0"}]})
                if p == REGISTER_PATH:
                    if m == "register-500":
                        return self._reply(500, {"detail": "boom"})
                    return self._reply(200, {"node_id": f"{(body or {}).get('name')}-t1", "status": "online"})
                if p.startswith("/compute/nodes/") and self.command == "DELETE":
                    return self._reply(200, {"removed": True})
                if p.endswith("/heartbeat"):
                    return self._reply(200, {"ok": True})
                return self._reply(404, {"detail": "not found"})

            do_POST = _handle  # noqa: N815 (http.server names)
            do_DELETE = _handle  # noqa: N815
            do_GET = _handle  # noqa: N815

        class S(http.server.ThreadingHTTPServer):
            def server_bind(self) -> None:  # skip getfqdn(): seconds per bind on Windows
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.server = S(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "StubPortal":
        self._t.start()
        return self

    def __exit__(self, *_a: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ── self-test ───────────────────────────────────────────────────────────────


def self_test() -> int:
    fails: List[str] = []

    def chk(cond: bool, name: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    # RFC 7748 section 5.2 and 6.1 vectors.
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    chk(x25519(k, u).hex() == "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552",
        "x25519 RFC 7748 5.2 vector")
    alice = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    chk(wg_public(base64.b64encode(alice).decode()) == base64.b64encode(bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")).decode(),
        "x25519 RFC 7748 6.1 public key")
    nine = (9).to_bytes(32, "little")
    chk(x25519(nine, nine).hex() == "422c8e7a6227d7bca1350b3e2bb7279f7897b87bb6854b783c60e80311ae3079",
        "x25519 RFC 7748 5.2 1-iteration vector")

    def run(root: str, argv: List[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        lines: List[str] = []
        rc = main(argv + ["--root", root], env=env or {}, out=lines.append)
        return rc, "\n".join(lines)

    with tempfile.TemporaryDirectory() as root, StubPortal() as portal:
        rc, out = run(root, ["join", StubPortal.ENROLL_OK, "--portal", portal.url, "--node-name", "box1"])
        chk(rc == EXIT_OK, f"join against the stub exits 0 (got {rc})")
        paths = Paths(root)
        st = open(paths.status, encoding="utf-8").read()
        chk("SECRET" not in st and StubPortal.ENROLL_OK not in st, "status.json carries no token")
        chk("SECRET" not in out and StubPortal.ENROLL_OK not in out, "stdout carries no token")
        kf = open(paths.keyfile, encoding="utf-8").read()
        chk("listen-port" not in kf and f"zone={ZONE}" in kf, f"keyfile has no listen-port and zone={ZONE}")
        chk("0.0.0.0/0" not in kf and "10.77.9.0/24" in kf, "no default route; spoke routed via hub")
        chk(not os.path.exists(paths.pending), "pending cleared after a full join")
        rc, _ = run(root, ["leave"])
        chk(rc == EXIT_OK and not os.path.exists(paths.keyfile) and not os.path.exists(paths.credential),
            "leave removes keyfile and credential")

    with tempfile.TemporaryDirectory() as root:
        rc, _ = run(root, ["join", StubPortal.ENROLL_OK, "--portal", closed_port_url()])
        chk(rc == EXIT_PENDING and os.path.exists(Paths(root).pending), "offline join is pending (exit 2)")
    with tempfile.TemporaryDirectory() as root, StubPortal("exchange-401") as portal:
        rc, _ = run(root, ["join", StubPortal.ENROLL_OK, "--portal", portal.url])
        chk(rc == EXIT_REFUSED and not os.path.exists(Paths(root).credential)
            and not os.path.exists(Paths(root).pending), "401 exchange is refused, nothing kept")
    with tempfile.TemporaryDirectory() as root:
        rc, _ = run(root, ["join", StubPortal.ENROLL_OK, "--portal", "https://203.0.113.5"],
                    env={"AITHER_AIR_GAP": "strict"})
        chk(rc == EXIT_REFUSED, "air gap refuses a public portal")
    with tempfile.TemporaryDirectory() as root, StubPortal() as portal:
        rc, _ = run(root, ["join", StubPortal.ENROLL_OK, "--portal", portal.url, "--node-name", "box2"])
        paths = Paths(root)
        os.makedirs(os.path.dirname(paths.profile_marker), exist_ok=True)
        with open(paths.profile_marker, "w", encoding="utf-8") as fh:
            fh.write("airgap\n")
        with open(paths.credential, encoding="utf-8") as fh:
            cred = json.load(fh)
        cred["portal"] = "https://203.0.113.5"
        _write_private(paths.credential, json.dumps(cred))
        rc2, _ = run(root, ["retry", "--quiet"], env={"AITHER_AIR_GAP": "0"})
        chk(rc == EXIT_OK and rc2 == EXIT_REFUSED and not os.path.exists(paths.keyfile),
            "air gap switched on after a join removes the overlay keyfile (profile marker)")
    with tempfile.TemporaryDirectory() as root:
        rc, _ = run(root, ["join", "short"])
        chk(rc == EXIT_REFUSED, "a malformed token is refused")

    print(f"awnix-mesh self-test: {'PASS' if not fails else 'FAIL'} ({len(fails)} failure(s))")
    return EXIT_OK if not fails else EXIT_REFUSED


# ── entry ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awnix mesh", description="Join this box to an AitherMesh.")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    sub = ap.add_subparsers(dest="verb")
    j = sub.add_parser("join", help="enroll with a token from the portal's Connect a device")
    j.add_argument("token", help="the enroll token, @FILE, or - for stdin (argv is visible in ps)")
    j.add_argument("--portal", default=None)
    j.add_argument("--node-name", default=None)
    j.add_argument("--role", choices=ROLES, default="worker")
    j.add_argument("--cafile", default=None, help="CA bundle for a private enclave portal")
    j.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("leave")
    r = sub.add_parser("retry")
    r.add_argument("--quiet", action="store_true")
    c = sub.add_parser("capabilities")
    c.add_argument("--json", action="store_true")
    for p in (j, s, r, c, sub.choices["leave"]):
        p.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return ap


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
         out: Callable[[str], None] = print, resolver: Resolver = _default_resolver) -> int:
    ap = build_parser()
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.list_verbs:
        for v in VERBS:
            out(v)
        return EXIT_OK
    if args.self_test:
        return self_test()
    if not args.verb:
        ap.print_help()
        return EXIT_PENDING
    environ = dict(os.environ) if env is None else env
    ctx = Ctx(Paths(args.root), environ, out, resolver)
    handler = {"join": cmd_join, "status": cmd_status, "leave": cmd_leave,
               "retry": cmd_retry, "capabilities": cmd_capabilities}[args.verb]
    try:
        if args.verb in ("join", "leave", "retry"):
            with StateLock(ctx.paths):
                return handler(ctx, args)
        return handler(ctx, args)
    except PermissionError as e:
        out(f"awnix-mesh: permission denied ({e.filename}); run as root")
        return EXIT_PENDING


if __name__ == "__main__":
    sys.exit(main())
