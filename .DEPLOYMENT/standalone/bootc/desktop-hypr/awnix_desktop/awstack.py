"""awnix-agent-status -- the aw* daemons of this desktop session, in one honest line.

    awnix-agent-status               a readable table (the menu opens this in a terminal)
    awnix-agent-status --waybar      one JSON object for waybar's custom/agent module
    awnix-agent-status --json        the full report
    awnix-agent-status --build-proof start each daemon the way its unit does, in a
                                     scratch HOME, and prove it answers on loopback ONLY
    awnix-agent-status --self-test

The desktop runs these user units, every one bound to 127.0.0.1 on THIS user's ports
(60-awnix-ports; base = 10000 + (uid - 1000) * 4 for uid 1000..5999):

    model    awnix-model.service    base     the local model (a no-op until one is chosen)
    agent    awsh-agent.service     base+1   the agent daemon awsh talks to
    harness  awsh-harness.service   base+2   the harness daemon (drives coding shells)
    hub      awnix-hub.service      base+3   the one local MCP endpoint (awnode hub)

A port answers for this user only if this user holds it: where the kernel's socket
table says another uid owns the listener (it bound the port while ours was down), the
row is "down: held by uid N", never a borrowed "up".

The bar shows ONE of three states, and nothing else decides it:

    down     the agent or the harness daemon does not answer
    offline  both answer and nobody is signed in -- the default: everything is local
    up       both answer and this user signed in (`awnix-signin`)

The other rows are reported in the tooltip and never change the state: a desktop with
no model chosen yet is not broken, it is unconfigured, and the tooltip says which.

Every probe is a GET to a LOOPBACK /health and the code refuses any other host, so this
tool cannot be the thing that dials out. "Signed in" is read from the presence of a
saved credential (~/.aither/config.json); the value is never read into a report.

--build-proof is what the image build runs (Containerfile.awnix-hypr). A unit file that
parses is not a daemon that starts: it takes each unit's own ExecStartPre/ExecStart and
Environment=, runs them in a scratch HOME, waits for /health, and then reads the kernel's
listener table (/proc/net/tcp, tcp6) to prove the port is bound to loopback and nothing
else. Exit 0 proven * 1 a daemon did not start or bound wider * 2 could not judge.

Stdlib only, Python 3.10-compatible (the repo checker imports it on 3.10).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

UNIT_DIR = Path(os.environ.get("AWNIX_USER_UNIT_DIR", "/usr/lib/systemd/user"))
MODEL_LINK = ".local/share/awnix/models/current.gguf"

#: name -> (unit, port variable, label, required). `required` daemons decide the state.
DAEMONS: Tuple[Tuple[str, str, str, str, bool], ...] = (
    ("model", "awnix-model.service", "AWNIX_MODEL_PORT", "local model", False),
    ("agent", "awsh-agent.service", "AWNIX_AGENT_PORT", "agent daemon", True),
    ("harness", "awsh-harness.service", "AWNIX_HARNESS_PORT", "harness daemon", True),
    ("hub", "awnix-hub.service", "AWNIX_HUB_PORT", "MCP hub", False),
)
#: The per-uid port block -- the SAME formula as units/60-awnix-ports (a test holds them
#: together). Below 32768 so no ephemeral client port can be holding it.
PORT_BASE, PORT_UID_MIN, PORT_UID_MAX, PORT_STRIDE = 10000, 1000, 6000, 4
PORT_OFFSETS = {"AWNIX_MODEL_PORT": 0, "AWNIX_AGENT_PORT": 1, "AWNIX_HARNESS_PORT": 2,
                "AWNIX_HUB_PORT": 3}
#: The uid --build-proof starts the daemons' ports for (the build runs as root).
PROOF_UID = 1000


def ports_for_uid(uid: int) -> Optional[Dict[str, int]]:
    """This uid's ports, or None for a uid the desktop gives none (system, out of range)."""
    if not PORT_UID_MIN <= uid < PORT_UID_MAX:
        return None
    base = PORT_BASE + (uid - PORT_UID_MIN) * PORT_STRIDE
    return {var: base + off for var, off in PORT_OFFSETS.items()}


def current_ports(env: Optional[Dict[str, str]] = None,
                  uid: Optional[int] = None) -> Optional[Dict[str, int]]:
    """The ports the session was given (environment), else derived from the uid."""
    env = dict(os.environ) if env is None else env
    try:
        given = {var: int(env[var]) for var in PORT_OFFSETS}
        return given
    except (KeyError, ValueError):
        pass
    if uid is None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return None
        uid = getuid()
    return ports_for_uid(uid)
#: What --build-proof starts. The model unit needs a downloaded GGUF, which a build has
#: not got; its server is proven by the Containerfile's own `llama-server --version`.
PROVABLE = ("agent", "harness", "hub")
#: The awnode the image bakes from the component lock; the hub is its awnode.hub module.
HUB_PYTHON = Path(os.environ.get("AWNIX_HUB_PYTHON",
                                 "/usr/lib/awnix/components/awnode/current/bin/python"))
STATES = ("down", "offline", "up")
ICON = ""  # Font Awesome 6 Free: robot


class CannotJudgeError(RuntimeError):
    """A required input is unreadable. Exit 2."""


# ── probes ───────────────────────────────────────────────────────────────────────────

def is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def health_url(port: int) -> str:
    return "http://127.0.0.1:%d/health" % port


def answers(url: str, timeout: float = 1.5) -> bool:
    """True when a LOOPBACK url answers HTTP at all. Never dials another host."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not is_loopback_host(host):
        raise PermissionError("refusing to dial a non-loopback url: %s" % url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


#: awdk's local credential (adk/local_auth.py): the header, the file the daemon mints in
#: its HOME, and a GET route its auth middleware gates (not /health, not a shell page).
LOCAL_TOKEN_HEADER = "X-Aither-Local-Token"
LOCAL_TOKEN_FILE = Path(".aither") / "daemon-token"
GATED_ROUTE = "/agents"


def status_of(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: float = 5.0) -> Optional[int]:
    """The HTTP status a LOOPBACK url answers with, or None when nothing answers."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not is_loopback_host(host):
        raise PermissionError("refusing to dial a non-loopback url: %s" % url)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def local_auth_problem(port: int, home: Path,
                       probe: Callable[..., Optional[int]] = status_of) -> Optional[str]:
    """None when the started agent enforces its owner's credential: a caller without
    ~/.aither/daemon-token is refused (401) and the owner's token is let through. A
    drop-in setting AITHER_LOCAL_AUTH=required on an awdk that ignores it ships green
    without this, and any local user then drives the agent."""
    url = "http://127.0.0.1:%d%s" % (port, GATED_ROUTE)
    anon = probe(url)
    if anon != 401:
        return "answered %s to a caller without the owner's token on %s, not 401" % (
            anon, GATED_ROUTE)
    try:
        token = (home / LOCAL_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        return "minted no ~/%s" % LOCAL_TOKEN_FILE.as_posix()
    owner = probe(url, {LOCAL_TOKEN_HEADER: token})
    if owner is None or owner == 401 or owner >= 500:
        return "answered %s to the owner's own token on %s" % (owner, GATED_ROUTE)
    return None


#: The awsh the image installs (desktop-open: `npm ci` of the pinned lockfile).
AWSH_CLIENT = Path(os.environ.get(
    "AWNIX_AWSH_CLIENT", "/usr/lib/awsh-npm/node_modules/@aitherium/awsh/dist/client.js"))
#: awsh's OWN request path: GenesisClient.getDetailed builds its headers through
#: authHeaders() -> localDaemonToken(), exactly as the shell's local rung does.
_AWSH_RUNG_JS = (
    "const {pathToFileURL} = await import('node:url');"
    "const [client, base] = process.argv.slice(1);"
    "const m = await import(pathToFileURL(client).href);"
    "const r = await new m.GenesisClient(base).getDetailed('%s');"
    "console.log(JSON.stringify({status: (r && r.error) ? r.status : 200,"
    " sent: m.localDaemonToken(base) !== null}));" % GATED_ROUTE)


def awsh_rung_problem(port: int, home: Path, client: Optional[Path] = None,
                      run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """None when the INSTALLED awsh, running as the owner (HOME = the daemon's), reaches
    the started agent's gated route with 200 -- i.e. its local rung presents the token
    the agent now demands. An awsh that predates the token reads 401 and fails here."""
    client = AWSH_CLIENT if client is None else client
    if not client.is_file():
        return "awsh is not installed at %s" % client
    url = "http://127.0.0.1:%d" % port
    try:
        out = run(["node", "--input-type=module", "-e", _AWSH_RUNG_JS, str(client), url],
                  env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
                  cwd=str(home), check=False,
                  capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "awsh's local rung did not run: %s" % exc
    lines = (out.stdout or "").strip().splitlines()
    try:
        res = json.loads(lines[-1]) if lines else {}
    except ValueError:
        res = {}
    if out.returncode != 0 or not res:
        return "awsh's local rung crashed (exit %s): %s" % (
            out.returncode, ((out.stderr or "").strip().splitlines() or [""])[-1][:200])
    if not res.get("sent"):
        return "awsh withheld the owner's token from its own agent on :%d" % port
    if res.get("status") != 200:
        return "awsh's local rung got %s from the agent on %s, not 200" % (
            res.get("status"), GATED_ROUTE)
    return None


def signed_in(home: Optional[Path] = None) -> bool:
    """A saved account credential exists. The credential itself is never returned."""
    home = Path.home() if home is None else home
    try:
        doc = json.loads((home / ".aither" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and bool(str(doc.get("api_key") or "").strip())


def model_chosen(home: Optional[Path] = None) -> bool:
    home = Path.home() if home is None else home
    return (home / MODEL_LINK).exists()


def hub_shipped(python: Optional[Path] = None) -> bool:
    """The baked awnode carries awnode.hub (it arrived in awnode 0.3.1)."""
    python = HUB_PYTHON if python is None else python
    venv = python.parent.parent
    return any(venv.glob("lib/python*/site-packages/awnode/hub.py"))


# ── the verdict (pure) ───────────────────────────────────────────────────────────────

def verdict(up: Dict[str, bool], account: bool) -> str:
    """down | offline | up -- decided by the REQUIRED daemons and the sign-in only."""
    for name, _unit, _port, _label, required in DAEMONS:
        if required and not up.get(name, False):
            return "down"
    return "up" if account else "offline"


def listener_owner(port: int, proc_net: Optional[Path] = None) -> Optional[int]:
    """The uid holding the LISTEN socket on ``port``, or None when unknown."""
    proc_net = Path(os.environ.get("AWNIX_PROC_NET", "/proc/net")) if proc_net is None else proc_net
    for name in ("tcp", "tcp6"):
        try:
            rows = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 8 or cols[3] != "0A":
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) == port:
                    return int(cols[7])
            except ValueError:
                continue
    return None


def report(probe: Callable[[str], bool] = answers, home: Optional[Path] = None,
           hub: Optional[bool] = None, ports: Optional[Dict[str, int]] = None,
           owner: Callable[[int], Optional[int]] = listener_owner,
           uid: Optional[int] = None) -> Dict[str, Any]:
    home = Path.home() if home is None else home
    hub = hub_shipped() if hub is None else hub
    if uid is None:
        getuid = getattr(os, "getuid", None)
        uid = getuid() if getuid else None
    ports = current_ports(uid=uid) if ports is None else ports
    account = signed_in(home)
    rows = []
    up: Dict[str, bool] = {}
    for name, unit, var, label, required in DAEMONS:
        port = (ports or {}).get(var)
        holder = owner(port) if port else None
        if port is None:
            up[name] = False
            note = "no per-user port for uid %s: the desktop starts no daemon for it" % uid
        elif holder is not None and uid is not None and holder != uid:
            up[name] = False
            note = "port %d is held by uid %d, not yours: not trusted" % (port, holder)
        else:
            up[name] = bool(probe(health_url(port)))
            if name == "model" and not up[name] and not model_chosen(home):
                note = "none chosen yet (Super+/ then 'Choose the local model')"
            elif name == "hub" and not up[name] and not hub:
                note = "not in this image's awnode (needs awnode >= 0.3.1 in the component lock)"
            elif up[name]:
                note = "up on 127.0.0.1:%d" % port
            else:
                note = "not answering on 127.0.0.1:%d (systemctl --user status %s)" % (port, unit)
        rows.append({"name": name, "unit": unit, "port": port, "label": label,
                     "required": required, "up": up[name], "note": note})
    return {"schema": 1, "state": verdict(up, account), "signed_in": account, "daemons": rows}


def tooltip(rep: Dict[str, Any]) -> str:
    lines = ["%-12s %s" % (r["label"], r["note"]) for r in rep["daemons"]]
    lines.append("%-12s %s" % ("account", "signed in" if rep["signed_in"]
                               else "none -- offline, everything stays on this machine"))
    return "\n".join(lines)


def waybar(rep: Dict[str, Any]) -> Dict[str, str]:
    words = {"down": "down", "offline": "local", "up": "online"}
    return {"text": "%s %s" % (ICON, words[rep["state"]]), "class": rep["state"],
            "alt": rep["state"], "tooltip": tooltip(rep)}


# ── the build proof ──────────────────────────────────────────────────────────────────

def parse_unit(text: str) -> Dict[str, List[str]]:
    """[Service] keys of a unit file as key -> every value, in order."""
    out: Dict[str, List[str]] = {}
    section = ""
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service" or "=" not in line:
            continue
        key, _, val = line.partition("=")
        _assign(out, key.strip(), val.strip())
    return out


#: Keys whose EMPTY assignment means "reset the list" in systemd. Environment= is not
#: one: `Environment=AITHER_MCP_GATEWAY=` sets a variable, it is never an empty value.
_RESETTABLE = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition",
               "EnvironmentFile")


def _assign(out: Dict[str, List[str]], key: str, val: str) -> None:
    if val == "" and key in _RESETTABLE:
        out[key] = []
    else:
        out.setdefault(key, []).append(val)


def unit_service(unit: str, unit_dir: Optional[Path] = None) -> Dict[str, List[str]]:
    """The unit's [Service] keys with its drop-ins (<unit>.d/*.conf) applied in order."""
    unit_dir = UNIT_DIR if unit_dir is None else unit_dir
    path = unit_dir / unit
    try:
        merged = parse_unit(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CannotJudgeError("cannot read %s: %s" % (path, exc)) from None
    dropins = unit_dir / (unit + ".d")
    if dropins.is_dir():
        for f in sorted(dropins.glob("*.conf")):
            section = ""
            for line in f.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"):
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                if section == "Service" and "=" in line:
                    key, _, val = line.partition("=")
                    _assign(merged, key.strip(), val.strip())
    return merged


def unit_env(svc: Dict[str, List[str]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for val in svc.get("Environment", []):
        for item in shlex.split(val):
            key, sep, v = item.partition("=")
            if sep:
                env[key] = v
    return env


def unit_env_files(svc: Dict[str, List[str]], home: Path) -> Dict[str, str]:
    """KEY=VALUE lines of every EnvironmentFile= that exists ('-' = optional), in order."""
    env: Dict[str, str] = {}
    for val in svc.get("EnvironmentFile", []):
        path = Path(val.lstrip("-").replace("%h", str(home)))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, v = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                env[key] = v
    return env


def unit_argv(command: str, home: Path,
              env: Optional[Dict[str, str]] = None) -> Tuple[List[str], bool]:
    """(argv, may_fail) for one Exec line: a leading '-' means its failure is ignored.
    ``${VAR}`` expands from ``env`` the way systemd expands it (one word)."""
    may_fail = command.startswith("-")
    words = shlex.split(command.lstrip("-@+!").replace("%h", str(home)))
    if env is not None:
        words = [re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                        lambda m: env.get(m.group(1), ""), w) for w in words]
    return words, may_fail


def _decode(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, words)


def listeners(proc_net: Optional[Path] = None) -> Optional[List[Tuple[str, int]]]:
    """TCP LISTEN sockets as (address, port); None when the table cannot be read."""
    proc_net = Path(os.environ.get("AWNIX_PROC_NET", "/proc/net")) if proc_net is None else proc_net
    found: List[Tuple[str, int]] = []
    seen = False
    for name in ("tcp", "tcp6"):
        try:
            rows = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        seen = True
        for row in rows:
            cols = row.split()
            if len(cols) < 4 or cols[3] != "0A":
                continue
            addr, _, port = cols[1].partition(":")
            try:
                found.append((_decode(addr), int(port, 16)))
            except (ValueError, OSError):
                continue
    return found if seen else None


def bind_problem(table: Optional[Sequence[Tuple[str, int]]], port: int) -> Optional[str]:
    """None when `port` is bound to loopback only; otherwise why not. Raises when the
    listener table is unreadable -- an unread table is not a loopback bind."""
    if table is None:
        raise CannotJudgeError("cannot read the listener table (/proc/net/tcp)")
    mine = sorted({addr for addr, p in table if p == port})
    if not mine:
        return "nothing listens on :%d" % port
    wide = [a for a in mine if not is_loopback_host(a)]
    return ("bound %s -- not loopback only" % ", ".join(wide)) if wide else None


def build_proof(unit_dir: Optional[Path] = None, wait_s: float = 120.0,
                err: Any = None) -> int:
    err = sys.stderr if err is None else err
    home = Path(tempfile.mkdtemp(prefix="awnix-awstack-proof-"))
    failures: List[str] = []
    # The build runs as root, which the desktop gives no ports: prove the units on the
    # ports the first desktop user (uid 1000) gets, exactly as 60-awnix-ports emits them.
    ports = ports_for_uid(PROOF_UID) or {}
    port_env = {var: str(p) for var, p in ports.items()}
    port_env["AW_HUB_PORT"] = port_env.get("AWNIX_HUB_PORT", "")
    try:
        for name, unit, var, label, required in DAEMONS:
            if name not in PROVABLE:
                continue
            port = ports[var]
            svc = unit_service(unit, unit_dir)
            starts = svc.get("ExecStart") or []
            if len(starts) != 1:
                failures.append("%s: %d ExecStart lines" % (unit, len(starts)))
                continue
            env = {**os.environ, **port_env, **unit_env(svc), "HOME": str(home)}
            skip = None
            for cond in svc.get("ExecCondition", []):
                argv, _ = unit_argv(cond, home, env)
                try:
                    rc = subprocess.run(argv, env=env, cwd=str(home), check=False,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=60).returncode
                except OSError as exc:
                    rc = 127
                    err.write("awstack proof: %s ExecCondition: %s\n" % (unit, exc))
                if rc == 255 or rc < 0:
                    failures.append("%s: ExecCondition %s crashed (%d)" % (unit, argv[0], rc))
                if rc != 0:
                    skip = "ExecCondition %s exited %d" % (" ".join(argv), rc)
                    break
            if skip and required:
                failures.append("%s (%s) would be skipped at login: %s" % (unit, label, skip))
                continue
            if skip:
                # systemd SKIPS such a unit (never fails it); the build says so plainly.
                err.write("awstack proof: %s SKIPPED, not proven: %s\n" % (unit, skip))
                continue
            for pre in svc.get("ExecStartPre", []):
                argv, may_fail = unit_argv(pre, home, env)
                rc = subprocess.run(argv, env=env, cwd=str(home), check=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=120).returncode
                if rc != 0 and not may_fail:
                    failures.append("%s: ExecStartPre %s exited %d" % (unit, argv[0], rc))
            env.update(unit_env_files(svc, home))  # systemd reads them just before ExecStart
            argv, _ = unit_argv(starts[0], home, env)
            if "--port" in argv and argv[argv.index("--port") + 1:][:1] != [str(port)]:
                failures.append("%s: ExecStart does not bind this user's port %d: %s"
                                % (unit, port, " ".join(argv)))
                continue
            log_path = home / (name + ".log")
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(argv, env=env, cwd=str(home), stdout=log,
                                        stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + wait_s
                ok = False
                while time.monotonic() < deadline and proc.poll() is None:
                    if answers(health_url(port)):
                        ok = True
                        break
                    time.sleep(1.0)
                problem = None if ok else (
                    "exited %s before answering" % proc.returncode if proc.poll() is not None
                    else "no answer on 127.0.0.1:%d after %.0fs" % (port, wait_s))
                if ok:
                    problem = bind_problem(listeners(), port)
                if ok and problem is None and env.get("AITHER_LOCAL_AUTH") == "required":
                    problem = local_auth_problem(port, home)
                    if problem is None:
                        err.write("awstack proof: %s refuses a caller without the owner's "
                                  "token and admits the owner\n" % unit)
                    if problem is None and name == "agent":
                        problem = awsh_rung_problem(port, home)
                        if problem is None:
                            err.write("awstack proof: the installed awsh's local rung gets "
                                      "200 from %s with the owner's token\n" % unit)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=20)
            if problem:
                failures.append("%s (%s): %s" % (unit, label, problem))
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                err.write("---- %s log tail ----\n%s\n" % (unit, "\n".join(tail)))
            else:
                err.write("awstack proof: %s answers on 127.0.0.1:%d, loopback only\n"
                          % (unit, port))
    except CannotJudgeError as exc:
        err.write("awstack proof: could not judge: %s\n" % exc)
        return 2
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for f in failures:
        err.write("awstack proof FAIL: %s\n" % f)
    return 1 if failures else 0


# ── self-test ────────────────────────────────────────────────────────────────────────

def self_test() -> int:
    fails = 0

    def chk(cond: bool, label: str) -> None:
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        if not cond:
            fails += 1

    all_up = {n: True for n, *_ in DAEMONS}
    chk(verdict(all_up, False) == "offline", "daemons up, no account -> offline")
    chk(verdict(all_up, True) == "up", "daemons up, signed in -> up")
    chk(verdict({**all_up, "agent": False}, True) == "down", "agent down -> down, even signed in")
    chk(verdict({**all_up, "harness": False}, False) == "down", "harness down -> down")
    chk(verdict({**all_up, "model": False, "hub": False}, False) == "offline",
        "a daemon that is not required never decides the state")
    refused = False
    try:
        answers("http://203.0.113.7:9001/health")
    except PermissionError:
        refused = True
    chk(refused, "a non-loopback probe is refused, not dialled")
    chk(not answers("http://127.0.0.1:1/health", timeout=0.3), "a dead loopback port is down")
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        chk(not signed_in(home), "no config.json -> not signed in")
        (home / ".aither").mkdir()
        (home / ".aither" / "config.json").write_text('{"api_key": ""}', encoding="utf-8")
        chk(not signed_in(home), "an empty credential -> not signed in")
        (home / ".aither" / "config.json").write_text('{"api_key": "SELFTEST-CREDENTIAL"}',
                                                      encoding="utf-8")
        chk(signed_in(home), "a saved credential -> signed in")
        mine = ports_for_uid(1000) or {}
        live = (":%d/" % mine["AWNIX_AGENT_PORT"], ":%d/" % mine["AWNIX_HARNESS_PORT"])
        rep = report(lambda url: url.endswith("/health") and any(p in url for p in live),
                     home, hub=False, ports=mine, owner=lambda p: None, uid=1000)
        bar = waybar(rep)
        chk("needs awnode >= 0.3.1" in bar["tooltip"], "a lock without the hub is said plainly")
        chk(rep["state"] == "up" and bar["class"] == "up", "report + waybar agree on the state")
        chk("SELFTEST-CREDENTIAL" not in json.dumps(rep) + json.dumps(bar),
            "the credential is in neither the report nor the bar")
        chk("none chosen yet" in bar["tooltip"], "an unchosen model is said plainly")
        squat = report(lambda url: True, home, hub=True, ports=mine,
                       owner=lambda p: 1001, uid=1000)
        chk(squat["state"] == "down" and "held by uid 1001" in tooltip(squat),
            "another user's listener on my port is down, never a borrowed 'up'")
        none = report(lambda url: True, home, hub=True, ports=None, uid=0)
        chk(none["state"] == "down" and "no per-user port" in tooltip(none),
            "a uid with no port block probes nobody else's daemon")
        a, b = ports_for_uid(1000) or {}, ports_for_uid(1001) or {}
        chk(not set(a.values()) & set(b.values()) and max((ports_for_uid(5999) or {}).values())
            < 32768 and ports_for_uid(999) is None and ports_for_uid(6000) is None,
            "per-uid port blocks never overlap and stay below the ephemeral range")
        chk(current_ports({"AWNIX_MODEL_PORT": "1", "AWNIX_AGENT_PORT": "2",
                           "AWNIX_HARNESS_PORT": "3", "AWNIX_HUB_PORT": "4"}, uid=0)
            == {"AWNIX_MODEL_PORT": 1, "AWNIX_AGENT_PORT": 2, "AWNIX_HARNESS_PORT": 3,
                "AWNIX_HUB_PORT": 4}, "the session's own ports win over the derived ones")
        unit = home / "u.service"
        unit.write_text("[Unit]\nAfter=a\n[Service]\nEnvironment=A=1 B=2\n"
                        "ExecStartPre=-/bin/pre --q\nExecStart=/bin/x --host 127.0.0.1 %h/y\n",
                        encoding="utf-8")
        (home / "u.service.d").mkdir()
        (home / "u.service.d" / "10.conf").write_text("[Service]\nEnvironment=C=3\n",
                                                      encoding="utf-8")
        svc = unit_service("u.service", home)
        chk(unit_env(svc) == {"A": "1", "B": "2", "C": "3"}, "Environment= and drop-ins merge")
        chk(unit_argv(svc["ExecStartPre"][0], home) == (["/bin/pre", "--q"], True),
            "a '-' prefix marks an ignorable ExecStartPre")
        chk(unit_argv(svc["ExecStart"][0], home)[0][-1].endswith("y")
            and "%h" not in " ".join(unit_argv(svc["ExecStart"][0], home)[0]), "%h expands")
        (home / "u.service.d" / "20.conf").write_text(
            "[Service]\nEnvironment=G=\nExecStart=\nExecStart=/bin/z --port ${P}\n",
            encoding="utf-8")
        svc = unit_service("u.service", home)
        chk(svc["ExecStart"] == ["/bin/z --port ${P}"], "an empty ExecStart= resets the list")
        chk(unit_env(svc).get("G") == "", "Environment=G= sets G to empty, it resets nothing")
        chk(unit_argv(svc["ExecStart"][0], home, {"P": "10001"})[0] == ["/bin/z", "--port",
                                                                         "10001"],
            "${VAR} expands like systemd")
        pn = home / "net"
        pn.mkdir()
        (pn / "tcp").write_text(
            "sl local rem st\n 0: 0100007F:2329 00000000:0000 0A\n"
            " 1: 00000000:20AA 00000000:0000 0A\n", encoding="ascii")
        table = listeners(pn)
        chk(bind_problem(table, 9001) is None, "127.0.0.1:9001 is loopback only")
        chk("not loopback" in str(bind_problem(table, 8362)), "0.0.0.0:8362 is refused")
        chk("nothing listens" in str(bind_problem(table, 47933)), "an unbound port is refused")
        dead = False
        try:
            bind_problem(listeners(home / "nope"), 9001)
        except CannotJudgeError:
            dead = True
        chk(dead, "an unreadable listener table is could-not-judge, never a pass")
        (home / ".aither" / "daemon-token").write_text("SELFTEST-TOKEN\n", encoding="utf-8")

        def enforcing(url: str, headers: Optional[Dict[str, str]] = None) -> Optional[int]:
            return 200 if (headers or {}).get(LOCAL_TOKEN_HEADER) == "SELFTEST-TOKEN" else 401
        chk(local_auth_problem(9001, home, enforcing) is None,
            "an agent that refuses strangers and admits the owner passes")
        chk("not 401" in str(local_auth_problem(9001, home, lambda u, h=None: 200)),
            "an agent that ignores AITHER_LOCAL_AUTH (anonymous 200) fails the proof")
        chk("owner's own token" in str(local_auth_problem(9001, home, lambda u, h=None: 401)),
            "an agent that refuses its owner too fails the proof")
        chk("minted no" in str(local_auth_problem(9001, home / "nobody", enforcing)),
            "a daemon that minted no token fails the proof")
        fake_client = home / "client.js"
        fake_client.write_text("// stand-in\n", encoding="utf-8")

        def node(out: str, rc: int = 0) -> Callable[..., Any]:
            return lambda *a, **k: subprocess.CompletedProcess(a, rc, out, "boom")
        chk(awsh_rung_problem(9001, home, fake_client,
                              node('{"status": 200, "sent": true}\n')) is None,
            "an awsh whose local rung gets 200 with the token passes")
        chk("not 200" in str(awsh_rung_problem(9001, home, fake_client,
                                                node('{"status": 401, "sent": true}'))),
            "an awsh refused by its own agent fails the proof")
        chk("withheld" in str(awsh_rung_problem(9001, home, fake_client,
                                                 node('{"status": 401, "sent": false}'))),
            "an awsh that never sends the token fails the proof")
        chk("crashed" in str(awsh_rung_problem(9001, home, fake_client, node("", 1))),
            "an awsh that cannot run fails the proof, never passes")
        chk("not installed" in str(awsh_rung_problem(9001, home, home / "none.js")),
            "no installed awsh fails the proof")
        units = home / "units"
        units.mkdir()
        no = '"%s" -c "raise SystemExit(1)"' % sys.executable.replace("\\", "/")
        for _n, u, _p, _l, _r in DAEMONS:
            (units / u).write_text("[Service]\nExecCondition=%s\nExecStart=%s\n" % (no, no),
                                   encoding="utf-8")
        null = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        chk(build_proof(units, wait_s=1, err=null) == 1,
            "a required daemon its ExecCondition would skip fails the proof, never passes")
        null.close()
    print("awnix-agent-status self-test: %s" % ("PASS" if not fails else "FAIL (%d)" % fails))
    return 0 if not fails else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix-agent-status", description=__doc__.split("\n")[0])
    ap.add_argument("--waybar", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--build-proof", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.build_proof:
        return build_proof()
    rep = report()
    if a.waybar:
        print(json.dumps(waybar(rep)))
    elif a.json:
        print(json.dumps(rep, indent=2))
    else:
        print("agent: %s" % rep["state"])
        print(tooltip(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
