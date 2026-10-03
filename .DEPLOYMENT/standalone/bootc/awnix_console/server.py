"""awnix-console HTTP(S) server: routes, auth enforcement, static bundle.

Routes (contract `appliance-web-api`):
  GET  /api/health                         open
  POST /api/login {code}                   sets the session cookie
  POST /api/logout
  GET  /api/session                        {mode, authenticated, profile, brand, variant}
  setup mode only:
  GET  /api/setup/steps | /api/setup/state
  POST /api/setup/steps/{id} {value} | /api/setup/finish      -> awnix_setup.api
  console mode only:
  GET  /api/appliance/{overview|license|updates|components|endpoints|surfaces|setup|
                       mesh|renewal|offline-update}
  POST /api/appliance/actions/{verb}       -> actions.ACTIONS
  GET  /guide.json                         /usr/share/doc/awnix/guide.json
  GET  /*                                  /usr/share/awnix-console (CSP default-src 'self')

Auth: a session cookie from /api/login, or -- from a loopback peer only -- an
`Authorization: Bearer <console token>` (the awkit-backend proxy). Every POST also needs
`X-Awnix-Console: 1` and, when the browser sends an Origin, a same-origin one; a POST
authenticated by cookie must carry an Origin.
"""
from __future__ import annotations

import http.server
import importlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from . import BOOTC_DIR, VERSION, Settings, auth, read_release, resolve_bind, usertheme
from . import actions as act
from . import tls as tlsmod

MAX_BODY = 64 * 1024
# A peer gets this long to finish the TLS handshake, and each request this long per
# socket read/write. Both run on the connection's own thread, never the accept loop.
HANDSHAKE_TIMEOUT = 10.0
REQUEST_TIMEOUT = 30.0
# Concurrent connections: overall, and per client key (IPv4 address / IPv6 /64).
# Loopback (the awkit-backend proxy, the doctor) is held only to the overall cap.
MAX_CONNECTIONS = 64
MAX_CONNECTIONS_PER_CLIENT = 8
SECTIONS = ("overview", "license", "updates", "components", "endpoints", "surfaces",
            "setup", "mesh", "renewal", "offline-update")
# The literal routes the wave-2 planes add (read by check_awnix_mesh AMS008 and the tests):
MESH_ROUTE = "/api/appliance/mesh"
CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff",
         ".txt": "text/plain; charset=utf-8", ".map": "application/json",
         ".webp": "image/webp"}

STUB_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>awnix console</title></head>
<body style="font-family:sans-serif;max-width:40em;margin:2em auto;padding:0 16px">
<h1>awnix console</h1><p>The web console is not included in this build.
Administer this machine with the <code>awnix</code> command
(<code>awnix help</code>). The JSON API at <code>/api/</code> is available.</p>
</body></html>"""


# --------------------------------------------------------------------------- helpers

def load_surfaces(settings: Settings) -> Optional[Dict[str, Any]]:
    """awnix-surfaces.yaml is a JSON document with full-line `#` comments (valid YAML,
    readable by the stdlib on the box). Installed path first, repo copy as dev fallback."""
    for p in (settings.surfaces_file, str(BOOTC_DIR / "awnix-surfaces.yaml")):
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _safe_host(host_header: str) -> str:
    h = (host_header or "").strip()
    if h.startswith("["):
        h = h[: h.find("]") + 1] if "]" in h else ""
    else:
        h = h.split(":", 1)[0]
    ok = all(c.isalnum() or c in "-.[]:" for c in h)
    return h if (h and ok and len(h) <= 253) else "localhost"


def primary_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: selects a route, sends nothing
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def console_url(settings: Settings) -> str:
    bind = resolve_bind(settings)
    host = "127.0.0.1" if bind.startswith("127.") or bind == "::1" else primary_ip()
    scheme = "https" if settings.tls else "http"
    return f"{scheme}://{host}:{settings.port}/"


class SetupApi:
    """Lazy import of awnix_setup.api (installer-setup gap). Import failure -> 501."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._mod: Any = None
        self._err: Optional[str] = None
        self._lock = threading.Lock()

    def get(self) -> Tuple[Any, Optional[str]]:
        with self._lock:
            if self._mod is not None or self._err is not None:
                return self._mod, self._err
            for p in (self.settings.setup_api_path, str(BOOTC_DIR)):
                if os.path.isdir(os.path.join(p, "awnix_setup")) and p not in sys.path:
                    sys.path.insert(0, p)
            try:
                self._mod = importlib.import_module("awnix_setup.api")
            except Exception as exc:  # noqa: BLE001 -- any import failure is a 501
                self._err = f"awnix_setup.api unavailable: {type(exc).__name__}: {exc}"
            return self._mod, self._err


# --------------------------------------------------------------------------- server

class ConsoleServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], settings: Settings, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self.sessions = auth.Sessions(clock=clock)
        self.guards = {
            # The setup code is never rotated by failures: see auth.py.
            "setup": auth.CodeGuard(settings.setup_code, clock=clock, on_rotate=self._rotated,
                                    rotate=False),
            "console": auth.CodeGuard(settings.console_token, clock=clock,
                                      on_rotate=self._rotated),
        }
        # Set by build_server when TLS is on. The handshake is done per connection in
        # finish_request (the handler thread), so one silent client cannot stall accept.
        self.ssl_ctx: Optional[ssl.SSLContext] = None
        self._conn_lock = threading.Lock()
        self._conns: Dict[str, int] = {}
        self._conn_total = 0
        self._code_lock = threading.Lock()
        self.login_rate = auth.RateLimiter(10, 60.0, clock=clock)
        self.setup_api = SetupApi(settings)
        self.release = read_release(settings)
        self.scheme = "https" if settings.tls else "http"
        super().__init__(addr, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            return  # a client that hung up or failed the handshake is not our error
        sys.stderr.write(f"awnix-console: error serving {client_address[0]}: {exc!r}\n")

    # -- connection admission + per-connection TLS --------------------------------

    def process_request(self, request: Any, client_address: Any) -> None:
        key = auth.client_key(client_address[0])
        with self._conn_lock:
            per = self._conns.get(key, 0)
            if self._conn_total >= MAX_CONNECTIONS or (
                    key != "loopback" and per >= MAX_CONNECTIONS_PER_CLIENT):
                admitted = False
            else:
                admitted = True
                self._conns[key] = per + 1
                self._conn_total += 1
        if not admitted:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(key)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)  # type: ignore[misc]
        finally:
            self._release(auth.client_key(client_address[0]))

    def _release(self, key: str) -> None:
        with self._conn_lock:
            n = self._conns.get(key, 0) - 1
            if n > 0:
                self._conns[key] = n
            else:
                self._conns.pop(key, None)
            self._conn_total = max(0, self._conn_total - 1)

    def finish_request(self, request: Any, client_address: Any) -> None:
        if self.ssl_ctx is None:
            request.settimeout(REQUEST_TIMEOUT)
            self.RequestHandlerClass(request, client_address, self)
            return
        request.settimeout(HANDSHAKE_TIMEOUT)
        try:
            tls_sock = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # silent, slow or non-TLS client: drop it, on this thread only
        try:
            tls_sock.settimeout(REQUEST_TIMEOUT)
            self.RequestHandlerClass(tls_sock, client_address, self)
        finally:
            # shutdown(SHUT_WR) before close, as socketserver does for the plain socket:
            # a bare close() with an unread request body left on the socket (an early
            # 401/403) sends RST, and the client loses the response it has not read.
            self.shutdown_request(tls_sock)

    # -- login codes --------------------------------------------------------------

    def ensure_mode_code(self, mode: str) -> None:
        """The mode is re-derived on every request, so it can flip under a running
        server (setup-rerun removes setup.json; finishing setup writes it). Make sure
        the code for the CURRENT mode exists, and refresh the banner when one is minted,
        instead of only at service start."""
        path = self.settings.code_path(mode)
        if auth.read_secret(path):
            return
        with self._code_lock:
            if auth.read_secret(path) or os.path.exists(path):
                return  # minted meanwhile, or present but unreadable: never clobber it
            try:
                auth.ensure_code(path)
            except OSError as exc:
                sys.stderr.write(f"awnix-console: cannot mint the {mode} code: {exc}\n")
                return
        try:
            self._rotated(path)
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(f"awnix-console: could not refresh the console banner: {exc}\n")

    def _rotated(self, path: str) -> None:
        # The banner shows the setup code; refresh it after a rotation.
        helper = self.settings.issue_helper
        if os.path.isfile(helper) and os.access(helper, os.X_OK):
            subprocess.run([helper], timeout=10, check=False, capture_output=True,
                           env=act.child_env(self.settings.child_env))


class Handler(http.server.BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = "awnix-console/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT  # StreamRequestHandler applies it to the socket

    # -- plumbing -----------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib name
        if os.environ.get("AWNIX_CONSOLE_QUIET"):
            return
        sys.stderr.write("awnix-console: %s %s\n" % (self.client_address[0], format % args))

    def _headers(self, status: int, ctype: str, length: int,
                 extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # One request per connection: an early refusal leaves an unread body on the
        # socket, and a low-traffic admin console gains nothing from keep-alive.
        self.send_header("Connection", "close")
        self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status: int, body: Any, extra: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(body, sort_keys=True).encode("utf-8")
        hdrs = {"Cache-Control": "no-store"}
        hdrs.update(extra or {})
        self._headers(status, "application/json", len(data), hdrs)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _err(self, status: int, detail: str, state: str = "error",
             extra: Optional[Dict[str, str]] = None, **more: Any) -> None:
        body = {"state": state, "detail": detail}
        body.update(more)
        self._json(status, body, extra)

    def _body(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._err(413, "request body too large")
            return None
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._err(400, "body is not JSON")
            return None
        if not isinstance(data, dict):
            self._err(400, "body must be a JSON object")
            return None
        return data

    def _cookie(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == auth.COOKIE_NAME and v:
                return v
        return None

    def _bearer_ok(self) -> bool:
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer ") or not auth.is_loopback(self.client_address[0]):
            return False
        tok = auth.read_secret(self.server.settings.console_token)
        return bool(tok) and auth.same(h[len("Bearer "):].strip(), tok)

    def _authed(self, mode: str) -> Tuple[bool, str]:
        if self._bearer_ok():
            return True, "bearer"
        if self.server.sessions.valid(self._cookie(), mode):
            return True, "cookie"
        return False, ""

    def _mutation_ok(self, how: str) -> bool:
        """X-Awnix-Console: 1 always; a same-origin Origin whenever one is sent, and
        always for cookie-authenticated (browser) requests."""
        if self.headers.get("X-Awnix-Console") != "1":
            self._err(403, "missing X-Awnix-Console header")
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            if how == "cookie":
                self._err(403, "cross-origin check failed: no Origin")
                return False
            return True
        expected = f"{self.server.scheme}://{self.headers.get('Host', '')}"
        if origin != expected:
            self._err(403, "cross-origin request refused")
            return False
        return True

    # -- verbs --------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- stdlib name
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def _not_allowed(self) -> None:
        self._err(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = _not_allowed  # noqa: N815

    def _route(self, method: str) -> None:
        path = urlsplit(self.path).path
        try:
            if path.startswith("/api/"):
                self._api(method, path)
            elif method != "GET":
                self._not_allowed()
            elif path == "/guide.json":
                self._file(self.server.settings.guide_json, missing_404=True)
            elif path == usertheme.ROUTE and self.server.settings.user_theme:
                self._user_theme()
            else:
                self._static(path)
        except BrokenPipeError:
            return  # the client went away mid-answer; there is nobody left to tell
        except Exception as exc:  # noqa: BLE001 -- never leak a traceback to a client
            sys.stderr.write(f"awnix-console: internal error on {path}: {exc!r}\n")
            try:
                self._err(500, "internal error")
            except OSError as exc2:
                sys.stderr.write(f"awnix-console: could not send the 500 for {path}: {exc2!r}\n")

    # -- API ----------------------------------------------------------------------

    def _api(self, method: str, path: str) -> None:
        s = self.server.settings
        mode = s.mode()
        self.server.ensure_mode_code(mode)
        if path == "/api/health" and method == "GET":
            self._json(200, {"ok": True, "mode": mode, "version": VERSION})
            return
        if path == "/api/session" and method == "GET":
            ok, _ = self._authed(mode)
            self._json(200, {"mode": mode, "authenticated": ok, "profile": s.profile,
                             "brand": s.brand,
                             "variant": self.server.release.get("AWNIX_VARIANT", "awnix"),
                             "version": VERSION})
            return
        if path == "/api/login" and method == "POST":
            self._login(mode)
            return
        if path == "/api/logout" and method == "POST":
            if self.headers.get("X-Awnix-Console") != "1":
                self._err(403, "missing X-Awnix-Console header")
                return
            self.server.sessions.revoke(self._cookie())
            self._json(200, {"authenticated": False}, {"Set-Cookie": self._set_cookie("", 0)})
            return

        ok, how = self._authed(mode)
        if not ok:
            self._err(401, "sign in with the console code", state="refused")
            return
        if method == "POST" and not self._mutation_ok(how):
            return

        if path.startswith("/api/setup/"):
            self._setup(method, path[len("/api/setup/"):], mode)
            return
        if path.startswith("/api/appliance/"):
            if mode != "console":
                self._err(409, "the appliance is in setup mode; finish setup first",
                          state="refused")
                return
            rest = path[len("/api/appliance/"):]
            if method == "POST" and rest.startswith("actions/"):
                verb = rest[len("actions/"):]
                body = self._body()
                if body is None:
                    return
                status, env = act.run_action(verb, body, dirs=s.tool_dirs(),
                                             lock_path=s.components_lock,
                                             extra_env=s.child_env)
                self._json(status, env)
                return
            if method == "GET" and rest in SECTIONS:
                self._section(rest)
                return
            self._err(404, "no such appliance route")
            return
        self._err(404, "no such API route")

    def _set_cookie(self, value: str, max_age: int) -> str:
        return (f"{auth.COOKIE_NAME}={value}; Path=/; HttpOnly; Secure; SameSite=Strict; "
                f"Max-Age={max_age}")

    def _login(self, mode: str) -> None:
        if self.headers.get("X-Awnix-Console") != "1":
            self._err(403, "missing X-Awnix-Console header")
            return
        origin = self.headers.get("Origin")
        own = f"{self.server.scheme}://{self.headers.get('Host', '')}"
        if origin is not None and origin != own:
            self._err(403, "cross-origin request refused")
            return
        client = auth.client_key(self.client_address[0])
        if not self.server.login_rate.hit(client):
            self._err(429, "too many attempts; slow down", extra={"Retry-After": "60"})
            return
        body = self._body()
        if body is None:
            return
        code = body.get("code")
        if not isinstance(code, str):
            self._err(400, "'code' is required")
            return
        verdict, retry = self.server.guards[mode].check(code, client)
        if verdict == "locked":
            self._err(423, "too many wrong codes; login is locked", state="locked",
                      extra={"Retry-After": str(retry)}, retry_after=retry)
            return
        if verdict == "unavailable":
            self._err(503, "no login code has been issued yet", state="unavailable")
            return
        if verdict != "ok":
            self._err(401, "wrong code", state="refused")
            return
        tok = self.server.sessions.create(mode)
        self._json(200, {"authenticated": True, "mode": mode},
                   {"Set-Cookie": self._set_cookie(tok, auth.SESSION_TTL)})

    def _setup(self, method: str, rest: str, mode: str) -> None:
        if mode != "setup":
            self._err(409, "setup is complete; rerun it from the Setup tab",
                      state="refused")
            return
        api, err = self.server.setup_api.get()
        if api is None:
            self._err(501, err or "setup API unavailable", state="unavailable",
                      available=False)
            return
        try:
            if method == "GET" and rest == "steps":
                self._json(200, api.steps())
            elif method == "GET" and rest == "state":
                self._json(200, api.state())
            elif method == "POST" and rest == "finish":
                if self._body() is None:
                    return
                self._json(200, api.finish())
            elif method == "POST" and rest.startswith("steps/"):
                sid = rest[len("steps/"):]
                if not act.ID_RE.match(sid):
                    self._err(422, "invalid step id")
                    return
                body = self._body()
                if body is None:
                    return
                self._json(200, api.apply(sid, body.get("value")))
            else:
                self._err(404, "no such setup route")
        except (ValueError, KeyError) as exc:
            # awnix_setup.api errors carry the status they mean (404 unknown step, 409
            # conflict); a bare ValueError/KeyError from anywhere else stays 422.
            code = getattr(exc, "status", 422)
            self._err(code if isinstance(code, int) and 400 <= code < 600 else 422,
                      str(exc))
        except PermissionError as exc:
            # SetupCompleteError (.status 410) says setup already ran; others are 403.
            code = getattr(exc, "status", 403)
            self._err(code if isinstance(code, int) and 400 <= code < 600 else 403,
                      str(exc), state="refused")

    def _section(self, name: str) -> None:
        s = self.server.settings
        if name == "surfaces":
            self._surfaces()
            return
        if name == "overview":
            self._json(200, act.envelope("overview", 200, "ok", exit_code=0,
                                         result=self._overview())[1])
            return
        status, env = act.run_read(name, dirs=s.tool_dirs(), extra_env=s.child_env)
        self._json(status, env)

    def _overview(self) -> Dict[str, Any]:
        s = self.server.settings
        rel = self.server.release
        lic = _read_json(s.license_status)
        upd = _read_json(s.update_status)
        days_left: Optional[int] = None
        if lic and isinstance(lic.get("exp"), (int, float)) and lic["exp"] > 0:
            days_left = int((lic["exp"] - time.time()) // 86400)
        try:
            with open("/proc/uptime", encoding="ascii") as fh:
                uptime: Optional[int] = int(float(fh.read().split()[0]))
        except (OSError, ValueError, IndexError):
            uptime = None
        return {
            "hostname": socket.gethostname(),
            "variant": rel.get("AWNIX_VARIANT", "awnix"),
            "image_repo": rel.get("AWNIX_IMAGE_REPO", ""),
            "profile": s.profile,
            "brand": s.brand,
            "uptime_s": uptime,
            "console": {"url": console_url(s), "fingerprint": tlsmod.read_fp(s.tls_dir)},
            "license": ({"state": lic.get("state", "unlicensed"), "sku": lic.get("sku"),
                         "tier": lic.get("tier"), "exp": lic.get("exp"),
                         "days_left": days_left,
                         "registry": (lic.get("registry") or {}).get("state")}
                        if lic else {"state": "unavailable"}),
            "updates": ({"state": upd.get("state"), "channel": upd.get("channel"),
                         "booted_digest": upd.get("booted_digest"),
                         "available_digest": upd.get("available_digest"),
                         "checked_at": upd.get("checked_at")}
                        if upd else {"state": "unavailable"}),
        }

    def _surfaces(self) -> None:
        s = self.server.settings
        data = load_surfaces(s)
        variant = self.server.release.get("AWNIX_VARIANT", "awnix")
        if not data:
            self._json(503, act.envelope("surfaces", 503, "unavailable",
                                         detail="surfaces manifest unreadable")[1])
            return
        row = (data.get("variants") or {}).get(variant)
        if not isinstance(row, dict):
            self._json(503, act.envelope("surfaces", 503, "unavailable",
                                         detail=f"no surfaces row for variant {variant}")[1])
            return
        catalog = data.get("surface_catalog") or {}
        host = _safe_host(self.headers.get("Host", ""))
        out = []
        for sid, ent in (row.get("surfaces") or {}).items():
            if not isinstance(ent, dict) or ent.get("delivery") == "absent":
                continue
            meta = catalog.get(sid) or {}
            item = {"id": sid, "label": meta.get("label", sid),
                    "delivery": ent.get("delivery"),
                    "licence": ent.get("licence") or meta.get("licence"),
                    "description": meta.get("description", "")}
            url = ent.get("url") or (meta.get("url") if ent.get("delivery") == "link" else None)
            if url:
                item["url"] = url.replace("<host>", host)
            cmd = ent.get("command") or meta.get("command")
            if cmd:
                item["command"] = cmd
            out.append(item)
        console = dict(row.get("console") or {})
        self._json(200, act.envelope("surfaces", 200, "ok", exit_code=0,
                                     result={"variant": variant, "console": console,
                                             "surfaces": out})[1])

    # -- files --------------------------------------------------------------------

    def _file(self, path: str, *, missing_404: bool = False) -> None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            if missing_404:
                self._err(404, "not found")
                return
            raise
        ext = os.path.splitext(path)[1].lower()
        if self.server.settings.user_theme and os.path.basename(path) == "index.html":
            data = usertheme.inject(data)
        extra = {"Cache-Control": "no-cache" if ext == ".html" or ext == ".json"
                 else "public, max-age=3600"}
        self._headers(200, TYPES.get(ext, "application/octet-stream"), len(data), extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _user_theme(self) -> None:
        """/awnix-theme.css: the connecting local user's theme tokens (usertheme.py).
        Open like the bundle's own CSS: colours only, and only the peer's own."""
        server_port = self.server.server_address[1]
        css = usertheme.css_for(self.client_address[0], self.client_address[1], server_port)
        data = css.encode("utf-8")
        self._headers(200, TYPES[".css"], len(data), {"Cache-Control": "no-cache"})
        if self.command != "HEAD":
            self.wfile.write(data)

    def _static(self, path: str) -> None:
        root = os.path.realpath(self.server.settings.static_dir)
        rel = unquote(path).lstrip("/")
        if "\x00" in rel or any(p == ".." for p in rel.replace("\\", "/").split("/")):
            self._err(404, "not found")
            return
        if not rel or rel.endswith("/"):
            rel += "index.html"
        full = os.path.realpath(os.path.join(root, rel))
        inside = full == root or full.startswith(root + os.sep)
        if inside and os.path.isfile(full):
            self._file(full)
            return
        index = os.path.join(root, "index.html")
        if "." not in os.path.basename(rel) or rel == "index.html":
            if os.path.isfile(index):
                self._file(index)
                return
            data = STUB_PAGE.encode("utf-8")
            self._headers(200, TYPES[".html"], len(data), {"Cache-Control": "no-cache"})
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        self._err(404, "not found")


# --------------------------------------------------------------------------- startup

def prepare(settings: Settings) -> Dict[str, Any]:
    """Idempotent pre-start: codes, certificate, firewalld runtime port."""
    info: Dict[str, Any] = {"mode": settings.mode()}
    auth.ensure_code(settings.console_token)
    if info["mode"] == "setup":
        auth.ensure_code(settings.setup_code)
    if settings.tls:
        _, _, fp = tlsmod.ensure_cert(settings.tls_dir)
        info["fingerprint"] = fp
    bind = resolve_bind(settings)
    info["bind"] = bind
    if not (bind.startswith("127.") or bind == "::1"):
        fw = (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", "firewalld"], check=False, capture_output=True
            )
            if _has("systemctl")
            else None
        )
        if fw is not None and fw.returncode == 0 and _has("firewall-cmd"):
            r = subprocess.run(["firewall-cmd", f"--add-port={settings.port}/tcp"],
                               check=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            info["firewalld"] = "opened" if r.returncode == 0 else "failed"
        else:
            info["firewalld"] = "inactive"
    return info


def _has(exe: str) -> bool:
    from shutil import which
    return which(exe) is not None


def build_server(settings: Settings, *, bind: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic) -> ConsoleServer:
    host = bind if bind is not None else resolve_bind(settings)
    srv = ConsoleServer((host, settings.port), settings, clock=clock)
    if settings.tls:
        cert, key, _ = tlsmod.ensure_cert(settings.tls_dir)
        # NOT wrapped around the listening socket: SSLSocket.accept() would handshake
        # on the serve_forever thread with no timeout, and one client that connects
        # and never sends a ClientHello would freeze the console for everyone.
        srv.ssl_ctx = tlsmod.server_context(cert, key)
    return srv


def serve(settings: Settings) -> int:
    bind = resolve_bind(settings)
    if not settings.tls and not (bind.startswith("127.") or bind == "::1"):
        sys.stderr.write("awnix-console: refusing plain http on a non-loopback bind\n")
        return 2
    info = prepare(settings)
    srv = build_server(settings)
    sys.stderr.write(f"awnix-console {VERSION}: {info['mode']} mode on "
                     f"{srv.server_address[0]}:{srv.server_address[1]} "
                     f"({'https' if settings.tls else 'http'})\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("awnix-console: interrupted, shutting down\n")
    finally:
        srv.server_close()
    return 0


__all__ = ["ConsoleServer", "Handler", "build_server", "prepare", "serve", "load_surfaces",
           "console_url", "HTTPStatus"]
