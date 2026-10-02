"""Login codes, lockout, sessions and rate limits for awnix-console.

Contract: codes are 10 characters drawn with `secrets`; 5 consecutive failures from one
client lock THAT client out for 60 s (HTTP 423), doubling on each repeat lock up to
15 min; in console mode 20 failures rotate the code.

Why the lock is per client and not global: a global lock let one LAN client, sending
5 wrong codes every 61 s from a single address (never tripping the 10/60 s rate limit),
keep login locked for everybody indefinitely, and on garg the console binds 0.0.0.0.
The code space is what a global lock was meant to protect, and it does not need it:
31^10 ~= 8.2e14 codes. Even an attacker holding thousands of addresses is held to 5
guesses per address per lock window, far short of anything that dents that space.
Clients are keyed by IPv4 address or by IPv6 /64 (one host owns a whole /64, so a
per-address key there would be free to rotate).

Rotation is console-mode only. The setup code is the one secret the owner holds (it is
on the local tty banner and nowhere on a headless box's serial marker); letting a remote
guesser rotate it would lock the owner out of setup. The console-mode code is always
re-readable with `sudo awnix console code`.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

# No 0/O, 1/I/L: the code is read off a serial console or a photo of a screen.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 10

LOCK_AFTER = 5
LOCK_SECONDS = 60
LOCK_MAX_SECONDS = 900
ROTATE_AFTER = 20
MAX_TRACKED_CLIENTS = 4096
SESSION_TTL = 12 * 3600
MAX_SESSIONS = 64
COOKIE_NAME = "awnix_console"


def new_code(n: int = CODE_LEN) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def write_secret(path: str, value: str) -> None:
    """Atomic 0600 write (tmp + rename in the same directory)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (value + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        # The file was created under UMask 0077 already; a chmod refusal (a foreign fs)
        # leaves it that way. Said, so a wider mode is never silent.
        sys.stderr.write(f"awnix-console: chmod 600 {path} failed: {exc}\n")


def read_secret(path: str) -> Optional[str]:
    try:
        with open(path, encoding="ascii") as fh:
            v = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return v or None


def ensure_code(path: str) -> str:
    cur = read_secret(path)
    if cur:
        return cur
    code = new_code()
    write_secret(path, code)
    return code


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


class CodeGuard:
    """Verifies a login code against the file at `path`, with per-client lockout and
    (when `rotate`) rotation after every ROTATE_AFTER failures overall."""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.monotonic,
                 on_rotate: Optional[Callable[[str], None]] = None,
                 rotate: bool = True) -> None:
        self.path = path
        self.clock = clock
        self.on_rotate = on_rotate
        self.rotate = rotate
        self.total_failures = 0
        # client -> [consecutive failures, locked_until, locks so far, last seen]
        self._c: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _entry(self, client: str, now: float) -> List[float]:
        ent = self._c.get(client)
        if ent is None:
            if len(self._c) >= MAX_TRACKED_CLIENTS:
                # Drop clients that are neither locked nor recently seen.
                self._c = {k: v for k, v in self._c.items()
                           if v[1] > now or now - v[3] < LOCK_MAX_SECONDS}
                if len(self._c) >= MAX_TRACKED_CLIENTS:
                    oldest = min(self._c.items(), key=lambda kv: kv[1][3])[0]
                    self._c.pop(oldest, None)
            ent = [0.0, 0.0, 0.0, now]
            self._c[client] = ent
        ent[3] = now
        return ent

    def check(self, supplied: str, client: str = "") -> Tuple[str, int]:
        """-> ('ok'|'bad'|'locked'|'unavailable', retry_after_seconds)."""
        with self._lock:
            now = self.clock()
            ent = self._entry(client, now)
            if now < ent[1]:
                return "locked", int(ent[1] - now) + 1
            code = read_secret(self.path)
            if not code:
                return "unavailable", 0
            if supplied and len(supplied) <= 64 and same(supplied.strip().upper(), code):
                ent[0] = 0.0
                ent[2] = 0.0
                return "ok", 0
            ent[0] += 1
            self.total_failures += 1
            if self.rotate and self.total_failures % ROTATE_AFTER == 0:
                fresh = new_code()
                write_secret(self.path, fresh)
                if self.on_rotate:
                    try:
                        self.on_rotate(self.path)
                    except Exception as exc:  # noqa: BLE001 -- a banner refresh must not break login
                        sys.stderr.write("awnix-console: banner refresh after rotation "
                                         f"failed: {exc}\n")
            if ent[0] >= LOCK_AFTER:
                ent[0] = 0.0
                ent[2] += 1
                secs = min(LOCK_SECONDS * (2 ** int(ent[2] - 1)), LOCK_MAX_SECONDS)
                ent[1] = now + secs
                return "locked", int(secs)
            return "bad", 0


class Sessions:
    """Opaque random session ids held in memory. A restart logs everyone out, which
    is also what a mode switch (setup -> console) needs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._s: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def create(self, mode: str) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._s = {k: v for k, v in self._s.items() if v[0] > now}
            if len(self._s) >= MAX_SESSIONS:
                oldest = min(self._s.items(), key=lambda kv: kv[1][0])[0]
                self._s.pop(oldest, None)
            self._s[tok] = (now + SESSION_TTL, mode)
        return tok

    def valid(self, tok: Optional[str], mode: str) -> bool:
        if not tok:
            return False
        with self._lock:
            ent = self._s.get(tok)
            if not ent:
                return False
            exp, smode = ent
            if exp <= self.clock() or smode != mode:
                # A setup-mode session never carries over into console mode.
                self._s.pop(tok, None)
                return False
            return True

    def revoke(self, tok: Optional[str]) -> None:
        if tok:
            with self._lock:
                self._s.pop(tok, None)


class RateLimiter:
    """Fixed window per key: at most `limit` hits per `window` seconds."""

    def __init__(self, limit: int, window: float, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._w: Dict[str, Tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        with self._lock:
            now = self.clock()
            start, n = self._w.get(key, (now, 0))
            if now - start >= self.window:
                start, n = now, 0
            n += 1
            self._w[key] = (start, n)
            if len(self._w) > 4096:
                self._w = {k: v for k, v in self._w.items() if now - v[0] < self.window}
            return n <= self.limit


def client_key(addr: str) -> str:
    """The lockout / rate-limit key for a peer address: IPv4 as is (an IPv4-mapped IPv6
    address counts as its IPv4), IPv6 by its /64, loopback as one key."""
    if is_loopback(addr):
        return "loopback"
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(ip) >> 64 << 64, 64)))
    return str(ip)


def is_loopback(addr: str) -> bool:
    return addr in ("127.0.0.1", "::1") or addr.startswith("127.") or addr == "::ffff:127.0.0.1"
