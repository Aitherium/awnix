#!/usr/bin/env python3
"""awnix proof verifier -- one stdlib-only file for the offline verifier laptop.

Air-gap proof plan Step 6: the node exports its audit chain plus the Step 1-5
evidence to USB; a SEPARATE, also-offline laptop verifies it. That laptop may
have nothing installed but a Python 3.10+ interpreter, so this file carries:

  * the awdit hash-chain format (digest_record is byte-equal to
    AitherOS/packages/awdit/awdit/log.py) and a verifier that names the FIRST
    broken record, 0-based, and why: altered | gap | truncated | unparseable;
  * the awseal seal format (awseal.json: file map + tree digest, Ed25519 over the
    canonical payload) -- sign and verify;
  * Ed25519 (RFC 8032) in pure Python, used when `cryptography` is absent or
    AWNIX_PROOF_FORCE_PUREPY=1 -- a verifier that needs pip on an air-gapped
    laptop is not a verifier;
  * a pcap / pcapng egress analyzer (Ethernet, Linux SLL, SLL2, raw IP) that
    allows only ARP, ICMPv6 ND/RS/RA/redirect (133-137) and MLD (130-132, 143);
  * `tamper-demo`: flip one byte in record k, delete record j, cut the tail, and
    prove each is detected at the right index.

Usage:
    python3 awnix_proof_verify.py verify <dir|tar> --node-key HEX [--json]
    python3 awnix_proof_verify.py tamper-demo <dir|tar> [--flip-index K] [--delete-index J] [--json]
    python3 awnix_proof_verify.py verify-chain <file.jsonl> [--json]
    python3 awnix_proof_verify.py analyze-pcap <file> [--json]
    python3 awnix_proof_verify.py pubkey <pkcs8.pem>
    python3 awnix_proof_verify.py --self-test

Exit codes: 0 verified / PASS, 1 violation, 2 could not judge.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # optional accelerator; the pure-Python path below is always available
    from cryptography.hazmat.primitives import serialization as _serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _HAVE_CRYPTO = True
except Exception:  # noqa: BLE001 - any import failure means "use pure Python"
    _HAVE_CRYPTO = False

VERSION = "1"

EXIT_OK, EXIT_FAIL, EXIT_CNJ = 0, 1, 2


class ProofError(RuntimeError):
    """Raised when something cannot be judged (exit 2), never for a violation."""


# ---------------------------------------------------------------------------
# Ed25519, RFC 8032 section 6 reference arithmetic (pure Python)
# ---------------------------------------------------------------------------
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(pt1: Tuple[int, int, int, int], pt2: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    a = (pt1[1] - pt1[0]) * (pt2[1] - pt2[0]) % _P
    b = (pt1[1] + pt1[0]) * (pt2[1] + pt2[0]) % _P
    c = 2 * pt1[3] * pt2[3] * _D % _P
    d = 2 * pt1[2] * pt2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, pt: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    acc = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        s >>= 1
    return acc


def _equal(pt1: Tuple[int, int, int, int], pt2: Tuple[int, int, int, int]) -> bool:
    if (pt1[0] * pt2[2] - pt2[0] * pt1[2]) % _P != 0:
        return False
    return (pt1[1] * pt2[2] - pt2[1] * pt1[2]) % _P == 0


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0) or 0
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt: Tuple[int, int, int, int]) -> bytes:
    zinv = _inv(pt[2])
    x = pt[0] * zinv % _P
    y = pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes) -> Optional[Tuple[int, int, int, int]]:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _expand(seed: bytes) -> Tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _modq(b: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _Q


def purepy_public(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def purepy_sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _modq(prefix + msg)
    rs = _compress(_mul(r, _G))
    h = _modq(rs + pub + msg)
    s = (r + h * a) % _Q
    return rs + int.to_bytes(s, 32, "little")


def purepy_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    a_pt = _decompress(pub)
    r_pt = _decompress(sig[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    h = _modq(sig[:32] + pub + msg)
    return _equal(_mul(s, _G), _add(r_pt, _mul(h, a_pt)))


def _purepy_forced() -> bool:
    return os.environ.get("AWNIX_PROOF_FORCE_PUREPY") == "1" or not _HAVE_CRYPTO


def ed25519_backend() -> str:
    return "pure-python" if _purepy_forced() else "cryptography"


def ed25519_public(seed: bytes) -> bytes:
    if _purepy_forced():
        return purepy_public(seed)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(
        encoding=_serialization.Encoding.Raw, format=_serialization.PublicFormat.Raw
    )


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    if _purepy_forced():
        return purepy_sign(seed, msg)
    return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if _purepy_forced():
        return purepy_verify(pub, msg, sig)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except Exception:  # noqa: BLE001 - any failure is a failed verification
        return False


# ---------------------------------------------------------------------------
# Keys: PKCS#8 PEM, the format awseal writes (so either tool reads the other's)
# ---------------------------------------------------------------------------
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def pem_from_seed(seed: bytes) -> bytes:
    der = _PKCS8_PREFIX + seed
    body = base64.encodebytes(der).decode("ascii").replace("\n", "")
    return ("-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----\n").encode("ascii")


def seed_from_pem(data: bytes) -> bytes:
    lines = [ln.strip() for ln in data.decode("ascii", "replace").splitlines()]
    body = "".join(ln for ln in lines if ln and not ln.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise ProofError(f"key is not PEM/base64: {exc}") from exc
    if len(der) != 48 or not der.startswith(_PKCS8_PREFIX):
        raise ProofError("key is not an unencrypted PKCS#8 Ed25519 private key")
    return der[16:]


def load_seed(path: Path) -> bytes:
    try:
        return seed_from_pem(Path(path).read_bytes())
    except OSError as exc:
        raise ProofError(f"cannot read key {path}: {exc}") from exc


def generate_key(path: Path) -> str:
    """Write a fresh PKCS#8 key (0600). Refuses to overwrite. Returns public hex."""
    path = Path(path)
    if path.exists():
        raise ProofError(f"{path} exists; refusing to overwrite a signing key")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = os.urandom(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem_from_seed(seed))
    return ed25519_public(seed).hex()


# ---------------------------------------------------------------------------
# awdit chain format (byte-equal to AitherOS/packages/awdit/awdit/log.py)
# ---------------------------------------------------------------------------
GENESIS = "0" * 64
ANCHOR_SUFFIX = ".anchor"


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_record(prev: str, body: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(prev.encode("ascii"))
    h.update(b"\x00")
    h.update(_canonical(body))
    return h.hexdigest()


def anchor_path(log_path: Path) -> Path:
    return Path(str(log_path) + ANCHOR_SUFFIX)


def read_records(log_path: Path) -> Iterator[Dict[str, Any]]:
    p = Path(log_path)
    if not p.is_file():
        return
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
                yield rec
            except ValueError:
                yield {"__unparseable__": True, "line": i}


def chain_head(log_path: Path) -> Tuple[str, int]:
    prev, n = GENESIS, 0
    for rec in read_records(log_path):
        prev = str(rec.get("hash", ""))
        n += 1
    return prev, n


def append_record(log_path: Path, event: str, **fields: Any) -> Dict[str, Any]:
    """Append one awdit record, fsync it, then move the anchor (awdit's order)."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev, count = chain_head(path)
    body = {"ts": time.time(), "event": event, "prev": prev, "data": fields}
    record = dict(body)
    record["hash"] = digest_record(prev, body)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    anchor_path(path).write_text(
        json.dumps({"head": record["hash"], "count": count + 1}, sort_keys=True), encoding="utf-8"
    )
    return record


def verify_chain(log_path: Path) -> Dict[str, Any]:
    """Verify one chain; report the FIRST broken record (0-based) and its kind."""
    path = Path(log_path)
    out: Dict[str, Any] = {
        "file": path.name,
        "chain_ok": False,
        "first_broken_index": None,
        "kind": None,
        "count": 0,
        "anchored": False,
        "problems": [],
    }
    if not path.is_file():
        out["kind"] = "unparseable"
        out["problems"].append("file missing")
        return out
    prev = GENESIS
    count = 0
    for idx, rec in enumerate(read_records(path)):
        count = idx + 1
        if rec.get("__unparseable__"):
            out.update(first_broken_index=idx, kind="unparseable")
            out["problems"].append(f"record {idx}: line {rec['line']} is not a JSON object")
            break
        stored = rec.get("hash")
        body = {k: rec[k] for k in ("ts", "event", "prev", "data") if k in rec}
        try:
            expected = digest_record(str(rec.get("prev", "")), body)
        except (TypeError, UnicodeEncodeError, ValueError):
            expected = ""
        if stored != expected:
            out.update(first_broken_index=idx, kind="altered")
            out["problems"].append(f"record {idx} ALTERED: content does not match its hash")
            break
        if rec.get("prev") != prev:
            out.update(first_broken_index=idx, kind="gap")
            out["problems"].append(f"record {idx} GAP: prev does not match record {idx - 1}'s hash")
            break
        prev = str(stored)
    out["count"] = count
    if out["kind"] is not None:
        return out

    ap = anchor_path(path)
    if not ap.is_file():
        out["chain_ok"] = True
        out["problems"].append("NO ANCHOR: tail truncation is undetectable for this file (warning)")
        return out
    try:
        anchor = json.loads(ap.read_text(encoding="utf-8"))
        a_count = int(anchor.get("count", 0))
        a_head = str(anchor.get("head", ""))
    except (OSError, ValueError, TypeError, AttributeError):
        out.update(kind="unparseable", first_broken_index=count)
        out["problems"].append("ANCHOR UNREADABLE")
        return out
    out["anchored"] = True
    if a_count > count:
        out.update(kind="truncated", first_broken_index=count)
        out["problems"].append(f"TRUNCATED: anchor says {a_count} records, file holds {count}")
        return out
    if count and a_head != prev:
        out.update(kind="anchor-mismatch", first_broken_index=max(count - 1, 0))
        out["problems"].append("HEAD MISMATCH: anchor head differs from the chain head")
        return out
    out["chain_ok"] = True
    return out


# ---------------------------------------------------------------------------
# awseal seal format
# ---------------------------------------------------------------------------
SEAL_NAME = "awseal.json"
SEAL_VERSION = 1
DEFAULT_EXCLUDES = ("__pycache__", ".git", ".ruff_cache", ".pytest_cache", ".DS_Store", ".mypy_cache")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_map(root: Path) -> Dict[str, str]:
    root = Path(root)
    ex = set(DEFAULT_EXCLUDES) | {SEAL_NAME}
    files: Dict[str, str] = {}
    for p in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix()):
        if not p.is_file() or ex & set(p.relative_to(root).parts):
            continue
        files[p.relative_to(root).as_posix()] = sha256_file(p)
    return files


def tree_digest(files: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _seal_canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal_dir(root: Path, seed: bytes, subject: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write root/awseal.json in the awseal v1 format, signed with `seed`."""
    root = Path(root)
    files = file_map(root)
    if not files:
        raise ProofError(f"{root} has no files to seal")
    payload = {
        "version": SEAL_VERSION,
        "tree_digest": tree_digest(files),
        "files": files,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subject": subject or root.name,
        "meta": dict(meta or {}),
    }
    doc = dict(payload)
    doc["public_key"] = ed25519_public(seed).hex()
    doc["signature"] = ed25519_sign(seed, _seal_canonical(payload)).hex()
    (root / SEAL_NAME).write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return doc


def verify_seal(root: Path, expect_key: Optional[str] = None) -> Dict[str, Any]:
    """awseal.verify semantics: signature_ok, content_ok, key_trusted, diff -- separately."""
    root = Path(root)
    res: Dict[str, Any] = {
        "present": False,
        "signature_ok": False,
        "content_ok": False,
        "key_trusted": None,
        "diff": {"added": [], "removed": [], "modified": []},
        "public_key": None,
        "meta": {},
        "ok": False,
        "error": None,
        "backend": ed25519_backend(),
    }
    target = root / SEAL_NAME
    if not target.is_file():
        res["error"] = "seal-missing"
        return res
    res["present"] = True
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
        if doc.get("version") != SEAL_VERSION:
            raise ValueError(f"seal version {doc.get('version')!r}")
        payload = {k: doc[k] for k in ("version", "tree_digest", "files", "created")}
        payload["subject"] = doc.get("subject", "")
        payload["meta"] = doc.get("meta") or {}
        pub = bytes.fromhex(str(doc["public_key"]))
        sig = bytes.fromhex(str(doc["signature"]))
        if not isinstance(doc["files"], dict) or not doc["files"]:
            raise ValueError("seal covers no files")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        res["error"] = f"seal-unreadable: {exc}"
        return res
    res["public_key"] = doc["public_key"]
    res["meta"] = payload["meta"]
    res["signature_ok"] = ed25519_verify(pub, _seal_canonical(payload), sig)
    actual = file_map(root)
    res["content_ok"] = tree_digest(actual) == doc["tree_digest"] and actual == doc["files"]
    exp, act = set(doc["files"]), set(actual)
    res["diff"] = {
        "added": sorted(act - exp),
        "removed": sorted(exp - act),
        "modified": sorted(p for p in exp & act if doc["files"][p] != actual[p]),
    }
    if expect_key is not None:
        res["key_trusted"] = str(doc["public_key"]).lower() == expect_key.strip().lower()
    res["ok"] = bool(res["signature_ok"] and res["content_ok"] and res["key_trusted"] is not False)
    return res


# ---------------------------------------------------------------------------
# Egress analyzer: pcap + pcapng
# ---------------------------------------------------------------------------
LT_NULL, LT_ETHERNET, LT_RAW, LT_RAW12, LT_SLL, LT_IPV4, LT_IPV6, LT_SLL2 = 0, 1, 101, 12, 113, 228, 229, 276
_ARPHRD_LOOPBACK = 772
ALLOWED_ICMPV6 = frozenset(range(130, 138)) | {143}
_IPV6_EXT = {0, 43, 60, 51, 135, 139, 140}  # 44 (fragment) is handled separately: always counted


class CaptureError(ProofError):
    """A capture that cannot be read end to end: never reported as clean."""


def _iter_pcap(data: bytes) -> Iterator[Tuple[int, Optional[float], bytes]]:
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        end = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        end = ">"
    else:
        raise CaptureError("not a pcap file")
    frac = 1e-9 if magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1e-6
    if len(data) < 24:
        raise CaptureError("truncated pcap global header")
    linktype = struct.unpack(end + "I", data[20:24])[0] & 0x0FFFFFFF
    off = 24
    while off < len(data):
        if off + 16 > len(data):
            raise CaptureError(f"truncated record header at byte {off}")
        ts_s, ts_f, incl = struct.unpack(end + "III", data[off: off + 12])
        off += 16
        if off + incl > len(data):
            raise CaptureError(f"truncated packet at byte {off} (needs {incl} bytes)")
        yield linktype, ts_s + ts_f * frac, data[off: off + incl]
        off += incl


def _idb_tsresol(body: bytes, end: str) -> float:
    """if_tsresol (option 9) of an Interface Description Block; default microseconds."""
    off = 8
    while off + 4 <= len(body):
        code, olen = struct.unpack(end + "HH", body[off: off + 4])
        if code == 0:
            break
        if code == 9 and olen >= 1:
            v = body[off + 4]
            return 2.0 ** -(v & 0x7F) if v & 0x80 else 10.0 ** -v
        off += 4 + ((olen + 3) & ~3)
    return 1e-6


def _iter_pcapng(data: bytes) -> Iterator[Tuple[int, Optional[float], bytes]]:
    off = 0
    end = "<"
    ifaces: List[Tuple[int, float]] = []
    while off < len(data):
        if off + 12 > len(data):
            raise CaptureError(f"truncated pcapng block at byte {off}")
        btype_raw = data[off: off + 4]
        if btype_raw == b"\x0a\x0d\x0d\x0a":
            bom = data[off + 8: off + 12]
            end = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
        btype, blen = struct.unpack(end + "II", data[off: off + 8])
        if blen < 12 or blen % 4 or off + blen > len(data):
            raise CaptureError(f"truncated or malformed pcapng block at byte {off}")
        body = data[off + 8: off + blen - 4]
        if btype == 1:  # IDB
            ifaces.append((struct.unpack(end + "H", body[0:2])[0], _idb_tsresol(body, end)))
        elif btype == 6:  # EPB
            iface, hi, lo, cap, _orig = struct.unpack(end + "IIIII", body[0:20])
            if iface >= len(ifaces) or 20 + cap > len(body):
                raise CaptureError(f"bad enhanced packet block at byte {off}")
            yield ifaces[iface][0], ((hi << 32) | lo) * ifaces[iface][1], body[20: 20 + cap]
        elif btype == 3:  # SPB
            if not ifaces:
                raise CaptureError("simple packet block before any interface")
            orig = struct.unpack(end + "I", body[0:4])[0]
            yield ifaces[0][0], None, body[4: 4 + min(orig, len(body) - 4)]
        off += blen


def _icmpv6_allowed(icmp_type: int, dst: bytes, hop_limit: int) -> bool:
    """ND/MLD only: link-local or multicast destination and the hop limit the RFCs pin.

    ND (133-137) must carry hop limit 255 (RFC 4861 s7.1); MLD (130-132, 143)
    carries 1 (RFC 2710 / 3810). A global destination is never allowed: it is
    routable, so it could carry data off the link (a NUD probe to a global
    address is therefore counted -- conservative on an air-gapped segment).
    """
    if icmp_type not in ALLOWED_ICMPV6:
        return False
    link_local = dst[0] == 0xFE and (dst[1] & 0xC0) == 0x80
    multicast = dst[0] == 0xFF
    if not (link_local or multicast):
        return False
    return hop_limit == (255 if 133 <= icmp_type <= 137 else 1)


def _classify_ip(pkt: bytes, ver_hint: Optional[int] = None) -> Dict[str, Any]:
    """Return {allowed, loopback, dns, tcp_syn, udp, what}."""
    # Loopback is NEVER inferred from IP addresses: 127/8 or ::1 on a wire is a
    # martian and counts. Only the link layer (SLL/SLL2 ARPHRD_LOOPBACK, DLT_NULL)
    # marks a frame as loopback -- see _classify_frame.
    res = {"allowed": False, "loopback": False, "dns": False, "tcp_syn": False, "udp": False, "what": "?"}
    if not pkt:
        res["what"] = "empty"
        return res
    ver = pkt[0] >> 4 if ver_hint is None else ver_hint
    if ver == 4 and len(pkt) >= 20:
        ihl = (pkt[0] & 0x0F) * 4
        proto = pkt[9]
        src, dst = pkt[12:16], pkt[16:20]
        l4 = pkt[ihl:]
        res["what"] = f"ipv4 proto={proto} {'.'.join(map(str, src))}->{'.'.join(map(str, dst))}"
    elif ver == 6 and len(pkt) >= 40:
        proto = pkt[6]
        hop_limit = pkt[7]
        src, dst = pkt[8:24], pkt[24:40]
        off = 40
        while proto in _IPV6_EXT and off + 8 <= len(pkt):
            nxt = pkt[off]
            if proto == 51:
                off += (pkt[off + 1] + 2) * 4
            else:
                off += (pkt[off + 1] + 1) * 8
            proto = nxt
        if proto == 44:
            # Any fragment counts: a non-first fragment's bytes are not an L4
            # header, and ND/MLD are never fragmented (RFC 6980).
            res["what"] = "ipv6 fragment"
            return res
        l4 = pkt[off:]
        res["what"] = f"ipv6 next={proto}"
        if proto == 58 and l4:
            res["what"] = f"icmpv6 type={l4[0]} hlim={hop_limit}"
            if _icmpv6_allowed(l4[0], dst, hop_limit):
                res["allowed"] = True
    else:
        res["what"] = f"ip version {ver} (malformed)"
        return res
    if proto == 17 and len(l4) >= 4:
        res["udp"] = True
        sport, dport = struct.unpack(">HH", l4[0:4])
        res["what"] += f" udp {sport}->{dport}"
        if 53 in (sport, dport) or 5353 in (sport, dport):
            res["dns"] = True
    elif proto == 6 and len(l4) >= 14:
        sport, dport = struct.unpack(">HH", l4[0:4])
        flags = l4[13]
        res["what"] += f" tcp {sport}->{dport} flags=0x{flags:02x}"
        if flags & 0x02 and not flags & 0x10:
            res["tcp_syn"] = True
        if 53 in (sport, dport):
            res["dns"] = True
    return res


def _classify_frame(linktype: int, frame: bytes) -> Dict[str, Any]:
    ethertype: Optional[int] = None
    payload = b""
    if linktype == LT_ETHERNET:
        if len(frame) < 14:
            return {"allowed": False, "loopback": False, "what": "runt ethernet frame"}
        ethertype, payload = struct.unpack(">H", frame[12:14])[0], frame[14:]
    elif linktype == LT_SLL:
        if len(frame) < 16:
            return {"allowed": False, "loopback": False, "what": "runt SLL frame"}
        if struct.unpack(">H", frame[2:4])[0] == _ARPHRD_LOOPBACK:
            return {"allowed": True, "loopback": True, "what": "loopback"}
        ethertype, payload = struct.unpack(">H", frame[14:16])[0], frame[16:]
    elif linktype == LT_SLL2:
        if len(frame) < 20:
            return {"allowed": False, "loopback": False, "what": "runt SLL2 frame"}
        if struct.unpack(">H", frame[8:10])[0] == _ARPHRD_LOOPBACK:
            return {"allowed": True, "loopback": True, "what": "loopback"}
        ethertype, payload = struct.unpack(">H", frame[0:2])[0], frame[20:]
    elif linktype in (LT_RAW, LT_RAW12, LT_IPV4, LT_IPV6):
        hint = 4 if linktype == LT_IPV4 else 6 if linktype == LT_IPV6 else None
        return _classify_ip(frame, hint)
    elif linktype == LT_NULL:
        # DLT_NULL is the BSD loopback interface: loopback by link type.
        return {"allowed": True, "loopback": True, "what": "loopback (DLT_NULL)"}
    else:
        raise CaptureError(f"unsupported link type {linktype}")
    while ethertype in (0x8100, 0x88A8) and len(payload) >= 4:
        ethertype, payload = struct.unpack(">H", payload[2:4])[0], payload[4:]
    if ethertype == 0x0806:
        return {"allowed": True, "loopback": False, "what": "arp"}
    if ethertype in (0x0800, 0x86DD):
        return _classify_ip(payload, 4 if ethertype == 0x0800 else 6)
    return {"allowed": False, "loopback": False, "what": f"ethertype 0x{ethertype:04x}"}


def analyze_capture(path: Path, role: str = "node") -> Dict[str, Any]:
    """Count frames outside the allowed link-layer list. Raises CaptureError when unreadable.

    role="tap": the independent wire capture. Link-layer loopback cannot occur on
    a wire, so on a tap such frames are counted, never skipped.
    ok is True only when the capture holds frames and none is disallowed; an
    empty capture proves nothing (wrong port, unplugged mirror) -> ok=None.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CaptureError(f"cannot read {path}: {exc}") from exc
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        fmt, it = "pcapng", _iter_pcapng(data)
    else:
        fmt, it = "pcap", _iter_pcap(data)
    res: Dict[str, Any] = {
        "file": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": fmt,
        "frames": 0,
        "allowed": 0,
        "loopback_skipped": 0,
        "disallowed": 0,
        "dns": 0,
        "tcp_syn": 0,
        "udp": 0,
        "first_disallowed": [],
        "linktypes": [],
        "role": role,
        "first_ts": None,
        "last_ts": None,
        "span_s": None,
    }
    for idx, (lt, ts, frame) in enumerate(it):
        res["frames"] += 1
        if lt not in res["linktypes"]:
            res["linktypes"].append(lt)
        if ts is not None:
            res["first_ts"] = ts if res["first_ts"] is None else min(res["first_ts"], ts)
            res["last_ts"] = ts if res["last_ts"] is None else max(res["last_ts"], ts)
        c = _classify_frame(lt, frame)
        if c.get("loopback") and role != "tap":
            res["loopback_skipped"] += 1
            continue
        if c.get("loopback"):
            c = {"allowed": False, "what": "link-layer loopback frame on a tap capture"}
        if c.get("dns"):
            res["dns"] += 1
        if c.get("tcp_syn"):
            res["tcp_syn"] += 1
        if c.get("udp"):
            res["udp"] += 1
        if c.get("allowed"):
            res["allowed"] += 1
        else:
            res["disallowed"] += 1
            if len(res["first_disallowed"]) < 10:
                res["first_disallowed"].append({"index": idx, "what": c.get("what", "?")})
    if res["first_ts"] is not None:
        res["span_s"] = round(res["last_ts"] - res["first_ts"], 3)
    if res["disallowed"]:
        res["ok"] = False
    elif res["frames"] == 0:
        res["ok"] = None
    else:
        res["ok"] = True
    return res


def capture_role(name: str) -> str:
    """Role from the exported file name: anything named *tap* is the wire capture."""
    return "tap" if "tap" in name.lower() else "node"


# ---------------------------------------------------------------------------
# Export verification (Step 6 on the verifier laptop)
# ---------------------------------------------------------------------------
def _safe_extract(tar_path: Path, dest: Path) -> None:
    try:
        with tarfile.open(tar_path, "r:*") as tf:
            for m in tf.getmembers():
                name = m.name
                if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
                    raise ProofError(f"refusing tar member with unsafe path: {name!r}")
                if not (m.isfile() or m.isdir()):
                    raise ProofError(f"refusing tar member that is not a file or dir: {name!r}")
            for m in tf.getmembers():
                target = dest / m.name
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise ProofError(f"cannot read tar member {m.name!r}")
                with src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except (tarfile.TarError, OSError) as exc:
        raise ProofError(f"cannot read tar {tar_path}: {exc}") from exc


def _materialize(src: Path, work: Path) -> Path:
    """Copy (dir) or safely extract (tar) the export into `work`; return its root."""
    src = Path(src)
    if src.is_dir():
        dest = work / src.name
        shutil.copytree(src, dest)
        return dest
    if src.is_file():
        dest = work / "export"
        dest.mkdir()
        _safe_extract(src, dest)
        entries = list(dest.iterdir())
        if not (dest / SEAL_NAME).exists() and len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return dest
    raise ProofError(f"{src} does not exist")


def _looks_like_awdit(path: Path) -> bool:
    """True when the first record carries the awdit keys (ts, event, prev, hash)."""
    for rec in read_records(path):
        return rec.get("__unparseable__") is True or {"event", "prev", "hash"} <= set(rec)
    return False


def _chain_files(root: Path, only: Optional[set] = None) -> List[Path]:
    """awdit chains: any file with an .anchor sibling, or a .jsonl in awdit format.

    Other JSONL (e.g. a policy log with its own seq/prev_hash scheme) is not
    judged by the awdit rules -- it would read as 'altered' for the wrong reason.
    `only`: relative posix paths allowed (the seal's file map); others are ignored.
    """
    out = []
    for p in sorted(root.rglob("*"), key=lambda q: q.as_posix()):
        if not p.is_file() or p.name.endswith(ANCHOR_SUFFIX):
            continue
        if only is not None and p.relative_to(root).as_posix() not in only:
            continue
        if anchor_path(p).is_file() or (p.suffix == ".jsonl" and _looks_like_awdit(p)):
            out.append(p)
    return out


def _sealed_paths(root: Path) -> Optional[set]:
    """Relative paths the seal's signed file map covers; None when there is no readable map."""
    try:
        files = json.loads((root / SEAL_NAME).read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return None
    return set(files) if isinstance(files, dict) else None


def verify_tree(root: Path, node_key: Optional[str]) -> Dict[str, Any]:
    """Judge ONLY what the seal covers.

    Every file on disk that is not in the signed file map is listed in
    `unsealed` and fails integrity (file_map skips __pycache__, .git, ...; an
    injected step*.json there must not be able to change a verdict). Steps,
    chains and captures are read from sealed paths only.
    """
    root = Path(root)
    report: Dict[str, Any] = {
        "schema": 1,
        "verifier": {"version": VERSION, "ed25519": ed25519_backend()},
        "seal": verify_seal(root, expect_key=node_key),
        "chains": [],
        "steps": {},
        "step_fixture_mode": [],
        "egress": {},
        "unsealed": [],
    }
    sealed = _sealed_paths(root) or set()
    on_disk = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    report["unsealed"] = [r for r in on_disk if r != SEAL_NAME and r not in sealed]

    def _sealed(p: Path) -> bool:
        return p.relative_to(root).as_posix() in sealed

    for cf in _chain_files(root, only=sealed):
        r = verify_chain(cf)
        r["file"] = cf.relative_to(root).as_posix()
        report["chains"].append(r)
    for sj in sorted(root.rglob("step*.json"), key=lambda q: q.as_posix()):
        if not _sealed(sj):
            continue
        rel = sj.relative_to(root).as_posix()
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
            name = str(d.get("step", sj.stem))
            if name in report["steps"]:
                report["steps"][f"{name} ({rel})"] = "DUPLICATE"
            else:
                report["steps"][name] = d.get("verdict", "UNKNOWN")
            if d.get("fixture_mode"):
                report["step_fixture_mode"].append(name)
        except (OSError, ValueError, AttributeError):
            report["steps"][rel] = "UNREADABLE"
    caps = [c for c in list(root.rglob("*.pcap")) + list(root.rglob("*.pcapng")) if _sealed(c)]
    for cap in sorted(caps, key=lambda q: q.as_posix()):
        rel = cap.relative_to(root).as_posix()
        try:
            report["egress"][rel] = analyze_capture(cap, role=capture_role(cap.name))
        except CaptureError as exc:
            report["egress"][rel] = {"error": str(exc), "ok": None}
    seal = report["seal"]
    report["fixture_mode"] = bool(report["step_fixture_mode"] or (seal.get("meta") or {}).get("fixture_mode"))
    integrity = bool(
        seal["signature_ok"] and seal["content_ok"] and seal["key_trusted"] is True
        and not report["unsealed"]
        and all(c["chain_ok"] for c in report["chains"])
    )
    report["integrity_ok"] = integrity
    report["ok"] = integrity
    return report


def verify_export(path: Path, node_key: Optional[str], require_steps_pass: bool = True,
                  judge_egress: bool = True, allow_fixtures: bool = False) -> Tuple[int, Dict[str, Any]]:
    """Exit 0 only when the export is intact AND says what the paper claims.

    Default judgement (what a reader of the exit code assumes): seal + key +
    chains intact, no unsealed file, every step verdict PASS, every sealed
    capture readable with frames and zero disallowed, and no fixture-mode
    evidence. `require_steps_pass=False, judge_egress=False` (--integrity-only)
    answers only "is this the node's untampered export?".
    """
    with tempfile.TemporaryDirectory(prefix="awnix-proof-verify-") as td:
        root = _materialize(Path(path), Path(td))
        report = verify_tree(root, node_key)
    report["input"] = str(path)
    notes = report.setdefault("notes", [])
    if not report["seal"]["present"] or report["seal"]["error"]:
        report["ok"] = False
        return EXIT_CNJ if not report["seal"]["present"] else EXIT_FAIL, report
    if not report["chains"]:
        report["ok"] = False
        notes.append("no audit chain in the export")
        return EXIT_CNJ, report
    if node_key is None:
        report["ok"] = False
        notes.append("no --node-key: signature proves consistency, not origin")
        return EXIT_CNJ, report
    if report["unsealed"]:
        notes.append(f"{len(report['unsealed'])} file(s) outside the seal: {', '.join(report['unsealed'][:10])}")
    if not report["ok"]:
        return EXIT_FAIL, report
    if report["fixture_mode"] and not allow_fixtures:
        report["ok"] = False
        notes.append("fixture-mode evidence (canned command output), not a real run; --allow-fixtures to rehearse")
        return EXIT_FAIL, report
    if require_steps_pass:
        bad = {k: v for k, v in report["steps"].items() if v != "PASS"}
        if bad:
            report["ok"] = False
            notes.append(f"step verdict(s) not PASS: {bad}")
            return EXIT_FAIL, report
    if judge_egress:
        dirty = [f for f, e in report["egress"].items() if e.get("ok") is False]
        if dirty:
            report["ok"] = False
            notes.append(f"egress capture(s) with disallowed frames: {', '.join(dirty)}")
            return EXIT_FAIL, report
        unjudged = [f for f, e in report["egress"].items() if e.get("ok") is None]
        if unjudged:
            report["ok"] = False
            notes.append(f"egress capture(s) unreadable or empty: {', '.join(unjudged)}")
            return EXIT_CNJ, report
    return EXIT_OK, report


# ---------------------------------------------------------------------------
# tamper-demo
# ---------------------------------------------------------------------------
def _flip_one_byte(line: str) -> Tuple[str, int]:
    """Flip one bit of one character inside the record's event value; stays valid JSON."""
    key = '"event":"'
    i = line.find(key)
    if i < 0:
        raise ProofError("record has no event field to flip")
    pos = i + len(key)
    for mask in (0x01, 0x02, 0x04):
        new = chr(ord(line[pos]) ^ mask)
        if new.isalnum():
            return line[:pos] + new + line[pos + 1:], pos
    raise ProofError("could not flip a byte and keep the record parseable")


def tamper_demo(path: Path, flip_index: Optional[int] = None, delete_index: Optional[int] = None,
                chain: Optional[str] = None, node_key: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    out: Dict[str, Any] = {"schema": 1, "input": str(path), "cases": {}}
    with tempfile.TemporaryDirectory(prefix="awnix-tamper-") as td:
        work = Path(td)
        (work / "clean").mkdir()
        root = _materialize(Path(path), work / "clean")
        chains = _chain_files(root)
        if chain:
            chains = [c for c in chains if c.name == chain or c.relative_to(root).as_posix() == chain]
        else:
            pref = [c for c in chains if c.name == "proof-audit.jsonl"]
            chains = pref or chains
        if not chains:
            out["error"] = "no audit chain found"
            return EXIT_CNJ, out
        target = chains[0]
        rel = target.relative_to(root).as_posix()
        out["chain"] = rel
        lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
        n = len(lines)
        out["count"] = n
        if n < 3:
            out["error"] = f"chain has {n} records; tamper-demo needs at least 3"
            return EXIT_CNJ, out
        k = n // 2 if flip_index is None else flip_index
        j = n // 2 if delete_index is None else delete_index
        if not (0 <= k < n) or not (0 < j < n - 1):
            out["error"] = f"flip index must be in [0,{n - 1}], delete index in [1,{n - 2}] (a middle record)"
            return EXIT_CNJ, out

        clean = verify_chain(target)
        clean_seal = verify_seal(root, expect_key=node_key) if (root / SEAL_NAME).exists() else None
        out["cases"]["clean"] = {"chain": clean, "expect": "chain_ok=true", "pass": clean["chain_ok"] is True}
        if clean_seal is not None:
            out["cases"]["clean"]["seal_ok"] = clean_seal["ok"]

        def _variant(name: str, new_lines: List[str], keep_anchor: bool = True) -> Tuple[Path, Dict[str, Any]]:
            vroot = work / name
            shutil.copytree(root, vroot)
            vt = vroot / rel
            vt.write_text("".join(ln + "\n" for ln in new_lines), encoding="utf-8", newline="\n")
            if not keep_anchor and anchor_path(vt).exists():
                anchor_path(vt).unlink()
            return vroot, verify_chain(vt)

        flipped_line, col = _flip_one_byte(lines[k])
        fl = list(lines)
        fl[k] = flipped_line
        froot, fres = _variant("flip", fl)
        fseal = verify_seal(froot, expect_key=node_key) if (froot / SEAL_NAME).exists() else None
        out["cases"]["flip"] = {
            "record": k, "column": col, "chain": fres,
            "expect": f"chain_ok=false first_broken_index={k} kind=altered",
            "pass": fres["chain_ok"] is False and fres["first_broken_index"] == k and fres["kind"] == "altered",
        }
        if fseal is not None:
            out["cases"]["flip"]["seal_content_ok"] = fseal["content_ok"]
            out["cases"]["flip"]["seal_diff"] = fseal["diff"]

        dl = lines[:j] + lines[j + 1:]
        _, dres = _variant("delete", dl)
        out["cases"]["delete"] = {
            "record": j, "chain": dres,
            "expect": f"chain_ok=false first_broken_index={j} kind=gap",
            "pass": dres["chain_ok"] is False and dres["first_broken_index"] == j and dres["kind"] == "gap",
        }

        tl = lines[:-1]
        _, tres = _variant("truncate", tl)
        out["cases"]["truncate"] = {
            "chain": tres,
            "expect": f"chain_ok=false kind=truncated first_broken_index={n - 1}",
            "pass": tres["chain_ok"] is False and tres["kind"] == "truncated" and tres["first_broken_index"] == n - 1,
        }
    out["ok"] = all(c["pass"] for c in out["cases"].values())
    return (EXIT_OK if out["ok"] else EXIT_FAIL), out


# ---------------------------------------------------------------------------
# Self-test (hermetic: tempdir, ephemeral key, no network)
# ---------------------------------------------------------------------------
RFC8032_T1 = {
    "secret": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
    "public": "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    "sig": "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
}


def build_pcap(frames: List[bytes], linktype: int = LT_ETHERNET, step_s: int = 1) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    for i, fr in enumerate(frames):
        out += struct.pack("<IIII", 1700000000 + i * step_s, 0, len(fr), len(fr)) + fr
    return out


def eth(ethertype: int, payload: bytes) -> bytes:
    return b"\x33\x33\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x01" + struct.pack(">H", ethertype) + payload


def ipv6_icmp(icmp_type: int, hop_by_hop: bool = False) -> bytes:
    icmp = bytes([icmp_type, 0, 0, 0]) + b"\x00" * 20
    src = b"\xfe\x80" + b"\x00" * 13 + b"\x01"
    dst = b"\xff\x02" + b"\x00" * 13 + b"\x16"
    if hop_by_hop:
        hbh = bytes([58, 0, 5, 2, 0, 0, 1, 0])  # router alert + padn
        return struct.pack(">IHBB", 0x60000000, len(hbh) + len(icmp), 0, 1) + src + dst + hbh + icmp
    return struct.pack(">IHBB", 0x60000000, len(icmp), 58, 255) + src + dst + icmp


def ipv6_frag_icmp(first_byte: int, offset_units: int = 1, dst: Optional[bytes] = None) -> bytes:
    """IPv6 with a fragment header (next=58); offset_units>0 makes it a non-first fragment."""
    src = b"\x20\x01\x0d\xb8" + b"\x00" * 11 + b"\x02"
    dst = dst or (b"\x20\x01\x0d\xb8" + b"\x00" * 11 + b"\x99")
    body = bytes([first_byte]) + b"SECRET-EXFIL-PAYLOAD" * 3
    frag = struct.pack(">BBHI", 58, 0, offset_units << 3, 0x1234)
    return struct.pack(">IHBB", 0x60000000, len(frag) + len(body), 44, 64) + src + dst + frag + body


def ipv4_udp(dport: int, src: bytes = b"\x0a\x00\x00\x02", dst: bytes = b"\x0a\x00\x00\x01") -> bytes:
    udp = struct.pack(">HHHH", 40000, dport, 8 + 12, 0) + b"\x00" * 12
    return struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0) + src + dst + udp


def ipv4_tcp(dport: int, flags: int = 0x02, src: bytes = b"\x0a\x00\x00\x02", dst: bytes = b"\x0a\x00\x00\x01") -> bytes:
    tcp = struct.pack(">HHIIBBHHH", 40000, dport, 0, 0, 0x50, flags, 1024, 0, 0)
    return struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0) + src + dst + tcp


def arp() -> bytes:
    return b"\x00\x01\x08\x00\x06\x04\x00\x01" + b"\x00" * 20


def self_test(verbose: bool = True) -> int:
    fails: List[str] = []

    def ok(cond: bool, what: str) -> None:
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            fails.append(what)

    seed = bytes.fromhex(RFC8032_T1["secret"])
    ok(purepy_public(seed).hex() == RFC8032_T1["public"], "pure-python Ed25519 RFC 8032 test 1 public key")
    sig = purepy_sign(seed, b"")
    ok(sig.hex() == RFC8032_T1["sig"], "pure-python Ed25519 RFC 8032 test 1 signature")
    ok(purepy_verify(bytes.fromhex(RFC8032_T1["public"]), b"", sig), "pure-python verify accepts the vector")
    bad = bytearray(sig)
    bad[0] ^= 0x01
    ok(not purepy_verify(bytes.fromhex(RFC8032_T1["public"]), b"", bytes(bad)), "pure-python verify rejects a one-bit flip")

    with tempfile.TemporaryDirectory(prefix="awnix-proof-selftest-") as td:
        base = Path(td)
        key = base / "node.key"
        pub = generate_key(key)
        nseed = load_seed(key)
        exp = base / "export"
        exp.mkdir()
        chain = exp / "proof-audit.jsonl"
        for i in range(6):
            append_record(chain, f"proof.step{i}", verdict="PASS", n=i)
        (exp / "step1.json").write_text(json.dumps({"schema": 1, "step": "step1", "verdict": "PASS"}), encoding="utf-8")
        (exp / "egress-tap.pcap").write_bytes(build_pcap([eth(0x0806, arp()), eth(0x86DD, ipv6_icmp(135))]))
        seal_dir(exp, nseed, subject="awnix-proof-selftest")
        rc, rep = verify_export(exp, pub)
        ok(rc == 0 and rep["ok"], f"clean export verifies (rc={rc})")
        rc, rep = verify_export(exp, "00" * 32)
        ok(rc == 1 and rep["seal"]["key_trusted"] is False, "export under a different node key is refused")
        rc, demo = tamper_demo(exp, node_key=pub)
        ok(rc == 0, f"tamper-demo: flip, delete and truncate each detected (rc={rc})")
        tar = base / "export.tar"
        with tarfile.open(tar, "w") as tf:
            tf.add(exp, arcname="proof-x")
        rc, rep = verify_export(tar, pub)
        ok(rc == 0, f"tar export verifies (rc={rc})")

        cap = base / "a.pcap"
        cap.write_bytes(build_pcap([eth(0x0806, arp()), eth(0x86DD, ipv6_icmp(143, True)), eth(0x86DD, ipv6_icmp(133))]))
        r = analyze_capture(cap)
        ok(r["disallowed"] == 0 and r["frames"] == 3, "ARP + MLDv2 (hop-by-hop) + RS are allowed")
        cap.write_bytes(build_pcap([eth(0x0806, arp()), eth(0x0800, ipv4_udp(53))]))
        r = analyze_capture(cap)
        ok(r["dns"] == 1 and r["disallowed"] == 1, "one DNS query is counted and disallowed")
        cap.write_bytes(build_pcap([eth(0x0800, ipv4_tcp(443))])[:-5])
        try:
            analyze_capture(cap)
            ok(False, "a truncated pcap raises CaptureError")
        except CaptureError:
            ok(True, "a truncated pcap raises CaptureError")
        lo4 = b"\x7f\x00\x00\x01"
        cap.write_bytes(build_pcap([eth(0x0800, ipv4_udp(4444, src=lo4, dst=lo4))] * 3))
        r = analyze_capture(cap, role="tap")
        ok(r["disallowed"] == 3 and r["loopback_skipped"] == 0, "127/8 on an Ethernet capture is a martian: counted")
        cap.write_bytes(build_pcap([eth(0x86DD, ipv6_frag_icmp(135))]))
        r = analyze_capture(cap)
        ok(r["disallowed"] == 1, "a non-first IPv6 fragment starting with 135 is counted, not read as ND")
        cap.write_bytes(build_pcap([]))
        r = analyze_capture(cap)
        ok(r["frames"] == 0 and r["ok"] is None, "an empty capture is not clean (ok=None)")

        dirty = base / "dirty"
        shutil.copytree(exp, dirty)
        (dirty / SEAL_NAME).unlink()
        (dirty / "egress-tap.pcap").write_bytes(build_pcap([eth(0x0800, ipv4_udp(53)), eth(0x0800, ipv4_tcp(443))]))
        seal_dir(dirty, nseed)
        rc, rep = verify_export(dirty, pub)
        ok(rc == 1 and rep["egress"]["egress-tap.pcap"]["disallowed"] == 2, f"sealed DNS + SYN tap capture fails verify (rc={rc})")
        failed = base / "failed"
        shutil.copytree(exp, failed)
        (failed / SEAL_NAME).unlink()
        (failed / "step1.json").write_text(json.dumps({"schema": 1, "step": "step1", "verdict": "FAIL"}), encoding="utf-8")
        seal_dir(failed, nseed)
        rc, _ = verify_export(failed, pub)
        ok(rc == 1, f"a FAIL step verdict fails verify by default (rc={rc})")
        inj = failed / "zz" / "__pycache__"
        inj.mkdir(parents=True)
        (inj / "step1.json").write_text(json.dumps({"schema": 1, "step": "step1", "verdict": "PASS"}), encoding="utf-8")
        rc, rep = verify_export(failed, pub)
        ok(rc == 1 and rep["unsealed"] == ["zz/__pycache__/step1.json"] and rep["steps"].get("step1") == "FAIL",
           f"an unsealed step1.json cannot flip a verdict (rc={rc})")
    if verbose:
        print(f"self-test: {'PASS' if not fails else 'FAIL'} ({len(fails)} failure(s)); ed25519 backend={ed25519_backend()}")
    return EXIT_OK if not fails else EXIT_FAIL


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _emit(obj: Dict[str, Any], as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        print(summary)


def _verify_summary(rc: int, rep: Dict[str, Any]) -> str:
    s = rep["seal"]
    lines = [
        f"seal: present={s['present']} signature_ok={s['signature_ok']} content_ok={s['content_ok']} "
        f"key_trusted={s['key_trusted']} backend={s['backend']}"
    ]
    if any(s["diff"].values()):
        lines.append(f"  diff: {json.dumps(s['diff'])}")
    for c in rep["chains"]:
        lines.append(
            f"chain {c['file']}: chain_ok={c['chain_ok']} count={c['count']} anchored={c['anchored']} "
            f"first_broken_index={c['first_broken_index']} kind={c['kind']}"
        )
    for u in rep.get("unsealed", []):
        lines.append(f"UNSEALED {u}")
    for st, v in rep["steps"].items():
        fx = " (fixture mode)" if st in rep.get("step_fixture_mode", []) else ""
        lines.append(f"step {st}: {v}{fx}")
    for f, e in rep["egress"].items():
        if "error" in e:
            lines.append(f"egress {f}: COULD-NOT-JUDGE {e['error']}")
        else:
            lines.append(f"egress {f} [{e.get('role')}]: frames={e['frames']} disallowed={e['disallowed']} "
                         f"dns={e['dns']} tcp_syn={e['tcp_syn']} span_s={e.get('span_s')}")
    if rep.get("fixture_mode"):
        lines.append("FIXTURE MODE: this export was produced from canned command output, not a real run")
    for n in rep.get("notes", []):
        lines.append(f"note: {n}")
    lines.append(f"VERIFY: {'OK' if rc == 0 else 'FAIL' if rc == 1 else 'COULD-NOT-JUDGE'} (exit {rc})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix_proof_verify.py", description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    v = sub.add_parser("verify", help="verify an exported proof dir or tar")
    v.add_argument("path")
    v.add_argument("--node-key", help="64-hex Ed25519 public key recorded at Step 0")
    v.add_argument("--require-steps-pass", action="store_true",
                   help="(default; kept for compatibility) every step verdict must be PASS")
    v.add_argument("--integrity-only", action="store_true",
                   help="judge only seal, key and chains; ignore step verdicts and egress captures")
    v.add_argument("--allow-fixtures", action="store_true",
                   help="accept fixture-mode (rehearsal) evidence; it is still reported")
    v.add_argument("--json", action="store_true")
    t = sub.add_parser("tamper-demo", help="flip one byte, delete one record, cut the tail; prove detection")
    t.add_argument("path")
    t.add_argument("--flip-index", type=int)
    t.add_argument("--delete-index", type=int)
    t.add_argument("--chain")
    t.add_argument("--node-key")
    t.add_argument("--json", action="store_true")
    c = sub.add_parser("verify-chain")
    c.add_argument("path")
    c.add_argument("--json", action="store_true")
    p = sub.add_parser("analyze-pcap")
    p.add_argument("path")
    p.add_argument("--role", choices=("node", "tap"), default="node")
    p.add_argument("--json", action="store_true")
    k = sub.add_parser("pubkey", help="print the public key hex of a PKCS#8 Ed25519 key")
    k.add_argument("path")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    try:
        if args.cmd == "verify":
            rc, rep = verify_export(Path(args.path), args.node_key,
                                    require_steps_pass=not args.integrity_only,
                                    judge_egress=not args.integrity_only,
                                    allow_fixtures=args.allow_fixtures)
            _emit(rep, args.json, _verify_summary(rc, rep))
            return rc
        if args.cmd == "tamper-demo":
            rc, out = tamper_demo(Path(args.path), args.flip_index, args.delete_index, args.chain, args.node_key)
            summary = [f"chain {out.get('chain')} ({out.get('count')} records)"]
            for name, case in out.get("cases", {}).items():
                ch = case["chain"]
                summary.append(
                    f"{name}: chain_ok={ch['chain_ok']} first_broken_index={ch['first_broken_index']} "
                    f"kind={ch['kind']} expect[{case['expect']}] -> {'PASS' if case['pass'] else 'FAIL'}"
                )
            if out.get("error"):
                summary.append(f"error: {out['error']}")
            summary.append(f"TAMPER-DEMO: {'PASS' if rc == 0 else 'FAIL' if rc == 1 else 'COULD-NOT-JUDGE'} (exit {rc})")
            _emit(out, args.json, "\n".join(summary))
            return rc
        if args.cmd == "verify-chain":
            r = verify_chain(Path(args.path))
            _emit(r, args.json, f"chain_ok={r['chain_ok']} count={r['count']} first_broken_index={r['first_broken_index']} kind={r['kind']}")
            if not Path(args.path).is_file():
                return EXIT_CNJ
            return EXIT_OK if r["chain_ok"] else EXIT_FAIL
        if args.cmd == "analyze-pcap":
            r = analyze_capture(Path(args.path), role=args.role)
            _emit(r, args.json, f"frames={r['frames']} disallowed={r['disallowed']} dns={r['dns']} tcp_syn={r['tcp_syn']} "
                                f"udp={r['udp']} span_s={r['span_s']}")
            return EXIT_OK if r["ok"] is True else EXIT_FAIL if r["ok"] is False else EXIT_CNJ
        if args.cmd == "pubkey":
            print(ed25519_public(load_seed(Path(args.path))).hex())
            return EXIT_OK
    except ProofError as exc:
        print(f"COULD-NOT-JUDGE: {exc}", file=sys.stderr)
        return EXIT_CNJ
    ap.print_help()
    return EXIT_CNJ


if __name__ == "__main__":
    sys.exit(main())
