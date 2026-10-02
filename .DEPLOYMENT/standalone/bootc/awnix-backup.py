#!/usr/bin/python3.11
"""awnix-backup -- `awnix backup|restore|reset` for every awnix variant.

Installed at /usr/libexec/awnix/awnix-backup; `awnix restore` and `awnix reset`
are sh shims that exec this file with the verb prepended, so the dispatcher
finds all three verbs by dropped file.

WHAT GETS BACKED UP is data, not code: per-variant profiles in
/usr/lib/awnix/backup.d/*.json (awnix base, garg, aitheros), overridable by a
same-named file in /etc/awnix/backup.d/. Each profile lists items with a kind
(dir, file, sqlite, qdrant, podman-volume, users) and a class (customer,
config, credential, license, model, cache).

THE RULES THIS TOOL ENFORCES

* A backup nobody has restored is a hypothesis. `backup create` ends with a
  real scratch restore and a per-item digest comparison, unless --no-verify.
* A half-restore is worse than none. `restore` verifies first, then copies each
  item beside its target, moves the live tree aside and renames the new one in.
  Every replaced tree is kept under /var/lib/awnix/restore-replaced/<ts> and
  `restore --undo <ts>` puts it back. A failure mid-way rolls back what it did.
* Credentials are never backed up. They are re-derived after a restore by
  `aitheros license refresh`.
* Reset is typed-confirmation only (`erase <hostname>`), never exposed over
  HTTP, and claims NIST SP 800-88 Clear (logical) only: files are unlinked and
  the filesystem is trimmed. Purge and crypto-erase are NOT claimed.

Snapshots are awshare bundles through awrecover (lazy import; missing -> exit 2).

Exit codes: 0 ok, 1 unverifiable/refused, 2 could-not-judge.
Test seams: AWNIX_ROOT (path prefix), AWNIX_BACKUP_PROFILES_DIR,
AWNIX_BACKUP_FAKE_SERVICES=1 (no systemctl, port kills, podman, fstrim,
userdel or reboot: each is logged to /var/log/awnix/backup-fake-services.log).
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = 1
VERBS = ("backup", "restore", "reset")
BACKUP_SUBVERBS = ("create", "list", "verify", "drop")
KINDS = ("dir", "file", "sqlite", "qdrant", "podman-volume", "users")
FS_KINDS = ("dir", "file", "sqlite", "qdrant")
CLASSES = ("customer", "config", "credential", "license", "model", "cache")
SCOPES = ("data", "factory")
#: What each class's `reset` list must be. The CLI obeys the declared list; the
#: checker (AXB001) holds the declaration to this table, so a profile cannot
#: quietly keep customer data through a data reset or wipe a license on one.
RESET_FOR_CLASS = {
    "customer": ["data", "factory"],
    "config": ["factory"],
    "credential": ["factory"],
    "license": ["factory"],
    "model": ["factory"],
    "cache": ["data", "factory"],
}
ALLOWED_PREFIXES = ("/var/", "/etc/", "/home/")
FORBIDDEN_PREFIXES = ("/usr", "/boot", "/sysroot", "/opt", "/run", "/proc", "/sys", "/dev")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")

STORE = "/var/lib/awnix/backups"
STAGING = "/var/lib/awnix/backup-staging"
REPLACED = "/var/lib/awnix/restore-replaced"
RECEIPT = "/var/lib/awnix/reset-receipt.json"
RELEASE_ENV = "/usr/lib/awnix/release.env"
VENDOR_PROFILES = "/usr/lib/awnix/backup.d"
ADMIN_PROFILES = "/etc/awnix/backup.d"
FAKE_LOG = "/var/log/awnix/backup-fake-services.log"
FAKE_VOLUMES = "/var/lib/awnix/fake-podman-volumes"
MANIFEST_NAME = "backup-manifest.json"

EXIT_OK, EXIT_REFUSED, EXIT_CNJ = 0, 1, 2

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: A manifest record id is `<profile id>.<item id>`; it is joined into scratch
#: and restore-replaced paths, so it is validated before any join.
_REC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.[a-z0-9][a-z0-9_-]{0,63}$")
_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")


class RefusedError(Exception):
    """Exit 1: the request was judged and refused (tamper, mismatch, wrong phrase)."""


class CannotJudgeError(Exception):
    """Exit 2: a precondition is missing, so no verdict is honest."""


# --------------------------------------------------------------------------- context


class Ctx:
    def __init__(self, root: Optional[str] = None, fake: Optional[bool] = None,
                 profiles_dir: Optional[str] = None) -> None:
        env_root = os.environ.get("AWNIX_ROOT", "/")
        self.root = root if root is not None else env_root
        if fake is None:
            fake = os.environ.get("AWNIX_BACKUP_FAKE_SERVICES") == "1"
        self.fake = bool(fake)
        self.profiles_dir = profiles_dir or os.environ.get("AWNIX_BACKUP_PROFILES_DIR") or None
        self.notes: List[str] = []
        self.errors: List[str] = []

    def p(self, path: str) -> Path:
        if self.root in ("", "/"):
            return Path(path)
        return Path(self.root) / path.lstrip("/")

    def fake_log(self, line: str) -> None:
        f = self.p(FAKE_LOG)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso() -> str:
    return _now().isoformat(timespec="seconds")


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%SZ")


def _awrecover() -> Tuple[Any, Any]:
    try:
        import awrecover  # noqa: PLC0415 -- lazy by design: missing is exit 2
        import awshare  # noqa: PLC0415
    except ImportError as exc:
        raise CannotJudgeError(
            f"awrecover/awshare are not importable ({exc}). A backup tool "
            f"without its archive layer would write records that look like "
            f"backups and are not") from exc
    return awrecover, awshare


def _mkdir_private(path: Path, only_if_new: bool = False) -> None:
    """Create `path` 0700. With only_if_new, an EXISTING directory keeps its mode:
    `--to /var/tmp` or `--to /home/admin` must never turn a shared dir root-only."""
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if only_if_new and existed:
        return
    try:
        os.chmod(path, 0o700)
    except OSError as exc:  # vfat USB sticks refuse modes; the data is still written
        _ = exc


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError as exc:
            _ = exc
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(base: Path) -> Tuple[int, int, str]:
    """(files, bytes, sha256) over every regular file under `base`, path-sorted."""
    h = hashlib.sha256()
    files = total = 0
    if base.is_file():
        size = base.stat().st_size
        h.update(f"{base.name}\0{_sha256_file(base)}\0{size}\n".encode())
        return 1, size, h.hexdigest()
    if not base.is_dir():
        return 0, 0, h.hexdigest()
    entries = []
    for dirpath, _dirs, names in os.walk(base):
        for n in names:
            fp = Path(dirpath) / n
            if fp.is_symlink() or not fp.is_file():
                continue
            entries.append((fp.relative_to(base).as_posix(), fp))
    for rel, fp in sorted(entries):
        size = fp.stat().st_size
        h.update(f"{rel}\0{_sha256_file(fp)}\0{size}\n".encode())
        files += 1
        total += size
    return files, total, h.hexdigest()


# --------------------------------------------------------------------------- profiles


def _path_ok(path: str) -> Optional[str]:
    if not path.startswith("/"):
        return f"path {path!r} is not absolute"
    norm = posixpath.normpath(path)
    if norm != path.rstrip("/") or ".." in path.split("/"):
        return f"path {path!r} is not normalised"
    for bad in FORBIDDEN_PREFIXES:
        if norm == bad or norm.startswith(bad + "/"):
            return f"path {path!r} is under {bad}, which a reset must never touch"
    if not norm.startswith(ALLOWED_PREFIXES):
        return f"path {path!r} is outside /var, /etc and /home"
    if len([p for p in norm.split("/") if p]) < 2:
        return f"path {path!r} is a whole top-level tree"
    return None


def validate_profile(data: Any) -> List[str]:
    """Every schema error in one profile. Shared with check_awnix_backup (AXB001-003)."""
    errs: List[str] = []
    if not isinstance(data, dict):
        return ["profile is not a JSON object"]
    if data.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}, got {data.get('schema')!r}")
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        errs.append(f"id {pid!r} must match {_ID_RE.pattern}")
    variants = data.get("variants")
    if not isinstance(variants, list) or not variants or \
            not all(isinstance(v, str) and v for v in variants):
        errs.append("variants must be a non-empty list of strings ('*' = all)")
    for key in ("quiesce", "resume"):
        blk = data.get(key)
        if not isinstance(blk, dict):
            errs.append(f"{key} must be an object")
            continue
        units = blk.get("units", [])
        if not isinstance(units, list) or not all(isinstance(u, str) for u in units):
            errs.append(f"{key}.units must be a list of strings")
        if key == "quiesce":
            ports = blk.get("ports", [])
            if not isinstance(ports, list) or not all(
                    isinstance(p, int) and 0 < p < 65536 for p in ports):
                errs.append("quiesce.ports must be a list of port numbers")
    health = data.get("health", [])
    if not isinstance(health, list):
        errs.append("health must be a list")
    else:
        for h in health:
            url = h.get("url") if isinstance(h, dict) else None
            if not isinstance(url, str) or not re.match(
                    r"^https?://(127\.0\.0\.1|\[::1\]|localhost)(:\d+)?/", url):
                errs.append(f"health entry {h!r} must be a loopback http(s) url")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        errs.append("items must be a non-empty list")
        return errs
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            errs.append(f"item {it!r} is not an object")
            continue
        iid = it.get("id")
        where = f"item {iid!r}"
        if not isinstance(iid, str) or not _ID_RE.match(iid):
            errs.append(f"{where}: id must match {_ID_RE.pattern}")
        elif iid in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(iid)
        kind, cls = it.get("kind"), it.get("class")
        if kind not in KINDS:
            errs.append(f"{where}: kind {kind!r} not in {KINDS}")
        if cls not in CLASSES:
            errs.append(f"{where}: class {cls!r} not in {CLASSES}")
        if not isinstance(it.get("backup"), bool):
            errs.append(f"{where}: backup must be true or false")
        reset = it.get("reset")
        if not isinstance(reset, list) or any(s not in SCOPES for s in reset):
            errs.append(f"{where}: reset must be a list drawn from {SCOPES}")
        elif cls in RESET_FOR_CLASS and cls != "cache" and \
                sorted(reset) != sorted(RESET_FOR_CLASS[cls]):
            errs.append(f"{where}: class {cls} must reset on {RESET_FOR_CLASS[cls]}, "
                        f"declares {reset}")
        # AXB003: credentials are re-derived, never carried in a backup.
        if cls == "credential" and it.get("backup") is True:
            errs.append(f"{where}: AXB003 class=credential with backup=true")
        if kind == "users" and it.get("backup") is True:
            errs.append(f"{where}: kind=users cannot be backed up")
        path = it.get("path")
        if not isinstance(path, str) or not path:
            errs.append(f"{where}: path missing")
        elif kind == "podman-volume":
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.*-]*$", path):
                errs.append(f"{where}: podman-volume path must be a volume name glob")
        else:
            bad = _path_ok(path)
            if bad:
                errs.append(f"{where}: AXB002 {bad}")
        excl = it.get("exclude", [])
        if not isinstance(excl, list) or any(
                not isinstance(e, str) or not e or "/" in e or "\\" in e or e in (".", "..")
                for e in excl):
            errs.append(f"{where}: exclude must be a list of plain child names")
        if excl and kind not in ("dir", "qdrant"):
            errs.append(f"{where}: exclude only applies to dir/qdrant items")
    return errs


def read_variant(ctx: Ctx) -> str:
    env = os.environ.get("AWNIX_VARIANT")
    if env:
        return env
    f = ctx.p(RELEASE_ENV)
    if f.is_file():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("AWNIX_VARIANT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or "awnix"
    return "awnix"


def _profile_files(ctx: Ctx) -> Dict[str, Path]:
    dirs = [Path(ctx.profiles_dir)] if ctx.profiles_dir else \
        [ctx.p(VENDOR_PROFILES), ctx.p(ADMIN_PROFILES)]
    by_name: Dict[str, Path] = {}
    for d in dirs:
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                by_name[f.name] = f  # admin dir comes second and wins
    return by_name


def load_profiles(ctx: Ctx, variant: str) -> List[Dict[str, Any]]:
    files = _profile_files(ctx)
    if not files:
        raise CannotJudgeError("no backup profiles found (backup.d is empty or missing)")
    out = []
    for name in sorted(files):
        try:
            data = json.loads(files[name].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CannotJudgeError(f"profile {files[name]} is unreadable: {exc}") from exc
        errs = validate_profile(data)
        if errs:
            raise CannotJudgeError(f"profile {files[name]} is invalid: {'; '.join(errs)}")
        vs = data["variants"]
        if "*" in vs or variant in vs:
            out.append(data)
    if not out:
        raise CannotJudgeError(f"no backup profile applies to variant {variant!r}")
    return out


def hostname(ctx: Ctx) -> str:
    f = ctx.p("/etc/hostname")
    if f.is_file():
        first = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if first and first[0].strip():
            return first[0].strip()
    return socket.gethostname()


def booted_digest(ctx: Ctx) -> Optional[str]:
    if ctx.fake or not shutil.which("bootc"):
        return None
    try:
        r = subprocess.run(["bootc", "status", "--json"], capture_output=True,
                           timeout=60, check=False)
        d = json.loads(r.stdout or b"{}")
        return (((d.get("status") or {}).get("booted") or {}).get("image") or {}).get(
            "imageDigest")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        ctx.notes.append(f"bootc status unreadable: {exc}")
        return None


# --------------------------------------------------------------------------- services


def _run(ctx: Ctx, argv: List[str], timeout: int = 120) -> int:
    if ctx.fake:
        ctx.fake_log(" ".join(argv))
        return 0
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout,
                              check=False).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        ctx.notes.append(f"{argv[0]} failed: {exc}")
        return 127


def _pids_on_port(port: int) -> Optional[List[int]]:
    """Listener pids on `port`, or None when `ss` cannot answer. None is NOT
    'nothing listening': copying a data dir under a live writer is the failure
    quiesce exists to prevent, so the caller must refuse rather than assume."""
    try:
        r = subprocess.run(["ss", "-Htlnp", f"sport = :{port}"], capture_output=True,
                           timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return sorted({int(x) for x in re.findall(rb"pid=(\d+)", r.stdout)})


def _listeners(port: int) -> List[int]:
    pids = _pids_on_port(port)
    if pids is None:
        raise CannotJudgeError(
            f"cannot see listeners on :{port} (`ss` missing or failed); refusing to "
            f"copy or replace data that a live service may still be writing")
    return pids


def _kill_port(ctx: Ctx, port: int) -> None:
    if ctx.fake:
        ctx.fake_log(f"kill-port {port}")
        return
    pids = _listeners(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            ctx.notes.append(f"SIGTERM {pid}: {exc}")
    deadline = time.monotonic() + 15
    while pids and time.monotonic() < deadline and _listeners(port):
        time.sleep(0.5)
    kill9 = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in _listeners(port):
        try:
            os.kill(pid, kill9)
        except OSError as exc:
            ctx.notes.append(f"SIGKILL {pid}: {exc}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _listeners(port):
        time.sleep(0.25)
    left = _listeners(port)
    if left:
        raise CannotJudgeError(f"port {port} is still held by pid(s) {left} after SIGKILL; "
                               f"refusing to touch its data")


#: `systemctl stop` exit 5 = unit not loaded (not installed on this variant):
#: nothing is running under that name, which is what quiesce needs.
_STOP_OK = (0, 5)


def quiesce(ctx: Ctx, profiles: List[Dict[str, Any]]) -> None:
    """Stop every writer the profiles name, or raise. Fails CLOSED: a failed stop
    or an unkillable port listener is exit 2, never a copy of a live tree.
    Callers must resume() in a finally that also covers a raise from here."""
    for prof in profiles:
        units = prof["quiesce"].get("units", [])
        if units:
            rc = _run(ctx, ["systemctl", "stop", *units])
            if rc not in _STOP_OK:
                raise CannotJudgeError(f"systemctl stop {' '.join(units)} exited {rc}; "
                                       f"services may still be writing")
        for port in prof["quiesce"].get("ports", []):
            _kill_port(ctx, port)


def resume(ctx: Ctx, profiles: List[Dict[str, Any]]) -> None:
    for prof in profiles:
        units = prof["resume"].get("units", [])
        if units:
            _run(ctx, ["systemctl", "start", "--no-block", *units])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from a loopback listener proves it is serving (e.g. the garg TLS
    front door answers plain http with a 308). Following it into a self-signed
    https cert would turn a healthy box into a false 'unhealthy'."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _probe(url: str) -> Tuple[bool, str]:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=5) as r:  # noqa: S310 -- loopback only (AXB001)
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        return 300 <= exc.code < 400, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def health(ctx: Ctx, profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    timeout = float(os.environ.get("AWNIX_BACKUP_HEALTH_TIMEOUT", "90"))
    for prof in profiles:
        for h in prof.get("health", []):
            url = h["url"]
            if ctx.fake:
                out.append({"url": url, "ok": True, "detail": "skipped (fake services)"})
                continue
            deadline = time.monotonic() + timeout
            ok, detail = False, ""
            while time.monotonic() < deadline:
                ok, detail = _probe(url)
                if ok:
                    break
                time.sleep(2)
            out.append({"url": url, "ok": ok, "detail": detail})
    return out


# --------------------------------------------------------------------------- podman


def _volumes(ctx: Ctx, pattern: str) -> List[str]:
    if ctx.fake:
        d = ctx.p(FAKE_VOLUMES)
        names = [p.name for p in d.iterdir() if p.is_dir()] if d.is_dir() else []
    else:
        try:
            r = subprocess.run(["podman", "volume", "ls", "--format", "{{.Name}}"],
                               capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CannotJudgeError(f"podman volume ls failed: {exc}") from exc
        if r.returncode != 0:
            raise CannotJudgeError(f"podman volume ls exited {r.returncode}")
        names = r.stdout.decode("utf-8", "replace").split()
    return sorted(n for n in names if fnmatch.fnmatchcase(n, pattern))


def _volume_export(ctx: Ctx, name: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if ctx.fake:
        src = ctx.p(FAKE_VOLUMES) / name
        with tarfile.open(out, "w") as tf:
            for fp in sorted(src.rglob("*")):
                if fp.is_file() and not fp.is_symlink():
                    tf.add(fp, arcname=fp.relative_to(src).as_posix())
        return
    if _run(ctx, ["podman", "volume", "export", name, "--output", str(out)], 3600) != 0:
        raise CannotJudgeError(f"podman volume export {name} failed")


def _volume_import(ctx: Ctx, name: str, tar: Path) -> None:
    if ctx.fake:
        dst = ctx.p(FAKE_VOLUMES) / name
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        with tarfile.open(tar, "r") as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.name.startswith("/") or ".." in m.name.split("/"):
                    continue
                target = dst / m.name
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is not None:
                    target.write_bytes(src.read())
        return
    _run(ctx, ["podman", "volume", "create", "--ignore", name])
    if _run(ctx, ["podman", "volume", "import", name, str(tar)], 3600) != 0:
        raise CannotJudgeError(f"podman volume import {name} failed")


def _volume_rm(ctx: Ctx, name: str) -> None:
    if ctx.fake:
        shutil.rmtree(ctx.p(FAKE_VOLUMES) / name, ignore_errors=True)
        ctx.fake_log(f"podman volume rm -f {name}")
        return
    _run(ctx, ["podman", "volume", "rm", "-f", name])


# --------------------------------------------------------------------------- staging


def _copy_tree(src: Path, dst: Path, exclude: List[str]) -> None:
    """Regular files and dirs only. Symlinks are skipped: awshare refuses them
    as archive members (a symlink is the classic extraction escape)."""
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        if child.name in exclude or child.is_symlink():
            continue
        if child.is_dir():
            _copy_tree(child, dst / child.name, [])
        elif child.is_file():
            shutil.copy2(child, dst / child.name)


def _stage_item(ctx: Ctx, item: Dict[str, Any], base: Path) -> Dict[str, Any]:
    kind, src = item["kind"], ctx.p(item["path"])
    present = True
    if kind in ("dir", "qdrant"):
        present = src.is_dir()
        if present:
            _copy_tree(src, base / "tree", item.get("exclude", []))
    elif kind == "file":
        present = src.is_file()
        if present:
            base.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, base / "file")
    elif kind == "sqlite":
        present = src.is_file()
        if present:
            base.mkdir(parents=True, exist_ok=True)
            s = sqlite3.connect(str(src))
            d = sqlite3.connect(str(base / "db"))
            try:
                s.backup(d)
            finally:
                d.close()
                s.close()
    elif kind == "podman-volume":
        names = _volumes(ctx, item["path"])
        present = bool(names)
        for n in names:
            _volume_export(ctx, n, base / "volumes" / f"{n}.tar")
    files, size, digest = tree_digest(base)
    return {"kind": kind, "path": item["path"], "class": item["class"],
            "present": present, "files": files, "bytes": size,
            "sha256": digest if present else None}


def default_label(ctx: Ctx) -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "-", hostname(ctx)).strip(".-") or "awnix"
    return f"{host}-{_stamp()}"


def _store(ctx: Ctx, where: Optional[str]) -> Path:
    return Path(where) if where else ctx.p(STORE)


def _expect_key(a: argparse.Namespace) -> Optional[str]:
    """The trusted awseal publisher key from --expect-key (64 hex chars) or
    --expect-key-file (a file holding them). Never private key material."""
    raw = getattr(a, "expect_key", None)
    kfile = getattr(a, "expect_key_file", None)
    if raw and kfile:
        raise RefusedError("pass --expect-key or --expect-key-file, not both")
    if kfile:
        try:
            raw = Path(kfile).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CannotJudgeError(f"--expect-key-file {kfile}: {exc}") from exc
    if not raw:
        return None
    key = raw.strip().lower()
    if not _PUBKEY_RE.match(key):
        raise RefusedError("expected key must be a 64-hex-char awseal PUBLIC key")
    return key


def _is_own_store(ctx: Ctx, store: Path) -> bool:
    """The box's own root-only store. Anything else (USB, NFS, --to DIR) is
    attacker-reachable media, whose index and digests the attacker also writes."""
    try:
        return store.resolve() == ctx.p(STORE).resolve()
    except OSError:
        return False


def _unpack_verified(ctx: Ctx, store: Path, label: str,
                     expect_key: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
    """Restore `label` into a fresh scratch dir and prove every item digest.

    awrecover.restore -> awshare.fetch checks the archive digest, member paths
    and (when sealed, or when `expect_key` is given) the awseal signature AND
    that it was made by `expect_key` before any byte lands. An unsealed backup
    with an expected key is refused. The per-item digests then prove the payload
    is the one the manifest describes -- which proves integrity only; WHO made
    it is proven by expect_key alone.
    """
    awrecover, _ = _awrecover()
    if not _LABEL_RE.match(label or ""):
        raise RefusedError(f"label {label!r} must match {_LABEL_RE.pattern}")
    try:
        index = awrecover.load_index(store)
    except awrecover.RecoverError as exc:
        raise CannotJudgeError(str(exc)) from exc
    if label not in index:
        raise RefusedError(f"no backup labelled {label!r} in {store}")
    parent = ctx.p(STAGING)
    _mkdir_private(parent)
    scratch = parent / f"unpack-{label}-{os.getpid()}-{int(time.time() * 1000)}"
    try:
        got = awrecover.restore(store, label, scratch, expect_key=expect_key,
                                keep_replaced=False)
    except Exception as exc:  # noqa: BLE001 -- RecoverError, SealError (no seal), ...: all refuse
        shutil.rmtree(scratch, ignore_errors=True)
        raise RefusedError(f"backup {label!r} is NOT restorable: {exc}") from exc
    seal = (got or {}).get("seal") if isinstance(got, dict) else None
    if expect_key is not None and not (isinstance(seal, dict) and seal.get("ok")
                                       and seal.get("key_trusted") is True):
        shutil.rmtree(scratch, ignore_errors=True)
        raise RefusedError(f"backup {label!r} is not sealed by the expected key")
    try:
        man = json.loads((scratch / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise RefusedError(f"backup {label!r} has no readable {MANIFEST_NAME}: {exc}") from exc
    if man.get("schema") != SCHEMA:
        shutil.rmtree(scratch, ignore_errors=True)
        raise RefusedError(f"backup {label!r} manifest schema {man.get('schema')!r} is unknown")
    items = man.get("items", [])
    if not isinstance(items, list):
        shutil.rmtree(scratch, ignore_errors=True)
        raise RefusedError(f"backup {label!r} manifest items is not a list")
    for rec in items:
        rid = rec.get("id") if isinstance(rec, dict) else None
        if not isinstance(rid, str) or not _REC_ID_RE.match(rid):
            shutil.rmtree(scratch, ignore_errors=True)
            raise RefusedError(f"backup {label!r} has a malformed item id {rid!r}")
        if not rec.get("present"):
            continue
        _, _, digest = tree_digest(scratch / "items" / rid)
        if digest != rec.get("sha256"):
            shutil.rmtree(scratch, ignore_errors=True)
            raise RefusedError(f"backup {label!r} item {rid} digest mismatch")
    man["_trust"] = {"sealed": bool(isinstance(seal, dict)),
                     "key_trusted": bool(expect_key is not None)}
    return scratch, man


# --------------------------------------------------------------------------- backup verbs


def backup_create(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    awrecover, _ = _awrecover()
    variant = read_variant(ctx)
    profiles = load_profiles(ctx, variant)
    store = _store(ctx, a.to)
    label = a.label or default_label(ctx)
    if not _LABEL_RE.match(label):
        raise RefusedError(f"label {label!r} must match {_LABEL_RE.pattern}")
    if a.seal and not a.key:
        raise RefusedError("--seal needs --key <path to the awseal private key file>")
    _mkdir_private(store, only_if_new=True)
    parent = ctx.p(STAGING)
    _mkdir_private(parent)
    stage = Path(tempfile.mkdtemp(dir=str(parent), prefix=f"create-{label}-"))
    items: List[Dict[str, Any]] = []
    try:
        quiesced = False
        try:
            if not a.live:
                quiesced = True  # before the call: a quiesce that raises half-way resumes
                quiesce(ctx, profiles)
            for prof in profiles:
                for it in prof["items"]:
                    if not it["backup"] or it["class"] == "credential" or it["kind"] == "users":
                        continue
                    iid = f"{prof['id']}.{it['id']}"
                    rec = _stage_item(ctx, it, stage / "items" / iid)
                    rec["id"] = iid
                    items.append(rec)
        finally:
            if quiesced:
                resume(ctx, profiles)
        manifest = {
            "schema": SCHEMA, "variant": variant, "booted_digest": booted_digest(ctx),
            "hostname": hostname(ctx), "created": _iso(), "live": bool(a.live),
            "profiles": [p["id"] for p in profiles], "items": items,
        }
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                           encoding="utf-8")
        meta = {"awnix_backup": SCHEMA, "variant": variant, "hostname": manifest["hostname"],
                "items": len(items), "bytes": sum(i["bytes"] for i in items)}
        try:
            snap = awrecover.snapshot(stage, store, label, seal=bool(a.seal),
                                      key_path=Path(a.key) if a.key else None, meta=meta)
        except awrecover.RecoverError as exc:
            raise RefusedError(f"snapshot refused: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    out: Dict[str, Any] = {"schema": SCHEMA, "op": "create", "label": label,
                           "store": str(store), "digest": snap.digest, "files": snap.files,
                           "items": items, "verified": False, "pruned": []}
    if not a.no_verify:
        scratch, _ = _unpack_verified(ctx, store, label)
        shutil.rmtree(scratch, ignore_errors=True)
        out["verified"] = True
    if a.keep:
        out["pruned"] = _prune(store, a.keep, label)
    word = "verified" if out["verified"] else "UNVERIFIED"
    out["marker"] = f"awnix-backup: {label} {word} files={snap.files} digest={snap.digest[:12]}"
    return out


def _prune(store: Path, keep: int, protect: str) -> List[str]:
    awrecover, _ = _awrecover()
    others = [s for s in awrecover.list_snapshots(store)
              if s.meta.get("awnix_backup") and s.label != protect]
    dropped = []
    for s in others[max(keep - 1, 0):]:  # the backup just taken is always kept
        awrecover.drop(store, s.label)
        dropped.append(s.label)
    return dropped


def backup_list(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    awrecover, _ = _awrecover()
    store = _store(ctx, a.src)
    if not store.is_dir():
        return {"schema": SCHEMA, "op": "list", "store": str(store), "backups": []}
    try:
        snaps = awrecover.list_snapshots(store)
    except awrecover.RecoverError as exc:
        raise CannotJudgeError(str(exc)) from exc
    return {"schema": SCHEMA, "op": "list", "store": str(store),
            "backups": [s.to_dict() for s in snaps if s.meta.get("awnix_backup")]}


def backup_verify(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    awrecover, _ = _awrecover()
    store = _store(ctx, a.src)
    scratch, man = _unpack_verified(ctx, store, a.label, _expect_key(a))
    shutil.rmtree(scratch, ignore_errors=True)
    snap = awrecover.load_index(store)[a.label]
    return {"schema": SCHEMA, "op": "verify", "label": a.label, "restorable": True,
            "variant": man.get("variant"), "items": len(man.get("items", [])),
            "digest": snap.digest, "sealed": man["_trust"]["sealed"],
            "key_trusted": man["_trust"]["key_trusted"],
            "marker": f"awnix-backup: {a.label} verified files={snap.files} "
                      f"digest={snap.digest[:12]}"}


def backup_drop(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    awrecover, _ = _awrecover()
    store = _store(ctx, a.src)
    try:
        awrecover.drop(store, a.label)
    except awrecover.RecoverError as exc:
        raise RefusedError(str(exc)) from exc
    return {"schema": SCHEMA, "op": "drop", "label": a.label, "store": str(store)}


# --------------------------------------------------------------------------- restore


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _payload(kind: str, base: Path) -> Path:
    return {"dir": base / "tree", "qdrant": base / "tree", "file": base / "file",
            "sqlite": base / "db"}[kind]


def _swap_in(ctx: Ctx, rec: Dict[str, Any], base: Path, saved_root: Path, ts: str,
             exclude: List[str]) -> Dict[str, Any]:
    kind, target = rec["kind"], ctx.p(rec["path"])
    entry: Dict[str, Any] = {"id": rec["id"], "kind": kind, "path": rec["path"],
                             "existed": False, "saved": None, "sidecars": []}
    save_dir = saved_root / rec["id"]
    if kind == "podman-volume":
        vols = sorted((base / "volumes").glob("*.tar")) if (base / "volumes").is_dir() else []
        entry["volumes"] = []
        for tar in vols:
            name = tar.name[:-4]
            # The tar NAME comes from the archive: hold it to the local profile's
            # volume glob, or a crafted backup imports over any podman volume.
            if not _VOLUME_RE.match(name) or not fnmatch.fnmatchcase(name, rec["path"]):
                raise RefusedError(f"item {rec['id']}: volume {name!r} is outside the "
                                   f"profile glob {rec['path']!r}")
            existed = name in _volumes(ctx, name)
            if existed:
                _volume_export(ctx, name, save_dir / f"{name}.tar")
            _volume_import(ctx, name, tar)
            entry["volumes"].append({"name": name, "existed": existed})
        return entry
    payload = _payload(kind, base)
    target.parent.mkdir(parents=True, exist_ok=True)
    new = target.with_name(f"{target.name}.awnix-restore-{ts}")
    aside = target.with_name(f"{target.name}.awnix-replaced-{ts}")
    _remove(new)
    if kind in ("dir", "qdrant"):
        if payload.is_dir():
            _copy_tree(payload, new, [])
        else:
            new.mkdir(parents=True)
        for name in exclude:  # keep what the backup deliberately did not carry
            cur = target / name
            if cur.is_dir() and not cur.is_symlink():
                _copy_tree(cur, new / name, [])
            elif cur.is_file():
                shutil.copy2(cur, new / name)
    else:
        shutil.copy2(payload, new)
    if target.exists() or target.is_symlink():
        entry["existed"] = True
        os.replace(target, aside)
    if kind == "sqlite":
        for suf in SQLITE_SIDECARS:
            side = Path(str(target) + suf)
            if side.exists():
                dst = save_dir / "sidecars" / side.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(side), str(dst))
                entry["sidecars"].append(side.name)
    os.replace(new, target)
    if entry["existed"]:
        save_dir.mkdir(parents=True, exist_ok=True)
        dst = save_dir / "data"
        shutil.move(str(aside), str(dst))
        entry["saved"] = str(dst.relative_to(saved_root).as_posix())
    return entry


def _swap_back(ctx: Ctx, entry: Dict[str, Any], saved_root: Path) -> None:
    kind = entry["kind"]
    save_dir = saved_root / entry["id"]
    if kind == "podman-volume":
        for v in entry.get("volumes", []):
            if v["existed"]:
                _volume_import(ctx, v["name"], save_dir / f"{v['name']}.tar")
            else:
                _volume_rm(ctx, v["name"])
        return
    target = ctx.p(entry["path"])
    _remove(target)
    if entry["existed"] and entry.get("saved"):
        shutil.move(str(saved_root / entry["saved"]), str(target))
    for name in entry.get("sidecars", []):
        src = save_dir / "sidecars" / name
        if src.exists():
            shutil.move(str(src), str(target.with_name(name)))


def restorable_items(profiles: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """`<profile>.<item>` -> item, for exactly the items backup_create carries."""
    out: Dict[str, Dict[str, Any]] = {}
    for prof in profiles:
        for it in prof["items"]:
            if not it["backup"] or it["class"] == "credential" or it["kind"] == "users":
                continue
            out[f"{prof['id']}.{it['id']}"] = it
    return out


def _rollback(ctx: Ctx, entries: List[Dict[str, Any]], saved_root: Path) -> List[str]:
    """Swap every entry back, newest first. One failure must not strand the rest."""
    errs: List[str] = []
    for e in reversed(entries):
        try:
            _swap_back(ctx, e, saved_root)
        except (OSError, CannotJudgeError, shutil.Error) as exc:
            errs.append(f"{e.get('id')}: {exc}")
    return errs


def _new_ts_dir(root: Path) -> Tuple[str, Path]:
    ts = _stamp()
    d, n = root / ts, 1
    while d.exists():
        n += 1
        d = root / f"{ts}-{n}"
    _mkdir_private(d)
    return d.name, d


def cmd_restore(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    if a.undo:
        return restore_undo(ctx, a.undo)
    if not a.label:
        raise CannotJudgeError("restore needs --label L (or --undo TS)")
    store = _store(ctx, a.src)
    variant = read_variant(ctx)
    key = _expect_key(a)
    if key is None and not _is_own_store(ctx, store) and \
            not getattr(a, "allow_unsigned", False):
        raise RefusedError(
            f"{store} is not this box's own backup store, so whoever wrote the media "
            f"also wrote its index and digests. Restore from it needs a sealed backup "
            f"and --expect-key/--expect-key-file (the publisher's public key), or "
            f"--allow-unsigned if you made that media yourself")
    scratch, man = _unpack_verified(ctx, store, a.label, key)
    try:
        if man.get("variant") != variant and not a.force_variant:
            raise RefusedError(f"backup is variant {man.get('variant')!r}, this box is "
                          f"{variant!r} (use --force-variant only if you mean it)")
        profiles = load_profiles(ctx, variant)
        allowed = restorable_items(profiles)
        recs: List[Dict[str, Any]] = []
        skipped: List[str] = []
        for r in man.get("items", []):
            if not r.get("present"):
                continue
            it = allowed.get(r["id"])
            # The manifest is written by whoever made the archive. Only an id the
            # LOCAL profiles declare restorable is honoured, and its path and kind
            # come from that profile -- never from the manifest.
            if it is None and a.force_variant:
                skipped.append(r["id"])  # another variant's item: never written here
                continue
            if it is None:
                raise RefusedError(f"backup item {r['id']!r} is not a restorable item "
                                   f"of this box's backup profiles; nothing was changed")
            if r.get("kind") != it["kind"]:
                raise RefusedError(f"backup item {r['id']!r} is kind {r.get('kind')!r}, "
                                   f"the profile says {it['kind']!r}; nothing was changed")
            recs.append({"id": r["id"], "kind": it["kind"], "path": it["path"],
                         "exclude": list(it.get("exclude", []))})
        if a.only:
            want = [w.strip() for w in a.only.split(",") if w.strip()]
            known = {r["id"] for r in recs}
            unknown = [w for w in want if w not in known]
            if unknown:
                raise RefusedError(f"--only names items not in this backup: {unknown}")
            recs = [r for r in recs if r["id"] in want]
        ts, saved_root = _new_ts_dir(ctx.p(REPLACED))
        journal: Dict[str, Any] = {"schema": SCHEMA, "ts": ts, "label": a.label,
                                   "started": _iso(), "items": [], "undone": False}
        jpath = saved_root / "journal.json"
        try:
            quiesce(ctx, profiles)
            for r in recs:
                e = _swap_in(ctx, r, scratch / "items" / r["id"], saved_root, ts,
                             r["exclude"])
                journal["items"].append(e)
                _atomic_write(jpath, json.dumps(journal, indent=2).encode())
        except (OSError, CannotJudgeError, RefusedError, shutil.Error) as exc:
            rb_errors = _rollback(ctx, journal["items"], saved_root)
            journal["undone"] = not rb_errors
            journal["failed"] = str(exc)
            journal["rollback_errors"] = rb_errors
            _atomic_write(jpath, json.dumps(journal, indent=2).encode())
            if rb_errors:
                raise CannotJudgeError(
                    f"restore failed mid-way ({exc}) and rollback was INCOMPLETE: "
                    f"{rb_errors}; replaced trees are under {saved_root}") from exc
            if isinstance(exc, RefusedError):
                raise RefusedError(f"{exc} (rolled back)") from exc
            raise CannotJudgeError(f"restore failed mid-way and was rolled back: {exc}") from exc
        finally:
            resume(ctx, profiles)
        journal["finished"] = _iso()
        _atomic_write(jpath, json.dumps(journal, indent=2).encode())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    checks = health(ctx, profiles)
    refresh = None
    if not ctx.fake and shutil.which("aitheros") and ctx.root in ("", "/"):
        refresh = _run(ctx, ["aitheros", "license", "refresh", "--quiet"], 300)
    ok = all(c["ok"] for c in checks)
    return {"schema": SCHEMA, "op": "restore", "label": a.label, "undo_ts": ts,
            "replaced": str(saved_root), "restored": [e["id"] for e in journal["items"]],
            "skipped": skipped, "sealed": man["_trust"]["sealed"],
            "key_trusted": man["_trust"]["key_trusted"],
            "health": checks, "license_refresh_rc": refresh,
            "state": "restored" if ok else "restored-unhealthy", "ok": ok}


def restore_undo(ctx: Ctx, ts: str) -> Dict[str, Any]:
    if not re.match(r"^[0-9TZ-]+$", ts):
        raise RefusedError(f"--undo {ts!r} is not a restore timestamp")
    saved_root = ctx.p(REPLACED) / ts
    jpath = saved_root / "journal.json"
    try:
        journal = json.loads(jpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RefusedError(f"no restore journal at {jpath}: {exc}") from exc
    if journal.get("undone"):
        raise RefusedError(f"restore {ts} was already undone")
    profiles = load_profiles(ctx, read_variant(ctx))
    try:
        quiesce(ctx, profiles)
        rb_errors = _rollback(ctx, journal.get("items", []), saved_root)
    finally:
        resume(ctx, profiles)
    if rb_errors:
        raise CannotJudgeError(f"undo {ts} was INCOMPLETE: {rb_errors}")
    journal["undone"] = True
    journal["undone_at"] = _iso()
    _atomic_write(jpath, json.dumps(journal, indent=2).encode())
    return {"schema": SCHEMA, "op": "undo", "ts": ts,
            "reverted": [e["id"] for e in journal.get("items", [])], "ok": True}


# --------------------------------------------------------------------------- reset


def _count(path: Path, exclude: List[str]) -> Tuple[int, int]:
    if path.is_symlink() or path.is_file():
        return 1, path.lstat().st_size
    files = size = 0
    if path.is_dir():
        for child in path.iterdir():
            if child.name in exclude:
                continue
            if child.is_dir() and not child.is_symlink():
                for dirpath, _d, names in os.walk(child):
                    for n in names:
                        files += 1
                        size += (Path(dirpath) / n).lstat().st_size
            else:
                files += 1
                size += child.lstat().st_size
    return files, size


def _admin_user(ctx: Ctx, setup_path: str) -> Optional[str]:
    try:
        d = json.loads(ctx.p(setup_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    u = d.get("admin_user")
    if isinstance(u, str) and re.match(r"^[a-z_][a-z0-9_-]{0,31}$", u) and u != "root":
        return u
    return None


def reset_plan(ctx: Ctx, profiles: List[Dict[str, Any]], scope: str, include_models: bool,
               keep_backups: bool) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    for prof in profiles:
        for it in prof["items"]:
            if scope not in it["reset"]:
                continue
            if it["class"] == "model" and not include_models:
                continue
            step = {"id": f"{prof['id']}.{it['id']}", "kind": it["kind"], "path": it["path"],
                    "class": it["class"], "exclude": it.get("exclude", [])}
            if it["kind"] == "users":
                step["user"] = _admin_user(ctx, it["path"])
            plan.append(step)
    # users first: the admin name is read from setup.json, which a later step removes
    plan.sort(key=lambda s: 0 if s["kind"] == "users" else 1)
    # Restore leftovers and staging hold customer data, so they always go.
    for p in (REPLACED, STAGING):
        plan.append({"id": f"awnix.{Path(p).name}", "kind": "dir", "path": p,
                     "class": "customer", "exclude": []})
    if not keep_backups:
        plan.append({"id": "awnix.backups", "kind": "dir", "path": STORE,
                     "class": "customer", "exclude": []})
    return plan


def _wipe(ctx: Ctx, step: Dict[str, Any]) -> Tuple[int, int]:
    kind = step["kind"]
    if kind == "users":
        user = step.get("user")
        if user:
            # -f: the operator usually runs reset from that admin's own sudo
            # session, and plain userdel refuses a logged-in user (rc 8) -- which
            # would leave the buyer of a resold box with the old admin's home.
            rc = _run(ctx, ["userdel", "-f", "-r", user])
            if rc != 0:
                ctx.errors.append(f"userdel {user} exited {rc}")
                return 0, 0
            return 1, 0
        return 0, 0
    if kind == "podman-volume":
        names = _volumes(ctx, step["path"])
        for n in names:
            _volume_rm(ctx, n)
        return len(names), 0
    target = ctx.p(step["path"])
    files, size = _count(target, step["exclude"])
    if kind in ("dir", "qdrant"):
        if target.is_dir() and not target.is_symlink():
            for child in list(target.iterdir()):
                if child.name not in step["exclude"]:
                    _remove(child)
        elif target.exists() or target.is_symlink():
            _remove(target)
    else:
        if target.exists() or target.is_symlink():
            _remove(target)
        if kind == "sqlite":
            for suf in SQLITE_SIDECARS:
                side = Path(str(target) + suf)
                if side.exists():
                    f2, s2 = _count(side, [])
                    files, size = files + f2, size + s2
                    side.unlink()
    return files, size


def cmd_reset(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    variant = read_variant(ctx)
    profiles = load_profiles(ctx, variant)
    host = hostname(ctx)
    phrase = f"erase {host}"
    plan = reset_plan(ctx, profiles, a.scope, a.include_models, a.keep_backups)
    if a.dry_run:
        rows = []
        for s in plan:
            f, b = (0, 0) if s["kind"] in ("users", "podman-volume") else \
                _count(ctx.p(s["path"]), s["exclude"])
            rows.append({"id": s["id"], "path": s["path"], "class": s["class"],
                         "files": f, "bytes": b})
        return {"schema": SCHEMA, "op": "reset", "scope": a.scope, "dry_run": True,
                "would_remove": rows, "confirm_phrase": phrase}
    confirm = a.confirm
    if confirm is None:
        if not sys.stdin.isatty():
            raise CannotJudgeError(f"reset needs a tty or --confirm '{phrase}'")
        print(f"This ERASES {a.scope} data on {host}. Type: {phrase}", file=sys.stderr)
        confirm = input("> ")
    if confirm.strip() != phrase:
        raise RefusedError(f"confirmation did not match '{phrase}'; nothing was removed")
    try:
        quiesce(ctx, profiles)
    except CannotJudgeError:
        resume(ctx, profiles)  # nothing was erased; put back what was stopped
        raise
    # A declared dir is EMPTIED, never removed, even when an enclosing item (garg
    # `residual` = /var/lib/gargbot) sweeps it afterwards: services expect their
    # data dirs to exist with the owner and mode the image gave them.
    skeleton = []
    for s in plan:
        t = ctx.p(s["path"])
        if s["kind"] in ("dir", "qdrant") and t.is_dir() and not t.is_symlink():
            st = t.stat()
            skeleton.append((t, st.st_mode & 0o7777, st.st_uid, st.st_gid))
    removed = []
    for s in plan:
        try:
            f, b = _wipe(ctx, s)
        except (OSError, CannotJudgeError, shutil.Error) as exc:
            # Keep erasing the rest: stopping half-way leaves MORE customer data
            # behind. The failure is on the receipt and the exit code is 1.
            ctx.errors.append(f"{s['path']}: {exc}")
            f, b = 0, 0
        removed.append({"path": s["path"], "files": f, "bytes": b})
    for t, mode, uid, gid in skeleton:
        try:
            t.mkdir(parents=True, exist_ok=True)
            os.chmod(t, mode)
            if hasattr(os, "chown") and not ctx.fake:
                os.chown(t, uid, gid)
        except OSError as exc:
            ctx.notes.append(f"could not re-create {t}: {exc}")
    if _run(ctx, ["fstrim", "-a"], 1800) != 0:
        ctx.notes.append("fstrim -a failed; unlinked blocks were not discarded")
    receipt = {"schema": SCHEMA, "at": _iso(), "scope": a.scope, "variant": variant,
               "include_models": bool(a.include_models), "keep_backups": bool(a.keep_backups),
               "removed": removed, "errors": list(ctx.errors),
               "method": "unlink+fstrim", "nist_800_88": "clear-logical"}
    body = json.dumps(receipt, indent=2, sort_keys=True).encode()
    _atomic_write(ctx.p(RECEIPT), body, 0o644)
    sha = hashlib.sha256(body).hexdigest()
    total = sum(r["files"] for r in removed)
    out = {"schema": SCHEMA, "op": "reset", "scope": a.scope, "receipt": RECEIPT,
           "receipt_sha256": sha, "removed_files": total, "reboot": False,
           "ok": not ctx.errors, "errors": list(ctx.errors),
           "marker": f"awnix-reset: scope={a.scope} removed={total} receipt={sha[:12]}"}
    if ctx.errors:
        # An incomplete erase must not reboot into setup mode looking finished.
        resume(ctx, profiles)
        out["state"] = "incomplete"
        return out
    if a.scope == "factory":
        if ctx.fake or os.environ.get("AWNIX_NO_REBOOT") == "1" or a.no_reboot:
            out["reboot"] = False
            out["note"] = "reboot skipped; next boot enters setup mode"
        else:
            _run(ctx, ["systemctl", "reboot", "--no-block"])
            out["reboot"] = True
    else:
        resume(ctx, profiles)
    return out


# --------------------------------------------------------------------------- self-test


def self_test() -> int:
    try:
        _awrecover()
    except CannotJudgeError as exc:
        print(f"awnix-backup self-test: COULD NOT JUDGE: {exc}", file=sys.stderr)
        return EXIT_CNJ
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="awnix-backup-selftest-"))
    try:
        root = tmp / "root"
        prof_dir = tmp / "profiles"
        prof_dir.mkdir(parents=True)
        (prof_dir / "00-t.json").write_text(json.dumps({
            "schema": 1, "id": "t", "variants": ["*"],
            "quiesce": {"units": ["t.service"], "ports": [1]},
            "resume": {"units": ["t.service"]}, "health": [],
            "items": [
                {"id": "data", "path": "/var/lib/t/data", "kind": "dir",
                 "class": "customer", "backup": True, "reset": ["data", "factory"]},
                {"id": "db", "path": "/var/lib/t/t.db", "kind": "sqlite",
                 "class": "customer", "backup": True, "reset": ["data", "factory"]},
                {"id": "conf", "path": "/etc/t/t.conf", "kind": "file",
                 "class": "config", "backup": True, "reset": ["factory"]},
                {"id": "key", "path": "/etc/t/key", "kind": "file",
                 "class": "credential", "backup": False, "reset": ["factory"]},
                {"id": "models", "path": "/var/lib/t/models", "kind": "dir",
                 "class": "model", "backup": False, "reset": ["factory"]},
            ]}), encoding="utf-8")
        ctx = Ctx(root=str(root), fake=True, profiles_dir=str(prof_dir))
        (root / "etc").mkdir(parents=True)
        (root / "etc/hostname").write_text("box1\n")
        d = root / "var/lib/t/data"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_text("alpha")
        (d / "sub/b.bin").write_bytes(os.urandom(1024))
        con = sqlite3.connect(str(root / "var/lib/t/t.db"))
        con.execute("create table x(v)")
        con.execute("insert into x values ('one')")
        con.commit()
        con.close()
        (root / "etc/t").mkdir(parents=True)
        (root / "etc/t/t.conf").write_text("k=v")
        (root / "etc/t/key").write_text("secret")
        (root / "var/lib/t/models").mkdir(parents=True)
        (root / "var/lib/t/models/m.gguf").write_text("weights")
        before = tree_digest(d)[2]

        ns = argparse.Namespace(to=None, label="st1", seal=False, key=None, no_verify=False,
                                live=False, keep=0)
        r = backup_create(ctx, ns)
        check("create verifies", r["verified"] is True and r["marker"].startswith(
            "awnix-backup: st1 verified"))
        check("credential not carried", all(i["class"] != "credential" for i in r["items"]))
        (d / "a.txt").write_text("MUTATED")
        rr = cmd_restore(ctx, argparse.Namespace(undo=None, label="st1", src=None,
                                                 force_variant=False, only=None))
        check("restore round-trips the tree", tree_digest(d)[2] == before)
        con = sqlite3.connect(str(root / "var/lib/t/t.db"))
        check("sqlite restored", con.execute("select v from x").fetchall() == [("one",)])
        con.close()
        cmd_restore(ctx, argparse.Namespace(undo=rr["undo_ts"], label=None, src=None,
                                            force_variant=False, only=None))
        check("undo puts the mutated tree back", (d / "a.txt").read_text() == "MUTATED")

        store = ctx.p(STORE)
        arch = next(store.glob("st1*.tar.gz"))
        raw = bytearray(arch.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        arch.write_bytes(bytes(raw))
        mutated = tree_digest(d)[2]
        try:
            cmd_restore(ctx, argparse.Namespace(undo=None, label="st1", src=None,
                                                force_variant=False, only=None))
            check("tampered archive refused", False)
        except RefusedError:
            check("tampered archive refused", True)
        check("tamper left the tree unchanged", tree_digest(d)[2] == mutated)

        rs = argparse.Namespace(scope="data", dry_run=False, confirm="erase wrong",
                                include_models=False, keep_backups=False, no_reboot=True)
        try:
            cmd_reset(ctx, rs)
            check("wrong phrase refused", False)
        except RefusedError:
            check("wrong phrase refused", True)
        rs.confirm = "erase box1"
        out = cmd_reset(ctx, rs)
        check("data reset empties customer data", not any(d.iterdir()))
        check("data reset keeps config", (root / "etc/t/t.conf").is_file())
        check("data reset keeps models", (root / "var/lib/t/models/m.gguf").is_file())
        check("receipt written", ctx.p(RECEIPT).is_file() and out["marker"].startswith(
            "awnix-reset: scope=data"))
        rs.scope = "factory"
        cmd_reset(ctx, rs)
        check("factory reset removes config and credential",
              not (root / "etc/t/t.conf").exists() and not (root / "etc/t/key").exists())
        check("factory reset keeps models by default",
              (root / "var/lib/t/models/m.gguf").is_file())
    except (RefusedError, CannotJudgeError, OSError, StopIteration) as exc:
        check(f"self-test ran to completion ({exc})", False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"awnix-backup self-test: {'FAIL' if fails else 'ok'} ({len(fails)} failed)")
    return EXIT_REFUSED if fails else EXIT_OK


# --------------------------------------------------------------------------- cli


def _key_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--expect-key", help="trusted awseal PUBLIC key (64 hex); the backup "
                                        "must be sealed by it")
    p.add_argument("--expect-key-file", help="file holding that public key")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awnix-backup",
                                 description="awnix backup | restore | reset")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    sub = ap.add_subparsers(dest="verb")

    b = sub.add_parser("backup", help="create, list, verify or drop backups")
    bs = b.add_subparsers(dest="sub")
    c = bs.add_parser("create")
    c.add_argument("--to", help="store directory (default /var/lib/awnix/backups)")
    c.add_argument("--label")
    c.add_argument("--seal", action="store_true")
    c.add_argument("--key", help="awseal private key FILE (never key material)")
    c.add_argument("--no-verify", action="store_true")
    c.add_argument("--live", action="store_true", help="do not quiesce services")
    c.add_argument("--keep", type=int, default=0, help="keep the newest N backups")
    c.add_argument("--json", action="store_true")
    ls = bs.add_parser("list")
    ls.add_argument("--from", dest="src")
    ls.add_argument("--json", action="store_true")
    for name in ("verify", "drop"):
        x = bs.add_parser(name)
        x.add_argument("--label", required=True)
        x.add_argument("--from", dest="src")
        x.add_argument("--json", action="store_true")
        if name == "verify":
            _key_args(x)

    r = sub.add_parser("restore", help="restore a verified backup (or --undo one)")
    r.add_argument("--label")
    r.add_argument("--from", dest="src")
    r.add_argument("--only", help="comma-separated item ids (profile.item)")
    r.add_argument("--force-variant", action="store_true")
    _key_args(r)
    r.add_argument("--allow-unsigned", action="store_true",
                   help="restore an unsealed backup from media other than this box's own "
                        "store (only for media you made yourself)")
    r.add_argument("--undo", metavar="TS")
    r.add_argument("--json", action="store_true")

    z = sub.add_parser("reset", help="erase customer data (data) or return to factory")
    z.add_argument("--scope", choices=SCOPES, required=True)
    z.add_argument("--dry-run", action="store_true")
    z.add_argument("--confirm", help="the phrase 'erase <hostname>'")
    z.add_argument("--include-models", action="store_true")
    z.add_argument("--keep-backups", action="store_true")
    z.add_argument("--no-reboot", action="store_true")
    z.add_argument("--json", action="store_true")
    return ap


def _emit(a: argparse.Namespace, out: Dict[str, Any]) -> None:
    if getattr(a, "json", False):
        print(json.dumps(out, indent=2, sort_keys=True))
        if out.get("marker"):
            print(out["marker"], file=sys.stderr)
        return
    if out.get("op") == "list":
        print(f"store: {out['store']}")
        for s in out["backups"]:
            print(f"  {s['label']}  {s['created']}  files={s['files']}  "
                  f"digest={s['digest'][:12]}  variant={s['meta'].get('variant')}")
        if not out["backups"]:
            print("  (no backups)")
        return
    for k in ("label", "store", "state", "undo_ts", "replaced", "receipt", "note"):
        if out.get(k) is not None:
            print(f"{k}: {out[k]}")
    if out.get("op") == "reset" and out.get("dry_run"):
        for row in out["would_remove"]:
            print(f"  would remove {row['path']} ({row['class']}, files={row['files']})")
        print(f"to proceed: --confirm '{out['confirm_phrase']}'")
    if out.get("marker"):
        print(out["marker"])


def main(argv: Optional[List[str]] = None) -> int:
    a = _parser().parse_args(argv)
    if a.list_verbs:
        print("\n".join(VERBS))
        return EXIT_OK
    if a.self_test:
        return self_test()
    if not a.verb:
        _parser().print_help(sys.stderr)
        return EXIT_CNJ
    ctx = Ctx()
    try:
        if a.verb == "backup":
            fn = {"create": backup_create, "list": backup_list,
                  "verify": backup_verify, "drop": backup_drop}.get(a.sub or "")
            if fn is None:
                print("awnix backup: create|list|verify|drop", file=sys.stderr)
                return EXIT_CNJ
            out = fn(ctx, a)
        elif a.verb == "restore":
            out = cmd_restore(ctx, a)
        else:
            out = cmd_reset(ctx, a)
    except RefusedError as exc:
        _emit(a, {"schema": SCHEMA, "ok": False, "state": "refused", "detail": str(exc)})
        print(f"awnix {a.verb}: REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except CannotJudgeError as exc:
        _emit(a, {"schema": SCHEMA, "ok": False, "state": "could-not-judge",
                  "detail": str(exc)})
        print(f"awnix {a.verb}: COULD NOT JUDGE: {exc}", file=sys.stderr)
        return EXIT_CNJ
    if ctx.notes:
        out["notes"] = ctx.notes
    _emit(a, out)
    if out.get("ok") is False:
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
