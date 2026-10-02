#!/usr/bin/python3.11
"""aitheros -- activate an appliance with its AITHER1 license (installed as /usr/bin/aitheros).

Verbs (the whole public surface; aitheros(1) documents exactly these four):

  aitheros login   --license <env|@file|->|<envelope>>   verify, install, exchange, arm
  aitheros up      --license ...                          login, then bring the stack up
  aitheros status  [--json]                               the activation state
  aitheros license activate --license ... | refresh [--quiet] [--json]
                   | import <path|-> | doctor | entitlements --json
  aitheros --self-test | --list-verbs

What it does, in one paragraph. The license file /etc/aither/appliance.lic is ONE line
``AITHER1.<b64url(payload)>.<b64url(ed25519 sig)>`` (lib/licensing/license_minter.py).
It is verified OFFLINE against the baked vendor public key
/usr/share/aither/license-pubkey.pem -- with the ``cryptography`` package when it is
importable, else ``openssl pkeyutl -verify -pubin -rawin`` (OpenSSL 3). A valid license is
exchanged at AITHER_LICENSE_EXCHANGE_URL for a 1 h ghcr pull token, written atomically
(0600) to /etc/ostree/auth.json, /run/ostree/auth.json and /etc/containers/auth.json. The
result is recorded in /var/lib/aither/license/status.json, which every other surface reads
and never re-derives. The status file never contains the envelope or the token.

This program VERIFIES only. It holds no signing key and has no signing code (LIP002,
check_licence_issuer_not_published.py): the self-test uses a baked, pre-signed throwaway
vector whose private half was discarded when it was made.

Exit codes: 0 ok / armed, 1 invalid / refused / revoked, 2 offline / could not judge.
stdlib only, Python 3.10-compatible.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PREFIX = "AITHER1"
# The vendor floor, keyed by its endpoints.env name: /usr/lib/awnix/endpoints.env ships
# the same value and /etc/awnix/endpoints.env (or the process env) overrides it.
ENDPOINT_FLOOR = {
    "AITHER_LICENSE_EXCHANGE_URL": "https://api.aitherium.com/v1/licenses/exchange-pull-token",
}
DEFAULT_EXCHANGE_URL = ENDPOINT_FLOOR["AITHER_LICENSE_EXCHANGE_URL"]
REGISTRY = "ghcr.io"
STATES = ("unlicensed", "valid", "expired", "invalid", "revoked", "refused", "offline")
REGISTRY_STATES = ("armed", "refused", "offline", "unconfigured", "legacy-token")
MAX_ENVELOPE_BYTES = 16 * 1024
SKEW_WINDOW_S = 24 * 3600
VERBS = ("login", "up", "status", "license")
LICENSE_VERBS = ("activate", "refresh", "import", "doctor", "entitlements")


# ── paths (every absolute path hangs off AITHEROS_ROOT so tests run in a temp tree) ──


class Paths:
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = root if root is not None else os.environ.get("AITHEROS_ROOT", "/")

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel.lstrip("/"))

    @property
    def license(self) -> str:
        return self.p("/etc/aither/appliance.lic")

    @property
    def pubkey(self) -> str:
        return self.p("/usr/share/aither/license-pubkey.pem")

    @property
    def spool(self) -> str:
        return self.p("/var/lib/aither/license/incoming.lic")

    @property
    def status(self) -> str:
        return self.p("/var/lib/aither/license/status.json")

    @property
    def auth_files(self) -> List[str]:
        return [
            self.p("/etc/ostree/auth.json"),
            self.p("/run/ostree/auth.json"),
            self.p("/etc/containers/auth.json"),
        ]

    @property
    def endpoint_envs(self) -> List[Tuple[str, str]]:
        # highest precedence first (process env is checked before these)
        return [
            ("admin-env", self.p("/etc/awnix/endpoints.env")),
            ("vendor-env", self.p("/usr/lib/awnix/endpoints.env")),
        ]


# ── small helpers ────────────────────────────────────────────────────────────


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    t = _now() if ts is None else ts
    return _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aitheros-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # best-effort temp cleanup; the original error re-raises
            os.unlink(tmp)
        raise


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE, no expansion; blank lines and # comments ignored; one layer of quotes."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def exchange_url(paths: Paths) -> Tuple[str, str]:
    """(url, source): process env > /etc/awnix/endpoints.env > /usr/lib/awnix/endpoints.env
    > the default."""
    v = os.environ.get("AITHER_LICENSE_EXCHANGE_URL", "").strip()
    if v:
        return v, "process-env"
    for source, path in paths.endpoint_envs:
        v = read_env_file(path).get("AITHER_LICENSE_EXCHANGE_URL", "").strip()
        if v:
            return v, source
    return DEFAULT_EXCHANGE_URL, "default"


# ── envelope + verification (verify ONLY) ────────────────────────────────────


class Envelope:
    def __init__(self, text: str) -> None:
        self.text = text.strip()
        parts = self.text.split(".")
        if len(parts) != 3:
            raise ValueError("not an AITHER1 envelope (expected 3 dot-separated parts)")
        if parts[0] != PREFIX:
            raise ValueError(f"wrong envelope prefix {parts[0][:16]!r} (expected {PREFIX})")
        try:
            self.payload_bytes = _b64url_decode(parts[1])
            self.sig = _b64url_decode(parts[2])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"envelope is not base64url: {exc}") from exc
        if len(self.sig) != 64:
            raise ValueError("signature is not 64 bytes (Ed25519)")
        try:
            payload = json.loads(self.payload_bytes)
        except ValueError as exc:
            raise ValueError("payload is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        self.payload: Dict[str, Any] = payload


def _have_cryptography() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - absent or broken wheel: the openssl branch serves
        return False


def _have_openssl() -> bool:
    return shutil.which("openssl") is not None


def _verify_cryptography(pubkey_pem: bytes, data: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(pubkey_pem)
    if not isinstance(pub, Ed25519PublicKey):
        return False
    try:
        pub.verify(sig, data)
        return True
    except InvalidSignature:
        return False


def _verify_openssl(pubkey_pem: bytes, data: bytes, sig: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="aitheros-verify-") as d:
        files = {"pub.pem": pubkey_pem, "data.bin": data, "sig.bin": sig}
        for name, blob in files.items():
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(blob)
        proc = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                os.path.join(d, "pub.pem"),
                "-rawin",
                "-in",
                os.path.join(d, "data.bin"),
                "-sigfile",
                os.path.join(d, "sig.bin"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return proc.returncode == 0 and "Verified Successfully" in (proc.stdout or "")


def verify_signature(
    pubkey_pem: bytes, data: bytes, sig: bytes, backend: Optional[str] = None
) -> Tuple[Optional[bool], str]:
    """(verdict, backend). verdict None = no backend could judge."""
    if backend in (None, "cryptography") and _have_cryptography():
        return _verify_cryptography(pubkey_pem, data, sig), "cryptography"
    if backend in (None, "openssl") and _have_openssl():
        try:
            return _verify_openssl(pubkey_pem, data, sig), "openssl"
        except (OSError, subprocess.SubprocessError):
            return None, "openssl"
    return None, backend or "none"


def ntp_synced() -> Optional[bool]:
    """timedatectl NTPSynchronized; None when unknown. AITHEROS_NTP_SYNCED is the test seam."""
    seam = os.environ.get("AITHEROS_NTP_SYNCED", "").strip().lower()
    if seam in ("yes", "no"):
        return seam == "yes"
    if not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def classify(
    text: str,
    pubkey_pem: bytes,
    now: Optional[float] = None,
    backend: Optional[str] = None,
    synced: Optional[bool] = None,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """(state, detail, payload) for an envelope: valid | expired | invalid | offline."""
    if len(text.encode("utf-8", "replace")) > MAX_ENVELOPE_BYTES:
        return "invalid", "license is larger than 16 KiB", None
    try:
        env = Envelope(text)
    except ValueError as exc:
        return "invalid", str(exc), None
    verdict, used = verify_signature(pubkey_pem, env.payload_bytes, env.sig, backend)
    if verdict is None:
        return (
            "offline",
            f"could not verify: no signature backend ({used}) -- install openssl 3",
            None,
        )
    if not verdict:
        return "invalid", f"signature does not verify against the vendor key ({used})", None
    t = _now() if now is None else now
    try:
        exp = int(env.payload.get("exp") or 0)
    except (TypeError, ValueError):
        return "invalid", "payload exp is not an integer", None
    if exp and t > exp:
        if t - exp < SKEW_WINDOW_S:
            s = ntp_synced() if synced is None else synced
            if s is False:
                return (
                    "offline",
                    "license expiry is within 24 h of this clock and the clock is "
                    "not NTP-synchronised; not judging it expired",
                    env.payload,
                )
        return "expired", f"license expired {_iso(exp)}", env.payload
    return "valid", f"verified ({used})", env.payload


# ── registry auth files (the ONLY writer on a box) ───────────────────────────


def _auth_doc(path: str) -> Dict[str, Any]:
    doc = _read_json(path) or {}
    if not isinstance(doc.get("auths"), dict):
        doc["auths"] = {}
    return doc


def write_registry_auth(paths: Paths, token: str) -> None:
    entry = base64.b64encode(("x-access-token:" + token).encode("utf-8")).decode("ascii")
    for path in paths.auth_files:
        doc = _auth_doc(path)
        doc["auths"][REGISTRY] = {"auth": entry}
        _atomic_write(path, (json.dumps(doc, indent=1) + "\n").encode("utf-8"), 0o600)


def remove_registry_auth(paths: Paths) -> int:
    removed = 0
    for path in paths.auth_files:
        doc = _read_json(path)
        if not doc or not isinstance(doc.get("auths"), dict) or REGISTRY not in doc["auths"]:
            continue
        del doc["auths"][REGISTRY]
        _atomic_write(path, (json.dumps(doc, indent=1) + "\n").encode("utf-8"), 0o600)
        removed += 1
    return removed


def has_registry_auth(paths: Paths) -> bool:
    for path in paths.auth_files:
        doc = _read_json(path) or {}
        if REGISTRY in (doc.get("auths") or {}):
            return True
    return False


# ── the exchange ──────────────────────────────────────────────────────────────


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # surface every 3xx as an HTTPError to exchange()


def exchange(url: str, envelope: str, timeout: float = 20.0) -> Tuple[str, Any]:
    """POST {license}. Returns ("ok", body) | ("refused", detail) | ("offline", detail).

    TLS verification is never disabled; plain http is accepted ONLY for a loopback host
    (the self-test stub and an operator's local relay)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and _is_loopback(parsed.hostname or "")
    ):
        return "offline", f"refusing a non-https exchange URL ({parsed.scheme}://{parsed.hostname})"
    req = urllib.request.Request(
        url,
        data=json.dumps({"license": envelope}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "aitheros-cli/1",
        },
    )
    try:
        # Never follow a redirect: the exchange is a POST API, and an edge that answers
        # 3xx (e.g. a web front sending the path to /login) is MISROUTED, not a server.
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            body = json.loads(resp.read(1 << 20) or b"{}")
        if not isinstance(body, dict) or not body.get("token"):
            return "offline", "exchange answered 200 without a token"
        return "ok", body
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            loc = (exc.headers.get("Location") if exc.headers else "") or "?"
            return "offline", (
                f"exchange endpoint misrouted: HTTP {exc.code} -> {loc[:200]} "
                f"({url} is not answered by the licence exchange; set "
                "AITHER_LICENSE_EXCHANGE_URL in /etc/awnix/endpoints.env)"
            )
        try:
            raw = json.loads(exc.read(1 << 16) or b"{}")
        except ValueError:
            raw = {}
        detail = raw.get("detail") if isinstance(raw, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("error") or json.dumps(detail)[:300]
        detail = str(detail or exc.reason or "")[:500]
        if exc.code == 403:
            return "refused", detail or "license refused"
        return "offline", f"exchange HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return "offline", f"exchange unreachable: {reason}"


# ── status ────────────────────────────────────────────────────────────────────


def _entitlements(
    payload: Optional[Dict[str, Any]], images: Optional[List[str]] = None
) -> Dict[str, Any]:
    ent = (payload or {}).get("entitlements") or {}
    imgs = images if images is not None else list(ent.get("images") or [])
    return {
        "appliance_tier": ent.get("appliance_tier"),
        "images": list(imgs),
        "packs": list((payload or {}).get("packs") or []),
    }


def build_status(
    state: str,
    detail: str,
    payload: Optional[Dict[str, Any]],
    registry_state: str,
    expires_at: Optional[str] = None,
    images: Optional[List[str]] = None,
) -> Dict[str, Any]:
    assert state in STATES, state
    assert registry_state in REGISTRY_STATES, registry_state
    p = payload or {}
    ent = _entitlements(payload, images)
    return {
        "schema": 1,
        "state": state,
        "lic_id": p.get("lic_id"),
        "sku": p.get("sku"),
        "tier": p.get("tier"),
        "exp": p.get("exp"),
        "checked_at": _iso(),
        "detail": detail,
        "entitlements": ent,
        "registry": {
            "state": registry_state,
            "expires_at": expires_at,
            "images": len(ent["images"]),
        },
    }


def write_status(paths: Paths, status: Dict[str, Any]) -> None:
    _atomic_write(
        paths.status, (json.dumps(status, indent=1, sort_keys=True) + "\n").encode("utf-8"), 0o644
    )


def exit_for(status: Dict[str, Any]) -> int:
    st, reg = status.get("state"), (status.get("registry") or {}).get("state")
    if st in ("invalid", "expired", "revoked", "refused"):
        return 1
    if st == "offline" or reg == "offline":
        return 2
    if st == "unlicensed":
        return 0 if reg == "legacy-token" else 1
    return 0


def _read_pubkey(paths: Paths) -> Optional[bytes]:
    try:
        with open(paths.pubkey, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(MAX_ENVELOPE_BYTES + 1)
    except OSError:
        return None


def install_license(paths: Paths, text: str) -> None:
    _atomic_write(paths.license, (text.strip() + "\n").encode("utf-8"), 0o600)


def local_status(paths: Paths) -> Dict[str, Any]:
    """The state with no network: used by `status` when status.json is absent."""
    text = _read_text(paths.license)
    if text is None:
        if has_registry_auth(paths):
            return build_status(
                "unlicensed",
                "no /etc/aither/appliance.lic; a legacy registry token is in place",
                None,
                "legacy-token",
            )
        return build_status("unlicensed", "no license installed", None, "unconfigured")
    pub = _read_pubkey(paths)
    if pub is None:
        return build_status("offline", "vendor public key missing", None, "unconfigured")
    state, detail, payload = classify(text, pub)
    reg = "armed" if (state == "valid" and has_registry_auth(paths)) else "unconfigured"
    return build_status(state, detail + " (local check; not refreshed)", payload, reg)


def refresh(paths: Paths, quiet: bool = False) -> Dict[str, Any]:
    """Import the spool, verify, exchange, arm or disarm, write status.json. Returns status."""
    pub = _read_pubkey(paths)
    import_note = ""
    spooled = _read_text(paths.spool)
    if spooled is not None:
        if pub is None:
            st = build_status(
                "offline", "vendor public key missing; spool left in place", None, "unconfigured"
            )
            write_status(paths, st)
            return st
        s_state, s_detail, _ = classify(spooled, pub)
        if s_state in ("valid", "offline"):
            install_license(paths, spooled)
            import_note = "imported the spooled license; "
        else:
            import_note = f"spooled license rejected ({s_state}: {s_detail}); "
        try:
            os.unlink(paths.spool)
        except OSError as exc:
            import_note += f"could not remove the spool ({exc.strerror}); "
        if s_state not in ("valid", "offline") and _read_text(paths.license) is None:
            st = build_status(
                s_state if s_state in STATES else "invalid",
                import_note.rstrip("; "),
                None,
                "unconfigured",
            )
            write_status(paths, st)
            return st

    text = _read_text(paths.license)
    if text is None:
        if has_registry_auth(paths):
            st = build_status(
                "unlicensed",
                import_note + "no license; legacy registry token left untouched",
                None,
                "legacy-token",
            )
        else:
            st = build_status(
                "unlicensed", import_note + "no license installed", None, "unconfigured"
            )
        write_status(paths, st)
        return st
    if pub is None:
        st = build_status("offline", "vendor public key missing", None, "unconfigured")
        write_status(paths, st)
        return st

    state, detail, payload = classify(text, pub)
    prev = _read_json(paths.status) or {}
    prev_reg = prev.get("registry") or {}
    if state in ("invalid", "expired"):
        remove_registry_auth(paths)
        st = build_status(
            state,
            import_note + detail,
            payload,
            "refused" if state == "expired" else "unconfigured",
        )
        write_status(paths, st)
        return st
    if state == "offline":
        st = build_status(
            "offline",
            import_note + detail,
            payload,
            "armed" if has_registry_auth(paths) else "unconfigured",
            prev_reg.get("expires_at"),
        )
        write_status(paths, st)
        return st

    url, _src = exchange_url(paths)
    kind, body = exchange(url, text.strip())
    if kind == "ok":
        write_registry_auth(paths, str(body["token"]))
        images = list(body.get("images") or _entitlements(payload)["images"])
        st = build_status(
            "valid",
            import_note + "registry armed",
            payload,
            "armed",
            body.get("expires_at"),
            images,
        )
    elif kind == "refused":
        remove_registry_auth(paths)
        new_state = "revoked" if "revoked" in str(body).lower() else "refused"
        st = build_status(new_state, import_note + str(body), payload, "refused")
    else:
        exp_at = prev_reg.get("expires_at")
        exp_ts = _parse_iso(exp_at)
        if exp_ts is not None and _now() >= exp_ts:
            remove_registry_auth(paths)
            note = "; the previous pull token has expired and was removed"
        else:
            note = (
                "; keeping the existing pull token until it expires"
                if has_registry_auth(paths)
                else ""
            )
        st = build_status("valid", import_note + str(body) + note, payload, "offline", exp_at)
    write_status(paths, st)
    return st


# ── license sources ───────────────────────────────────────────────────────────


def read_license_arg(value: Optional[str]) -> str:
    """<env|@file|->|envelope>. 'env' or absent -> $AITHER_LICENSE."""
    if value is None or value == "env":
        v = os.environ.get("AITHER_LICENSE", "")
        if not v:
            raise ValueError(
                "no license given: pass --license @file, --license - or set AITHER_LICENSE"
            )
        return v.strip()
    if value == "-":
        return sys.stdin.read(MAX_ENVELOPE_BYTES + 1).strip()
    if value.startswith("@"):
        text = _read_text(value[1:])
        if text is None:
            raise ValueError(f"cannot read {value[1:]}")
        return text.strip()
    return value.strip()


# ── verbs ─────────────────────────────────────────────────────────────────────


def _emit(status: Dict[str, Any], as_json: bool, quiet: bool = False) -> None:
    if as_json:
        print(json.dumps(status, indent=1, sort_keys=True))
        return
    if quiet:
        return
    reg = status.get("registry") or {}
    print(f"license:  {status.get('state')}  {status.get('lic_id') or ''}".rstrip())
    if status.get("sku"):
        exp = status.get("exp")
        print(
            f"sku:      {status.get('sku')}  tier={status.get('tier')}  "
            f"exp={'perpetual' if not exp else _iso(float(exp))}"
        )
    print(
        f"registry: {reg.get('state')}"
        + (f"  until {reg.get('expires_at')}" if reg.get("expires_at") else "")
        + (f"  images={reg.get('images')}" if reg.get("images") else "")
    )
    if status.get("detail"):
        print(f"detail:   {status.get('detail')}")


def cmd_login(paths: Paths, license_arg: Optional[str], as_json: bool = False) -> int:
    try:
        text = read_license_arg(license_arg)
    except ValueError as exc:
        print(f"aitheros: {exc}", file=sys.stderr)
        return 2
    return _login_text(paths, text, as_json)


def cmd_up(paths: Paths, license_arg: Optional[str], as_json: bool = False) -> int:
    rc = cmd_login(paths, license_arg, as_json)
    if rc == 1:
        return 1
    if os.path.exists(paths.p("/run/ostree-booted")) and shutil.which("bootc"):
        return subprocess.run(["bootc", "upgrade", "--check"], check=False).returncode or rc
    bundle = os.environ.get("AITHEROS_BUNDLE") or "docker-compose.sovereign.yml"
    if not os.path.exists(bundle):
        print(
            f"aitheros up: no bootc host and no compose bundle at {bundle} (set AITHEROS_BUNDLE)",
            file=sys.stderr,
        )
        return 2
    engine = shutil.which("podman") or shutil.which("docker")
    if not engine:
        print("aitheros up: neither podman nor docker is installed", file=sys.stderr)
        return 2
    for step in (["compose", "-f", bundle, "pull"], ["compose", "-f", bundle, "up", "-d"]):
        r = subprocess.run([engine] + step, check=False).returncode
        if r:
            return 1
    return rc


def cmd_status(paths: Paths, as_json: bool) -> int:
    st = _read_json(paths.status) or local_status(paths)
    _emit(st, as_json)
    return exit_for(st)


def cmd_import(paths: Paths, source: str, as_json: bool = False) -> int:
    try:
        text = read_license_arg(source if source == "-" else "@" + source)
    except ValueError as exc:
        print(f"aitheros: {exc}", file=sys.stderr)
        return 2
    return _login_text(paths, text, as_json)


def _login_text(paths: Paths, text: str, as_json: bool) -> int:
    pub = _read_pubkey(paths)
    if pub is None:
        print(f"aitheros: vendor public key missing at {paths.pubkey}", file=sys.stderr)
        return 2
    state, detail, _payload = classify(text, pub)
    if state not in ("valid", "offline"):
        st = build_status(state, detail, None, "unconfigured")
        _emit(st, as_json)
        return 1
    install_license(paths, text)
    st = refresh(paths)
    _emit(st, as_json)
    return exit_for(st)


def cmd_doctor(paths: Paths) -> int:
    bad = 0

    def line(ok: Optional[bool], what: str) -> None:
        nonlocal bad
        tag = {True: "ok  ", False: "FAIL", None: "info"}[ok]
        if ok is False:
            bad += 1
        print(f"[{tag}] {what}")

    pub = _read_pubkey(paths)
    line(pub is not None, f"vendor public key {paths.pubkey}")
    line(
        _have_cryptography() or _have_openssl(),
        f"signature backends: cryptography={_have_cryptography()} openssl={_have_openssl()}",
    )
    lic = _read_text(paths.license)
    if lic is None:
        line(None, f"no license at {paths.license}")
    else:
        try:
            mode = oct(os.stat(paths.license).st_mode & 0o777)
        except OSError:
            mode = "?"
        line(mode in ("0o600", "0o400") or os.name == "nt", f"license present, mode {mode}")
        if pub is not None:
            state, detail, _ = classify(lic, pub)
            line(state == "valid", f"license verifies offline: {state} ({detail})")
    url, src = exchange_url(paths)
    line(None, f"exchange URL {url} (from {src})")
    line(None, f"NTP synchronised: {ntp_synced()}")
    for path in paths.auth_files:
        doc = _read_json(path)
        armed = bool(doc and REGISTRY in (doc.get("auths") or {}))
        line(None, f"{path}: {'ghcr.io entry present' if armed else 'no ghcr.io entry'}")
    st = _read_json(paths.status)
    if st is None:
        line(None, f"no {paths.status} yet (run: aitheros license refresh)")
    else:
        line(
            st.get("state") in STATES,
            f"status: state={st.get('state')} "
            f"registry={(st.get('registry') or {}).get('state')} checked_at={st.get('checked_at')}",
        )
    return 1 if bad else 0


def cmd_entitlements(paths: Paths) -> int:
    st = _read_json(paths.status) or local_status(paths)
    print(json.dumps(st.get("entitlements") or _entitlements(None), indent=1, sort_keys=True))
    return 0 if st.get("state") == "valid" else exit_for(st)


# ── self-test (hermetic: temp root, loopback stub server, baked vector) ──────

# A THROWAWAY Ed25519 key pair made once for this test; its private half was discarded.
# It is NOT the vendor key, and nothing here can sign.
_TEST_PUBKEY = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAu/XT7bwTIFdU0Yz19ceYGhiLP5MpRF78w4QwUACVEEg=
-----END PUBLIC KEY-----
"""
_TV_VALID = (
    "AITHER1.eyJlbWFpbCI6InNlbGZ0ZXN0QGV4YW1wbGUuaW52YWxpZCIsImVudGl0bGVtZW50cyI6eyJhcHBsaWFu"
    "Y2VfdGllciI6ImdhcmciLCJpbWFnZXMiOlsiZ2hjci5pby9haXRoZXJpdW0vZ2FyZy1hcHBsaWFuY2UiXX0sImV4"
    "cCI6MCwiaWF0IjoxNzkwMDAwMDAwLCJsaWNfaWQiOiJsaWNfc2VsZnRlc3QwMDAwMDAwMDAxIiwicGFja3MiOlsi"
    "Z2FyZyJdLCJza3UiOiJzZWxmdGVzdC1nYXJnIiwidGllciI6InNvdmVyZWlnbiJ9.lyyakAgLtV6MB61s6XVqZKCA"
    "FKqTYudUFUcmHs3lgGJ6uZzCzvkr7C4x7as012QTDSu4Agn5X8mE9nxAYDVdDQ"
)
_TV_EXPIRED = (
    "AITHER1.eyJlbWFpbCI6InNlbGZ0ZXN0QGV4YW1wbGUuaW52YWxpZCIsImVudGl0bGVtZW50cyI6eyJhcHBsaWFu"
    "Y2VfdGllciI6ImdhcmciLCJpbWFnZXMiOlsiZ2hjci5pby9haXRoZXJpdW0vZ2FyZy1hcHBsaWFuY2UiXX0sImV4"
    "cCI6MTc5MDAwMzYwMCwiaWF0IjoxNzkwMDAwMDAwLCJsaWNfaWQiOiJsaWNfc2VsZnRlc3QwMDAwMDAwMDAyIiwi"
    "cGFja3MiOlsiZ2FyZyJdLCJza3UiOiJzZWxmdGVzdC1nYXJnIiwidGllciI6InNvdmVyZWlnbiJ9.j1StXHPLKGsR"
    "DL522yUqsrY2-IDTJleAGkqM1q3OUfMfWr9oxBvHAgiiVnsg7M4R6MAD4JUX4k5Wnv8UsOTWBg"
)
_TV_EXPIRED_EXP = 1790003600
_TV_TAMPERED = (
    "AITHER1.eyJlbWFpbCI6InNlbGZ0ZXN0QGV4YW1wbGUuaW52YWxpZCIsImVudGl0bGVtZW50cyI6eyJhcHBsaWFu"
    "Y2VfdGllciI6ImdhcmciLCJpbWFnZXMiOlsiZ2hjci5pby9haXRoZXJpdW0vZ2FyZy1hcHBsaWFuY2UiXX0sImV4"
    "cCI6MCwiaWF0IjoxNzkwMDAwMDAwLCJsaWNfaWQiOiJsaWNfc2VsZnRlc3QwMDAwMDAwMDAxIiwicGFja3MiOlsi"
    "Z2FyZyJdLCJza3UiOiJzZWxmdGVzdC1nYXJnIiwidGllciI6ImVudGVycHJpc2UifQ.lyyakAgLtV6MB61s6XVqZKCA"
    "FKqTYudUFUcmHs3lgGJ6uZzCzvkr7C4x7as012QTDSu4Agn5X8mE9nxAYDVdDQ"
)
_TV_WRONG_PREFIX = "AITHER2" + _TV_VALID[len("AITHER1") :]


def _stub_server(behaviour: Dict[str, Any]):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # silence
            pass

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            behaviour["seen"] = body
            code, out = behaviour["code"], behaviour["body"]
            data = json.dumps(out).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (behaviour.get("headers") or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


def self_test() -> int:
    failures: List[str] = []

    def check(cond: bool, what: str) -> None:
        print(("PASS " if cond else "FAIL ") + what)
        if not cond:
            failures.append(what)

    backends = [
        b for b, ok in (("cryptography", _have_cryptography()), ("openssl", _have_openssl())) if ok
    ]
    if not backends:
        print("SELF-TEST CANNOT RUN: neither the cryptography package nor openssl is available")
        return 2
    require_both = os.environ.get("AITHEROS_SELFTEST_REQUIRE_BOTH") == "1"
    for b in ("cryptography", "openssl"):
        if b not in backends:
            print(f"SKIP {b} branch: not available on this host")
            if require_both:
                failures.append(f"{b} branch required but unavailable")
    now = float(_TV_EXPIRED_EXP + 10 * 86400)
    for b in backends:
        check(
            classify(_TV_VALID, _TEST_PUBKEY, now, b)[0] == "valid", f"[{b}] valid vector verifies"
        )
        check(
            classify(_TV_EXPIRED, _TEST_PUBKEY, now, b, synced=True)[0] == "expired",
            f"[{b}] expired vector -> expired",
        )
        check(
            classify(_TV_TAMPERED, _TEST_PUBKEY, now, b)[0] == "invalid",
            f"[{b}] tampered -> invalid",
        )
        check(
            classify(_TV_WRONG_PREFIX, _TEST_PUBKEY, now, b)[0] == "invalid",
            f"[{b}] wrong prefix -> invalid",
        )
        near = float(_TV_EXPIRED_EXP + 3600)
        check(
            classify(_TV_EXPIRED, _TEST_PUBKEY, near, b, synced=False)[0] == "offline",
            f"[{b}] expiry within 24 h on an unsynced clock -> offline, not expired",
        )
        check(
            classify(_TV_EXPIRED, _TEST_PUBKEY, near, b, synced=True)[0] == "expired",
            f"[{b}] expiry within 24 h on a synced clock -> expired",
        )
        vendor = (
            b"-----BEGIN PUBLIC KEY-----\n"
            b"MCowBQYDK2VwAyEAGkaKMy1s/FN47fcIO22EW8/fFB+84o5tvlId1rhOIz8=\n"
            b"-----END PUBLIC KEY-----\n"
        )
        check(
            classify(_TV_VALID, vendor, now, b)[0] == "invalid",
            f"[{b}] the throwaway vector does NOT verify against the vendor key",
        )
    check(classify("garbage", _TEST_PUBKEY)[0] == "invalid", "garbage -> invalid")
    check(
        classify("A" * (MAX_ENVELOPE_BYTES + 1), _TEST_PUBKEY)[0] == "invalid",
        "oversize -> invalid",
    )

    behaviour: Dict[str, Any] = {"code": 200, "body": {}}
    srv = _stub_server(behaviour)
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/licenses/exchange-pull-token"
    saved = {k: os.environ.get(k) for k in ("AITHER_LICENSE_EXCHANGE_URL", "AITHEROS_NTP_SYNCED")}
    os.environ["AITHEROS_NTP_SYNCED"] = "yes"
    try:
        with tempfile.TemporaryDirectory(prefix="aitheros-selftest-") as root:
            paths = Paths(root)
            os.makedirs(os.path.dirname(paths.pubkey))
            with open(paths.pubkey, "wb") as fh:
                fh.write(_TEST_PUBKEY)
            # endpoints chain: vendor < admin < process
            os.makedirs(os.path.dirname(paths.endpoint_envs[1][1]))
            with open(paths.endpoint_envs[1][1], "w", encoding="utf-8") as fh:
                fh.write("AITHER_LICENSE_EXCHANGE_URL=https://vendor.invalid/x\n")
            os.environ.pop("AITHER_LICENSE_EXCHANGE_URL", None)
            check(
                exchange_url(paths) == ("https://vendor.invalid/x", "vendor-env"),
                "endpoint: vendor env",
            )
            os.makedirs(os.path.dirname(paths.endpoint_envs[0][1]))
            with open(paths.endpoint_envs[0][1], "w", encoding="utf-8") as fh:
                fh.write(f'# admin\nAITHER_LICENSE_EXCHANGE_URL="{url}"\n')
            check(exchange_url(paths) == (url, "admin-env"), "endpoint: admin env beats vendor")
            os.environ["AITHER_LICENSE_EXCHANGE_URL"] = url
            check(exchange_url(paths)[1] == "process-env", "endpoint: process env beats admin")

            # an edge that answers the POST with a redirect (a web front sending the path
            # to /login) is misrouted: offline with a named detail, never followed
            behaviour.update(code=307, body={}, headers={"Location": "/login?returnUrl=x"})
            kind, detail = exchange(url, _TV_VALID)
            check(
                kind == "offline" and "misrouted" in str(detail) and "/login" in str(detail),
                "exchange: a 307 to /login -> offline 'misrouted', not followed",
            )
            behaviour.update(code=200, body={}, headers={})

            # unlicensed, then legacy token
            st = refresh(paths)
            check(
                st["state"] == "unlicensed"
                and st["registry"]["state"] == "unconfigured"
                and exit_for(st) == 1,
                "no license -> unlicensed/unconfigured, exit 1",
            )
            legacy = paths.auth_files[0]
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            with open(legacy, "w", encoding="utf-8") as fh:
                json.dump({"auths": {REGISTRY: {"auth": "bGVnYWN5OnRva2Vu"}}}, fh)
            st = refresh(paths)
            check(
                st["registry"]["state"] == "legacy-token"
                and exit_for(st) == 0
                and (_read_json(legacy) or {})["auths"][REGISTRY]["auth"] == "bGVnYWN5OnRva2Vu",
                "legacy token only -> legacy-token, file untouched, exit 0",
            )

            # spool import + 200 -> armed
            behaviour.update(
                code=200,
                body={
                    "registry": "ghcr.io",
                    "username": "x-access-token",
                    "token": "ghs_selftestTOKEN",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "expires_in": 3600,
                    "images": ["ghcr.io/aitherium/garg-appliance"],
                    "appliance_tier": "garg",
                    "lic_id": "lic_selftest0000000001",
                },
            )
            os.makedirs(os.path.dirname(paths.spool), exist_ok=True)
            with open(paths.spool, "w", encoding="utf-8") as fh:
                fh.write(_TV_VALID + "\n")
            st = refresh(paths)
            check(
                st["state"] == "valid" and st["registry"]["state"] == "armed" and exit_for(st) == 0,
                "spooled valid license + 200 -> valid/armed, exit 0",
            )
            check(
                not os.path.exists(paths.spool) and os.path.exists(paths.license),
                "spool moved to appliance.lic",
            )
            check(behaviour.get("seen") == {"license": _TV_VALID}, "exchange body is {license}")
            ok_auth = all(
                base64.b64decode((_read_json(p) or {})["auths"][REGISTRY]["auth"]).decode()
                == "x-access-token:ghs_selftestTOKEN"
                for p in paths.auth_files
            )
            check(ok_auth, "all three auth files carry x-access-token:<token>")
            if os.name != "nt":
                check(
                    all(
                        (os.stat(p).st_mode & 0o777) == 0o600
                        for p in paths.auth_files + [paths.license]
                    ),
                    "auth files and license are 0600",
                )
            raw = open(paths.status, encoding="utf-8").read()
            check(
                "ghs_selftestTOKEN" not in raw and _TV_VALID not in raw,
                "status.json holds neither the token nor the envelope",
            )
            sj = json.loads(raw)
            check(
                sj["schema"] == 1
                and sj["entitlements"]["images"] == ["ghcr.io/aitherium/garg-appliance"]
                and sj["entitlements"]["packs"] == ["garg"]
                and sj["registry"]["images"] == 1,
                "status.json carries entitlements",
            )

            # 503 -> offline, keep entry while not expired
            behaviour.update(
                code=503,
                body={
                    "detail": {
                        "error": "registry_app_not_installed",
                        "detail": "app not configured",
                    }
                },
            )
            st = refresh(paths)
            check(
                st["state"] == "valid"
                and st["registry"]["state"] == "offline"
                and exit_for(st) == 2
                and has_registry_auth(paths),
                "503 -> registry offline, entry kept, exit 2",
            )
            # unreachable -> offline, and an expired token is removed
            srv.shutdown()
            srv.server_close()
            prev = _read_json(paths.status) or {}
            prev["registry"]["expires_at"] = "2000-01-01T00:00:00Z"
            write_status(paths, prev)
            st = refresh(paths)
            check(
                st["registry"]["state"] == "offline"
                and not has_registry_auth(paths)
                and "unreachable" in st["detail"],
                "unreachable + token past expires_at -> entry removed",
            )
            srv = _stub_server(behaviour)
            url = f"http://127.0.0.1:{srv.server_address[1]}/v1/licenses/exchange-pull-token"
            os.environ["AITHER_LICENSE_EXCHANGE_URL"] = url
            behaviour.update(
                code=200,
                body={"token": "ghs_again", "expires_at": "2099-01-01T00:00:00Z", "images": []},
            )
            st = refresh(paths)
            check(st["registry"]["state"] == "armed", "re-armed after the exchange comes back")
            # 403 -> refused/revoked, entry removed
            behaviour.update(code=403, body={"detail": "license revoked"})
            st = refresh(paths)
            check(
                st["state"] == "revoked"
                and st["registry"]["state"] == "refused"
                and not has_registry_auth(paths)
                and exit_for(st) == 1,
                "403 revoked -> revoked/refused, ghcr entry removed, exit 1",
            )
            behaviour.update(code=403, body={"detail": "license unknown to this platform"})
            st = refresh(paths)
            check(
                st["state"] == "refused"
                and st["detail"].endswith("license unknown to this platform"),
                "403 unknown -> refused with the detail verbatim",
            )
            # a tampered spool never replaces the installed license
            with open(paths.spool, "w", encoding="utf-8") as fh:
                fh.write(_TV_TAMPERED)
            behaviour.update(
                code=200, body={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
            st = refresh(paths)
            check(
                open(paths.license, encoding="utf-8").read().strip() == _TV_VALID
                and "rejected" in st["detail"],
                "tampered spool rejected, installed license kept",
            )
            # expired license disarms
            install_license(paths, _TV_EXPIRED)
            st = refresh(paths)
            check(
                st["state"] == "expired" and not has_registry_auth(paths) and exit_for(st) == 1,
                "expired license -> expired, disarmed, exit 1",
            )
            # http to a non-loopback host is refused
            check(
                exchange("http://example.invalid/x", _TV_VALID)[0] == "offline",
                "non-loopback http refused",
            )
    finally:
        srv.shutdown()
        srv.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # the parser defines exactly the documented verbs
    parser = build_parser()
    sub = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]  # noqa: SLF001
    check(tuple(sub.choices) == VERBS, f"top-level verbs are exactly {VERBS}")
    print(
        f"self-test: {'FAILED ' + str(len(failures)) if failures else 'ok'} "
        f"(backends: {', '.join(backends)})"
    )
    return 1 if failures else 0


# ── argparse ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aitheros", description="Activate this appliance with its AITHER1 license."
    )
    p.add_argument("--self-test", action="store_true", help="hermetic self-test (no network)")
    p.add_argument("--list-verbs", action="store_true", help="print the verbs, one per line")
    sub = p.add_subparsers(dest="verb")
    lg = sub.add_parser("login", help="verify, install and exchange a license")
    lg.add_argument("--license", default=None, help="env | @file | - | <envelope>")
    lg.add_argument("--json", action="store_true")
    up = sub.add_parser("up", help="login, then bring the stack up")
    up.add_argument("--license", default=None, help="env | @file | - | <envelope>")
    up.add_argument("--json", action="store_true")
    stp = sub.add_parser("status", help="show the activation state")
    stp.add_argument("--json", action="store_true")
    lic = sub.add_parser("license", help="license maintenance")
    lsub = lic.add_subparsers(dest="lverb")
    a = lsub.add_parser("activate", help="same as login")
    a.add_argument("--license", default=None)
    a.add_argument("--json", action="store_true")
    r = lsub.add_parser("refresh", help="import the spool, verify, exchange, write status")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--json", action="store_true")
    i = lsub.add_parser("import", help="import a license file (or - for stdin)")
    i.add_argument("source")
    i.add_argument("--json", action="store_true")
    lsub.add_parser("doctor", help="diagnose the activation plane")
    e = lsub.add_parser("entitlements", help="print the entitlements")
    e.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.list_verbs:
        print("\n".join(VERBS))
        return 0
    paths = Paths()
    if args.verb == "login":
        return cmd_login(paths, args.license, args.json)
    if args.verb == "up":
        return cmd_up(paths, args.license, args.json)
    if args.verb == "status":
        return cmd_status(paths, args.json)
    if args.verb == "license":
        lv = args.lverb
        if lv == "activate":
            return cmd_login(paths, args.license, args.json)
        if lv == "refresh":
            st = refresh(paths, quiet=args.quiet)
            _emit(st, args.json, args.quiet)
            return exit_for(st)
        if lv == "import":
            return cmd_import(paths, args.source, args.json)
        if lv == "doctor":
            return cmd_doctor(paths)
        if lv == "entitlements":
            return cmd_entitlements(paths)
        parser.parse_args(["license", "--help"])
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
