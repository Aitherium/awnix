"""The console's self-signed ECDSA certificate.

The stdlib cannot mint a certificate, so this shells to `openssl` (argv, no shell) --
it is in every EL/Fedora bootc base. The cert lives in /var/lib/awnix-console/tls and
survives upgrades (it is /var), so the fingerprint the admin wrote down on day one is
still the fingerprint after every update. cert.fp holds the lowercase hex sha256 of the
DER certificate: the tty/serial banner shows its first 12 characters, and the
awkit-backend proxy pins the whole value instead of ever using verify=False.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
from typing import Optional, Tuple

CERT = "cert.pem"
KEY = "key.pem"
FP = "cert.fp"


def fingerprint_pem(pem_text: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem_text)
    return hashlib.sha256(der).hexdigest()


def read_fp(tls_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(tls_dir, FP), encoding="ascii") as fh:
            v = fh.read().strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return v if len(v) == 64 and all(c in "0123456789abcdef" for c in v) else None


def _san_host(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-.")
    return safe or "awnix"


def ensure_cert(tls_dir: str, *, hostname: Optional[str] = None,
                openssl: Optional[str] = None, timeout: int = 60) -> Tuple[str, str, str]:
    """Create the cert once; return (cert_path, key_path, sha256_hex). Raises
    RuntimeError when it cannot (no openssl, openssl failed) -- the caller decides
    whether that is fatal (serve: yes; self-test: skip the TLS leg and say so)."""
    cert = os.path.join(tls_dir, CERT)
    key = os.path.join(tls_dir, KEY)
    if os.path.isfile(cert) and os.path.isfile(key):
        with open(cert, encoding="ascii") as fh:
            fp = fingerprint_pem(fh.read())
        if read_fp(tls_dir) != fp:
            _write_fp(tls_dir, fp)
        return cert, key, fp
    exe = openssl or shutil.which("openssl")
    if not exe:
        raise RuntimeError("openssl not found; cannot mint the console certificate")
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    host = _san_host(hostname or socket.gethostname())
    tmp_key, tmp_cert = key + ".new", cert + ".new"
    argv = [
        exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", tmp_key, "-out", tmp_cert, "-days", "3650",
        "-subj", f"/CN={host}",
        "-addext", f"subjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "extendedKeyUsage=serverAuth",
    ]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MSYS_NO_PATHCONV": "1",
           "MSYS2_ARG_CONV_EXCL": "*"}
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
    )
    if proc.returncode != 0 or not os.path.isfile(tmp_cert):
        raise RuntimeError(f"openssl req failed ({proc.returncode}): {proc.stderr.strip()[-300:]}")
    try:
        os.chmod(tmp_key, 0o600)
    except OSError as exc:
        # openssl already wrote the key under the service's UMask 0077; say it anyway.
        sys.stderr.write(f"awnix-console: chmod 600 {tmp_key} failed: {exc}\n")
    os.replace(tmp_key, key)
    os.replace(tmp_cert, cert)
    with open(cert, encoding="ascii") as fh:
        fp = fingerprint_pem(fh.read())
    _write_fp(tls_dir, fp)
    return cert, key, fp


def _write_fp(tls_dir: str, fp: str) -> None:
    path = os.path.join(tls_dir, FP)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(fp + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError as exc:
        sys.stderr.write(f"awnix-console: chmod 644 {path} failed: {exc}\n")


def server_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def pinned_client_context() -> ssl.SSLContext:
    """A client context that does no CA validation BUT is only ever used together with
    an explicit sha256 pin check on the peer certificate (see verify_pin)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # replaced by the pin; never used without it
    return ctx


def verify_pin(sock: ssl.SSLSocket, expected_fp: str) -> bool:
    der = sock.getpeercert(binary_form=True)
    return bool(der) and hashlib.sha256(der).hexdigest() == expected_fp.lower()
