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
    enforce-edge   everything enforce-absent may do, plus the edge forwarders
                   described under EDGE: unit files this agent itself creates
                   under the `aither-edge-` prefix, carrying its marker line.
                   It still never starts or installs a unit of present[].
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
  must be routed between nodes once a service runs on another node. In mode
  enforce-edge or enforce the agent applies them, after the converge step, as
  plain TCP relays (TLS passes through untouched; the relay is this file run as
  `edge-relay`):
    egress   a quadlet <quadlet dir>/aither-edge-out-<name>-<port>.container: a
             container that carries the routed service's container NAME on the
             network the manifest names (edge.network), runs the image the
             manifest pins by digest (edge.image) with this file mounted
             read-only, listens on the service's port and relays to
             target_ip:target_port. `host_port` also publishes it on 127.0.0.1.
    ingress  via "relay": a systemd unit <systemd dir>/aither-edge-in-<port>.service
             that binds listen_ip:port, relays to 127.0.0.1:port and closes any
             peer that is not in the entry's allow list before a byte is relayed.
             via "published": nothing is created (the unit already publishes on
             that address) and the allow list is NOT enforced here; the report
             says `allow_enforced: false`. Because nothing is created, such an
             entry is reported `published` only when this node sees the port
             answering on listen_ip: the agent verifies, never takes the
             manifest's word. On a node whose manifest declares a workspace it is
             `refused` outright: the unit publishes on the fleet address, nothing
             here filters it, and the flat overlay would admit every other node
             to the service.
  The rules that bound it:
    * Nothing is built unless the manifest pins the image by digest, podman is
      here, the image is present or pulls, and one `podman run --network none`
      proved its python3 can import what the relay needs. A manifest with an
      egress entry also needs the network's quadlet to exist here (it arrives
      with the first unit a bundle installs, so a node that hosts nothing yet
      and is asked for no forwarder is not held to it). Otherwise
      `applied: false`, a reason, and nothing written.
    * Every file it writes starts with `# X-Aither-EdgeManaged=1`. A file under
      the prefix WITHOUT that line is not this agent's: it is never written,
      stopped or removed, the entry is `blocked`, and the file is listed in
      `foreign`.
    * An egress forwarder is not started while another container this node's
      runtime knows about holds the name (`blocked`) — running or not, because
      `podman ps` alone hides a crashed service whose container still exists —
      not while a local quadlet declares that `ContainerName`, and not while a
      local unit name implies it (`aither-<svc>.service` runs the container
      `aitheros-<svc>`), because a crashed unit is what would start it again. The
      one proof that the name went with a move is the same manifest listing that
      unit `absent`, with its container down.
    * An entry whose values are not a plain container name, ports 1-65535 and IP
      addresses is `refused` and nothing is written for it.
    * Same entry and unit active: no command is run. A changed entry is
      rewritten and restarted; an entry the manifest dropped is stopped and its
      file removed. A start is tried at most 3 times per entry and generation.
    * Below enforce-edge, or with no accepted manifest, NOTHING is written or
      removed: forwarders already running keep running. Lowering a mode never
      tears routing down; `edge-clear` (below) and `awnix fleet leave` do.
  The report's `edge` block says what is (`applied`, `ready`, one row per entry
  with its `state`: active | published | blocked | refused | failed | pending,
  and for egress whether the target accepted a TCP connection, `reachable`).
  `ready` is reported on every cycle, entries or none: `image`, `network` (the
  network's `.network` quadlet is on this node) and `runtime`. That is what tells
  a placer, before it moves anything, whether this node could carry the egress
  entry the move would hand it. The placer opens no move while a node involved
  reports `applied: false`, and completes none until the rows it needs say
  active and reachable. A generation that would take a name off this node is not
  acted on while no forwarder can be built here: the local unit is kept serving
  (its absence is `hold`, with the reason) instead of the name going dark.

  `<this file> edge-relay --listen IP:PORT --target IP:PORT [--allow ip,ip]`
  runs one relay in the foreground. `<this file> edge-clear [--env-file FILE]`
  stops and removes every edge unit this agent owns and exits 0, or 1 when one
  could not be removed.

  SECRETS. One kind of secret can be delivered to this node: a TLS certificate
  and key the controller's side minted for ONE service on THIS node. A manifest
  that needs any says so: `secrets: {generation, units: {unit: [paths]}}`.
    * They are fetched from <url>/secrets (signed like every other request) ONLY
      when the controller URL is https AND the controller is verified by
      AITHER_PLACEMENT_CONTROLLER_SHA256 or AITHER_PLACEMENT_CA_FILE. A plain
      http join, or an https one that trusts the system store alone, fetches
      nothing and the report says why: an endpoint behind a public CA may be a
      proxy that ends TLS.
    * Nothing is written unless the answer's HMAC verifies under the node key,
      it names this node, its generation is exactly the one the accepted
      manifest names (an older answer is a replay), and every file in it is one
      the manifest names for a unit. One failure refuses the whole answer.
    * Files land in <dir>/secrets/<path>, 0600, in 0700 directories, written
      atomically. Nothing is fetched again while the files on disk are the ones
      last delivered for that generation.
    * A file under <dir>/secrets that the accepted manifest no longer names is
      removed. In observe nothing is fetched, written or removed.
    * A unit whose manifest names secrets that are not on disk is never
      started: `hold: secrets for this unit are not delivered`.
    * When a delivery changes the files of a unit that is running, enforce
      restarts it (`systemctl try-restart`, one unit per cycle).
  What this does not protect: root or a stolen disk on this node (the files are
  plaintext at rest), and whoever holds node.key. The report's `secrets` block
  carries unit names and paths, never a value, a hash or a size.

  The report is POSTed to <url>/report as JSON with three headers the controller
  verifies (lib/orchestration/placement_store.py):
      X-Aither-Node       this node's id
      X-Aither-Timestamp  unix seconds
      X-Aither-Signature  hex HMAC-SHA256(node key, METHOD "\n" PATH "\n" TIMESTAMP
                          "\n" sha256_hex(body)) over the exact bytes sent
  It carries `units: {unit: {active, healthy, masked}}`, the shape the placer reads.

  AITHER_PLACEMENT_URL         controller base for this node, e.g.
                               https://<controller>/api/placement/nodes/<id>;
                               <url>/manifest, <url>/bundle and (see
                               SECRETS) <url>/secrets are fetched, the
                               report goes to <url>/report
  AITHER_PLACEMENT_KEY_FILE    default /etc/aither/placement/node.key (0600)
  AITHER_PLACEMENT_MODE        observe | enforce-absent | enforce-edge | enforce
                               (default observe)
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
  AITHER_SYSTEMD_DIR           default /etc/systemd/system (ingress relay units)
  AITHER_PLACEMENT_EDGE_HOOK   a local executable run (no arguments, 60 s) after
                               any edge unit changed, e.g. one that refreshes a
                               host's name map. Unset = none. A non-zero exit
                               makes the report say `applied: false`.
"""
import base64
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import platform
import re
import selectors
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
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
SYSTEMD_DIR = os.getenv("AITHER_SYSTEMD_DIR", "/etc/systemd/system")
PLACEMENT_EDGE_HOOK = os.getenv("AITHER_PLACEMENT_EDGE_HOOK", "")

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

    def tcp_ok(self, host, port, timeout=3.0):
        """True when host:port accepts a TCP connection. Nothing is sent."""
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except (OSError, ValueError):
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


_QUADLET_NAME_RE = re.compile(r"(?m)^\s*ContainerName\s*=\s*(\S+)\s*$")


def _row_running(row) -> bool:
    """True when a `podman ps -a` / `docker ps -a` row is a container that is up."""
    word = str(row.get("status") or "").strip().lower()
    return word.startswith("up") or word.startswith("running")


def _quadlet_names(folder):
    """{container name: owning unit} for every `ContainerName=` a `.container` file in
    `folder` declares. Read-only. None when a file there cannot be read: a name is only
    free when the whole directory says so."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return {} if not os.path.exists(folder) else None
    out = {}
    for fname in names:
        if not fname.endswith(".container"):
            continue
        raw = _read_bytes(os.path.join(folder, fname), _MAX_UNIT_FILE_BYTES)
        if raw is None:
            return None
        for found in _QUADLET_NAME_RE.finditer(raw.decode("utf-8", "replace")):
            out.setdefault(found.group(1), fname[:-len(".container")] + ".service")
    return out


def _implied_container(unit: str):
    """The container name a fleet unit name implies: `aither-<service>.service` is the
    unit that runs the container `aitheros-<service>`. None for a name that is not one
    of the fleet's. A unit is evidence a name belongs to this node even when its
    container is gone: the unit is what will start it again."""
    stem = unit[:-len(".service")] if unit.endswith(".service") else unit
    if stem.startswith("aitheros-"):
        return stem
    if stem.startswith("aither-"):
        return "aitheros-" + stem[len("aither-"):]
    return None


def inventory(runner=None, all_containers=False, strict=False):
    """(rows, runtimes): containers from podman and/or docker, one `ps` per runtime
    that is installed. ``all_containers`` lists stopped ones too (``ps -a``), which
    is what a name guard needs: a crashed service still owns its container name.
    rows is None when no runtime answered -- or, with ``strict``, when a runtime
    that IS installed did not answer either: one silent runtime proves nothing."""
    runner = runner or _RUNNER
    rows, seen, answered, found, silent = [], set(), False, [], False
    for rt in _RUNTIMES:
        if not runner.which(rt):
            continue
        found.append(rt)
        try:
            if rt == "podman":
                rc, out, _ = runner.run(
                    ["podman", "ps"] + (["-a"] if all_containers else [])
                    + ["--format", "json"], timeout=20)
                got = _podman_rows(out) if rc == 0 else None
            else:
                rc, out, _ = runner.run(
                    ["docker", "ps"] + (["-a"] if all_containers else [])
                    + ["--format", "{{.Names}}|{{.Status}}|{{.Image}}"], timeout=8)
                got = _docker_rows(out) if rc == 0 else None
        except Exception:
            got = None
        if got is None:
            silent = True
            continue
        answered = True
        for row in got:  # `docker` may be the podman shim: one row per name
            if row["name"] not in seen:
                seen.add(row["name"])
                rows.append(row)
    if not answered or (strict and silent):
        return None, found
    return rows, found


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
MODES = ("observe", "enforce-absent", "enforce-edge", "enforce")  # weakest first
# What a mode may do is asked by NAME, never by a bare number: a mode added to the
# tuple above must not silently move the line between "stops" and "starts".
R_ABSENT = MODES.index("enforce-absent")  # may stop, mask and record absent[]
R_EDGE = MODES.index("enforce-edge")      # may also build the edge forwarders it owns
R_ENFORCE = MODES.index("enforce")        # may also install and start present[]
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


# ── delivered secrets ──

SECRETS_SCHEMA = "aither-placement-secrets/v1"
_SECRET_KINDS = ("tls-cert",)
_SECRET_PATH_RE = re.compile(r"^tls(/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){1,4}$")
_MAX_SECRETS_BYTES = 8 * 1024 * 1024
_MAX_SECRET_FILE_BYTES = 64 * 1024
_MAX_SECRET_ITEMS = 256


def manifest_secrets(man):
    """(generation, {unit: [paths]}, problem) of a manifest's `secrets` block.
    A manifest without one names none: (None, {}, None). A block that is not
    exactly what the controller writes is a problem, and names nothing."""
    if man is None or "secrets" not in man:
        return None, {}, None
    block = man["secrets"]
    gen = block.get("generation") if isinstance(block, dict) else None
    units = block.get("units") if isinstance(block, dict) else None
    if (isinstance(gen, bool) or not isinstance(gen, int) or gen < 1
            or not isinstance(units, dict) or not units):
        return None, {}, "the manifest's secrets block is malformed"
    listed = {e.get("unit") for e in man.get("present", []) if isinstance(e, dict)}
    out = {}
    for unit, paths in units.items():
        if unit not in listed:
            return None, {}, "the manifest names secrets for a unit it does not list present"
        if (not isinstance(paths, list) or not paths or not all(
                isinstance(p, str) and _SECRET_PATH_RE.match(p) for p in paths)):
            return None, {}, "the manifest names a secret path that is not under tls/"
        out[unit] = sorted(set(paths))
    return gen, out, None


def _write_private(path: str, data: bytes) -> None:
    """Atomic write of a file that never exists with a mode wider than 0600."""
    tmp = f"{path}.tmp.{os.getpid()}"
    if os.path.lexists(tmp):
        os.remove(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise


# ── edge forwarders ──
EDGE_MARKER = "# X-Aither-EdgeManaged=1"
_EDGE_OUT = "aither-edge-out-"
_EDGE_IN = "aither-edge-in-"
_EDGE_KINDS = ((_EDGE_OUT, ".container"), (_EDGE_IN, ".service"))
_EDGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_EDGE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_EDGE_RELAY_PATH = "/opt/aither-edge-relay.py"  # where the forwarder container sees this file
# Every module this file imports at the top: the forwarder runs THIS file, so the
# image is proven able to load all of it, not only the two the relay loop uses.
_EDGE_PROBE = ("import base64,hashlib,hmac,http.client,io,ipaddress,json,os,platform,re,"
               "selectors,shutil,socket,ssl,subprocess,sys,tarfile,threading,time,"
               "urllib.parse,urllib.request")
_EDGE_HOOK_TIMEOUT = 60
_MAX_EDGE_ENTRIES = 256
_MAX_EDGE_ALLOW = 64
_RELAY_BUFFER = 64 * 1024
_RELAY_MAX_CONNECTIONS = 512
_RELAY_CONNECT_TIMEOUT = 10.0


def _port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _ip(value) -> bool:
    """True for one IP address written in its canonical form."""
    if not isinstance(value, str):
        return False
    try:
        return str(ipaddress.ip_address(value)) == value
    except ValueError:
        return False


def edge_egress_problem(ent):
    """Why an egress entry may not become a unit file, or None. Everything that is
    written into the file is checked here first: a name is a plain container
    name, a port is 1-65535, an address parses as one IP."""
    if not isinstance(ent, dict):
        return "entry is not an object"
    name = ent.get("name")
    if not isinstance(name, str) or not _EDGE_NAME_RE.match(name):
        return "name is not a plain container name"
    if not _port(ent.get("listen_port")) or not _port(ent.get("target_port")):
        return "listen_port / target_port is not an integer 1-65535"
    if not _ip(ent.get("target_ip")):
        return "target_ip is not an IP address"
    if "host_port" in ent and not _port(ent["host_port"]):
        return "host_port is not an integer 1-65535"
    return None


def edge_ingress_problem(ent):
    """Why an ingress entry may not be applied, or None."""
    if not isinstance(ent, dict):
        return "entry is not an object"
    if not _port(ent.get("port")):
        return "port is not an integer 1-65535"
    if ent.get("via") not in ("relay", "published"):
        return "via is neither relay nor published"
    allow = ent.get("allow")
    if (not isinstance(allow, list) or len(allow) > _MAX_EDGE_ALLOW
            or not all(_ip(a) for a in allow)):
        return "allow is not a short list of IP addresses"
    if ent["via"] == "relay":
        if not allow:
            return "a relay with an empty allow list would admit nobody"
        if not _ip(ent.get("listen_ip")):
            return "listen_ip is not an IP address"
    return None


def edge_spec(entry: dict, image, network) -> str:
    """The identity of one applied entry: what it says, and what it would run."""
    return hashlib.sha256(canonical({"entry": entry, "image": image,
                                     "network": network})).hexdigest()


def edge_egress_unit(ent: dict, image: str, network: str, relay_file: str):
    """(file name, unit name, quadlet text, spec) of one VALID egress entry."""
    name, listen, tip, tport = (ent["name"], ent["listen_port"], ent["target_ip"],
                                ent["target_port"])
    entry = {k: ent[k] for k in ("name", "listen_port", "target_ip", "target_port",
                                 "host_port") if k in ent}
    spec = edge_spec(entry, image, network)
    stem = f"{_EDGE_OUT}{name}-{listen}"
    lines = [
        f"{EDGE_MARKER} spec={spec}",
        "# Written by the Aither node agent from a signed placement manifest.",
        "# Not to be edited: it is rewritten, and removed when the manifest drops it.",
        "[Unit]",
        f"Description=Aither edge forwarder {name}:{listen} -> {tip}:{tport}",
        "",
        "[Container]",
        f"ContainerName={name}",
        f"Image={image}",
        f"Network={network}.network",
    ]
    if "host_port" in ent:
        lines.append(f"PublishPort=127.0.0.1:{ent['host_port']}:{listen}")
    lines += [
        f"Volume={relay_file}:{_EDGE_RELAY_PATH}:ro",
        "PodmanArgs=--entrypoint=python3",
        f"Exec={_EDGE_RELAY_PATH} edge-relay --listen 0.0.0.0:{listen} --target {tip}:{tport}",
        "",
        "[Service]",
        "Restart=always",
        "",
        "[Install]",
        "WantedBy=multi-user.target default.target",
        "",
    ]
    return stem + ".container", stem + ".service", "\n".join(lines), spec


def edge_ingress_unit(ent: dict, image, network, python: str, agent_file: str):
    """(file name, unit name, unit text, spec) of one VALID `via: relay` entry."""
    port, listen_ip = ent["port"], ent["listen_ip"]
    allow = sorted(set(ent["allow"]))
    entry = {"port": port, "listen_ip": listen_ip, "allow": allow, "via": "relay"}
    spec = edge_spec(entry, image, network)
    unit = f"{_EDGE_IN}{port}.service"
    lines = [
        f"{EDGE_MARKER} spec={spec}",
        "# Written by the Aither node agent from a signed placement manifest.",
        "# Not to be edited: it is rewritten, and removed when the manifest drops it.",
        "[Unit]",
        f"Description=Aither edge ingress relay {listen_ip}:{port} -> 127.0.0.1:{port}",
        "After=network-online.target",
        "Wants=network-online.target",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        f"ExecStart={python} {agent_file} edge-relay --listen {listen_ip}:{port} "
        f"--target 127.0.0.1:{port} --allow {','.join(allow)}",
        "DynamicUser=yes",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "NoNewPrivileges=yes",
        "Restart=always",
        "RestartSec=2",
        "",
    ]
    return unit, unit, "\n".join(lines), spec


def edge_owned(path: str) -> bool:
    """True when the file's first line is this agent's marker."""
    raw = _read_bytes(path, _MAX_UNIT_FILE_BYTES)
    if raw is None:
        return False
    first = raw.decode("utf-8", "replace").splitlines()[:1]
    return bool(first) and first[0].startswith(EDGE_MARKER)


def _relay_pump(a, b, bufsize=_RELAY_BUFFER) -> None:
    """Copy bytes both ways between two connected sockets until both directions
    have ended. Non-blocking with a bounded queue per direction, so one side
    that stops reading cannot stall the other direction or grow memory. A half
    close (one side finished sending) is passed on once its bytes are delivered.
    Raises OSError when either connection breaks."""
    peer = {a: b, b: a}
    queue = {a: b"", b: b""}        # bytes waiting to be written TO that socket
    reading = {a: True, b: True}    # that socket may still send us bytes
    finish = {a: False, b: False}   # half-close that socket once its queue is empty
    registered = {}
    sel = selectors.DefaultSelector()
    try:
        a.setblocking(False)
        b.setblocking(False)
        while True:
            busy = False
            for s in (a, b):
                if finish[s] and not queue[s]:
                    finish[s] = False
                    s.shutdown(socket.SHUT_WR)
                mask = 0
                if reading[s] and len(queue[peer[s]]) < bufsize:
                    mask |= selectors.EVENT_READ
                if queue[s]:
                    mask |= selectors.EVENT_WRITE
                if mask != registered.get(s, 0):
                    if s in registered:
                        sel.unregister(s)
                        del registered[s]
                    if mask:
                        sel.register(s, mask)
                        registered[s] = mask
                busy = busy or bool(mask)
            if not busy:
                return
            for key, mask in sel.select(timeout=60):
                s = key.fileobj
                if mask & selectors.EVENT_READ:
                    try:
                        data = s.recv(bufsize)
                    except (BlockingIOError, InterruptedError):
                        data = None
                    if data:
                        queue[peer[s]] += data
                    elif data is not None:
                        reading[s] = False
                        finish[peer[s]] = True
                if mask & selectors.EVENT_WRITE and queue[s]:
                    try:
                        sent = s.send(queue[s])
                    except (BlockingIOError, InterruptedError):
                        sent = 0
                    queue[s] = queue[s][sent:]
    finally:
        sel.close()


class EdgeRelay:
    """A TCP relay: each connection accepted on `listen` is piped to `target`.

    One thread per connection, at most `max_connections` at a time (one more is
    closed at once). With a non-empty `allow`, a peer whose address is not in it
    is closed before the target is dialled and before any byte is relayed. It
    reads no key, no manifest and no file: its whole input is its arguments."""

    def __init__(self, listen, target, allow=(), max_connections=_RELAY_MAX_CONNECTIONS,
                 connect_timeout=_RELAY_CONNECT_TIMEOUT, log=None):
        self.target = (str(target[0]), int(target[1]))
        self.allow = frozenset(str(ipaddress.ip_address(x)) for x in allow)
        self.connect_timeout = connect_timeout
        self.log = log or (lambda msg: print(f"[edge-relay] {msg}", flush=True))
        self.refused = 0      # peers outside the allow list
        self.unreachable = 0  # connections whose target did not answer
        self._closed = False
        self._slots = threading.BoundedSemaphore(max_connections)
        family = socket.AF_INET6 if ":" in str(listen[0]) else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "posix":
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((str(listen[0]), int(listen[1])))
            self.sock.listen(128)
            self.sock.settimeout(0.5)  # so close() is noticed by the accept loop
        except OSError:
            self.sock.close()
            raise
        self.address = self.sock.getsockname()[:2]

    def serve_forever(self) -> None:
        while not self._closed:
            try:
                conn, peer = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed:
                    return
                time.sleep(0.05)  # out of descriptors: let some connections end
                continue
            if self.allow and str(peer[0]) not in self.allow:
                self.refused += 1
                conn.close()
                continue
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        upstream = None
        try:
            try:
                upstream = socket.create_connection(self.target, timeout=self.connect_timeout)
            except OSError as exc:
                self.unreachable += 1
                if self.unreachable == 1 or self.unreachable % 100 == 0:
                    self.log(f"target {self.target[0]}:{self.target[1]} did not answer "
                             f"({exc.__class__.__name__}); {self.unreachable} so far")
                return
            try:
                _relay_pump(conn, upstream)
            except OSError:
                return  # one side reset: this connection is over, the relay is not
        finally:
            conn.close()
            if upstream is not None:
                upstream.close()
            self._slots.release()

    def close(self) -> None:
        self._closed = True
        self.sock.close()


def _endpoint(text: str):
    """("ip", port) from IP:PORT. The host must be an IP address: a relay that
    resolved names would depend on the very naming it stands in for."""
    host, _, port = str(text).rpartition(":")
    host = host.strip("[]")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"{text!r} is not IP:PORT")
    return str(ipaddress.ip_address(host)), int(port)


def edge_relay_main(argv) -> int:
    """`edge-relay --listen IP:PORT --target IP:PORT [--allow ip,ip]`. Exit 2 = bad
    arguments, 1 = the listen address could not be bound."""
    opts = {"--listen": "", "--target": "", "--allow": ""}
    args = list(argv)
    try:
        while args:
            flag = args.pop(0)
            if flag not in opts or not args:
                raise ValueError(f"unexpected argument {flag!r}")
            opts[flag] = args.pop(0)
        listen, target = _endpoint(opts["--listen"]), _endpoint(opts["--target"])
        allow = [str(ipaddress.ip_address(a)) for a in opts["--allow"].split(",") if a]
    except ValueError as exc:
        print(f"[edge-relay] {exc}; usage: edge-relay --listen IP:PORT --target IP:PORT "
              "[--allow ip,ip]", flush=True)
        return 2
    try:
        relay = EdgeRelay(listen, target, allow)
    except OSError as exc:
        print(f"[edge-relay] cannot listen on {listen[0]}:{listen[1]}: {exc}", flush=True)
        return 1
    print(f"[edge-relay] {listen[0]}:{listen[1]} -> {target[0]}:{target[1]} "
          f"allow={','.join(sorted(allow)) or 'any'}", flush=True)
    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        relay.close()
    return 0


def _masked(state: dict) -> list:
    """The units this agent masked, as recorded in state.json."""
    got = state.get("masked")
    return [u for u in got if isinstance(u, str)] if isinstance(got, list) else []


class PlacementAgent:
    """One node's converge step. Constructed once; cycle() is called from the loop."""

    def __init__(self, runner=None, root=None, node_id=None, mode=None, url=None,
                 key_file=None, cap_file=None, bundle=None, quadlet_dir=None,
                 headroom_mb=None, health_wait=None, log=None, stable_checks=None,
                 systemd_dir=None, edge_hook=None, agent_file=None, pin=None, ca_file=None):
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
        self.systemd_dir = systemd_dir or SYSTEMD_DIR
        self.edge_hook = PLACEMENT_EDGE_HOOK if edge_hook is None else edge_hook
        # The file an ingress relay runs and a forwarder container mounts: this one.
        self.agent_file = agent_file or os.path.abspath(__file__)
        self.edge_dir = os.path.join(self.root, "edge")
        self.relay_file = os.path.join(self.edge_dir, "relay.py")
        # How this node verifies its controller; secrets are fetched under these
        # two and nothing weaker.
        self.pin = PLACEMENT_CONTROLLER_SHA256 if pin is None else pin
        self.ca_file = PLACEMENT_CA_FILE if ca_file is None else ca_file
        self.secrets_dir = os.path.join(self.root, "secrets")
        self._secrets_line = None
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

    def converge(self, man: dict, mode: str, states, state: dict, hold_absent=None) -> list:
        """Verdicts for this manifest, acting as far as `mode` allows. `hold_absent`
        is a reason: when set, no unit in `absent` is stopped or masked — the caller
        has proved this node cannot yet route the name somewhere else, and the local
        copy is what is still answering."""
        rank = MODES.index(mode)
        verdicts = []
        ours = _masked(state)
        # Units whose delivered secrets changed while they ran (pull_secrets).
        rotate = [u for u in (state.get("secrets_restart") or []) if isinstance(u, str)]
        restarted = False
        before = (list(ours), dict(state.get("starts") or {}), sorted(rotate))

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
            if rank < R_ABSENT:
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
            if hold_absent:
                verdict(ent, "absent", st, "hold", hold_absent, masked=masked)
                continue
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
        if rank >= R_ABSENT and not hold_absent:
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
                if rank < R_ABSENT:
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
            if st not in _LIVE and unit in rotate:
                rotate.remove(unit)  # down: its next start reads the new files
            if st == "active" and unit in rotate and rank >= R_ENFORCE and not restarted:
                restarted = True
                self._stable.pop(unit, None)
                rc, _, err = self.runner.run(["systemctl", "try-restart", unit], timeout=300)
                if rc == 0:
                    rotate.remove(unit)
                    verdict(ent, "present", st, "ok", "restarted (secrets rotated)",
                            "restarted (secrets rotated)", masked=False)
                else:
                    verdict(ent, "present", st, "hold", "restart after a secrets rotation "
                            f"failed: {err.strip()[:120]}", masked=False)
                continue
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
            elif rank < R_ENFORCE:
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
        if rank >= R_ABSENT:
            state["masked"] = sorted(set(ours))
            listed = {ent["unit"] for ent in man.get("present", [])}
            rotate = sorted(u for u in set(rotate) if u in listed)
            if rotate:
                state["secrets_restart"] = rotate
            else:
                state.pop("secrets_restart", None)
            if (state["masked"], dict(state.get("starts") or {}), rotate) != (
                    sorted(set(before[0])), before[1], before[2]):
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
        if self._secrets_missing(man, unit, state):
            return "hold", "secrets for this unit are not delivered", "none", None, False
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

    # ── delivered secrets ───────────────────────────────────────────────

    def _secret_file(self, path: str) -> str:
        return os.path.join(self.secrets_dir, *path.split("/"))

    def _secret_on_disk(self, path: str) -> bool:
        target = self._secret_file(path)
        return os.path.isfile(target) and not os.path.islink(target)

    def _secrets_missing(self, man, unit: str, state: dict) -> bool:
        """True when the manifest names secrets for `unit` that this node does not
        hold for the manifest's generation. A unit it names none for: False."""
        gen, units, problem = manifest_secrets(man)
        if problem:
            return True
        paths = units.get(unit)
        if not paths:
            return False
        held = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
        return held.get("generation") != gen or not all(
            self._secret_on_disk(p) for p in paths)

    def _secrets_refusal(self, mode: str):
        """Why this node may not receive a secret right now, or None."""
        if not self.url:
            return "this node does not pull from a controller (no AITHER_PLACEMENT_URL)"
        if urllib.parse.urlsplit(self.url).scheme != "https":
            return "the controller URL is not https: no secret is fetched over it"
        if self.pin:
            if not _HEX64_RE.match(self.pin):
                return "AITHER_PLACEMENT_CONTROLLER_SHA256 is not 64 hex digits"
        elif not self.ca_file:
            return ("the controller is verified through the system trust store only: "
                    "secrets need AITHER_PLACEMENT_CONTROLLER_SHA256 or "
                    "AITHER_PLACEMENT_CA_FILE")
        if MODES.index(mode) < R_ABSENT:
            return f"mode {mode} writes no secret"
        return None

    def _secrets_current(self, gen, wanted, state: dict) -> bool:
        """True when the files on disk are exactly the ones last delivered for
        this generation: nothing needs fetching."""
        held = state.get("secrets") if isinstance(state.get("secrets"), dict) else {}
        files = held.get("files") if isinstance(held.get("files"), dict) else {}
        if held.get("generation") != gen or set(files) != set(wanted):
            return False
        for path in wanted:
            data = _read_bytes(self._secret_file(path), _MAX_SECRET_FILE_BYTES)
            if (data is None or os.path.islink(self._secret_file(path))
                    or hashlib.sha256(data).hexdigest() != files[path]):
                return False
        return True

    def _parse_secrets(self, raw: bytes, key: bytes, gen, units: dict):
        """({path: bytes}, None) for an answer that may be written, else (None,
        reason). The signature is checked before any field is believed, and one
        bad item refuses the whole answer."""
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None, "secrets answer is not JSON"
        if not isinstance(doc, dict):
            return None, "secrets answer is not an object"
        sig = doc.get("sig")
        try:
            want = sign(doc, key)
        except (TypeError, ValueError):
            return None, "secrets answer cannot be canonicalised"
        if not isinstance(sig, str) or not hmac.compare_digest(sig.lower().encode(), want.encode()):
            return None, "bad signature on the secrets answer"
        if doc.get("schema") != SECRETS_SCHEMA:
            return None, "unknown secrets schema"
        if doc.get("node") != self.node_id:
            return None, "the secrets answer is for another node"
        got = doc.get("generation")
        if isinstance(got, bool) or not isinstance(got, int) or got != gen:
            return None, (f"secrets generation {str(got)[:20]} is not the manifest's {gen} "
                          "(a replay, or the controller is ahead of this manifest)")
        items = doc.get("items")
        if not isinstance(items, list) or not items or len(items) > _MAX_SECRET_ITEMS:
            return None, "the secrets answer carries no usable item list"
        now = self.runner.now()
        files = {}
        for item in items:
            item = item if isinstance(item, dict) else {}
            unit, path = item.get("unit"), item.get("path")
            if (not isinstance(unit, str) or not isinstance(path, str)
                    or not _SECRET_PATH_RE.match(path) or path not in units.get(unit, [])):
                return None, "the secrets answer carries a file the manifest does not name"
            if item.get("kind") not in _SECRET_KINDS:
                return None, f"{path}: not a kind of secret this agent writes"
            expires = item.get("expires_at")
            if isinstance(expires, bool) or not isinstance(expires, int) or expires <= now:
                return None, f"{path}: already expired, or no expiry"
            try:
                data = base64.b64decode(str(item.get("data_b64") or ""), validate=True)
            except ValueError:
                return None, f"{path}: not base64"
            if not data or len(data) > _MAX_SECRET_FILE_BYTES:
                return None, f"{path}: empty or over the size cap"
            if files.get(path, data) != data:
                return None, f"{path}: two items disagree on its content"
            files[path] = data
        return files, None

    def _write_secrets(self, files: dict):
        """Write the verified files: (paths whose content changed, None), or
        (None, why) when the secrets directory cannot be trusted or written."""
        base = self.secrets_dir
        changed = []
        try:
            if os.path.islink(base):
                return None, "the secrets directory is a symbolic link; nothing was written"
            os.makedirs(base, mode=0o700, exist_ok=True)
            os.chmod(base, 0o700)
            for path in sorted(files):
                parts = path.split("/")
                folder = base
                for seg in parts[:-1]:
                    folder = os.path.join(folder, seg)
                    if os.path.islink(folder):
                        return None, ("a directory under the secrets directory is a "
                                      "symbolic link; nothing more was written")
                    if not os.path.isdir(folder):
                        os.mkdir(folder, 0o700)
                    os.chmod(folder, 0o700)
                target = os.path.join(folder, parts[-1])
                if os.path.islink(target):
                    os.remove(target)  # never write through a link someone left here
                if _read_bytes(target, _MAX_SECRET_FILE_BYTES) != files[path]:
                    _write_private(target, files[path])
                    changed.append(path)
                else:
                    os.chmod(target, 0o600)
        except OSError as exc:
            return None, f"the secrets could not be written ({exc.__class__.__name__})"
        return changed, None

    def prune_secrets(self, man, mode: str, state: dict) -> list:
        """Remove every file under the secrets directory that the accepted
        manifest does not name. Returns the removed paths. Below enforce-absent,
        or without an accepted manifest, nothing is removed."""
        if man is None or MODES.index(mode) < R_ABSENT:
            return []
        if os.path.islink(self.secrets_dir) or not os.path.isdir(self.secrets_dir):
            return []
        _, units, problem = manifest_secrets(man)
        if problem:
            return []  # cannot tell what it names: remove nothing
        wanted = {p for paths in units.values() for p in paths}
        removed = []
        for folder, dirs, names in os.walk(self.secrets_dir, topdown=False):
            rel_dir = os.path.relpath(folder, self.secrets_dir).replace(os.sep, "/")
            for name in names + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if rel not in wanted:
                    os.remove(os.path.join(folder, name))
                    removed.append(rel)
            if folder != self.secrets_dir and not os.listdir(folder):
                os.rmdir(folder)
        held = state.get("secrets") if isinstance(state.get("secrets"), dict) else None
        if held is not None and not wanted:
            state.pop("secrets", None)
            self._save_state(state)
        return sorted(removed)

    def pull_secrets(self, man, key, state: dict, mode: str, reason=None) -> dict:
        """Fetch and write the secrets the accepted manifest names, when this node
        may receive any. Returns the report's `secrets` block: names, never a
        value, a hash or a size. Any failure writes nothing and keeps what is
        there."""
        out = {"ready": False, "reason": reason, "generation": None, "units": {}}
        if man is None:
            return out
        gen, units, problem = manifest_secrets(man)
        out["generation"] = gen

        def done(why=None):
            out["units"] = {u: {"ok": not self._secrets_missing(man, u, state),
                                "files": list(paths)} for u, paths in sorted(units.items())}
            out["reason"] = why
            out["ready"] = why is None and all(v["ok"] for v in out["units"].values())
            return out

        if problem:
            return done(problem)
        refusal = self._secrets_refusal(mode)
        if refusal:
            return done(refusal)
        if not units:
            return done()
        wanted = sorted({p for paths in units.values() for p in paths})
        if self._secrets_current(gen, wanted, state):
            return done()
        url = f"{self.url}/secrets"
        try:
            headers = request_headers(key, self.node_id, "GET", url, b"",
                                      int(self.runner.now()))
            status, body = self.runner.get(url, headers, _MAX_SECRETS_BYTES, pin=self.pin,
                                           ca_file=self.ca_file)
        except Exception as exc:  # the text may quote the exchange: the class only
            return done(f"secrets fetch failed ({exc.__class__.__name__}); nothing written")
        if int(status) != 200:
            return done(f"controller answered HTTP {int(status)} for the secrets; "
                        "nothing written")
        files, why = self._parse_secrets(body or b"", key, gen, units)
        if files is None:
            return done(f"secrets refused: {why}; nothing written")
        absent = [p for p in wanted if p not in files]
        changed, why = self._write_secrets(files)
        if changed is None:
            return done(why)
        state["secrets"] = {"generation": gen, "files": {
            p: hashlib.sha256(files[p]).hexdigest() for p in sorted(files)}}
        touched = sorted(u for u, paths in units.items() if set(paths) & set(changed))
        if touched:
            state["secrets_restart"] = sorted(
                set(touched) | set(state.get("secrets_restart") or []))
        self._save_state(state)
        if absent:
            return done(f"the controller did not send {len(absent)} file(s) the manifest "
                        "names")
        return done()

    # ── edge ────────────────────────────────────────────────────────────

    @staticmethod
    def _edge_blank(man, reason=None) -> dict:
        """The report's `edge` block before anything is known to be applied."""
        edge = man.get("edge") if man is not None and isinstance(man.get("edge"), dict) else {}
        return {"applied": False, "reason": reason,
                "generation": man["generation"] if man is not None else None,
                "ready": {"image": False, "network": False, "runtime": False},
                "egress": len(edge["egress"]) if isinstance(edge.get("egress"), list) else 0,
                "ingress": len(edge["ingress"]) if isinstance(edge.get("ingress"), list) else 0,
                "entries": {"egress": [], "ingress": []},
                "removed": [], "foreign": [],
                "hook": {"configured": False, "ok": None}}

    @staticmethod
    def _edge_rows(edge: dict):
        """One report row per manifest entry, plus (for a valid entry) the entry
        itself. Only checked values are echoed: a hostile entry is reported as
        refused, never copied into a report or a file."""
        egress, ingress = [], []
        for ent in edge["egress"] if isinstance(edge.get("egress"), list) else []:
            problem = edge_egress_problem(ent)
            ok = problem is None
            egress.append(({"name": ent["name"] if ok else None,
                            "listen_port": ent["listen_port"] if ok else None,
                            "target_ip": ent["target_ip"] if ok else None,
                            "target_port": ent["target_port"] if ok else None,
                            "unit": None, "state": "pending" if ok else "refused",
                            "reason": problem, "reachable": None}, ent if ok else None))
        for ent in edge["ingress"] if isinstance(edge.get("ingress"), list) else []:
            problem = edge_ingress_problem(ent)
            ok = problem is None
            ingress.append(({"port": ent["port"] if ok else None,
                             "via": ent["via"] if ok else None, "unit": None,
                             "state": "pending" if ok else "refused", "reason": problem,
                             "allow_enforced": bool(ok and ent["via"] == "relay"),
                             "allow": sorted(set(ent["allow"])) if ok else []},
                            ent if ok else None))
        return egress, ingress

    def _edge_ready(self, edge: dict, state: dict, needs_network: bool,
                    probe: bool = True):
        """(ready, reason): whether this node can build a forwarder at all. Checked
        before anything is written, so a node that cannot is said so by the report
        while every service is still where it was. `needs_network`: the manifest
        asks for an egress forwarder, which joins the network.

        ``probe=False`` answers the same question WITHOUT side effects: no pull, no
        probe run, no relay write and no state save. A node that applies no edge
        entry (mode observe or enforce-absent) still reports this block, so the
        operator can see whether it WOULD be able to carry one -- and a node in any
        mode can answer for its forwarder image and network before a move toward it
        is opened."""
        ready = {"image": False, "network": False, "runtime": False}
        image, network = edge.get("image"), edge.get("network")
        if not isinstance(image, str) or not _EDGE_IMAGE_RE.match(image):
            return ready, ("the manifest pins no forwarder image (edge.image is not "
                           "<ref>@sha256:<digest>)")
        if not isinstance(network, str) or not _EDGE_NAME_RE.match(network):
            return ready, "the manifest names no forwarder network (edge.network)"
        if not self.runner.which("podman"):
            return ready, "this node has no podman"
        ready["runtime"] = True
        ready["network"] = os.path.isfile(os.path.join(self.quadlet_dir, network + ".network"))
        if needs_network and not ready["network"] and probe:
            return ready, f"no {network}.network quadlet in {self.quadlet_dir}"
        if any(c.isspace() for c in self.relay_file + self.agent_file):
            return ready, "the agent's own path contains white space"
        if not probe:
            # A read-only presence check and nothing else: readiness is REPORTED in
            # every mode, so it may not pull, run or write anything.
            rc, _, _ = self.runner.run(["podman", "image", "exists", image], timeout=30)
            if rc != 0:
                return ready, "the forwarder image is not present on this node"
            ready["image"] = True
            return ready, None
        rc, _, _ = self.runner.run(["podman", "image", "exists", image], timeout=30)
        if rc != 0:
            rc, _, err = self.runner.run(["podman", "pull", image], timeout=600)
            if rc != 0:
                return ready, f"the forwarder image could not be pulled: {err.strip()[:120]}"
        if state.get("edge_ready") != image:
            rc, _, _ = self.runner.run(
                ["podman", "run", "--rm", "--network", "none", "--entrypoint", "python3",
                 image, "-c", _EDGE_PROBE], timeout=120)
            if rc != 0:
                return ready, ("the forwarder image cannot run the relay (its python3 "
                               "failed the import probe)")
            state["edge_ready"] = image
            self._save_state(state)
        src = _read_bytes(self.agent_file, _MAX_MANIFEST_BYTES)
        if src is None:
            return ready, "the agent file could not be read for the forwarder"
        try:
            os.makedirs(self.edge_dir, mode=0o755, exist_ok=True)
            if _read_bytes(self.relay_file, _MAX_MANIFEST_BYTES) != src:
                _write_atomic(self.relay_file, src, 0o644)
        except OSError as exc:
            return ready, f"the relay file could not be written ({exc.__class__.__name__})"
        ready["image"] = True
        return ready, None

    def _edge_stop(self, unit: str, states):
        """Stop one of this agent's edge units; None, or why it is still up."""
        if states is not None and self._unit(unit, states)[1] not in _LIVE:
            return None
        rc, _, err = self.runner.run(["systemctl", "stop", unit], timeout=120)
        return None if rc == 0 else f"stop {unit} failed: {err.strip()[:120]}"

    def _edge_remove(self, keep, states):
        """Stop and delete every edge unit file this agent owns that is not in
        `keep` (full paths). (removed names, foreign names, problems). A file
        under the prefix that lacks the marker is foreign and is not touched."""
        removed, foreign, problems = [], [], []
        for folder, (prefix, suffix) in zip((self.quadlet_dir, self.systemd_dir), _EDGE_KINDS):
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for fname in names:
                path = os.path.join(folder, fname)
                if not (fname.startswith(prefix) and fname.endswith(suffix)) or path in keep:
                    continue
                if not edge_owned(path):
                    foreign.append(fname)
                    continue
                problem = self._edge_stop(fname[:-len(suffix)] + ".service", states)
                if problem is None:
                    try:
                        os.remove(path)
                        removed.append(fname)
                        continue
                    except OSError as exc:
                        problem = f"remove {fname} failed ({exc.__class__.__name__})"
                problems.append(problem)
        return removed, foreign, problems

    def _edge_hook(self, state: dict, changed: bool):
        """Run the local hook when an edge unit changed, or when its last run
        failed. (ok | None when it did not run, problem | None)."""
        if not self.edge_hook or not (changed or state.get("edge_hook_pending")):
            if changed and state.pop("edge_hook_pending", None):
                self._save_state(state)
            return None, None
        rc, _, _ = self.runner.run([self.edge_hook], timeout=_EDGE_HOOK_TIMEOUT)
        pending = rc != 0
        if bool(state.get("edge_hook_pending")) != pending:
            if pending:
                state["edge_hook_pending"] = True
            else:
                state.pop("edge_hook_pending", None)
            self._save_state(state)
        return not pending, ("edge hook failed" if pending else None)

    def _name_holders(self, rows):
        """{container name: (owning unit or None, running)} this node declares, from
        every container its runtimes know about (running OR NOT), every quadlet's
        `ContainerName=`, and every local unit name that implies one. None when that
        cannot be established, in which case no name is proved free. `podman ps` lists
        running containers only: a crashed service is exactly the container a forwarder
        must not take the name of."""
        if rows is None:
            return None
        holders = {}
        for row in rows:
            name = row.get("name")
            if name:
                holders[name] = (row.get("unit"), True)
        # `podman ps` above lists what is RUNNING; this lists what is stopped too, in
        # one place for both runtimes (``strict``: a silent installed runtime proves
        # no name free).
        got, _ = inventory(self.runner, all_containers=True, strict=True)
        if got is None:
            return None
        for row in got:
            name = row.get("name")
            if name and name not in holders:
                holders[name] = (row.get("unit"), _row_running(row))
        declared = _quadlet_names(self.quadlet_dir)
        if declared is None:
            return None
        for name, unit in declared.items():
            if name not in holders:
                holders[name] = (unit, False)
        states = self.unit_states()
        if states is None:
            return None
        for unit, (_load, st) in states.items():
            if unit.startswith((_EDGE_OUT, _EDGE_IN)):
                continue          # this agent's own relays never hold a service name
            implied = _implied_container(unit)
            if implied and implied not in holders:
                holders[implied] = (unit, st in _LIVE)
        return holders

    @staticmethod
    def _name_taken(holders: dict, name: str, our_unit: str, absent: set) -> bool:
        """True when an egress forwarder must not carry `name`. Only this node's own
        declarations can prove the name free: the forwarder itself, or a unit the same
        manifest lists absent whose container is down (the move took the name with it)."""
        if name not in holders:
            return False
        holder, running = holders[name]
        if holder == our_unit:
            return False
        return not (isinstance(holder, str) and holder in absent and not running)

    def _published_ingress(self, man: dict, ent: dict):
        """(state, reason) for one `via: published` ingress entry. Nothing is created
        for it, so `published` is said only when this node SEES the port answering on
        the address the manifest names. A node whose manifest declares a workspace is
        refused: its unit publishes on the fleet address and this agent filters nothing
        there, so every node on the overlay could reach the service."""
        workspace = man.get("workspace")
        if isinstance(workspace, str) and workspace and workspace != "platform":
            return ("refused",
                    f"a published ingress on workspace {workspace[:64]!r} has no allow "
                    "list this node can enforce")
        listen_ip, port = ent.get("listen_ip"), ent["port"]
        if not _ip(listen_ip):
            return "refused", "listen_ip is not an IP address"
        if not self.runner.tcp_ok(listen_ip, port):
            return "pending", f"nothing answers on {listen_ip}:{port} yet"
        return "published", None

    def apply_edge(self, man, mode, state, rows, reason=None) -> dict:
        """Make this node's forwarders match the manifest's edge entries, as far as
        `mode` allows, and say what is. See EDGE in the module docstring; `rows` is
        the container inventory taken AFTER the converge step."""
        out = self._edge_blank(man, reason)
        out["hook"]["configured"] = bool(self.edge_hook)
        if man is None:
            return out
        edge = man.get("edge") if isinstance(man.get("edge"), dict) else {}
        egress, ingress = self._edge_rows(edge)
        out["entries"] = {"egress": [r for r, _ in egress], "ingress": [r for r, _ in ingress]}
        if MODES.index(mode) < R_EDGE:
            # Nothing is applied in this mode, but the operator still reads here
            # whether this node could carry an entry at all: readiness is probed
            # without pulling, running or writing anything.
            out["ready"], _why = self._edge_ready(
                edge, state, any(ent is not None for _row, ent in egress), probe=False)
            out["reason"] = f"mode {mode} applies no edge entry"
            return out
        if len(egress) + len(ingress) > _MAX_EDGE_ENTRIES:
            out["reason"] = f"more than {_MAX_EDGE_ENTRIES} edge entries: none applied"
            return out
        out["ready"], problem = self._edge_ready(
            edge, state, any(ent is not None for _row, ent in egress))
        if problem:
            out["reason"] = problem
            return out
        states = self.unit_states()
        if states is None:
            out["reason"] = "unit state unknown (systemctl did not answer)"
            return out
        image, network = edge["image"], edge["network"]

        # What should exist: path -> [unit, text, spec, row, container name or None]
        desired = {}
        for row, ent in egress:
            if ent is None:
                continue
            fname, unit, text, spec = edge_egress_unit(ent, image, network, self.relay_file)
            path = os.path.join(self.quadlet_dir, fname)
            row["unit"] = unit
            if path in desired:
                row.update(state="refused", reason="the same name and port is listed twice")
                continue
            desired[path] = [unit, text, spec, row, ent["name"]]
        for row, ent in ingress:
            if ent is None:
                continue
            if ent["via"] == "published":
                # Nothing is created for such an entry, so nothing here can vouch for
                # it: it is reported applied only when the port is seen answering, and
                # refused outright on a node whose traffic nothing but the overlay
                # reaches.
                state_word, why = self._published_ingress(man, ent)
                row.update(state=state_word, reason=why)
                continue
            fname, unit, text, spec = edge_ingress_unit(ent, image, network, sys.executable,
                                                        self.agent_file)
            path = os.path.join(self.systemd_dir, fname)
            row["unit"] = unit
            if path in desired:
                row.update(state="refused", reason="the same port is listed twice")
                continue
            desired[path] = [unit, text, spec, row, None]

        # Units the manifest no longer lists go first: a dropped forwarder frees its
        # name or port before a new one is started.
        out["removed"], out["foreign"], problems = self._edge_remove(set(desired), states)
        changed = bool(out["removed"])
        reload_needed = changed
        starts = state.get("edge_starts") if isinstance(state.get("edge_starts"), dict) else {}
        before = json.dumps(starts, sort_keys=True)
        to_start = []
        holders = None
        absent = {ent.get("unit") for ent in man.get("absent") or [] if isinstance(ent, dict)}
        for path in sorted(desired):
            unit, text, spec, row, name = desired[path]
            if os.path.lexists(path) and not edge_owned(path):
                row.update(state="blocked", reason="a unit by this name exists that this "
                                                   "agent did not create")
                continue
            current = _read_bytes(path, _MAX_UNIT_FILE_BYTES)
            same = current is not None and current.decode("utf-8", "replace") == text
            st = self._unit(unit, states)[1]
            if same and st == "active":
                row.update(state="active")
                starts.pop(unit, None)
                continue
            if same and st in ("activating", "reloading"):
                row.update(state="pending", reason=f"unit is {st}")
                continue
            if name is not None:
                if holders is None:
                    holders = self._name_holders(rows)
                if holders is None:
                    row.update(state="blocked", reason="container inventory unknown: cannot "
                                                       "prove the name is free")
                    continue
                if self._name_taken(holders, name, unit, absent):
                    row.update(state="blocked", reason="a local container still holds the name")
                    continue
            tried = starts.get(unit) if isinstance(starts.get(unit), dict) else {}
            count = tried.get("n", 0) if tried.get("spec") == spec else 0
            if count >= MAX_START_ATTEMPTS:
                row.update(state="failed", reason=f"start already tried {MAX_START_ATTEMPTS} "
                                                  "times for this entry")
                continue
            if not same:
                try:
                    _write_atomic(path, text.encode("utf-8"), 0o644)
                except OSError as exc:
                    row.update(state="failed",
                               reason=f"unit file could not be written ({exc.__class__.__name__})")
                    continue
                changed = reload_needed = True
            starts[unit] = {"spec": spec, "n": count + 1}
            to_start.append((unit, row, "start" if same else "restart"))
        if reload_needed:
            rc, _, err = self.runner.run(["systemctl", "daemon-reload"], timeout=120)
            if rc != 0:
                problems.append(f"daemon-reload failed: {err.strip()[:120]}")
                for _unit_name, row, _verb in to_start:
                    row.update(state="failed", reason="daemon-reload failed")
                to_start = []
        if to_start:
            state["edge_starts"] = starts
            self._save_state(state)  # the attempt is on record before it is made
        for unit, row, verb in to_start:
            changed = True
            rc, _, err = self.runner.run(["systemctl", verb, unit], timeout=180)
            if rc != 0:
                row.update(state="failed", reason=f"{verb} failed: {err.strip()[:140]}")
                continue
            rc, text, _ = self.runner.run(["systemctl", "is-active", unit], timeout=20)
            word = text.strip().splitlines()[0].strip() if text.strip() else ""
            if word == "active":
                row.update(state="active")
                starts.pop(unit, None)
            elif word in ("activating", "reloading"):
                row.update(state="pending", reason=f"unit is {word}")
            else:
                row.update(state="failed", reason=f"unit is {word or 'unknown'} after {verb}")
        if json.dumps(starts, sort_keys=True) != before:
            state["edge_starts"] = starts
            self._save_state(state)

        out["hook"]["ok"], hook_problem = self._edge_hook(state, changed)
        if hook_problem:
            problems.append(hook_problem)
        for row, ent in egress:
            if row["state"] == "active":
                row["reachable"] = bool(self.runner.tcp_ok(ent["target_ip"], ent["target_port"]))
        not_up = [r for r, _ in egress + ingress if r["state"] not in ("active", "published")]
        if not_up:
            problems.append(f"{len(not_up)} of {len(egress) + len(ingress)} edge entries "
                            "are not applied")
        out["applied"] = not problems
        out["reason"] = "; ".join(problems) or None
        return out

    def clear_edge(self) -> dict:
        """Stop and remove EVERY edge unit this agent owns (the `edge-clear` verb;
        `awnix fleet leave` runs it). Files under the prefix without the marker
        are listed as foreign and left alone."""
        removed, foreign, problems = self._edge_remove(set(), self.unit_states())
        if removed:
            rc, _, err = self.runner.run(["systemctl", "daemon-reload"], timeout=120)
            if rc != 0:
                problems.append(f"daemon-reload failed: {err.strip()[:120]}")
            if self.edge_hook:
                rc, _, _ = self.runner.run([self.edge_hook], timeout=_EDGE_HOOK_TIMEOUT)
                if rc != 0:
                    problems.append("edge hook failed")
        return {"removed": removed, "foreign": foreign, "problems": problems}

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
                # A forwarder image proven once stays proven, and a hook that failed
                # is still owed; edge start attempts begin again like `starts`.
                kept = {k: state[k] for k in ("edge_ready", "edge_hook_pending", "secrets",
                                              "secrets_restart") if k in state}
                state = {"node": self.node_id, "generation": gen, "sig": sig,
                         "accepted_generation": gen, "accepted_sig": sig, "starts": {},
                         # What this agent masked outlives a generation: only a
                         # manifest that lists the unit present again undoes it.
                         "masked": _masked(state),
                         "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                state.update(kept)
                dirty = True
            try:
                if dirty:
                    self._save_state(state)
            except OSError as exc:
                # A floor that cannot be recorded is a replay window: hold.
                man, mode = None, "observe"
                reason = f"state.json cannot be written ({exc.__class__.__name__})"
        edge_rows = rows
        try:
            # Before converge: a unit is started only with its secrets on disk.
            secrets_report = self.pull_secrets(man, key, state, mode, reason)
        except Exception as exc:  # a delivery fault must never stop the converge
            secrets_report = {"ready": False, "generation": None, "units": {},
                              "reason": f"secrets pull failed ({exc.__class__.__name__})"}
        if man is not None and "secrets" in man:
            # Only a node whose manifest names secrets logs about them, and only
            # transitions: unit names and a reason, never a value.
            line = "secrets: " + (secrets_report["reason"] or
                                  f"generation {secrets_report['generation']} on disk")
            if line != self._secrets_line:
                self.log(line)
                self._secrets_line = line
        if man is not None:
            hold_absent = None
            edge = man.get("edge") if isinstance(man.get("edge"), dict) else {}
            if MODES.index(mode) >= R_EDGE and edge.get("egress"):
                # This generation takes a name off this node. If no forwarder can be
                # built here the local copy is all that answers, so it is not stopped:
                # a move that cannot be routed holds instead of going dark.
                _ready, problem = self._edge_ready(edge, state, True)
                if problem:
                    hold_absent = f"this node cannot route the names it would give up: {problem}"
            verdicts = self.converge(man, mode, self.unit_states(), state,
                                     hold_absent=hold_absent)
            if any(v["action"] != "none" for v in verdicts):
                # A unit was stopped or started: the name it held may be free now.
                edge_rows = inventory(self.runner)[0]
            try:
                # After converge: a unit the manifest dropped is already stopped.
                secrets_report["removed"] = self.prune_secrets(man, mode, state)
            except OSError as exc:
                secrets_report["ready"] = False
                secrets_report["reason"] = ("a secret the manifest no longer names could "
                                            f"not be removed ({exc.__class__.__name__})")
        try:
            # After converge: a unit this manifest lists absent is already down.
            edge_report = self.apply_edge(man, mode, state, edge_rows, reason)
        except Exception as exc:  # an edge fault must never stop the report
            edge_report = self._edge_blank(
                man, f"edge apply failed: {exc.__class__.__name__}: {str(exc)[:120]}")

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
            # What this agent did about the manifest's edge entries and what each
            # one is now. `applied` is true only when every entry is up; the counts
            # are what the accepted manifest carries. The placer routes no name on
            # anything less.
            "edge": edge_report,
            # Delivered secrets: whether this node can receive any, and which units
            # hold theirs. Unit names and paths only.
            "secrets": secrets_report,
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


def _env_file(path: str) -> dict:
    """KEY=VALUE lines of an env file ({} when it cannot be read)."""
    values = {}
    raw = _read_bytes(path, 65536)
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


def edge_clear_main(argv) -> int:
    """`edge-clear [--env-file FILE]`: remove every edge unit this agent owns.
    The env file is the agent's own (agent.env), so the directories it was told
    to use are the ones that are cleared. Exit 0, or 1 when a unit is left."""
    args = list(argv)
    if args and (len(args) != 2 or args[0] != "--env-file"):
        print("[dgx-agent] usage: edge-clear [--env-file FILE]", flush=True)
        return 2
    env = _env_file(args[1]) if args else {}
    agent = PlacementAgent(
        root=env.get("AITHER_PLACEMENT_DIR") or None,
        quadlet_dir=env.get("AITHER_QUADLET_DIR") or None,
        systemd_dir=env.get("AITHER_SYSTEMD_DIR") or None,
        edge_hook=env.get("AITHER_PLACEMENT_EDGE_HOOK") if "AITHER_PLACEMENT_EDGE_HOOK" in env
        else None)
    result = agent.clear_edge()
    print(f"[dgx-agent] edge-clear: removed {len(result['removed'])} unit(s)"
          + (f", left {len(result['foreign'])} that are not this agent's"
             if result["foreign"] else "")
          + (f"; NOT DONE: {'; '.join(result['problems'])}" if result["problems"] else ""),
          flush=True)
    return 1 if result["problems"] else 0


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    # The two verbs that are not the agent loop. Neither reads a key or a sink.
    if argv and argv[0] == "edge-relay":
        raise SystemExit(edge_relay_main(argv[1:]))
    if argv and argv[0] == "edge-clear":
        raise SystemExit(edge_clear_main(argv[1:]))
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
