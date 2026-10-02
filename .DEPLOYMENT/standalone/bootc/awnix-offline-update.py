#!/usr/bin/python3.11
"""awnix offline-update -- verify a signed update bundle from removable media, THEN stage it.

    awnix offline-update scan [--json] [DIR ...]   list bundles on mounted media (unverified)
    awnix offline-update verify BUNDLE [--json]     full verification, nothing staged
      (verify/stage/apply take --allow-downgrade: accept a bundle older than the floor)
    awnix offline-update stage BUNDLE [--json]      verify, then `bootc switch --transport oci-archive`
    awnix offline-update apply BUNDLE [--json]      stage, then schedule a reboot (detached)
    awnix offline-update status [--json]            last verdict
    awnix offline-update trust list [--json]        trusted Ed25519 signer keys
    awnix offline-update trust add HEX64            trust one more signer on THIS node
    awnix offline-update gc [--json]                drop staging copies bootc no longer references
    awnix offline-update --self-test | --list-verbs

WHY (air-gap proof plan gap G5, demo Step 4). An air-gapped node gets updates on a USB
stick. The stick is hostile until proven otherwise, so nothing on it reaches `bootc`
until three independent questions are answered from a ROOT-ONLY COPY (never from the
media, which could change between the check and the use):

  1. who signed it     -- awseal Ed25519 signature, signer key in the node trust store
  2. is it what they signed -- every byte of every file matches the seal's digest map
  3. is it for this box -- archive sha256 == update.json, OCI manifest digest in
                           index.json == update.json, variant == /usr/lib/awnix/release.env
  4. is it not OLDER     -- the signed update.json `created` is >= the rollback floor:
                           the newest `created` this node ever staged
                           (/var/lib/awnix/offline-update-floor.json) and the booted
                           image's timestamp. A validly signed OLD bundle (an N-1 with a
                           known CVE) is refused as `rollback` unless the operator passes
                           --allow-downgrade, which is written into the audit record.

Only a `VerifiedBundle` -- which only `_verify_copy` constructs -- reaches the one
`bootc switch` call site (`_stage_verified`). Every refusal is appended to the awdit
hash chain /var/log/awnix/update-audit.jsonl and fsync'd BEFORE the process exits; if
the audit append fails the answer is exit 2 (could not judge) and nothing is staged.

Bundle layout (a directory; built by AitherOS/dev/tools/build_offline_update_bundle.py):
    update.json                    {schema:1, kind:'awnix-offline-update', variant, ...}
    image.oci.tar                  the oci-archive, OR image.oci.tar.awchunk.json + parts
    awseal.json                    Ed25519 seal over every file above
An awshare form (`<name>.awshare.json` + `<name>.tar.gz` of that directory) is accepted.

Exit: 0 ok · 1 refused / failed · 2 could not judge.
Test seams: AWNIX_OFFLINE_ROOT (path prefix), AWNIX_OFFLINE_BOOTC (stub bootc: a path,
or a JSON argv list), AWNIX_NO_REBOOT=1.

Python 3.10-compatible. awseal / awshare / awdit are imported lazily so `--list-verbs`
works without them; any verb that needs them exits 2 when they are missing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNJUDGED = 2

VERBS = ("scan", "verify", "stage", "apply", "status", "trust", "gc")

UPDATE_JSON = "update.json"
SEAL_NAME = "awseal.json"
CHUNK_SUFFIX = ".awchunk.json"
AWSHARE_SUFFIX = ".awshare.json"
UPDATE_KIND = "awnix-offline-update"
UPDATE_SCHEMA = 1

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")

REASONS = (
    "seal-missing", "seal-unreadable", "signature-invalid", "content-mismatch",
    "untrusted-key", "archive-digest-mismatch", "oci-manifest-mismatch",
    "variant-mismatch", "schema-unknown", "path-escape", "part-mismatch", "rollback",
)

# Media mount roots `scan` looks under (udisks, legacy, and admin mounts).
MEDIA_ROOTS = ("run/media", "media", "var/mnt", "mnt")

_SMALL_BLOB_MAX = 4 * 1024 * 1024
_IO = 8 * 1024 * 1024


# ── configuration (all paths hang off one prefix so tests never touch /) ─────────────


@dataclass
class Config:
    root: Path
    bootc: List[str]
    no_reboot: bool

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Config":
        env = dict(os.environ if env is None else env)
        root = Path(env.get("AWNIX_OFFLINE_ROOT") or "/")
        raw = (env.get("AWNIX_OFFLINE_BOOTC") or "bootc").strip()
        if raw.startswith("["):
            bootc = [str(x) for x in json.loads(raw)]
        else:
            bootc = [raw]
        return cls(root=root, bootc=bootc, no_reboot=env.get("AWNIX_NO_REBOOT") == "1")

    def p(self, rel: str) -> Path:
        return self.root / rel

    @property
    def staging(self) -> Path:
        return self.p("var/lib/awnix/offline")

    @property
    def status_path(self) -> Path:
        return self.p("var/lib/awnix/offline-update-status.json")

    @property
    def floor_path(self) -> Path:
        """Anti-rollback floor: the newest signed `created` this node ever staged."""
        return self.p("var/lib/awnix/offline-update-floor.json")

    @property
    def audit_path(self) -> Path:
        return self.p("var/log/awnix/update-audit.jsonl")

    @property
    def trust_dirs(self) -> List[Path]:
        return [self.p("usr/share/awnix/offline-trust.d"), self.p("etc/awnix/offline-trust.d")]

    @property
    def admin_trust_dir(self) -> Path:
        return self.p("etc/awnix/offline-trust.d")

    @property
    def release_env(self) -> Path:
        return self.p("usr/lib/awnix/release.env")

    @property
    def os_release(self) -> Path:
        return self.p("etc/os-release")


# ── errors ────────────────────────────────────────────────────────────────────────


class Refused(Exception):  # noqa: N818 -- a verdict, not an error
    """The bundle is wrong. Caught in exactly one place, which audits it (OFU004)."""

    def __init__(self, reason: str, detail: str):
        if reason not in REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Unjudged(Exception):  # noqa: N818 -- a verdict, not an error
    """We could not decide (missing tool, no space, unreadable media, audit down)."""


# ── lazy imports: every one of them is a hard requirement of a verdict ──────────────


def _need(mod: str):
    try:
        return __import__(mod)
    except ImportError as exc:
        raise Unjudged(f"python module {mod!r} is not installed ({exc}); cannot judge") from exc


# ── small helpers ─────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(_IO)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _fsync_path(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path: Path, doc: Dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def node_variant(cfg: Config) -> str:
    """AWNIX_VARIANT from release.env (the update-channels contract), else VARIANT_ID."""
    v = _read_env_file(cfg.release_env).get("AWNIX_VARIANT", "")
    if not v:
        v = _read_env_file(cfg.os_release).get("VARIANT_ID", "")
    if not v:
        raise Unjudged(f"node variant unknown: no AWNIX_VARIANT in {cfg.release_env} "
                       f"and no VARIANT_ID in {cfg.os_release}")
    return v


# ── trust store ───────────────────────────────────────────────────────────────────


def trusted_keys(cfg: Config) -> Dict[str, str]:
    """{pubkey_hex: source file}. Only *.pub files whose first data line is 64 hex."""
    keys: Dict[str, str] = {}
    for d in cfg.trust_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.pub")):
            if not f.is_file():
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line in lines:
                s = line.strip().lower()
                if not s or s.startswith("#"):
                    continue
                if HEX64.match(s):
                    keys.setdefault(s, str(f))
                break
    return keys


# ── audit + status ────────────────────────────────────────────────────────────────


def _audit(cfg: Config, event: str, **data: Any) -> str:
    """Append one awdit record and fsync it (log AND anchor). Returns the record hash.

    Any failure is Unjudged: a refusal nobody can later prove happened is not an
    audited refusal, and a stage whose audit record is missing must not happen.
    """
    awdit = _need("awdit")
    try:
        cfg.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(cfg.audit_path.parent, 0o750)
        rec = awdit.append(str(cfg.audit_path), event, **data)
        _fsync_path(Path(str(cfg.audit_path) + ".anchor"))
        _fsync_path(cfg.audit_path.parent)
        return str(rec.get("hash", ""))
    except Unjudged:
        raise
    except Exception as exc:  # noqa: BLE001 -- any audit failure is a could-not-judge
        raise Unjudged(f"audit append to {cfg.audit_path} failed: {exc}") from exc


def _audit_preflight(cfg: Config) -> None:
    """Prove we CAN audit before touching the media (spec: no awdit -> exit 2, no stage)."""
    _need("awdit")
    try:
        cfg.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.audit_path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise Unjudged(f"audit log {cfg.audit_path} is not writable: {exc}") from exc


def _write_status(cfg: Config, **fields: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "schema": 1, "checked_at": _now(), "state": "could-not-judge", "reason": "",
        "detail": "", "bundle": "", "variant": "", "version": "", "manifest_digest": "",
        "signer_pubkey": "", "audit_head": "",
    }
    doc.update(fields)
    with contextlib.suppress(OSError):
        _atomic_json(cfg.status_path, doc)
    return doc


# ── the verified-bundle type: the ONLY input `_stage_verified` accepts ───────────────


@dataclass(frozen=True)
class VerifiedBundle:
    bundle_id: str
    source: str
    workdir: Path
    archive: Path
    update: Dict[str, Any] = field(hash=False, compare=False)
    signer_pubkey: str = ""
    labels: Dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    downgrade_override: str = ""

    @property
    def manifest_digest(self) -> str:
        return str(self.update["manifest_digest"])

    def summary(self) -> Dict[str, Any]:
        return {
            "bundle": self.source, "bundle_id": self.bundle_id,
            "variant": self.update.get("variant", ""),
            "version": self.update.get("version", ""),
            "manifest_digest": self.manifest_digest,
            "archive_sha256": self.update.get("archive_sha256", ""),
            "signer_pubkey": self.signer_pubkey,
            "archive": str(self.archive),
            "created": self.update.get("created", ""),
            "downgrade_override": self.downgrade_override,
        }


# ── OCI archive reading (shared with the bundle builder) ────────────────────────────


def _norm_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def read_oci_archive(path: Path) -> Dict[str, Any]:
    """Parse an oci-archive's index.json and verify the manifest + config blobs.

    Returns {manifest_digest, media_type, config_digest, labels}. Raises ValueError with
    a human reason on anything a node must not stage.
    """
    try:
        tf = tarfile.open(path, "r:")  # oci-archive is an UNCOMPRESSED tar
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"not a readable uncompressed tar: {exc}") from exc
    with tf:
        members = {}
        for m in tf.getmembers():
            members[_norm_member(m.name)] = m

        def small(name: str) -> bytes:
            m = members.get(name)
            if m is None or not m.isfile():
                raise ValueError(f"{name} missing from the archive")
            if m.size > _SMALL_BLOB_MAX:
                raise ValueError(f"{name} is {m.size} bytes; refusing to parse it")
            fh = tf.extractfile(m)
            if fh is None:
                raise ValueError(f"cannot read {name}")
            return fh.read()

        def blob(digest: str) -> bytes:
            if not DIGEST_RE.match(digest or ""):
                raise ValueError(f"malformed digest {digest!r}")
            data = small("blobs/sha256/" + digest.split(":", 1)[1])
            got = "sha256:" + hashlib.sha256(data).hexdigest()
            if got != digest:
                raise ValueError(f"blob {digest} hashes to {got}")
            return data

        try:
            layout = json.loads(small("oci-layout"))
            index = json.loads(small("index.json"))
        except ValueError as exc:
            raise ValueError(f"not an OCI layout: {exc}") from exc
        if not isinstance(layout, dict) or "imageLayoutVersion" not in layout:
            raise ValueError("oci-layout has no imageLayoutVersion")
        mans = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(mans, list) or len(mans) != 1:
            raise ValueError(f"index.json must name exactly one manifest, names "
                             f"{len(mans) if isinstance(mans, list) else 'none'}")
        entry = mans[0]
        digest = str(entry.get("digest", ""))
        media = str(entry.get("mediaType", ""))
        if "index" in media or "manifest.list" in media:
            raise ValueError(f"index.json points at a multi-arch index ({media}); "
                             f"a node stages one platform manifest")
        manifest = json.loads(blob(digest))
        cfg_desc = manifest.get("config") or {}
        cfg_digest = str(cfg_desc.get("digest", ""))
        config = json.loads(blob(cfg_digest))
        labels = ((config.get("config") or {}).get("Labels") or {})
        if not isinstance(labels, dict):
            labels = {}
        return {"manifest_digest": digest, "media_type": media,
                "config_digest": cfg_digest,
                "labels": {str(k): str(v) for k, v in labels.items()}}


# ── copying the bundle off the media ────────────────────────────────────────────────


def _new_workdir(cfg: Config) -> Tuple[str, Path]:
    cfg.staging.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(cfg.staging, 0o700)
    bid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    wd = cfg.staging / bid
    wd.mkdir(mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(wd, 0o700)
    return bid, wd


def _require_space(cfg: Config, need: int) -> None:
    free = shutil.disk_usage(str(cfg.staging)).free
    if free < need:
        raise Unjudged(f"not enough space in {cfg.staging}: need {need} bytes "
                       f"(bundle x1.1), {free} free")


def _flat_files(src: Path) -> List[Path]:
    """The bundle is FLAT regular files. A symlink or a subdirectory is refused."""
    out = []
    for p in sorted(src.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue  # AppleDouble / editor litter: never copied, so never staged
        if p.is_symlink():
            raise Refused("path-escape", f"{p.name} is a symlink; a bundle holds regular files only")
        if p.is_dir():
            raise Refused("path-escape", f"{p.name} is a directory; a bundle is flat")
        if not p.is_file():
            raise Refused("path-escape", f"{p.name} is not a regular file")
        if not PLAIN_NAME.match(p.name):
            raise Refused("path-escape", f"file name {p.name!r} is not a plain name")
        out.append(p)
    return out


def _copy_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as a, open(dst, "xb") as b:
        shutil.copyfileobj(a, b, _IO)
        b.flush()
        os.fsync(b.fileno())


def _copy_dir_bundle(cfg: Config, src: Path, dest: Path) -> None:
    files = _flat_files(src)
    total = sum(f.stat().st_size for f in files)
    chunked = any(f.name.endswith(CHUNK_SUFFIX) for f in files)
    # x1.1 for the copy, plus the stitched archive when the image arrived in parts.
    _require_space(cfg, int(total * 1.1) + (total if chunked else 0))
    dest.mkdir(mode=0o700)
    for f in files:
        try:
            _copy_file(f, dest / f.name)
        except OSError as exc:
            raise Unjudged(f"copying {f} off the media failed: {exc}") from exc


def _unpack_awshare(cfg: Config, manifest_path: Path, wd: Path, dest: Path) -> None:
    """Verify the awshare archive digest, then extract it with the awshare path rules."""
    _need("awshare")
    from awshare import bundle as aws_bundle  # type: ignore
    from awshare import store as aws_store  # type: ignore

    try:
        man = aws_bundle.load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        raise Refused("schema-unknown", f"awshare manifest unreadable: {exc}") from exc
    # The awshare manifest is UNSIGNED media content read before any seal check: type
    # every field before it is used, so hostile JSON is an audited refusal, never a crash.
    name = getattr(man, "name", None)
    if not isinstance(name, str) or not PLAIN_NAME.match(name):
        raise Refused("path-escape", f"awshare bundle name {name!r} is not a plain name")
    if not isinstance(getattr(man, "digest", None), str):
        raise Refused("schema-unknown", "awshare manifest digest is not a string")
    files = getattr(man, "files", None)
    if not isinstance(files, (list, tuple)) or not all(isinstance(f, str) for f in files):
        raise Refused("schema-unknown", "awshare manifest files is not a list of names")
    archive = manifest_path.with_name(man.name + aws_bundle.ARCHIVE_SUFFIX)
    if not archive.is_file() or archive.is_symlink():
        raise Unjudged(f"awshare archive {archive} not found beside its manifest")
    _require_space(cfg, int(archive.stat().st_size * 1.1))
    local = wd / archive.name
    try:
        _copy_file(archive, local)
    except OSError as exc:
        raise Unjudged(f"copying {archive} off the media failed: {exc}") from exc
    if aws_store.digest_file(local) != man.digest:
        raise Refused("content-mismatch", "awshare archive digest does not match its manifest")
    dest.mkdir(mode=0o700)
    seen: List[str] = []
    try:
        with tarfile.open(local, "r:gz") as tf:
            for m in tf:
                if m.isdir():
                    raise Refused("path-escape", f"awshare member {m.name!r} is a directory; a bundle is flat")
                if not m.isfile():
                    raise Refused("path-escape", f"awshare member {m.name!r} is not a regular file")
                if "/" in m.name or not PLAIN_NAME.match(m.name):
                    raise Refused("path-escape", f"awshare member name {m.name!r} is not a plain name")
                target = aws_store.safe_member_path(dest, m.name)
                free = shutil.disk_usage(str(dest)).free
                if m.size > free * 0.95:
                    raise Unjudged(f"not enough space to unpack {m.name} ({m.size} bytes)")
                fh = tf.extractfile(m)
                if fh is None:
                    raise Refused("path-escape", f"cannot read awshare member {m.name!r}")
                with open(target, "xb") as out:
                    shutil.copyfileobj(fh, out, _IO)
                    out.flush()
                    os.fsync(out.fileno())
                seen.append(m.name)
    except (tarfile.TarError, EOFError, OSError) as exc:
        if isinstance(exc, FileExistsError):
            raise Refused("path-escape", f"awshare archive repeats a member: {exc}") from exc
        raise Refused("content-mismatch", f"awshare archive unreadable: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            local.unlink()
    if sorted(seen) != sorted(files):
        raise Refused("content-mismatch", "awshare archive members disagree with its manifest")


def _resolve_source(bundle: str) -> Tuple[str, Path]:
    """-> (form, path). form = 'dir' or 'awshare'."""
    p = Path(bundle)
    if not p.exists():
        raise Unjudged(f"bundle {bundle} does not exist (is the media mounted?)")
    p = p.resolve()
    if p.is_dir():
        if (p / UPDATE_JSON).exists() or (p / SEAL_NAME).exists():
            return "dir", p
        shares = sorted(p.glob("*" + AWSHARE_SUFFIX))
        if len(shares) == 1:
            return "awshare", shares[0]
        return "dir", p
    if p.is_file() and p.name.endswith(AWSHARE_SUFFIX):
        return "awshare", p
    raise Refused("schema-unknown", f"{bundle} is neither a bundle directory nor a *{AWSHARE_SUFFIX}")


# ── verification: nothing here stages, and nothing here audits (the caller does) ─────


def _load_update(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise Refused("schema-unknown", f"update.json unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refused("schema-unknown", "update.json is not an object")
    if doc.get("schema") != UPDATE_SCHEMA or doc.get("kind") != UPDATE_KIND:
        raise Refused("schema-unknown", f"update.json schema={doc.get('schema')!r} "
                      f"kind={doc.get('kind')!r}; this node knows schema 1 {UPDATE_KIND}")
    for key in ("variant", "image_repo", "version", "manifest_digest", "archive",
                "archive_sha256", "archive_size", "created"):
        if key not in doc:
            raise Refused("schema-unknown", f"update.json is missing {key!r}")
    if not DIGEST_RE.match(str(doc["manifest_digest"])):
        raise Refused("schema-unknown", "update.json manifest_digest is not sha256:<64 hex>")
    if not HEX64.match(str(doc["archive_sha256"])):
        raise Refused("schema-unknown", "update.json archive_sha256 is not 64 hex")
    if not isinstance(doc["archive_size"], int) or doc["archive_size"] <= 0:
        raise Refused("schema-unknown", "update.json archive_size is not a positive int")
    if not PLAIN_NAME.match(str(doc["archive"])) or str(doc["archive"]).endswith(CHUNK_SUFFIX):
        raise Refused("path-escape", f"update.json archive {doc['archive']!r} is not a plain file name")
    if _parse_ts(doc["created"]) is None:
        raise Refused("schema-unknown", f"update.json created {doc['created']!r} is not an ISO-8601 timestamp")
    return doc


# -- anti-rollback: a validly signed OLDER bundle is still refused -----------------


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 -> aware UTC datetime, or None. Python 3.10 fromisoformat has no 'Z'."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    # bootc/containers timestamps may carry nanoseconds; 3.10 takes at most 6 digits.
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_floor(cfg: Config) -> Dict[str, Any]:
    """The persisted floor. Absent = no floor yet; present but unreadable = could not
    judge (fail closed: a corrupted floor must not silently re-open downgrades)."""
    try:
        raw = cfg.floor_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Unjudged(f"rollback floor {cfg.floor_path} unreadable: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise Unjudged(f"rollback floor {cfg.floor_path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or _parse_ts(doc.get("created")) is None:
        raise Unjudged(f"rollback floor {cfg.floor_path} has no valid `created`")
    return doc


def rollback_floor(cfg: Config) -> Tuple[Optional[datetime], str]:
    """-> (floor, where it came from) = max(persisted floor, booted image timestamp)."""
    best: Optional[datetime] = None
    src = ""
    doc = _read_floor(cfg)
    if doc:
        best, src = _parse_ts(doc.get("created")), f"{cfg.floor_path} (version {doc.get('version', '?')})"
    booted = ((((_bootc_status(cfg) or {}).get("status") or {}).get("booted") or {})
              .get("image") or {}).get("timestamp")
    bts = _parse_ts(booted)
    if bts is not None and (best is None or bts > best):
        best, src = bts, "the booted image timestamp (bootc status)"
    return best, src


def _check_not_rollback(cfg: Config, upd: Dict[str, Any], allow_downgrade: bool) -> str:
    """Raise Refused('rollback') when the signed `created` is older than the floor.
    Returns '' or the note that an operator override let an older bundle through."""
    created = _parse_ts(upd.get("created"))
    if created is None:  # _load_update already refused this; belt and braces
        raise Refused("schema-unknown", "update.json created is not a timestamp")
    floor, src = rollback_floor(cfg)
    if floor is None or created >= floor:
        return ""
    msg = (f"bundle created {created.isoformat()} (version {upd.get('version')!r}) is older "
           f"than the rollback floor {floor.isoformat()} from {src}")
    if not allow_downgrade:
        raise Refused("rollback", msg + "; re-run with --allow-downgrade to install it on purpose")
    return "downgrade allowed by operator: " + msg


def _raise_floor(cfg: Config, vb: "VerifiedBundle") -> None:
    """After a successful stage the floor only ever moves UP (a deliberate downgrade
    does not lower it, so the next routine update cannot go further back)."""
    created = _parse_ts(vb.update.get("created"))
    if created is None:
        return
    try:
        cur = _read_floor(cfg)
    except Unjudged:
        cur = {}
    cur_ts = _parse_ts(cur.get("created")) if cur else None
    if cur_ts is not None and cur_ts >= created:
        return
    _atomic_json(cfg.floor_path, {
        "schema": 1, "created": created.isoformat(), "version": str(vb.update.get("version", "")),
        "manifest_digest": vb.manifest_digest, "bundle_id": vb.bundle_id, "raised_at": _now(),
    }, mode=0o600)


def _verify_copy(cfg: Config, bid: str, source: str, wd: Path, bundle_dir: Path,
                 allow_downgrade: bool = False) -> VerifiedBundle:
    """Verify the ROOT-ONLY copy. Order: seal -> trust -> update.json -> archive digest
    -> OCI manifest digest -> variant -> rollback floor. Raises Refused / Unjudged;
    returns the only object `_stage_verified` accepts."""
    awseal = _need("awseal")
    _need("cryptography")

    if not (bundle_dir / SEAL_NAME).is_file():
        raise Refused("seal-missing", f"no {SEAL_NAME} in the bundle; unsealed is never staged")
    try:
        seal = awseal.load(bundle_dir)
    except Exception as exc:  # noqa: BLE001 -- SealError or a malformed document
        raise Refused("seal-unreadable", str(exc)) from exc
    try:
        res = awseal.verify(bundle_dir)
    except Exception as exc:  # noqa: BLE001
        raise Refused("seal-unreadable", f"seal could not be evaluated: {exc}") from exc
    if not res.get("signature_ok"):
        raise Refused("signature-invalid", "the Ed25519 signature does not verify over the seal payload")
    if not res.get("content_ok"):
        d = res.get("diff") or {}
        raise Refused("content-mismatch", "files differ from what was signed: "
                      f"added={d.get('added', [])[:5]} removed={d.get('removed', [])[:5]} "
                      f"modified={d.get('modified', [])[:5]}")
    signer = str(seal.public_key).strip().lower()
    trust = trusted_keys(cfg)
    if signer not in trust:
        raise Refused("untrusted-key", f"signed by {signer[:16]}..., which is not in "
                      f"{', '.join(str(d) for d in cfg.trust_dirs)} "
                      f"({len(trust)} trusted key(s))")

    if UPDATE_JSON not in seal.files:
        raise Refused("seal-missing", "the seal does not cover update.json")
    upd = _load_update(bundle_dir / UPDATE_JSON)
    archive_name = str(upd["archive"])
    want = str(upd["archive_sha256"])

    chunk_name = archive_name + CHUNK_SUFFIX
    if (bundle_dir / chunk_name).is_file():
        _need("awshare")
        from awshare import chunk as aws_chunk  # type: ignore

        if chunk_name not in seal.files:
            raise Refused("part-mismatch", f"{chunk_name} is not covered by the seal")
        try:
            cm = aws_chunk.load_manifest(bundle_dir / chunk_name)
        except Exception as exc:  # noqa: BLE001
            raise Refused("part-mismatch", f"chunk manifest invalid: {exc}") from exc
        if cm.name != archive_name:
            raise Refused("part-mismatch", f"chunk manifest names {cm.name!r}, update.json {archive_name!r}")
        for part in cm.parts:
            if not PLAIN_NAME.match(part.name):
                raise Refused("path-escape", f"part name {part.name!r} is not a plain name")
            if part.name not in seal.files:
                raise Refused("part-mismatch", f"part {part.name} is not covered by the seal")
        if cm.digest != want or cm.total_bytes != upd["archive_size"]:
            raise Refused("archive-digest-mismatch", "chunk manifest digest/size disagree with update.json")
        archive = wd / archive_name
        try:
            aws_chunk.stitch(cm, bundle_dir, archive, verify_parts=True)
        except OSError as exc:
            raise Unjudged(f"stitching the archive failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 -- VerificationFailedError / ShareError
            raise Refused("part-mismatch", f"stitch refused: {exc}") from exc
        for part in cm.parts:  # parts are no longer needed; the seal receipt stays
            with contextlib.suppress(OSError):
                (bundle_dir / part.name).unlink()
    else:
        sealed_sha = seal.files.get(archive_name)
        if sealed_sha is None:
            raise Refused("archive-digest-mismatch", f"the seal does not cover {archive_name}")
        # content_ok above already proved the file on disk hashes to sealed_sha.
        if sealed_sha != want:
            raise Refused("archive-digest-mismatch", f"sealed sha256 {sealed_sha[:16]}... != "
                          f"update.json archive_sha256 {want[:16]}...")
        archive = bundle_dir / archive_name
        if archive.stat().st_size != upd["archive_size"]:
            raise Refused("archive-digest-mismatch", "archive size differs from update.json")

    try:
        oci = read_oci_archive(archive)
    except ValueError as exc:
        raise Refused("oci-manifest-mismatch", str(exc)) from exc
    if oci["manifest_digest"] != upd["manifest_digest"]:
        raise Refused("oci-manifest-mismatch", f"index.json names {oci['manifest_digest']}, "
                      f"update.json {upd['manifest_digest']}")

    mine = node_variant(cfg)
    if str(upd["variant"]) != mine:
        raise Refused("variant-mismatch", f"bundle is for {upd['variant']!r}, this node is {mine!r}")

    override = _check_not_rollback(cfg, upd, allow_downgrade)
    return VerifiedBundle(bundle_id=bid, source=source, workdir=wd, archive=archive,
                          update=upd, signer_pubkey=signer, labels=oci["labels"],
                          downgrade_override=override)


# ── staging: the ONE bootc switch call site (OFU003) ────────────────────────────────


def _bootc(cfg: Config, *args: str, timeout: int = 3600) -> Tuple[int, str]:
    try:
        cp = subprocess.run(cfg.bootc + list(args), capture_output=True, text=True,
                            timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise Unjudged(f"bootc not found ({cfg.bootc[0]}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise Unjudged(f"bootc {' '.join(args)} timed out after {timeout}s") from exc
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def _bootc_status(cfg: Config) -> Optional[Dict[str, Any]]:
    try:
        rc, out = _bootc(cfg, "status", "--format=json", timeout=120)
    except Unjudged:
        return None
    if rc != 0:
        return None
    try:
        start = out.index("{")
        doc = json.loads(out[start:])
        return doc if isinstance(doc, dict) else None
    except ValueError:
        return None


def _slot_digest(status: Optional[Dict[str, Any]], slot: str) -> str:
    if not status:
        return ""
    s = ((status.get("status") or {}).get(slot) or {})
    return str(((s.get("image") or {}).get("imageDigest")) or "")


def _stage_verified(cfg: Config, vb: VerifiedBundle) -> Tuple[bool, str]:
    """Hand the VERIFIED root-only archive to bootc. Stage only; never reboots."""
    if not isinstance(vb, VerifiedBundle):
        raise TypeError("_stage_verified accepts only a VerifiedBundle")
    rc, out = _bootc(cfg, "switch", "--transport", "oci-archive", str(vb.archive))
    if rc != 0:
        return False, f"bootc switch rc={rc}: {out.strip()[-400:]}"
    staged = _slot_digest(_bootc_status(cfg), "staged")
    if staged and staged != vb.manifest_digest:
        return False, f"bootc staged {staged}, expected {vb.manifest_digest}"
    return True, f"staged {vb.manifest_digest}" + ("" if staged else " (bootc status did not report a digest)")


# ── the verbs ─────────────────────────────────────────────────────────────────────


def _refuse(cfg: Config, source: str, bid: str, err: Refused, extra: Dict[str, Any]) -> int:
    """The single refusal path: AUDIT (fsync'd) first, then status, then exit 1."""
    head = _audit(cfg, "offline-update.refused", reason=err.reason, detail=err.detail,
                  bundle=source, bundle_id=bid, **extra)
    _write_status(cfg, state="refused", reason=err.reason, detail=err.detail,
                  bundle=source, audit_head=head, **{k: v for k, v in extra.items()
                                                     if k in ("signer_pubkey",)})
    return EXIT_REFUSED


def _unjudged(cfg: Config, source: str, bid: str, detail: str) -> int:
    head = ""
    with contextlib.suppress(Unjudged):
        head = _audit(cfg, "offline-update.could-not-judge", detail=detail,
                      bundle=source, bundle_id=bid)
    _write_status(cfg, state="could-not-judge", detail=detail, bundle=source, audit_head=head)
    return EXIT_UNJUDGED


def _signer_of(bundle_dir: Optional[Path]) -> Dict[str, Any]:
    if bundle_dir is None:
        return {}
    try:
        d = json.loads((bundle_dir / SEAL_NAME).read_text(encoding="utf-8"))
        return {"signer_pubkey": str(d.get("public_key", ""))[:64]}
    except (OSError, ValueError, AttributeError):
        return {}


@contextlib.contextmanager
def _lock(cfg: Config):
    cfg.staging.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.staging / ".lock", "a+")
    try:
        try:
            import fcntl  # type: ignore
        except ImportError:  # Windows dev hosts: tests only
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Unjudged("another awnix offline-update is running") from exc
        yield
    finally:
        fh.close()


def run_bundle(cfg: Config, bundle: str, mode: str,
               allow_downgrade: bool = False) -> Tuple[int, Dict[str, Any]]:
    """mode: verify | stage | apply. Returns (exit, result)."""
    source = str(bundle)
    bid = ""
    wd: Optional[Path] = None
    bundle_dir: Optional[Path] = None
    keep = False
    try:
        _audit_preflight(cfg)
        with _lock(cfg):
            gc(cfg, quiet=True)
            form, src = _resolve_source(bundle)
            source = str(src)
            bid, wd = _new_workdir(cfg)
            bundle_dir = wd / "bundle"
            try:
                if form == "awshare":
                    _unpack_awshare(cfg, src, wd, bundle_dir)
                else:
                    _copy_dir_bundle(cfg, src, bundle_dir)
                vb = _verify_copy(cfg, bid, source, wd, bundle_dir, allow_downgrade)
            except Refused as err:
                rc = _refuse(cfg, source, bid, err, _signer_of(bundle_dir))
                return rc, {"ok": False, "state": "refused", "reason": err.reason,
                            "detail": err.detail, "bundle": source}
            head = _audit(cfg, "offline-update.verified", **vb.summary())
            if mode == "verify":
                st = _write_status(cfg, state="verified", audit_head=head, **_status_fields(vb))
                return EXIT_OK, {"ok": True, **st}
            ok, detail = _stage_verified(cfg, vb)
            if not ok:
                head = _audit(cfg, "offline-update.stage-failed", detail=detail, **vb.summary())
                st = _write_status(cfg, state="could-not-judge", reason="stage-failed",
                                   detail=detail, audit_head=head, **_status_fields(vb))
                return EXIT_UNJUDGED, {"ok": False, **st}
            keep = True
            try:
                _raise_floor(cfg, vb)
            except OSError as exc:  # staged, but the floor did not move: say so, loudly
                detail += f"; WARNING rollback floor not raised: {exc}"
            head = _audit(cfg, "offline-update.staged", detail=detail, **vb.summary())
            st = _write_status(cfg, state="staged", detail=detail, audit_head=head,
                               staged_path=str(vb.archive), **_status_fields(vb))
            if mode == "apply":
                st["reboot"] = _schedule_reboot(cfg)
            return EXIT_OK, {"ok": True, **st}
    except Unjudged as exc:
        return _unjudged(cfg, source, bid, str(exc)), {"ok": False, "state": "could-not-judge",
                                                        "detail": str(exc), "bundle": source}
    except Exception as exc:  # noqa: BLE001 -- hostile media must never crash past the audit
        detail = f"internal error judging the bundle: {type(exc).__name__}: {exc}"
        return _unjudged(cfg, source, bid, detail), {"ok": False, "state": "could-not-judge",
                                                      "detail": detail, "bundle": source}
    finally:
        if wd is not None and not keep:
            shutil.rmtree(wd, ignore_errors=True)


def _status_fields(vb: VerifiedBundle) -> Dict[str, Any]:
    s = vb.summary()
    return {k: s[k] for k in ("bundle", "variant", "version", "manifest_digest", "signer_pubkey",
                              "created", "downgrade_override")}


def _schedule_reboot(cfg: Config) -> str:
    if cfg.no_reboot:
        return "skipped (AWNIX_NO_REBOOT=1)"
    try:
        subprocess.Popen(["systemctl", "--no-block", "reboot"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "scheduled"
    except OSError as exc:
        return f"not scheduled: {exc}"


def gc(cfg: Config, quiet: bool = False) -> Dict[str, Any]:
    """Drop staging copies bootc does not reference. Unknown bootc state = keep all."""
    kept: List[str] = []
    removed: List[str] = []
    if not cfg.staging.is_dir():
        return {"ok": True, "kept": kept, "removed": removed}
    dirs = [d for d in sorted(cfg.staging.iterdir()) if d.is_dir()]
    if not dirs:
        return {"ok": True, "kept": kept, "removed": removed}
    st = _bootc_status(cfg)
    if st is None:
        return {"ok": False, "kept": [d.name for d in dirs], "removed": [],
                "detail": "bootc status unavailable; kept everything"}
    blob = json.dumps(st)
    for d in dirs:
        if str(d) in blob or d.name in blob:
            kept.append(d.name)
        else:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return {"ok": True, "kept": kept, "removed": removed}


def scan(cfg: Config, dirs: List[str]) -> List[Dict[str, Any]]:
    roots = [Path(d) for d in dirs] if dirs else [cfg.p(r) for r in MEDIA_ROOTS]
    found: List[Dict[str, Any]] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for cur, subdirs, files in os.walk(root, followlinks=False):
            cp = Path(cur)
            if len(cp.parts) - base_depth >= 4:
                subdirs[:] = []
            subdirs[:] = [s for s in subdirs if not s.startswith(".")]
            if UPDATE_JSON in files and str(cp) not in seen:
                seen.add(str(cp))
                item: Dict[str, Any] = {"path": str(cp), "form": "dir", "verified": False,
                                        "sealed": SEAL_NAME in files}
                with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
                    d = json.loads((cp / UPDATE_JSON).read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        for k in ("variant", "version", "manifest_digest", "archive_size"):
                            item[k] = d.get(k)
                found.append(item)
                subdirs[:] = []
                continue
            for f in files:
                if f.endswith(AWSHARE_SUFFIX):
                    found.append({"path": str(cp / f), "form": "awshare", "verified": False,
                                  "sealed": None})
    return found


def trust_add(cfg: Config, hexkey: str) -> Tuple[int, Dict[str, Any]]:
    k = hexkey.strip().lower()
    if not HEX64.match(k):
        return EXIT_REFUSED, {"ok": False, "detail": "a trusted key is 64 hex characters (Ed25519 public key)"}
    try:
        awseal = _need("awseal")
        awseal.load_public_key(k)
        _audit_preflight(cfg)
        d = cfg.admin_trust_dir
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{k[:16]}.pub"
        target.write_text(k + "\n", encoding="utf-8", newline="\n")
        with contextlib.suppress(OSError):
            os.chmod(target, 0o644)
        head = _audit(cfg, "offline-update.trust-added", pubkey=k, file=str(target))
    except Unjudged as exc:
        return EXIT_UNJUDGED, {"ok": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- SealError: not a valid Ed25519 point
        return EXIT_REFUSED, {"ok": False, "detail": str(exc)}
    return EXIT_OK, {"ok": True, "pubkey": k, "file": str(target), "audit_head": head}


# ── self-test: good / flipped byte / wrong key with a stub bootc, all in a tempdir ────

_STUB_BOOTC = r'''
import json, os, sys
state = os.environ["STUB_BOOTC_STATE"]
with open(state + ".calls", "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:1] == ["switch"]:
    path = args[-1]
    digest = ""
    import tarfile
    with tarfile.open(path, "r:") as tf:
        for m in tf.getmembers():
            if m.name.lstrip("./") == "index.json":
                digest = json.load(tf.extractfile(m))["manifests"][0]["digest"]
    json.dump({"path": path, "digest": digest}, open(state, "w"))
    print("Queued for next boot")
    sys.exit(0)
if args[:1] == ["status"]:
    staged = None
    if os.path.exists(state):
        s = json.load(open(state))
        staged = {"image": {"image": {"image": s["path"], "transport": "oci-archive"},
                            "imageDigest": s["digest"]}}
    print(json.dumps({"status": {"booted": {"image": {"image": {"image": "ghcr.io/x/awnix",
                      "transport": "registry"}, "imageDigest": "sha256:" + "0" * 64}},
                      "staged": staged, "rollback": None}}))
    sys.exit(0)
sys.exit(2)
'''


def make_fake_oci_archive(path: Path, variant: str = "selftest", banner: str = "N+1") -> str:
    """A tiny, valid, deterministic oci-archive. Returns its manifest digest."""
    blobs: Dict[str, bytes] = {}

    def put(data: bytes) -> Dict[str, Any]:
        d = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs[d] = data
        return {"digest": d, "size": len(data)}

    layer_buf = io.BytesIO()
    with tarfile.open(fileobj=layer_buf, mode="w") as lt:
        body = (banner + "\n").encode()
        ti = tarfile.TarInfo("etc/awnix-banner")
        ti.size, ti.mtime = len(body), 0
        lt.addfile(ti, io.BytesIO(body))
    layer = put(layer_buf.getvalue())
    config = put(json.dumps({
        "architecture": "amd64", "os": "linux",
        "config": {"Labels": {"com.aitheros.variant": variant}},
        "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
    }, sort_keys=True).encode())
    manifest = put(json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **config},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", **layer}],
    }, sort_keys=True).encode())
    index = json.dumps({"schemaVersion": 2, "manifests": [{
        "mediaType": "application/vnd.oci.image.manifest.v1+json", **manifest,
        "annotations": {"org.opencontainers.image.ref.name": "selftest"}}]}).encode()
    entries = [("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')]
    entries += [("blobs/sha256/" + d.split(":", 1)[1], b) for d, b in sorted(blobs.items())]
    entries.append(("index.json", index))
    with tarfile.open(path, "w") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime = len(data), 0
            tf.addfile(ti, io.BytesIO(data))
    return manifest["digest"]


def make_update_json(*, variant: str, image_repo: str, version: str, manifest_digest: str,
                     archive: str, archive_sha256: str, archive_size: int,
                     created: Optional[str] = None) -> Dict[str, Any]:
    return {"schema": UPDATE_SCHEMA, "kind": UPDATE_KIND, "variant": variant,
            "image_repo": image_repo, "version": version, "manifest_digest": manifest_digest,
            "archive": archive, "archive_sha256": archive_sha256,
            "archive_size": archive_size, "created": created or _now()}


def _selftest_bundle(out: Path, key: Path, variant: str, created: Optional[str] = None) -> None:
    import awseal  # noqa: F401 -- checked by the caller

    out.mkdir(parents=True)
    arc = out / "image.oci.tar"
    digest = make_fake_oci_archive(arc, variant=variant)
    upd = make_update_json(variant=variant, image_repo="localhost/selftest", version="n+1",
                           manifest_digest=digest, archive=arc.name,
                           archive_sha256=sha256_file(arc), archive_size=arc.stat().st_size,
                           created=created)
    (out / UPDATE_JSON).write_text(json.dumps(upd, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8", newline="\n")
    seal = awseal.sign(out, key_path=key, subject="awnix-offline-update",
                       meta={"variant": variant, "manifest_digest": digest})
    awseal.write(seal, out)


def self_test() -> int:
    fails: List[str] = []

    def check(cond: bool, what: str) -> None:
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            fails.append(what)

    try:
        awseal = _need("awseal")
        awdit = _need("awdit")
        _need("cryptography")
    except Unjudged as exc:
        print(f"SELF-TEST UNJUDGED: {exc}")
        return EXIT_UNJUDGED

    with tempfile.TemporaryDirectory(prefix="awnix-ofu-st-") as td:
        t = Path(td)
        root = t / "root"
        (root / "usr/lib/awnix").mkdir(parents=True)
        (root / "usr/lib/awnix/release.env").write_text("AWNIX_VARIANT=selftest\n", encoding="utf-8")
        good_key = awseal.keygen(t / "keys" / "good.key")
        bad_key = awseal.keygen(t / "keys" / "bad.key")
        tdir = root / "usr/share/awnix/offline-trust.d"
        tdir.mkdir(parents=True)
        (tdir / "selftest.pub").write_text(awseal.public_key_hex(path=good_key) + "\n", encoding="utf-8")

        media = t / "media"
        _selftest_bundle(media / "good", good_key, "selftest")
        shutil.copytree(media / "good", media / "flipped")
        arc = media / "flipped" / "image.oci.tar"
        data = bytearray(arc.read_bytes())
        data[len(data) // 2] ^= 0x01
        arc.write_bytes(bytes(data))
        _selftest_bundle(media / "wrongkey", bad_key, "selftest")
        _selftest_bundle(media / "older", good_key, "selftest", created="2020-01-01T00:00:00+00:00")

        stub = t / "stub_bootc.py"
        stub.write_text(_STUB_BOOTC, encoding="utf-8")
        state = t / "bootc.state"
        env = {"AWNIX_OFFLINE_ROOT": str(root), "AWNIX_NO_REBOOT": "1",
               "AWNIX_OFFLINE_BOOTC": json.dumps([sys.executable, str(stub)])}
        os.environ["STUB_BOOTC_STATE"] = str(state)
        cfg = Config.from_env(env)

        def switch_calls() -> int:
            calls = Path(str(state) + ".calls")
            if not calls.exists():
                return 0
            return sum(1 for ln in calls.read_text().splitlines() if json.loads(ln)[:1] == ["switch"])

        rc, res = run_bundle(cfg, str(media / "flipped"), "stage")
        check(rc == EXIT_REFUSED and res.get("reason") == "content-mismatch",
              f"flipped byte refused as content-mismatch (rc={rc} reason={res.get('reason')})")
        check(switch_calls() == 0, "flipped byte: bootc switch never called")
        rc, res = run_bundle(cfg, str(media / "wrongkey"), "stage")
        check(rc == EXIT_REFUSED and res.get("reason") == "untrusted-key",
              f"wrong key refused as untrusted-key (rc={rc} reason={res.get('reason')})")
        check(switch_calls() == 0, "wrong key: bootc switch never called")
        rc, res = run_bundle(cfg, str(media / "good"), "stage")
        check(rc == EXIT_OK and res.get("state") == "staged", f"good bundle staged (rc={rc} state={res.get('state')})")
        check(switch_calls() == 1, "good bundle: bootc switch called exactly once")
        rc, res = run_bundle(cfg, str(media / "older"), "stage")
        check(rc == EXIT_REFUSED and res.get("reason") == "rollback",
              f"validly signed OLDER bundle refused as rollback (rc={rc} reason={res.get('reason')})")
        check(switch_calls() == 1, "older bundle: bootc switch never called")

        recs = list(awdit.read(str(cfg.audit_path)))
        events = [r.get("event") for r in recs]
        check(events.count("offline-update.refused") == 3, f"3 refused audit records ({events})")
        check(events.count("offline-update.staged") == 1, "1 staged audit record")
        v = awdit.verify(str(cfg.audit_path))
        check(bool(v.ok) and v.count == len(recs), f"awdit chain_ok (count={v.count})")
        st = json.loads(cfg.status_path.read_text(encoding="utf-8"))
        check(st.get("state") == "refused" and st.get("reason") == "rollback"
              and st.get("audit_head") == recs[-1].get("hash"),
              "status.json state=refused/rollback, audit_head = last record")
        left = [d.name for d in cfg.staging.iterdir() if d.is_dir()]
        check(len(left) == 1, f"only the staged copy is kept in staging ({left})")
        os.environ.pop("STUB_BOOTC_STATE", None)

    if fails:
        print(f"SELF-TEST FAIL: {len(fails)} check(s)")
        return EXIT_REFUSED
    print("SELF-TEST PASS")
    return EXIT_OK


# ── CLI ───────────────────────────────────────────────────────────────────────────


def _emit(obj: Any, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(human)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--list-verbs"]:
        print("\n".join(VERBS))
        return EXIT_OK
    if argv[:1] == ["--self-test"]:
        return self_test()

    ap = argparse.ArgumentParser(prog="awnix offline-update",
                                 description="verify, then stage, a signed offline update bundle")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("scan")
    s.add_argument("dirs", nargs="*")
    s.add_argument("--json", action="store_true")
    for v in ("verify", "stage", "apply"):
        s = sub.add_parser(v)
        s.add_argument("bundle")
        s.add_argument("--json", action="store_true")
        s.add_argument("--allow-downgrade", action="store_true",
                       help="accept a signed bundle OLDER than the rollback floor (audited)")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("gc")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("trust")
    tsub = s.add_subparsers(dest="tverb", required=True)
    tl = tsub.add_parser("list")
    tl.add_argument("--json", action="store_true")
    ta = tsub.add_parser("add")
    ta.add_argument("pubkey")
    ta.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cfg = Config.from_env()

    if a.verb == "scan":
        items = scan(cfg, a.dirs)
        _emit({"bundles": items}, a.json, "\n".join(
            f"{i['path']}  {i.get('variant', '?')} {i.get('version', '?')}  (unverified)"
            for i in items) or "no bundles found")
        return EXIT_OK
    if a.verb in ("verify", "stage", "apply"):
        rc, res = run_bundle(cfg, a.bundle, a.verb, allow_downgrade=a.allow_downgrade)
        _emit(res, a.json, f"{res.get('state')}: {res.get('reason') or ''} {res.get('detail') or ''}".strip())
        return rc
    if a.verb == "status":
        try:
            doc = json.loads(cfg.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"schema": 1, "state": "never-checked"}
        doc["trusted_keys"] = len(trusted_keys(cfg))
        _emit(doc, a.json, f"{doc.get('state')}: {doc.get('reason', '')} {doc.get('detail', '')}".strip())
        return EXIT_OK
    if a.verb == "gc":
        try:
            with _lock(cfg):
                res = gc(cfg)
        except Unjudged as exc:
            res = {"ok": False, "detail": str(exc)}
        _emit(res, a.json, f"kept {res.get('kept', [])} removed {res.get('removed', [])}")
        return EXIT_OK if res.get("ok") else EXIT_UNJUDGED
    if a.verb == "trust":
        if a.tverb == "list":
            keys = trusted_keys(cfg)
            _emit({"keys": [{"pubkey": k, "file": f} for k, f in keys.items()]}, a.json,
                  "\n".join(f"{k}  {f}" for k, f in keys.items()) or "no trusted keys")
            return EXIT_OK
        rc, res = trust_add(cfg, a.pubkey)
        _emit(res, a.json, res.get("file") or res.get("detail", ""))
        return rc
    return EXIT_UNJUDGED


if __name__ == "__main__":
    sys.exit(main())
