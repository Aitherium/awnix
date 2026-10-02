"""The console's privileged broker: a LITERAL argv allowlist.

This table is the whole attack surface of a root web server on a LAN, so it is data,
not code paths:

  * every command is an argv list -- no shell, ever;
  * a user value only ever fills a placeholder that is one WHOLE argv element
    ("{channel}"), after validation against an enum or a regex, or goes to stdin
    (the license envelope);
  * the child gets a scrubbed environment and a timeout;
  * reboot-scheduling verbs (update-apply, update-rollback) are started detached via
    `systemd-run --no-block`, so the HTTP answer never waits on a reboot.

Exit-code contract of every CLI in the program: 0 ok, 1 failed/refused, 2 could not
judge / offline, 3 not entitled (awnix component only). HTTP mapping: 0 -> 200,
1 -> 409, 2 -> 503, 3 -> 402, binary absent -> 501 {available:false}, timeout -> 504.

check_awnix_surfaces AWS006 imports ACTIONS and READS and asserts every binary+verb
here is listed by the owning CLI's `--list-verbs`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# A mesh enroll token (awnix-mesh join): opaque, bounded, and passed on STDIN (`join -`),
# never argv -- argv is visible in ps.
MESH_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{16,4096}$")
# An offline update bundle: an absolute path on removable media only. The CLI re-verifies
# signature, digest and anti-rollback before anything is staged (awnix-offline-update).
MEDIA_ROOTS = ("/run/media/", "/var/mnt/", "/media/")
MEDIA_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]{1,1024}$")
LICENSE_RE = re.compile(r"^AITHER1\.[A-Za-z0-9_-]+={0,2}\.[A-Za-z0-9_-]+={0,2}$")
MAX_LICENSE = 16 * 1024
TAIL = 4000

# param kinds: ("enum", (...)), ("component",), ("license",), ("mesh-token",),
# ("media-path",)
ACTIONS: Dict[str, Dict[str, Any]] = {
    "update-check": {"argv": ["awnix-update", "check"], "timeout": 900},
    "update-apply": {"argv": ["awnix-update", "apply"], "detached": True},
    "update-rollback": {"argv": ["awnix-update", "rollback"], "detached": True},
    "update-channel": {"argv": ["awnix-update", "channel", "{channel}"],
                       "params": {"channel": ("enum", ("stable", "beta"))}},
    "update-auto-apply": {"argv": ["awnix-update", "auto-apply", "{value}"],
                          "params": {"value": ("enum", ("on", "off"))}},
    "license-import": {"argv": ["aitheros", "license", "import", "-", "--json"],
                       "params": {"license": ("license",)}, "stdin": "license",
                       "timeout": 120},
    "license-refresh": {"argv": ["aitheros", "license", "refresh", "--json"],
                        "timeout": 120},
    "component-install": {"argv": ["awnix-component", "install", "{id}", "--json"],
                          "params": {"id": ("component",)}, "timeout": 1800},
    "component-remove": {"argv": ["awnix-component", "remove", "{id}", "--json"],
                         "params": {"id": ("component",)}, "timeout": 600},
    "component-rollback": {"argv": ["awnix-component", "rollback", "{id}", "--json"],
                           "params": {"id": ("component",)}, "timeout": 600},
    "endpoints-probe": {"argv": ["awnix-endpoints", "probe", "--json"], "timeout": 120},
    "setup-rerun": {"argv": ["awnix-setup", "--reset", "--yes"], "timeout": 60},
    # the mesh (awnix-mesh): join takes the token on stdin; leave/retry take nothing
    "mesh-join": {"argv": ["awnix-mesh", "join", "-"],
                  "params": {"token": ("mesh-token",)}, "stdin": "token", "timeout": 180},
    "mesh-leave": {"argv": ["awnix-mesh", "leave"], "timeout": 120},
    "mesh-retry": {"argv": ["awnix-mesh", "retry", "--quiet"], "timeout": 180},
    # offline signed updates from removable media (awnix-offline-update)
    "update-offline-scan": {"argv": ["awnix-offline-update", "scan", "--json"],
                            "timeout": 300},
    "update-offline-stage": {"argv": ["awnix-offline-update", "stage", "{bundle}", "--json"],
                             "params": {"bundle": ("media-path",)}, "timeout": 1800},
}

# GET /api/appliance/<section> -> the owning CLI's --json status.
READS: Dict[str, List[str]] = {
    "license": ["aitheros", "status", "--json"],
    "updates": ["awnix-update", "status", "--json"],
    "components": ["awnix-component", "list", "--json"],
    "endpoints": ["awnix-endpoints", "show", "--json"],
    "setup": ["awnix-setup", "--status", "--json"],
    "mesh": ["awnix-mesh", "status", "--json"],
    "renewal": ["awnix-renewal", "status", "--json"],
    "offline-update": ["awnix-offline-update", "status", "--json"],
}

EXIT_HTTP = {0: (200, "ok"), 1: (409, "refused"), 2: (503, "unavailable"),
             3: (402, "not-entitled")}

_REDACT = [
    (re.compile(r"AITHER1\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+"), "AITHER1.<redacted>"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), "<redacted-token>"),
    (re.compile(r"(x-access-token:)[^\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(\"(?:token|auth|password|secret)\"\s*:\s*\")[^\"]*"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


def resolve_tool(name: str, dirs: List[str]) -> Optional[List[str]]:
    """argv prefix that runs `name`, or None when no candidate exists.

    Dev/test only: on Windows a `<name>.py` stub is run with this interpreter because
    Windows cannot exec a shebang. On a box, the name is an executable file."""
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p) and (os.name == "nt" or os.access(p, os.X_OK)):
            if os.name == "nt":
                return [sys.executable, p]
            return [p]
        if os.name == "nt" and os.path.isfile(p + ".py"):
            return [sys.executable, p + ".py"]
    return None


def lock_ids(lock_path: str) -> Optional[set]:
    try:
        with open(lock_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    comps = data.get("components") if isinstance(data, dict) else None
    if not isinstance(comps, list):
        return None
    return {c.get("id") for c in comps if isinstance(c, dict) and isinstance(c.get("id"), str)}


class RefusalError(Exception):
    def __init__(self, http: int, detail: str) -> None:
        super().__init__(detail)
        self.http = http
        self.detail = detail


def validate(verb: str, body: Dict[str, Any], lock_path: str) -> Dict[str, str]:
    spec = ACTIONS[verb]
    out: Dict[str, str] = {}
    for name, kind in spec.get("params", {}).items():
        val = body.get(name)
        if not isinstance(val, str):
            raise RefusalError(422, f"'{name}' is required and must be a string")
        if kind[0] == "enum":
            if val not in kind[1]:
                raise RefusalError(422, f"'{name}' must be one of {', '.join(kind[1])}")
        elif kind[0] == "component":
            if not ID_RE.match(val):
                raise RefusalError(422, f"'{name}' is not a valid component id")
            ids = lock_ids(lock_path)
            if ids is None:
                raise RefusalError(503, "the component lock is unreadable; cannot judge the id")
            if val not in ids:
                raise RefusalError(422, f"component '{val}' is not in this image's lock")
        elif kind[0] == "mesh-token":
            if not MESH_TOKEN_RE.match(val):
                raise RefusalError(422, f"'{name}' is not a mesh enroll token")
        elif kind[0] == "media-path":
            real = os.path.normpath(val)
            if (not MEDIA_PATH_RE.match(val) or ".." in val.split("/")
                    or not real.replace("\\", "/").startswith(MEDIA_ROOTS)):
                raise RefusalError(422, f"'{name}' must be a file on removable media "
                                        f"({', '.join(r.rstrip('/') for r in MEDIA_ROOTS)})")
            if os.name != "nt" and os.path.realpath(val) != real:
                raise RefusalError(422, f"'{name}' must not traverse a symlink")
        elif kind[0] == "license":
            if len(val.encode("utf-8")) > MAX_LICENSE:
                raise RefusalError(413, "license envelope exceeds 16 KiB")
            if not LICENSE_RE.match(val.strip()):
                raise RefusalError(422, "not an AITHER1.<payload>.<signature> license envelope")
            val = val.strip()
        out[name] = val
    return out


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/root",
           "AWNIX_CALLER": "awnix-console"}
    if os.name == "nt":
        for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP"):
            if k in os.environ:
                env[k] = os.environ[k]
    if extra:
        env.update(extra)
    return env


def envelope(verb: str, http: int, state: str, *, exit_code: Optional[int] = None,
             result: Any = None, stdout: str = "", detail: str = "",
             available: bool = True) -> Tuple[int, Dict[str, Any]]:
    body: Dict[str, Any] = {"verb": verb, "exit": exit_code, "state": state,
                            "result": result, "stdout_tail": redact(stdout[-TAIL:])}
    if detail:
        body["detail"] = redact(detail[-TAIL:])
    if not available:
        body["available"] = False
    return http, body


def run_argv(verb: str, argv: List[str], dirs: List[str], *, stdin: Optional[str] = None,
             timeout: int = 60, detached: bool = False,
             extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    prefix = resolve_tool(argv[0], dirs)
    if prefix is None:
        return envelope(verb, 501, "unavailable", detail=f"{argv[0]} is not installed",
                        available=False)
    cmd = prefix + argv[1:]
    if detached:
        sr = resolve_tool("systemd-run", dirs + ["/usr/bin", "/bin"])
        if sr is None:
            return envelope(verb, 501, "unavailable", detail="systemd-run is not available",
                            available=False)
        unit = f"awnix-console-{verb}-{int(time.time())}"
        cmd = sr + ["--no-block", "--collect", "--unit", unit,
                    "--description", f"awnix-console {verb}"] + cmd
    try:
        io: Dict[str, Any] = ({"input": stdin} if stdin is not None
                              else {"stdin": subprocess.DEVNULL})
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=child_env(extra_env),
            shell=False,
            check=False,
            errors="replace",
            **io,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return envelope(verb, 504, "error", exit_code=None, stdout=out or "",
                        detail=f"timed out after {timeout}s")
    except OSError as exc:
        return envelope(verb, 501, "unavailable", detail=f"cannot execute: {exc}",
                        available=False)
    rc = proc.returncode
    http, state = EXIT_HTTP.get(rc, (500, "error"))
    result: Any = None
    text = proc.stdout.strip()
    if text:
        try:
            result = json.loads(text)
        except ValueError:
            # Some CLIs print progress lines before the final JSON document.
            last = text.rsplit("\n", 1)[-1]
            try:
                result = json.loads(last)
            except ValueError:
                result = None
    if detached and rc == 0:
        result = result or {"scheduled": True}
    return envelope(verb, http, state, exit_code=rc, result=result, stdout=proc.stdout,
                    detail=proc.stderr)


def run_action(verb: str, body: Dict[str, Any], *, dirs: List[str], lock_path: str,
               extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    if verb not in ACTIONS:
        return envelope(verb, 404, "error", detail="unknown action")
    spec = ACTIONS[verb]
    try:
        vals = validate(verb, body, lock_path)
    except RefusalError as r:
        state = "unavailable" if r.http == 503 else "error"
        return envelope(verb, r.http, state, detail=r.detail)
    argv: List[str] = []
    for el in spec["argv"]:
        if el.startswith("{") and el.endswith("}"):
            argv.append(vals[el[1:-1]])
        else:
            argv.append(el)
    stdin = vals.get(spec["stdin"]) + "\n" if spec.get("stdin") else None
    return run_argv(verb, argv, dirs, stdin=stdin, timeout=int(spec.get("timeout", 60)),
                    detached=bool(spec.get("detached")), extra_env=extra_env)


def run_read(section: str, *, dirs: List[str],
             extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    if section not in READS:
        return envelope(section, 404, "error", detail="unknown section")
    return run_argv(f"{section}-status", READS[section], dirs, timeout=60,
                    extra_env=extra_env)
