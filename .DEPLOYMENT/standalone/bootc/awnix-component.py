#!/usr/bin/python3.11
"""awnix component -- install, remove and roll back optional components from the lock.

    awnix component list [--json]
    awnix component info <id> [--json]
    awnix component install <id>... [--json] [--force] [--wheelhouse DIR]
    awnix component remove <id>... [--json]
    awnix component rollback <id> [--json]
    awnix component sync [--json] [--serial PATH]
    awnix component gc [--keep N] [--json]
    awnix component --self-test | --list-verbs

WHY THIS EXISTS. First boot used to `pip install <name>` into the system python3.11:
unpinned, unhashed, outside bootc rollback, impossible to undo. Now every installable
thing is a row in the lock baked into the image (/usr/share/awnix/components.lock.json,
generated in CI by gen_awnix_component_lock.py), and this tool installs exactly that:

  pypi/git  -> its own venv /var/lib/awnix/components/<id>/<pin12>/ built with
               `pip --require-hashes --no-deps` from the lock's hashed closure (git
               sources by commit), a `current` symlink, and /usr/local/bin shims that
               exec through `current` -- so rollback is one symlink flip.
  container -> a quadlet /etc/containers/systemd/awnix-<id>.container, Image pinned
               @sha256, ports on 127.0.0.1 only, health-probed before the state is
               committed; on failure the prior quadlet is restored.
               A container row may also join a private podman network (`network`, with
               `aliases`), carry static `env`, an `exec` command, `after` ordering on
               other components, box-local `secrets` (generated ONCE on this box into
               /var/lib/awnix/components/_secrets/<group>.env, 0600, kept across
               remove so a re-install still opens its vault), sha256-verified `files`
               downloaded into its data dir, and `public: true` (pulled anonymously).
  bundle    -> `requires` = an ordered list of other rows, installed as ONE operation:
               one click installs every member; a member failing removes what this
               operation added, so nothing is left half-done. Progress is written to
               /var/lib/awnix/components/_progress/<id>.json and shown by `list --json`
               (row.progress), so a detached install (console `component-install-start`)
               can be watched. On success the bundle's `endpoints` are written to
               /etc/awnix/components/<id>.endpoints.env for the apps on the box; remove
               deletes that file first, so no app keeps calling a stack that is gone.
  pack      -> `awpack install <id> --dest /var/lib/awnix/packs` (awpack is baked only
               into private images).
  baked     -> read-only rows from /usr/share/awnix/components.d/*.yaml: what the image
               IS. They cannot be removed, and nothing shadows their commands without
               --force.

The lock is per-image, so after `bootc upgrade` / `bootc rollback` the pins move with
the OS; `sync` (awnix-component-sync.service, every boot) re-aligns what is installed.

LICENSE. A requires_license row installs only when /var/lib/aither/license/status.json
says state=valid and its .entitlements cover the row. This tool never mints, stores or
writes a registry credential: private pulls read /etc/containers/auth.json, which only
`aitheros license refresh` writes.

Exit: 0 ok * 1 op failed (nothing changed) * 2 could-not-judge (lock missing/unreadable)
* 3 not entitled. --json prints {ok, op, results[{id, kind, state, version, pin,
previous_pin, reason, log_tail}]}.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import secrets as _secrets
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERBS = ("list", "info", "install", "remove", "rollback", "sync", "gc")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+$")
SHIM_MARK = "# awnix-component shim id="
#: Where a BAKED command lives. A shim in /usr/local/bin would shadow these on PATH.
BAKED_BIN_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/sbin")
#: Where systemd reads unit files -- and so where an installed aither-tier<N>.target is.
SYSTEMD_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system")
#: The fleet boot ladder (generate-deploy-units.py). 99 is the held-out marker, not a rung.
TIER_TARGET_RE = re.compile(r"^aither-tier(\d+)\.target$")
HELD_OUT_TIER = 99

EXIT_OK, EXIT_FAILED, EXIT_UNJUDGED, EXIT_NOT_ENTITLED = 0, 1, 2, 3


class Paths:
    """Every path the tool touches; env overrides exist for tests and dev boxes."""

    def __init__(self, env: dict | None = None):
        e = os.environ if env is None else env

        def p(key: str, default: str) -> Path:
            return Path(e.get(key) or default)

        self.lock = p("AWNIX_COMPONENT_LOCK", "/usr/share/awnix/components.lock.json")
        self.components_d = p("AWNIX_COMPONENTS_D", "/usr/share/awnix/components.d")
        self.state_dir = p("AWNIX_COMPONENT_STATE_DIR", "/var/lib/awnix/components")
        self.shim_dir = p("AWNIX_SHIM_DIR", "/usr/local/bin")
        self.quadlet_dir = p("AWNIX_QUADLET_DIR", "/etc/containers/systemd")
        self.env_dir = p("AWNIX_COMPONENT_ENV_DIR", "/etc/awnix/components")
        self.packs_dir = p("AWNIX_PACKS_DIR", "/var/lib/awnix/packs")
        self.log = p("AWNIX_COMPONENT_LOG", "/var/log/awnix/components.log")
        self.license_status = p("AITHER_LICENSE_STATUS", "/var/lib/aither/license/status.json")
        self.authfile = p("AWNIX_REGISTRY_AUTHFILE", "/etc/containers/auth.json")
        self.python = e.get("AWNIX_PYTHON") or "/usr/bin/python3.11"
        self.awpack = e.get("AWNIX_AWPACK") or "awpack"
        self.awpack_shelf = e.get("AWPACK_SHELF") or "/usr/share/awpack/packs"
        dirs = e.get("AWNIX_BAKED_BIN_DIRS")
        self.baked_bin_dirs = tuple(dirs.split(os.pathsep)) if dirs else BAKED_BIN_DIRS
        udirs = e.get("AWNIX_SYSTEMD_UNIT_DIRS")
        self.systemd_unit_dirs = (tuple(Path(d) for d in udirs.split(os.pathsep)) if udirs
                                  else tuple(Path(d) for d in SYSTEMD_UNIT_DIRS))
        self.health_timeout = float(e.get("AWNIX_COMPONENT_HEALTH_TIMEOUT") or 90)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"


class Runner:
    """subprocess, argv only (never a shell). Tests swap in a fake."""

    def run(self, argv: list[str], timeout: float = 900, env: dict | None = None,
            cwd: str | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env, cwd=cwd)
        except FileNotFoundError as e:
            return 127, f"{argv[0]}: not found ({e})"
        except subprocess.TimeoutExpired:
            return 124, f"{argv[0]}: timed out after {timeout:.0f}s"
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def http_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - 127.0.0.1 probe
                return 200 <= r.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def download(self, url: str, dest: Path, sha256: str) -> tuple[bool, str]:
        """Stream ``url`` to ``dest`` (atomic) only if its bytes hash to ``sha256``."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        h = hashlib.sha256()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "awnix-component"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
        except (urllib.error.URLError, OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            return False, f"download {url}: {e}"
        if h.hexdigest() != sha256:
            tmp.unlink(missing_ok=True)
            return False, f"download {url}: sha256 {h.hexdigest()[:12]} != locked {sha256[:12]}"
        os.replace(tmp, dest)
        return True, f"{dest.name}: sha256 verified"


class CannotJudgeError(Exception):
    """Exit 2: the lock (or another required input) is missing or unreadable."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail(text: str, n: int = 20) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _env_items(env) -> list[tuple[str, str]]:
    """(key, value) pairs from a lock `env` (dict, or list of "K=V") or env-file text."""
    if isinstance(env, dict):
        items = [(str(k), str(v)) for k, v in env.items()]
    elif isinstance(env, str):
        items = [tuple(ln.split("=", 1)) for ln in env.splitlines()
                 if "=" in ln and not ln.lstrip().startswith("#")]
    else:
        items = [tuple(str(e).split("=", 1)) for e in env or [] if "=" in str(e)]
    out = []
    for k, v in items:
        k = k.strip()
        if not _ENV_KEY_RE.match(k) or "\n" in v:
            raise ValueError(f"bad env entry {k!r}")
        out.append((k, v))
    return out


def _read_env(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _quote(arg: str) -> str:
    """One systemd/quadlet word: quoted when it holds whitespace, quotes or `$`/`%`."""
    if arg and not re.search(r"[\s\"'\\$%;]", arg):
        return arg
    esc = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{esc}"'


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _real_symlink(target: str, link: Path) -> None:
    tmp = link.with_name(f".{link.name}.new")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _emulated_symlink(target: str, link: Path) -> None:
    """Test-only stand-in on a host that cannot create symlinks (Windows, no privilege)."""
    atomic_write(link, target)


def _real_readlink(link: Path) -> str:
    return os.readlink(link)


def _emulated_readlink(link: Path) -> str:
    return link.read_text(encoding="utf-8")


#: Swapped ONLY by the self-test / pytest when the host cannot symlink; the image
#: (Linux, root) always uses real symlinks.
LINKS = {"make": _real_symlink, "read": _real_readlink}


def atomic_symlink(target: str, link: Path) -> None:
    LINKS["make"](target, link)


def read_link(link: Path) -> str:
    return LINKS["read"](link)


def symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as t:
        try:
            os.symlink("x", os.path.join(t, "l"))
            return True
        except (OSError, NotImplementedError):
            return False


# ── baked manifests (YAML without PyYAML) ─────────────────────────────────────


def _scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v in ("null", "~", ""):
        return None
    return v


def parse_baked_yaml(text: str) -> list[dict]:
    """`components:` list of flat mappings (the components.d shape). PyYAML if present."""
    try:
        import yaml  # noqa: PLC0415 - optional on the image
    except ImportError:
        yaml = None  # the flat parser below handles the components.d shape
    if yaml is not None:
        data = yaml.safe_load(text) or {}
        return list(data.get("components") or [])
    rows: list[dict] = []
    cur: dict | None = None
    in_list = False
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if not line[0].isspace():
            in_list = line.startswith("components:")
            continue
        if not in_list:
            continue
        s = line.strip()
        if s.startswith("- "):
            cur = {}
            rows.append(cur)
            s = s[2:]
        if cur is not None and ":" in s:
            k, _, v = s.partition(":")
            cur[k.strip()] = _scalar(v)
    return rows


def load_baked(paths: Paths) -> list[dict]:
    rows: list[dict] = []
    if not paths.components_d.is_dir():
        return rows
    for f in sorted(paths.components_d.iterdir()):
        try:
            if f.suffix == ".json":
                items = json.loads(f.read_text(encoding="utf-8")).get("components") or []
            elif f.suffix in (".yaml", ".yml"):
                items = parse_baked_yaml(f.read_text(encoding="utf-8"))
            else:
                continue
        except (OSError, ValueError) as e:
            raise CannotJudgeError(f"baked manifest {f} unreadable: {e}") from e
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            rows.append({**it, "id": str(it["id"]).lower(), "kind": "baked",
                         "removable": False, "source_file": f.name,
                         "provides": list(it.get("provides") or [])})
    return rows


# ── lock, state, license ──────────────────────────────────────────────────────


def load_lock(paths: Paths) -> dict:
    if not paths.lock.is_file():
        raise CannotJudgeError(f"component lock {paths.lock} is missing")
    try:
        lock = json.loads(paths.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CannotJudgeError(f"component lock {paths.lock} unreadable: {e}") from e
    if lock.get("schema") != 1 or not isinstance(lock.get("components"), list):
        raise CannotJudgeError(f"component lock {paths.lock} is not schema 1")
    return lock


def load_state(paths: Paths) -> dict:
    try:
        st = json.loads(paths.state_file.read_text(encoding="utf-8"))
        if isinstance(st, dict) and isinstance(st.get("components"), dict):
            return st
    except FileNotFoundError:
        return {"schema": 1, "lock_catalogue_sha256": "", "components": {}}
    except (OSError, ValueError) as e:
        raise CannotJudgeError(f"state {paths.state_file} unreadable: {e}") from e
    return {"schema": 1, "lock_catalogue_sha256": "", "components": {}}


def save_state(paths: Paths, state: dict) -> None:
    atomic_write(paths.state_file, json.dumps(state, indent=1, sort_keys=True) + "\n")


def license_view(paths: Paths) -> dict:
    """{valid: bool, state, entitlements} from the activation gap's status.json."""
    try:
        st = json.loads(paths.license_status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"valid": False, "state": "unlicensed", "entitlements": {}}
    return {"valid": st.get("state") == "valid", "state": st.get("state", "unknown"),
            "entitlements": st.get("entitlements") or {}}


def entitled(row: dict, lic: dict) -> bool:
    """Does the license cover this row? Only ever read from status.json .entitlements.

    Covered when the license is valid AND one of: the row's entitlement or id is in
    entitlements.packs (or packs has '*'); for a container, its image repo (full ref
    or basename) is in entitlements.images; for a pack, 'pack:<id>' is in packs.
    """
    if not row.get("requires_license"):
        return True
    if not lic.get("valid"):
        return False
    ent = lic.get("entitlements") or {}
    packs = {str(x) for x in ent.get("packs") or []}
    images = {str(x) for x in ent.get("images") or []}
    if "*" in packs:
        return True
    if str(row.get("entitlement") or "") in packs or row["id"] in packs:
        return True
    if row.get("kind") == "container":
        url = str(row.get("url") or "")
        if url and (url in images or url.rsplit("/", 1)[-1] in images):
            return True
    if row.get("kind") == "pack" and f"pack:{row['id']}" in packs:
        return True
    return False


def pin12(pin: str) -> str:
    return pin.split(":", 1)[-1][:12]


# ── the tool ──────────────────────────────────────────────────────────────────


class Tool:
    def __init__(self, paths: Paths | None = None, runner: Runner | None = None):
        self.paths = paths or Paths()
        self.runner = runner or Runner()
        self.lock = load_lock(self.paths)
        self.baked = {r["id"]: r for r in load_baked(self.paths)}
        self.rows = {str(r["id"]).lower(): r for r in self.lock["components"]}
        self.state = load_state(self.paths)
        self.lic = license_view(self.paths)

    # logging -----------------------------------------------------------------
    def log(self, op: str, cid: str, outcome: str, detail: str = "") -> None:
        try:
            self.paths.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.paths.log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now(), "op": op, "id": cid, "outcome": outcome,
                                    "detail": detail[-2000:]}) + "\n")
        except OSError as e:
            print(f"awnix component: could not append to {self.paths.log}: {e}",
                  file=sys.stderr)

    # state view --------------------------------------------------------------
    def status_of(self, cid: str) -> dict:
        """The row's state, plus (bundles) members, the live op's progress and endpoints."""
        res = self._status_core(cid)
        row = self.rows.get(cid) or {}
        if row.get("title"):
            res["title"] = row["title"]
        if row.get("kind") == "bundle":
            res["requires"] = [str(m).lower() for m in row.get("requires") or []]
            prog = self.read_progress(cid)
            if prog is not None:
                res["progress"] = prog
            if res["state"] == "installed":
                res["endpoints"] = dict(_env_items(_read_env(self._endpoints_path(cid))))
        return res

    def _status_core(self, cid: str) -> dict:
        if cid in self.baked:
            b = self.baked[cid]
            return self._res(cid, "baked", "baked", version=str(b.get("version") or ""),
                             pin=str(b.get("source") or ""), reason="baked into this image")
        row = self.rows.get(cid)
        st = self.state["components"].get(cid)
        if st and st.get("status") == "installed":
            reason = ""
            if row is None:
                reason = "no longer in this image's lock (kept; `remove` to drop it)"
            elif row.get("pin") != st.get("pin"):
                reason = f"lock pins {pin12(str(row.get('pin')))}; `sync` to follow"
            return self._res(cid, st.get("kind", ""), "installed", version=st.get("version", ""),
                             pin=st.get("pin", ""), previous_pin=self._previous(st),
                             reason=reason)
        if row is None:
            return self._res(cid, "", "unavailable", reason=f"{cid!r} is not in the lock")
        if not row.get("available"):
            return self._res(cid, row["kind"], "unavailable", version=row.get("version", ""),
                             reason=row.get("reason") or "unavailable")
        if row["kind"] == "pack" and not self._awpack_present():
            return self._res(cid, "pack", "unavailable", version=row.get("version", ""),
                             pin=row.get("pin", ""),
                             reason="awpack is not baked into this image")
        if not entitled(row, self.lic):
            why = ("no valid license (state=%s)" % self.lic["state"] if not self.lic["valid"]
                   else f"license does not include {row.get('entitlement')}")
            return self._res(cid, row["kind"], "needs-license", version=row.get("version", ""),
                             pin=row.get("pin", ""), reason=why)
        return self._res(cid, row["kind"], "available", version=row.get("version", ""),
                         pin=row.get("pin", ""), reason="")

    @staticmethod
    def _res(cid: str, kind: str, state: str, version: str = "", pin: str = "",
             previous_pin: str = "", reason: str = "", log_tail: str = "") -> dict:
        return {"id": cid, "kind": kind, "state": state, "version": version, "pin": pin,
                "previous_pin": previous_pin, "reason": reason, "log_tail": log_tail}

    @staticmethod
    def _previous(st: dict) -> str:
        gens = [g for g in st.get("generations") or [] if g != st.get("pin")]
        return gens[-1] if gens else ""

    def _awpack_present(self) -> bool:
        a = self.paths.awpack
        return bool(shutil.which(a)) if os.sep not in a and "/" not in a else Path(a).exists()

    # verbs -------------------------------------------------------------------
    def list(self) -> tuple[int, list[dict]]:
        ids = list(self.baked) + [i for i in self.rows if i not in self.baked]
        ids += [i for i in self.state["components"] if i not in ids]
        return EXIT_OK, [self.status_of(i) for i in ids]

    def info(self, cid: str) -> tuple[int, list[dict]]:
        res = self.status_of(cid)
        row = self.baked.get(cid) or self.rows.get(cid)
        if row is None and cid not in self.state["components"]:
            return EXIT_FAILED, [res]
        detail = {k: v for k, v in (row or {}).items() if k != "deps"}
        detail["deps"] = len((row or {}).get("deps") or [])
        res["detail"] = detail
        res["installed"] = self.state["components"].get(cid)
        return EXIT_OK, [res]

    def install(self, ids: list[str], force: bool = False,
                wheelhouse: str | None = None) -> tuple[int, list[dict]]:
        out = []
        for cid in ids:
            out.append(self._install_one(cid, force=force, wheelhouse=wheelhouse))
        return self._rc(out), out

    def _install_one(self, cid: str, force: bool = False, wheelhouse: str | None = None,
                     row: dict | None = None, op: str = "install") -> dict:
        if not ID_RE.match(cid):
            return self._res(cid, "", "failed", reason="invalid component id")
        if cid in self.baked:
            return self.status_of(cid)
        row = row or self.rows.get(cid)
        if row is None:
            return self._res(cid, "", "failed", reason=f"{cid!r} is not in the lock")
        cur = self.status_of(cid)
        if cur["state"] in ("unavailable", "needs-license"):
            return cur
        st = self.state["components"].get(cid)
        if st and st.get("status") == "installed" and st.get("pin") == row.get("pin"):
            return self._res(cid, row["kind"], "installed", version=st.get("version", ""),
                             pin=st["pin"], previous_pin=self._previous(st),
                             reason="already installed at the locked pin")
        kind = row["kind"]
        try:
            if kind in ("pypi", "git"):
                res = self._install_python(cid, row, force, wheelhouse)
            elif kind == "container":
                res = self._install_container(cid, row)
            elif kind == "pack":
                res = self._install_pack(cid, row)
            elif kind == "bundle":
                res = self._install_bundle(cid, row)
            else:
                res = self._res(cid, kind, "failed", reason=f"unknown kind {kind!r}")
        except OSError as e:
            res = self._res(cid, kind, "failed", reason=f"{type(e).__name__}: {e}")
        self.log(op, cid, res["state"], res.get("reason", "") + "\n" + res.get("log_tail", ""))
        return res

    # python ------------------------------------------------------------------
    def _venv_dir(self, cid: str, pin: str) -> Path:
        return self.paths.state_dir / cid / pin12(pin)

    def _requirements(self, row: dict, wheelhouse: str | None) -> str:
        wheels = self.lock.get("wheels") or {}
        lines = []
        for key in row.get("deps") or []:
            w = wheels.get(key)
            if not w:
                raise ValueError(f"lock names dep {key} but carries no hash for it")
            name, ver = key.split("==", 1)
            lines.append(f"{name}=={ver} --hash=sha256:{w['sha256']}" if wheelhouse
                         else f"{name} @ {w['url']} --hash=sha256:{w['sha256']}")
        if row["kind"] == "pypi":
            dist = row.get("dist") or row.get("name") or row["id"]
            sha = str(row["pin"]).split(":", 1)[1]
            lines.append(f"{dist}=={row['version']} --hash=sha256:{sha}" if wheelhouse
                         else f"{dist} @ {row['url']} --hash=sha256:{sha}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _install_python(self, cid: str, row: dict, force: bool,
                        wheelhouse: str | None) -> dict:
        pin = str(row["pin"])
        if row["kind"] == "pypi" and not DIGEST_RE.match(pin):
            return self._res(cid, "pypi", "failed", reason=f"lock pin {pin!r} is not sha256")
        if row["kind"] == "git" and not SHA_RE.match(pin):
            return self._res(cid, "git", "failed", reason=f"lock pin {pin!r} is not a commit")
        vdir = self._venv_dir(cid, pin)
        built_now = False
        log = ""
        if not (vdir / ".complete").is_file():
            if vdir.exists():
                shutil.rmtree(vdir)
            vdir.parent.mkdir(parents=True, exist_ok=True)
            built_now = True
            ok, log = self._build_venv(vdir, row, wheelhouse)
            if not ok:
                shutil.rmtree(vdir, ignore_errors=True)
                self._prune_empty(cid)
                return self._res(cid, row["kind"], "failed", version=row.get("version", ""),
                                 pin=pin, reason="pip install failed; nothing changed",
                                 log_tail=_tail(log))
        provides = sorted(set(row.get("provides") or []) | set(self._entry_points(vdir, row)))
        clash = self._shadow_check(cid, provides, force)
        if clash:
            if built_now:
                shutil.rmtree(vdir, ignore_errors=True)
                self._prune_empty(cid)
            return self._res(cid, row["kind"], "failed", version=row.get("version", ""),
                             pin=pin, reason=clash)
        return self._commit_python(cid, row, provides, log)

    def _build_venv(self, vdir: Path, row: dict, wheelhouse: str | None) -> tuple[bool, str]:
        logs = []
        rc, out = self.runner.run([self.paths.python, "-m", "venv", str(vdir)], timeout=300)
        logs.append(out)
        if rc != 0:
            return False, "\n".join(logs)
        py = str(vdir / "bin" / "python")
        base = [py, "-m", "pip", "install", "--no-deps", "--no-cache-dir",
                "--disable-pip-version-check", "--no-input"]
        if wheelhouse:
            base += ["--no-index", "--find-links", wheelhouse]
        try:
            req_text = self._requirements(row, wheelhouse)
        except ValueError as e:
            return False, str(e)
        if req_text:
            req = vdir / "awnix-requirements.txt"
            req.write_text(req_text, encoding="utf-8")
            rc, out = self.runner.run(base + ["--require-hashes", "-r", str(req)])
            logs.append(out)
            if rc != 0:
                return False, "\n".join(logs)
        if row["kind"] == "git":
            if wheelhouse:
                return False, "a git-pinned component cannot install from a wheelhouse"
            rc, out = self.runner.run(base + [str(row["url"])])
            logs.append(out)
            if rc != 0:
                return False, "\n".join(logs)
        (vdir / ".complete").write_text(json.dumps({"pin": row["pin"], "at": _now()}),
                                        encoding="utf-8")
        return True, "\n".join(logs)

    @staticmethod
    def _entry_points(vdir: Path, row: dict) -> list[str]:
        """console_scripts the installed brick actually declares (dist-info truth)."""
        dist = str(row.get("dist") or row.get("name") or row["id"]).lower().replace("-", "_")
        out: list[str] = []
        for ep in vdir.glob("lib/python*/site-packages/*.dist-info/entry_points.txt"):
            if ep.parent.name.lower().split("-", 1)[0].replace("-", "_") != dist:
                continue
            section = ""
            for line in ep.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]").strip()
                elif section == "console_scripts" and "=" in line:
                    out.append(line.split("=", 1)[0].strip())
        return out

    def _shadow_check(self, cid: str, provides: list[str], force: bool) -> str:
        """Refuse to shadow a baked command or another component's shim, unless --force."""
        for cmd in provides:  # a shim name becomes a path under /usr/local/bin: never --force'd
            if not re.match(r"^[A-Za-z0-9_+-][A-Za-z0-9_.+-]*$", cmd):
                return f"refusing odd command name {cmd!r}"
        if force:
            return ""
        baked_cmds = {c for b in self.baked.values() for c in b.get("provides") or []}
        for cmd in provides:
            if cmd in baked_cmds:
                return f"would shadow baked command {cmd!r} (use --force)"
            for d in self.paths.baked_bin_dirs:
                if Path(d, cmd).exists():
                    return f"would shadow baked command {d}/{cmd} (use --force)"
            shim = self.paths.shim_dir / cmd
            if shim.exists() or shim.is_symlink():
                owner = self._shim_owner(shim)
                if owner != cid:
                    what = f"component {owner!r}" if owner else "a file this tool did not write"
                    return f"{shim} belongs to {what} (use --force)"
        return ""

    @staticmethod
    def _shim_owner(shim: Path) -> str:
        try:
            for line in shim.read_text(encoding="utf-8", errors="replace").splitlines()[:3]:
                if line.startswith(SHIM_MARK):
                    return line[len(SHIM_MARK):].strip()
        except OSError:
            return ""  # unreadable -> not provably ours -> treated as foreign
        return ""

    def _write_shims(self, cid: str, provides: list[str]) -> None:
        for cmd in provides:
            target = self.paths.state_dir / cid / "current" / "bin" / cmd
            atomic_write(self.paths.shim_dir / cmd,
                         f"#!/bin/sh\n{SHIM_MARK}{cid}\nexec {target.as_posix()} \"$@\"\n",
                         mode=0o755)

    def _remove_shims(self, cid: str, provides: list[str]) -> None:
        for cmd in provides:
            shim = self.paths.shim_dir / cmd
            if self._shim_owner(shim) == cid:
                shim.unlink()

    def _commit_python(self, cid: str, row: dict, provides: list[str], log: str) -> dict:
        st = self.state["components"].get(cid) or {}
        prev = st.get("pin", "") if st.get("status") == "installed" else ""
        atomic_symlink(pin12(row["pin"]), self.paths.state_dir / cid / "current")
        old_provides = (st.get("meta") or {}).get(prev, {}).get("provides") or []
        self._remove_shims(cid, [c for c in old_provides if c not in provides])
        self._write_shims(cid, provides)
        self._record(cid, row, provides)
        return self._res(cid, row["kind"], "installed", version=row.get("version", ""),
                         pin=row["pin"], previous_pin=prev, log_tail=_tail(log, 5))

    def _record(self, cid: str, row: dict, provides: list[str], extra: dict | None = None) -> None:
        st = self.state["components"].setdefault(cid, {"generations": [], "meta": {}})
        gens = [g for g in st.get("generations") or [] if g != row["pin"]] + [row["pin"]]
        meta = st.setdefault("meta", {})
        meta[row["pin"]] = {"version": row.get("version", ""), "provides": provides,
                            **(extra or {})}
        st.update(kind=row["kind"], pin=row["pin"], version=row.get("version", ""),
                  installed_at=_now(), generations=gens, status="installed")
        self.state["lock_catalogue_sha256"] = self.lock.get("catalogue_sha256", "")
        save_state(self.paths, self.state)

    def _prune_empty(self, cid: str) -> None:
        d = self.paths.state_dir / cid
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError as e:
            print(f"awnix component: left empty {d}: {e}", file=sys.stderr)

    # container ---------------------------------------------------------------
    def _quadlet_path(self, cid: str) -> Path:
        return self.paths.quadlet_dir / f"awnix-{cid}.container"

    def _boot_target(self) -> str:
        """The rung a component joins: the highest INSTALLED aither-tier<N>.target.

        A component is late/optional, so it starts after the whole fleet ladder (the
        role tier 50 has in generate-deploy-units.py), never flat on multi-user.target
        racing it (CUT007). But a WantedBy= naming a target systemd has never seen does
        not start late -- it does not start at all -- so on a box with no ladder (a
        sellable awnix with no fleet) the unit falls back to multi-user.target.

        Two limits, both intended under the core-only ruling:
          * it checks that the target FILE exists, not that it is ENABLED. A ladder
            that is installed but disabled leaves the component waiting on a rung
            nothing pulls in at boot;
          * the rung is fixed at INSTALL time. A ladder installed, removed or grown
            later is picked up only when the component is installed again (or by
            `sync` after a pin moves), never live.
        """
        tiers = set()
        for d in self.paths.systemd_unit_dirs:
            if d.is_dir():
                for f in d.iterdir():
                    m = TIER_TARGET_RE.match(f.name)
                    if m and int(m.group(1)) != HELD_OUT_TIER:
                        tiers.add(int(m.group(1)))
        return f"aither-tier{max(tiers)}.target" if tiers else "multi-user.target"

    def _quadlet_text(self, cid: str, row: dict, image: str, digest: str) -> str:
        q = row.get("quadlet") or {}
        rung = self._boot_target()
        after = "network-online.target" + ("" if rung == "multi-user.target" else f" {rung}")
        deps = [f"awnix-{d}.service" for d in q.get("after") or []]
        for d in q.get("after") or []:
            if not ID_RE.match(str(d)):
                raise ValueError(f"bad after id {d!r}")
        lines = ["# Written by awnix-component. Edits are replaced on the next install.",
                 "[Unit]", f"Description=awnix component {cid} {row.get('version', '')}",
                 "Wants=network-online.target" + "".join(f" {d}" for d in deps),
                 f"After={after}" + "".join(f" {d}" for d in deps), "",
                 "[Container]", f"Image={image}@{digest}", f"ContainerName=awnix-{cid}"]
        net = str(q.get("network") or "")
        if net:
            if not ID_RE.match(net):
                raise ValueError(f"bad network {net!r}")
            lines.append(f"Network=awnix-{net}.network")
            for a in q.get("aliases") or []:
                if not ID_RE.match(str(a)):
                    raise ValueError(f"bad alias {a!r}")
                lines.append(f"NetworkAlias={a}")
        for p in q.get("ports") or []:
            host, cport = int(p["host"]), int(p.get("container") or p["host"])
            lines.append(f"PublishPort=127.0.0.1:{host}:{cport}")
        data = self.paths.state_dir / cid / "data"
        for v in q.get("volumes") or []:
            dest = str(v.get("dest") or "")
            if not dest.startswith("/") or ":" in dest:
                raise ValueError(f"bad volume dest {dest!r}")
            shared = str(v.get("shared") or "")
            if shared and not ID_RE.match(shared):
                raise ValueError(f"bad shared volume {shared!r}")
            src = self.paths.state_dir / "_shared" / shared if shared else data
            lines.append(f"Volume={src}:{dest}:{'ro,' if v.get('ro') else ''}Z")
        for k, v in _env_items(q.get("env")):
            lines.append(f"Environment={_quote(f'{k}={v}')}")
        lines.append(f"EnvironmentFile={self.paths.env_dir / (cid + '.env')}")
        exe = [str(a) for a in q.get("exec") or []]
        if exe:
            lines.append("Exec=" + " ".join(_quote(a) for a in exe))
        lines += ["", "[Service]", "Restart=on-failure", "TimeoutStartSec=900", "",
                  "[Install]", f"WantedBy={rung}", ""]
        return "\n".join(lines)

    def _network_unit(self, net: str) -> None:
        """The private network a stack's members share (DNS by alias); written once."""
        path = self.paths.quadlet_dir / f"awnix-{net}.network"
        text = ("# Written by awnix-component. Shared by every component on this network.\n"
                f"[Network]\nNetworkName=awnix-{net}\n")
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            atomic_write(path, text)

    def _secrets_env(self, cid: str, row: dict) -> None:
        """Write the row's env file: its box-local secrets, generated once per group.

        The values are made HERE (secrets.token_hex) and never leave the box; the group
        file survives `remove`, so a re-install opens the same vault and database.
        """
        q = row.get("quadlet") or {}
        names = [str(n) for n in q.get("secrets") or []]
        envf = self.paths.env_dir / f"{cid}.env"
        if not names:
            if not envf.exists():
                atomic_write(envf, "", mode=0o600)
            return
        group = str(q.get("secrets_group") or cid)
        if not ID_RE.match(group):
            raise ValueError(f"bad secrets_group {group!r}")
        store = self.paths.state_dir / "_secrets" / f"{group}.env"
        have = dict(_env_items(_read_env(store)))
        missing = [n for n in names if n not in have]
        for n in missing:
            if not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", n):
                raise ValueError(f"bad secret name {n!r}")
            have[n] = _secrets.token_hex(32)
        if missing:
            atomic_write(store, "".join(f"{k}={v}\n" for k, v in sorted(have.items())),
                         mode=0o600)
        atomic_write(envf, "".join(f"{n}={have[n]}\n" for n in names), mode=0o600)

    def _fetch_files(self, cid: str, row: dict) -> tuple[bool, str]:
        """Download each locked file into the row's data dir, sha256-verified."""
        logs = []
        data = self.paths.state_dir / cid / "data"
        for f in (row.get("quadlet") or {}).get("files") or []:
            name, sha = str(f.get("name") or ""), str(f.get("sha256") or "")
            if not re.match(r"^[A-Za-z0-9._-]{1,128}$", name) or not re.match(
                    r"^[0-9a-f]{64}$", sha):
                return False, f"bad file spec {f!r}"
            dest = data / name
            if dest.is_file() and _sha256_file(dest) == sha:
                logs.append(f"{name}: already present, sha256 verified")
                continue
            ok, out = self.runner.download(str(f.get("url") or ""), dest, sha)
            logs.append(out)
            if not ok:
                return False, "\n".join(logs)
        return True, "\n".join(logs)

    def _install_container(self, cid: str, row: dict,
                           digest: str | None = None) -> dict:
        digest = digest or str(row.get("pin") or "")
        image = str(row.get("url") or "")
        if not DIGEST_RE.match(digest) or not IMAGE_RE.match(image):
            return self._res(cid, "container", "failed",
                             reason=f"lock row is not an @sha256 image ({image}@{digest})")
        logs = []
        q = row.get("quadlet") or {}
        pull = ["podman", "pull"]
        if self.paths.authfile.is_file() and not q.get("public"):
            pull += ["--authfile", str(self.paths.authfile)]
        rc, out = self.runner.run(pull + [f"{image}@{digest}"], timeout=1800)
        logs.append(out)
        if rc != 0:
            return self._res(cid, "container", "failed", version=row.get("version", ""),
                             pin=digest, reason="image pull failed; nothing changed",
                             log_tail=_tail("\n".join(logs)))
        (self.paths.state_dir / cid / "data").mkdir(parents=True, exist_ok=True)
        for v in q.get("volumes") or []:
            if v.get("shared") and ID_RE.match(str(v["shared"])):
                (self.paths.state_dir / "_shared" / str(v["shared"])).mkdir(
                    parents=True, exist_ok=True)
        ok, out = self._fetch_files(cid, row)
        logs.append(out)
        if not ok:
            return self._res(cid, "container", "failed", version=row.get("version", ""),
                             pin=digest, reason="a locked file did not download or verify; "
                                                "nothing changed",
                             log_tail=_tail("\n".join(logs)))
        qpath = self._quadlet_path(cid)
        prior = qpath.read_text(encoding="utf-8") if qpath.is_file() else None
        try:
            self._secrets_env(cid, row)
            text = self._quadlet_text(cid, row, image, digest)
            if q.get("network"):
                self._network_unit(str(q["network"]))
        except (ValueError, KeyError, TypeError) as e:
            return self._res(cid, "container", "failed", reason=f"bad quadlet spec: {e}")
        atomic_write(qpath, text)
        unit = f"awnix-{cid}.service"
        ok, out = self._start_and_probe(unit, row)
        logs.append(out)
        if not ok:
            if prior is None:
                qpath.unlink()
            else:
                atomic_write(qpath, prior)
            self.runner.run(["systemctl", "daemon-reload"], timeout=120)
            if prior is not None:
                self.runner.run(["systemctl", "restart", unit], timeout=900)
            else:
                self.runner.run(["systemctl", "stop", unit], timeout=300)
            return self._res(cid, "container", "failed", version=row.get("version", ""),
                             pin=digest, reason="health probe failed; prior quadlet restored",
                             log_tail=_tail("\n".join(logs)))
        st = self.state["components"].get(cid) or {}
        prev = st.get("pin", "") if st.get("status") == "installed" else ""
        self._record(cid, {**row, "pin": digest}, [], {"image": image})
        return self._res(cid, "container", "installed", version=row.get("version", ""),
                         pin=digest, previous_pin=prev, log_tail=_tail("\n".join(logs), 5))

    def _start_and_probe(self, unit: str, row: dict) -> tuple[bool, str]:
        logs = []
        for argv in (["systemctl", "daemon-reload"], ["systemctl", "restart", unit]):
            rc, out = self.runner.run(argv, timeout=900)
            logs.append(out)
            if rc != 0:
                return False, "\n".join(logs)
        q = row.get("quadlet") or {}
        health = str(q.get("health") or "")
        # a row may need longer than the default (Nexus loads its index ~90 s);
        # never SHORTER than the box-wide floor
        deadline = max(self.paths.health_timeout, float(q.get("health_timeout") or 0))
        waited = 0.0
        while waited <= deadline:
            rc, out = self.runner.run(["systemctl", "is-active", unit], timeout=30)
            active = rc == 0 and out.strip().startswith("active")
            if active and (not health or self.runner.http_ok(health)):
                return True, "\n".join(logs)
            self.runner.sleep(3)
            waited += 3
        logs.append(f"{unit} not healthy within {deadline:.0f}s ({health or 'is-active'})")
        return False, "\n".join(logs)

    # bundle ------------------------------------------------------------------
    def _progress_path(self, cid: str) -> Path:
        return self.paths.state_dir / "_progress" / f"{cid}.json"

    def _progress(self, cid: str, op: str, step: int, steps: int, current: str,
                  message: str, done: bool = False, ok: bool | None = None,
                  error: str = "") -> None:
        """What a watcher (the console, the app, the bot) shows, in plain words."""
        atomic_write(self._progress_path(cid), json.dumps({
            "op": op, "step": step, "steps": steps, "current": current, "message": message,
            "done": done, "ok": ok, "error": error, "updated_at": _now()}, sort_keys=True))

    def read_progress(self, cid: str) -> dict | None:
        try:
            return json.loads(self._progress_path(cid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _endpoints_path(self, cid: str) -> Path:
        return self.paths.env_dir / f"{cid}.endpoints.env"

    def _title(self, cid: str) -> str:
        return str((self.rows.get(cid) or {}).get("title") or cid)

    def _install_bundle(self, cid: str, row: dict, op: str = "install") -> dict:
        """Install every member in order as ONE operation; undo this op's members on failure."""
        members = [str(m).lower() for m in row.get("requires") or []]
        bad = [m for m in members if m not in self.rows or self.rows[m].get("kind") == "bundle"]
        if not members or bad:
            return self._res(cid, "bundle", "failed",
                             reason=f"bundle names no installable members ({bad or 'none'})")
        steps = len(members) + 1
        added: list[str] = []
        logs: list[str] = []
        for i, m in enumerate(members, 1):
            title = self._title(m)
            self._progress(cid, op, i, steps, m, f"Setting up {title}")
            st = self.state["components"].get(m) or {}
            was = st.get("status") == "installed"
            res = self._install_one(m, op=op)
            logs.append(f"{m}: {res['state']} {res.get('reason', '')}".strip())
            if res["state"] != "installed":
                why = res.get("reason") or res["state"]
                for n in reversed(added):
                    self._remove_one(n)
                err = (f"{title} could not be set up ({why}). Nothing was left half-installed"
                       + ("" if not added else f": undid {len(added)} part(s) added just now")
                       + ".")
                self._progress(cid, op, i, steps, m, f"{title} failed", done=True, ok=False,
                               error=err)
                return self._res(cid, "bundle", "failed", version=row.get("version", ""),
                                 pin=row.get("pin", ""), reason=err,
                                 log_tail=_tail("\n".join(logs) + "\n" + res.get("log_tail", "")))
            if not was:
                added.append(m)
        self._progress(cid, op, steps, steps, "", "Pointing GargBot at this machine")
        try:
            env = _env_items(row.get("endpoints") or {})
        except ValueError as e:
            return self._res(cid, "bundle", "failed", reason=f"bad endpoints: {e}")
        atomic_write(self._endpoints_path(cid), "# Written by awnix-component install "
                     f"{cid}; removed by `remove {cid}`.\n"
                     + "".join(f"{k}={v}\n" for k, v in env))
        self._record(cid, row, [], {"members": members})
        self._progress(cid, op, steps, steps, "", "Done: memory and search run on this machine",
                       done=True, ok=True)
        return self._res(cid, "bundle", "installed", version=row.get("version", ""),
                         pin=row.get("pin", ""), log_tail=_tail("\n".join(logs), 12))

    def _remove_bundle(self, cid: str, st: dict) -> tuple[bool, str]:
        """Endpoints file FIRST (no app keeps calling a stack that is going), then members."""
        members = (st.get("meta") or {}).get(st.get("pin"), {}).get("members") or \
            [str(m).lower() for m in (self.rows.get(cid) or {}).get("requires") or []]
        self._endpoints_path(cid).unlink(missing_ok=True)
        steps = len(members)
        errors = []
        for i, m in enumerate(reversed(members), 1):
            self._progress(cid, "remove", i, steps, m, f"Removing {self._title(m)}")
            mst = self.state["components"].get(m)
            if not mst or mst.get("status") != "installed":
                continue
            res = self._remove_one(m)
            if res["state"] == "failed":
                errors.append(f"{self._title(m)}: {res.get('reason')}")
        return (not errors), "; ".join(errors)

    # pack --------------------------------------------------------------------
    def _awpack_env(self) -> dict:
        return {**os.environ, "AWPACK_SHELF": self.paths.awpack_shelf,
                "AWPACK_INSTALL_DIR": str(self.paths.packs_dir)}

    def _install_pack(self, cid: str, row: dict) -> dict:
        name = str(row.get("name") or cid)
        argv = [self.paths.awpack, "install", name, "--dest", str(self.paths.packs_dir),
                "--ref", str(row["pin"]), "--json"]
        st = self.state["components"].get(cid) or {}
        if st.get("status") == "installed":
            # a new lock pin (bootc upgrade/rollback moved the baked shelf): swap the
            # installed copy in one rename; awpack restores the old copy on failure
            argv.append("--replace")
        rc, out = self.runner.run(argv, timeout=300, env=self._awpack_env())
        if rc != 0:
            return self._res(cid, "pack", "failed", version=row.get("version", ""),
                             pin=row["pin"], reason="awpack install failed; nothing changed",
                             log_tail=_tail(out))
        self._record(cid, row, [])
        return self._res(cid, "pack", "installed", version=row.get("version", ""),
                         pin=row["pin"], log_tail=_tail(out, 5))

    # remove ------------------------------------------------------------------
    def remove(self, ids: list[str]) -> tuple[int, list[dict]]:
        out = [self._remove_one(cid) for cid in ids]
        return self._rc(out), out

    def _remove_one(self, cid: str) -> dict:
        if cid in self.baked:
            return self._res(cid, "baked", "failed",
                             reason="baked into this image; not removable")
        st = self.state["components"].get(cid)
        if not st or st.get("status") != "installed":
            return self._res(cid, "", "failed", reason="not installed")
        kind = st.get("kind", "")
        log = ""
        try:
            if kind in ("pypi", "git"):
                provides = {c for m in (st.get("meta") or {}).values()
                            for c in m.get("provides") or []}
                self._remove_shims(cid, sorted(provides))
                d = self.paths.state_dir / cid
                if d.exists():
                    shutil.rmtree(d)
            elif kind == "bundle":
                ok, err = self._remove_bundle(cid, st)
                if not ok:
                    msg = f"some parts could not be removed: {err}"
                    self._progress(cid, "remove", 0, 0, "", "Remove failed", done=True,
                                   ok=False, error=msg)
                    return self._res(cid, "bundle", "failed", reason=msg)
                self._progress(cid, "remove", 0, 0, "", "Removed: GargBot is back on its "
                               "default memory (your data on this machine is kept)",
                               done=True, ok=True)
            elif kind == "container":
                unit = f"awnix-{cid}.service"
                rc, log = self.runner.run(["systemctl", "stop", unit], timeout=300)
                q = self._quadlet_path(cid)
                if q.exists():
                    q.unlink()
                self.runner.run(["systemctl", "daemon-reload"], timeout=120)
            elif kind == "pack":
                name = str((self.rows.get(cid) or {}).get("name") or cid)
                rc, log = self.runner.run([self.paths.awpack, "remove", name, "--dest",
                                           str(self.paths.packs_dir), "--json"],
                                          timeout=120, env=self._awpack_env())
                if rc != 0:
                    return self._res(cid, "pack", "failed", reason="awpack remove failed",
                                     log_tail=_tail(log))
        except OSError as e:
            return self._res(cid, kind, "failed", reason=f"{type(e).__name__}: {e}")
        prev_pin = st.get("pin", "")
        del self.state["components"][cid]
        save_state(self.paths, self.state)
        self.log("remove", cid, "removed")
        row = self.rows.get(cid)
        res = self.status_of(cid) if row else self._res(cid, kind, "unavailable")
        res["previous_pin"] = prev_pin
        res["log_tail"] = _tail(log, 5)
        return res

    # rollback ----------------------------------------------------------------
    def rollback(self, cid: str) -> tuple[int, list[dict]]:
        res = self._rollback_one(cid)
        self.log("rollback", cid, res["state"], res.get("reason", ""))
        return self._rc([res]), [res]

    def _rollback_one(self, cid: str) -> dict:
        if cid in self.baked:
            return self._res(cid, "baked", "failed",
                             reason="baked: roll the OS back with `awnix update rollback`")
        st = self.state["components"].get(cid)
        if not st or st.get("status") != "installed":
            return self._res(cid, "", "failed", reason="not installed")
        prev = self._previous(st)
        if not prev:
            return self._res(cid, st.get("kind", ""), "failed", pin=st.get("pin", ""),
                             reason="no previous generation to roll back to")
        kind = st["kind"]
        meta = (st.get("meta") or {}).get(prev, {})
        if kind in ("pypi", "git"):
            vdir = self._venv_dir(cid, prev)
            if not (vdir / ".complete").is_file():
                return self._res(cid, kind, "failed", pin=st["pin"],
                                 reason=f"generation {pin12(prev)} was garbage-collected")
            cur_prov = (st.get("meta") or {}).get(st["pin"], {}).get("provides") or []
            provides = list(meta.get("provides") or [])
            atomic_symlink(pin12(prev), self.paths.state_dir / cid / "current")
            self._remove_shims(cid, [c for c in cur_prov if c not in provides])
            self._write_shims(cid, provides)
        elif kind == "bundle":
            return self._res(cid, kind, "failed", pin=st["pin"],
                             reason="a bundle has no generation of its own: roll back its "
                                    "members one by one")
        elif kind == "container":
            row = {**(self.rows.get(cid) or {}), "url": meta.get("image") or
                   (self.rows.get(cid) or {}).get("url"), "version": meta.get("version", "")}
            res = self._install_container(cid, row, digest=prev)
            if res["state"] != "installed":
                return res
        else:
            return self._res(cid, kind, "failed", pin=st["pin"],
                             reason=f"{kind} components keep one copy, taken from the baked "
                                    "shelf; they follow the OS via `sync` after "
                                    "`awnix update rollback`")
        old = st["pin"]
        gens = [g for g in st["generations"] if g not in (prev, old)] + [old, prev]
        st.update(pin=prev, version=meta.get("version", st.get("version", "")),
                  generations=gens, installed_at=_now())
        save_state(self.paths, self.state)
        return self._res(cid, kind, "installed", version=st["version"], pin=prev,
                         previous_pin=old, reason="rolled back")

    # sync --------------------------------------------------------------------
    def sync(self) -> tuple[int, list[dict]]:
        """Re-align installed components to this image's lock (after bootc up/rollback)."""
        out = []
        for cid, st in sorted(self.state["components"].items()):
            if st.get("status") != "installed":
                continue
            row = self.rows.get(cid)
            if row is None or not row.get("available"):
                out.append(self._res(cid, st.get("kind", ""), "installed",
                                     version=st.get("version", ""), pin=st.get("pin", ""),
                                     reason="drift: not offered by this image's lock; kept"))
                continue
            if row.get("pin") == st.get("pin"):
                out.append(self._res(cid, row["kind"], "installed", version=st.get("version", ""),
                                     pin=st["pin"], previous_pin=self._previous(st)))
                continue
            if not entitled(row, self.lic):
                out.append(self._res(cid, row["kind"], "needs-license", pin=st.get("pin", ""),
                                     reason="lock moved but the license no longer covers it; kept"))
                continue
            out.append(self._install_one(cid, row=row, op="sync"))
        self.state["lock_catalogue_sha256"] = self.lock.get("catalogue_sha256", "")
        save_state(self.paths, self.state)
        failed = [r for r in out if r["state"] == "failed"]
        return (EXIT_FAILED if failed else EXIT_OK), out

    def marker(self) -> str:
        n_baked = len(self.baked)
        m = sum(1 for s in self.state["components"].values() if s.get("status") == "installed")
        return (f"awnix-components: {n_baked} baked, {m} installed, "
                f"lock {str(self.lock.get('catalogue_sha256') or '')[:12] or 'none'}")

    # gc ----------------------------------------------------------------------
    def gc(self, keep: int = 2) -> tuple[int, list[dict]]:
        keep = max(1, keep)
        out = []
        for cid, st in sorted(self.state["components"].items()):
            if st.get("kind") not in ("pypi", "git"):
                continue
            gens = list(st.get("generations") or [])
            others = [g for g in gens if g != st.get("pin")]
            keep_set = set(others[-(keep - 1):]) if keep > 1 else set()
            keep_set.add(st.get("pin"))
            dropped = []
            for g in others:
                if g in keep_set:
                    continue
                d = self._venv_dir(cid, g)
                if d.exists():
                    shutil.rmtree(d)
                dropped.append(g)
                (st.get("meta") or {}).pop(g, None)
            st["generations"] = [g for g in gens if g in keep_set]
            out.append(self._res(cid, st["kind"], "installed", version=st.get("version", ""),
                                 pin=st.get("pin", ""), previous_pin=self._previous(st),
                                 reason=f"gc dropped {len(dropped)} generation(s)"))
        save_state(self.paths, self.state)
        return EXIT_OK, out

    @staticmethod
    def _rc(results: list[dict]) -> int:
        states = [r["state"] for r in results]
        if "failed" in states or "unavailable" in states:
            return EXIT_FAILED
        if "needs-license" in states:
            return EXIT_NOT_ENTITLED
        return EXIT_OK


# ── output ────────────────────────────────────────────────────────────────────


def render(op: str, rc: int, results: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": rc == 0, "op": op, "results": results}, sort_keys=True))
        return
    if op in ("list", "sync", "gc"):
        print(f"{'ID':<24} {'KIND':<10} {'STATE':<14} {'VERSION':<12} {'PIN':<14} NOTE")
    for r in results:
        pin = pin12(r["pin"]) if r.get("pin") else ""
        print(f"{r['id']:<24} {r['kind']:<10} {r['state']:<14} {str(r['version'])[:12]:<12} "
              f"{pin:<14} {r.get('reason', '')}")
        if r.get("log_tail") and r["state"] == "failed":
            for line in r["log_tail"].splitlines()[-8:]:
                print(f"    | {line}")
        if op == "info" and r.get("detail"):
            print(json.dumps(r["detail"], indent=1, sort_keys=True))


def main(argv: list[str] | None = None, paths: Paths | None = None,
         runner: Runner | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        return self_test()
    if "--list-verbs" in argv:
        print("\n".join(VERBS))
        return 0
    ap = argparse.ArgumentParser(prog="awnix component", description=__doc__.split("\n")[0])
    ap.add_argument("verb", choices=VERBS)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--wheelhouse")
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--serial", help="sync: also write the boot marker line to this path")
    a = ap.parse_args(argv)
    ids = [i.lower() for i in a.ids]
    try:
        tool = Tool(paths, runner)
    except CannotJudgeError as e:
        if a.json:
            print(json.dumps({"ok": False, "op": a.verb, "results": [], "reason": str(e)}))
        print(f"awnix component: cannot judge -- {e}", file=sys.stderr)
        if a.serial and a.verb == "sync":
            _serial(a.serial, "awnix-components: lock missing -- could not judge")
        return EXIT_UNJUDGED
    bad = [i for i in ids if not ID_RE.match(i)]
    if bad:
        print(f"awnix component: invalid id(s): {', '.join(bad)}", file=sys.stderr)
        return EXIT_FAILED
    need_ids = a.verb in ("info", "install", "remove", "rollback")
    if need_ids and not ids:
        print(f"awnix component {a.verb}: needs at least one id", file=sys.stderr)
        return EXIT_FAILED
    if a.verb in ("install", "remove", "rollback", "sync", "gc") and not _op_lock(tool.paths):
        msg = ("another component operation is already running on this box; "
               "wait for it to finish")
        if a.json:
            print(json.dumps({"ok": False, "op": a.verb, "results": [], "reason": msg}))
        print(f"awnix component: {msg}", file=sys.stderr)
        return EXIT_FAILED
    if a.verb == "list":
        rc, res = tool.list()
    elif a.verb == "info":
        rc, res = tool.info(ids[0])
    elif a.verb == "install":
        rc, res = tool.install(ids, force=a.force, wheelhouse=a.wheelhouse)
    elif a.verb == "remove":
        rc, res = tool.remove(ids)
    elif a.verb == "rollback":
        rc, res = tool.rollback(ids[0])
    elif a.verb == "sync":
        rc, res = tool.sync()
        line = tool.marker()
        print(line, file=sys.stderr)
        if a.serial:
            _serial(a.serial, line)
    else:
        rc, res = tool.gc(keep=a.keep)
    render(a.verb, rc, res, a.json)
    return rc


_LOCK_FD: list = []


def _op_lock(paths: Paths) -> bool:
    """One mutating op at a time (a click and the bot can race). Held until exit."""
    try:
        import fcntl  # noqa: PLC0415 - Linux only; the box always has it
    except ImportError:
        return True
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    f = open(paths.state_dir / ".op.lock", "a")  # noqa: SIM115 - held for the process
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCK_FD.append(f)
    return True


def _serial(path: str, line: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"awnix component: could not write the marker to {path}: {e}", file=sys.stderr)


# ── self-test (hermetic: temp dirs, a fake runner, no network) ────────────────


class FakeRunner(Runner):
    """Simulates python -m venv, pip, podman, systemctl and awpack."""

    def __init__(self, scripts: dict | None = None):
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()
        self.healthy = True
        self.scripts = scripts or {}

    def run(self, argv, timeout=900, env=None, cwd=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key in self.fail:
            if key in joined:
                return 1, f"simulated failure: {key}"
        if argv[1:3] == ["-m", "venv"]:
            v = Path(argv[3])
            (v / "bin").mkdir(parents=True, exist_ok=True)
            (v / "bin" / "python").write_text("", encoding="utf-8")
            return 0, "venv ok"
        if "pip" in argv and "install" in argv:
            v = Path(argv[0]).parent.parent
            for dist, scripts in self.scripts.items():
                di = v / "lib" / "python3.11" / "site-packages" / f"{dist}-1.0.dist-info"
                di.mkdir(parents=True, exist_ok=True)
                (di / "entry_points.txt").write_text(
                    "[console_scripts]\n" + "".join(f"{s} = x:y\n" for s in scripts),
                    encoding="utf-8")
                for s in scripts:
                    (v / "bin" / s).write_text("", encoding="utf-8")
            return 0, "Successfully installed"
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active\n") if self.healthy else (3, "failed\n")
        if Path(argv[0]).name == "awpack" and len(argv) > 2 and "--dest" in argv:
            # awpack's real contract: install refuses an installed pack unless --replace
            target = Path(argv[argv.index("--dest") + 1]) / argv[2]
            if argv[1] == "install":
                if target.exists() and "--replace" not in argv:
                    return 1, f"already installed at {target}; `awpack remove` first"
                target.mkdir(parents=True, exist_ok=True)
                (target / "pack.yaml").write_text("id: " + argv[2] + "\n", encoding="utf-8")
            elif argv[1] == "remove":
                shutil.rmtree(target, ignore_errors=True)
            return 0, "ok"
        return 0, "ok"

    WEIGHTS = b"fake-weights"

    def http_ok(self, url):
        return self.healthy

    def sleep(self, s):
        return None

    def download(self, url, dest, sha256):
        self.calls.append(["download", url])
        if hashlib.sha256(self.WEIGHTS).hexdigest() != sha256:
            return False, "sha256 mismatch"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.WEIGHTS)
        return True, "ok"


def _fixture(tmp: Path, lock_rows: list[dict], license_state: dict | None = None,
             baked: str | None = None, wheels: dict | None = None) -> Paths:
    root = tmp
    for d in ("share", "state", "shims", "quadlets", "env", "packs", "log", "bakedbin"):
        (root / d).mkdir(parents=True, exist_ok=True)
    lock = {"schema": 1, "catalogue_sha256": "ab" * 32, "generated_at": "x",
            "wheels": wheels or {}, "components": lock_rows}
    (root / "share" / "components.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    if baked is not None:
        (root / "share" / "components.d").mkdir(exist_ok=True)
        (root / "share" / "components.d" / "garg.yaml").write_text(baked, encoding="utf-8")
    if license_state is not None:
        (root / "status.json").write_text(json.dumps(license_state), encoding="utf-8")
    env = {
        "AWNIX_COMPONENT_LOCK": str(root / "share" / "components.lock.json"),
        "AWNIX_COMPONENTS_D": str(root / "share" / "components.d"),
        "AWNIX_COMPONENT_STATE_DIR": str(root / "state"),
        "AWNIX_SHIM_DIR": str(root / "shims"),
        "AWNIX_QUADLET_DIR": str(root / "quadlets"),
        "AWNIX_COMPONENT_ENV_DIR": str(root / "env"),
        "AWNIX_PACKS_DIR": str(root / "packs"),
        "AWNIX_COMPONENT_LOG": str(root / "log" / "components.log"),
        "AITHER_LICENSE_STATUS": str(root / "status.json"),
        "AWNIX_REGISTRY_AUTHFILE": str(root / "auth.json"),
        "AWNIX_PYTHON": "python3.11",
        "AWNIX_AWPACK": str(root / "awpack"),
        "AWNIX_BAKED_BIN_DIRS": str(root / "bakedbin"),
        "AWNIX_SYSTEMD_UNIT_DIRS": str(root / "systemd"),
        "AWNIX_COMPONENT_HEALTH_TIMEOUT": "6",
    }
    return Paths(env)


def bundle_fixture_rows() -> list[dict]:
    """A two-member stack + its bundle, shaped like the generated memory-stack rows."""
    lic = {"requires_license": True, "entitlement": "garg", "available": True, "reason": ""}
    wsha = hashlib.sha256(FakeRunner.WEIGHTS).hexdigest()
    db = {"id": "st-db", "kind": "container", "version": "16", "pin": "sha256:" + "1" * 64,
          "url": "docker.io/library/postgres", "title": "the database", **lic,
          "quadlet": {"network": "st", "aliases": ["db"], "public": True,
                      "volumes": [{"dest": "/var/lib/postgresql/data"}],
                      "env": {"POSTGRES_USER": "app"}, "secrets": ["POSTGRES_PASSWORD"],
                      "secrets_group": "st", "exec": ["-c", "listen_addresses=*"]}}
    api = {"id": "st-api", "kind": "container", "version": "1", "pin": "sha256:" + "2" * 64,
           "url": "ghcr.io/aitherium/garg-memory", "title": "the search service", **lic,
           "quadlet": {"network": "st", "aliases": ["api"], "after": ["st-db"],
                       "ports": [{"host": 8171, "container": 8171}],
                       "volumes": [{"dest": "/models"}, {"dest": "/logs", "shared": "st-logs"}],
                       "env": {"MODE": "has space"}, "secrets": ["POSTGRES_PASSWORD", "KEY"],
                       "secrets_group": "st", "health": "http://127.0.0.1:8171/health",
                       "files": [{"name": "w.gguf", "url": "https://x/w.gguf", "sha256": wsha}]}}
    bundle = {"id": "st", "kind": "bundle", "version": "1", "pin": "sha256:" + "3" * 64,
              "url": "", "title": "the stack", **lic, "requires": ["st-db", "st-api"],
              "endpoints": {"RETRIEVAL_URL": "http://127.0.0.1:8171"}}
    return [db, api, bundle]


def _bundle_self_test(tmp: Path, chk) -> bool:
    lic = {"state": "valid", "entitlements": {"packs": ["garg"], "images": []}}
    paths = _fixture(tmp, bundle_fixture_rows(), lic)
    fr = FakeRunner()
    tool = Tool(paths, fr)
    rc, res = tool.install(["st"])
    chk(rc == 0 and res[0]["state"] == "installed", "bundle install -> installed")
    st = tool.state["components"]
    chk(all(st.get(m, {}).get("status") == "installed" for m in ("st-db", "st-api")),
        "one bundle install installs every member")
    ep = (paths.env_dir / "st.endpoints.env").read_text(encoding="utf-8")
    chk("RETRIEVAL_URL=http://127.0.0.1:8171" in ep, "bundle writes its endpoints file")
    qa = (paths.quadlet_dir / "awnix-st-api.container").read_text(encoding="utf-8")
    chk("Network=awnix-st.network" in qa and "NetworkAlias=api" in qa
        and (paths.quadlet_dir / "awnix-st.network").is_file(),
        "members share a private network with aliases")
    chk('Environment="MODE=has space"' in qa and "After=network-online.target "
        "awnix-st-db.service" in qa, "env quoted; member ordered after its dependency")
    chk(f"Volume={paths.state_dir / '_shared' / 'st-logs'}:/logs:Z" in qa,
        "shared volume mounts one dir")
    qd = (paths.quadlet_dir / "awnix-st-db.container").read_text(encoding="utf-8")
    chk("Exec=-c listen_addresses=*" in qd, "exec args rendered")
    db_env = (paths.env_dir / "st-db.env").read_text(encoding="utf-8")
    api_env = (paths.env_dir / "st-api.env").read_text(encoding="utf-8")
    pw = [ln for ln in db_env.splitlines() if ln.startswith("POSTGRES_PASSWORD=")]
    chk(len(pw) == 1 and len(pw[0]) > 40 and pw[0] in api_env and "KEY=" not in db_env,
        "box-local secrets generated once, shared by group, each row gets only its own")
    chk((paths.state_dir / "st-api" / "data" / "w.gguf").read_bytes() == FakeRunner.WEIGHTS,
        "locked file downloaded into the data dir")
    pulls = [c for c in fr.calls if c[:2] == ["podman", "pull"]]
    chk(len(pulls) == 2, "one pull per member")
    row = tool.status_of("st")
    chk(row.get("progress", {}).get("done") is True and row["progress"]["ok"] is True
        and row.get("endpoints", {}).get("RETRIEVAL_URL"), "list shows progress + endpoints")

    rc, res = Tool(paths, fr).remove(["st"])
    t2 = Tool(paths, fr)
    chk(rc == 0 and not (paths.env_dir / "st.endpoints.env").exists()
        and "st-api" not in t2.state["components"] and "st-db" not in t2.state["components"],
        "bundle remove drops the endpoints file and every member")
    chk((paths.state_dir / "_secrets" / "st.env").read_text(encoding="utf-8").count(pw[0]) == 1,
        "secrets survive remove (a re-install opens the same vault)")

    # a member failing undoes the members THIS op added
    fr3 = FakeRunner()
    fr3.fail.add("garg-memory")
    rc, res = Tool(paths, fr3).install(["st"])
    t3 = Tool(paths, fr3)
    prog = t3.status_of("st").get("progress") or {}
    chk(rc == 1 and "st-db" not in t3.state["components"]
        and prog.get("ok") is False and "the search service" in prog.get("error", ""),
        "a failed member removes what this op added and says which part failed")

    # a bad sha refuses the file and installs nothing
    rows = bundle_fixture_rows()
    rows[1]["quadlet"]["files"][0]["sha256"] = "0" * 64
    p4 = _fixture(tmp / "badsha", rows, lic)
    rc, res = Tool(p4, FakeRunner()).install(["st-api"])
    chk(rc == 1 and not (p4.quadlet_dir / "awnix-st-api.container").exists(),
        "a file whose sha256 does not match is refused; no quadlet written")
    # not entitled -> needs-license, nothing pulled
    p5 = _fixture(tmp / "nolic", bundle_fixture_rows(), {"state": "valid",
                                                          "entitlements": {"packs": ["x"]}})
    fr5 = FakeRunner()
    rc, res = Tool(p5, fr5).install(["st"])
    chk(rc == 3 and not any(c[:2] == ["podman", "pull"] for c in fr5.calls),
        "an unlicensed bundle pulls nothing (exit 3)")
    return True


def self_test() -> int:
    fails: list[str] = []

    def chk(cond: bool, label: str) -> None:
        print(f"  {'ok ' if cond else 'FAIL'} {label}")
        if not cond:
            fails.append(label)

    if not symlinks_supported():
        LINKS.update(make=_emulated_symlink, read=_emulated_readlink)
        print("  note: this host cannot create symlinks; `current` is emulated "
              "(the image and CI use real symlinks)")
    d1, d2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    rows = [
        {"id": "brick", "name": "brick", "dist": "brick", "kind": "pypi", "version": "1.0",
         "pin": d1, "url": "https://files/brick-1.0.whl", "provides": [], "deps": ["dep==1"],
         "requires_license": False, "entitlement": "core", "available": True, "reason": ""},
        {"id": "gone", "kind": "pypi", "available": False, "reason": "status: planned",
         "requires_license": False, "pin": "", "version": ""},
        {"id": "svc", "kind": "container", "version": "latest@1", "pin": d1,
         "url": "ghcr.io/aitherium/svc", "requires_license": True, "entitlement": "addon:x",
         "available": True, "reason": "",
         "quadlet": {"ports": [{"host": 8100, "container": 8100}], "volumes": [{"dest": "/data"}],
                     "health": "http://127.0.0.1:8100/health"}},
    ]
    wheels = {"dep==1": {"url": "https://files/dep-1.whl", "sha256": "3" * 64}}
    baked = ("schema: 1\ncomponents:\n  - id: gargbot-backend\n    kind: baked\n"
             "    version: \"1\"\n    provides: [garg]\n    removable: false\n")
    with tempfile.TemporaryDirectory(prefix="awnix-component-st-") as t:
        tmp = Path(t)
        paths = _fixture(tmp, rows, {"state": "valid", "entitlements":
                                     {"packs": ["addon:x"], "images": []}}, baked, wheels)
        fr = FakeRunner({"brick": ["brick"]})
        tool = Tool(paths, fr)
        rc, res = tool.install(["brick"])
        chk(rc == 0 and res[0]["state"] == "installed", "pypi install -> installed")
        pip_calls = [c for c in fr.calls if "pip" in c]
        chk(bool(pip_calls) and all("--require-hashes" in c and "--no-deps" in c
                                    for c in pip_calls), "pip runs --require-hashes --no-deps")
        req = (paths.state_dir / "brick" / "111111111111" / "awnix-requirements.txt")
        chk("--hash=sha256:" + "3" * 64 in req.read_text(encoding="utf-8"),
            "every requirement line carries its lock hash")
        chk((paths.shim_dir / "brick").is_file(), "a shim is written for the console script")
        chk(read_link(paths.state_dir / "brick" / "current") == "111111111111",
            "current -> <pin12>")
        chk(not any(c[:3] == [sys.executable, "-m", "pip"] or c[:1] == ["pip"]
                    for c in fr.calls), "the system python's pip is never called")

        # needs-license and exit 3
        p2 = _fixture(tmp / "nolic", rows, None, baked, wheels)
        rc, res = Tool(p2, FakeRunner()).install(["svc"])
        chk(rc == 3 and res[0]["state"] == "needs-license",
            "requires_license with no status.json -> needs-license, exit 3")
        p3 = _fixture(tmp / "expired", rows, {"state": "expired", "entitlements":
                                               {"packs": ["addon:x"]}}, baked, wheels)
        rc, _ = Tool(p3, FakeRunner()).install(["svc"])
        chk(rc == 3, "an expired license is not entitled even with the pack listed")

        # baked rows
        rc, res = tool.remove(["gargbot-backend"])
        chk(rc == 1 and "not removable" in res[0]["reason"], "baked rows cannot be removed")
        _, lst = tool.list()
        chk(any(r["id"] == "gargbot-backend" and r["state"] == "baked" for r in lst),
            "list shows baked rows")
        chk(any(r["id"] == "gone" and r["state"] == "unavailable" and r["reason"] for r in lst),
            "unavailable rows carry the reason")

        # collision rule
        rows2 = [dict(rows[0], id="brick2", name="brick2", dist="brick2", pin=d2)]
        p4 = _fixture(tmp / "clash", rows2, None, baked, wheels)
        (tmp / "clash" / "bakedbin" / "brick2").write_text("", encoding="utf-8")
        rc, res = Tool(p4, FakeRunner({"brick2": ["brick2"]})).install(["brick2"])
        chk(rc == 1 and "shadow" in res[0]["reason"], "never shadows a baked command")
        chk(not (p4.state_dir / "brick2" / "222222222222").exists(),
            "a refused install leaves no venv behind")
        rc, res = Tool(p4, FakeRunner({"brick2": ["brick2"]})).install(["brick2"], force=True)
        chk(rc == 0, "--force overrides the collision rule")

        # container failing its probe restores the prior quadlet
        fr2 = FakeRunner()
        tool2 = Tool(paths, fr2)
        rc, res = tool2.install(["svc"])
        q = paths.quadlet_dir / "awnix-svc.container"
        text = q.read_text(encoding="utf-8")
        chk(rc == 0 and f"Image=ghcr.io/aitherium/svc@{d1}" in text
            and "PublishPort=127.0.0.1:8100:8100" in text, "container quadlet @sha256 on 127.0.0.1")
        chk(not any("--authfile" in c for c in fr2.calls if c[:2] == ["podman", "pull"]),
            "no --authfile when /etc/containers/auth.json is absent")
        chk("WantedBy=multi-user.target" in text and "aither-tier" not in text,
            "no installed ladder -> multi-user.target (a WantedBy on an absent target "
            "starts nothing)")
        sysd = paths.systemd_unit_dirs[0]
        sysd.mkdir(parents=True, exist_ok=True)
        for n in ("aither-tier3.target", "aither-tier50.target", "aither-tier99.target"):
            (sysd / n).write_text("[Unit]\n", encoding="utf-8")
        laddered = tool2._quadlet_text("svc", rows[2], "ghcr.io/aitherium/svc", d1)
        chk("WantedBy=aither-tier50.target" in laddered
            and "After=network-online.target aither-tier50.target" in laddered
            and "multi-user.target" not in laddered,
            "an installed ladder -> the highest normal rung, never 99 (CUT007)")
        for n in ("aither-tier3.target", "aither-tier50.target", "aither-tier99.target"):
            (sysd / n).unlink()
        (paths.authfile).write_text("{}", encoding="utf-8")
        rows[2]["pin"] = d2
        lock = json.loads(paths.lock.read_text(encoding="utf-8"))
        lock["components"] = rows
        paths.lock.write_text(json.dumps(lock), encoding="utf-8")
        tool3 = Tool(paths, FakeRunner())
        tool3.runner.healthy = False
        rc, res = tool3.install(["svc"])
        chk(rc == 1 and q.read_text(encoding="utf-8") == text,
            "a failed health probe restores the prior quadlet")
        chk(any("--authfile" in c for c in tool3.runner.calls if c[:2] == ["podman", "pull"]),
            "private pulls use --authfile /etc/containers/auth.json")

        # a pack follows a moved lock pin through `sync` (bootc upgrade/rollback)
        prow = {"id": "persona", "name": "persona", "kind": "pack", "version": "0.1.0",
                "pin": "a" * 40, "url": "awpack://persona", "requires_license": False,
                "entitlement": "core", "available": True, "reason": ""}
        p5 = _fixture(tmp / "pack", [prow], None, baked, wheels)
        Path(p5.awpack).write_text("", encoding="utf-8")
        fr5 = FakeRunner()
        rc, _ = Tool(p5, fr5).install(["persona"])
        chk(rc == 0, "pack install -> installed")
        lock5 = json.loads(p5.lock.read_text(encoding="utf-8"))
        lock5["components"] = [dict(prow, pin="b" * 40)]
        p5.lock.write_text(json.dumps(lock5), encoding="utf-8")
        rc, res = Tool(p5, fr5).sync()
        chk(rc == 0 and res[0]["state"] == "installed" and res[0]["pin"] == "b" * 40,
            "sync moves an installed pack to the new lock pin")
        chk(any("--replace" in c for c in fr5.calls if c[1:2] == ["install"]),
            "a pack re-pin passes awpack --replace")

        # a bundle: one install = every member, endpoints written, undo on failure
        bad_ok = _bundle_self_test(tmp / "bundle", chk)
        chk(bad_ok, "bundle self-test ran")

        # missing lock -> 2
        empty = Paths({**{k: v for k, v in os.environ.items()},
                       "AWNIX_COMPONENT_LOCK": str(tmp / "nope.json")})
        chk(main(["list"], paths=empty) == 2, "a missing lock exits 2")

    print(f"self-test: {'FAIL' if fails else 'ok'} ({len(fails)} failed)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
