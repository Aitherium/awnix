"""The desktop theme, applied to the console page of the person looking at it.

On the awnix desktop the console is the Settings app: a root service on 127.0.0.1, read
by the user's own Firefox. awnix-theme renders the awkit design tokens for the chosen
theme into ~/.config/awnix/theme/awkit.css. The console bundle is a built Vite app
whose stylesheet sets the same tokens on :root, so a stylesheet loaded AFTER it with
those tokens restyles the page with no rebuild. Two hooks do that:

  * inject()     adds <link rel="stylesheet" href="/awnix-theme.css"> to index.html as
                 it is served (the bundle is not rebuilt; CSP default-src 'self' allows
                 a same-origin stylesheet and nothing inline is added);
  * css_for()    answers /awnix-theme.css with THAT user's tokens.

"That user" is not a claim the browser makes. The peer of a loopback connection is a
socket on this machine, and the kernel's table (/proc/net/tcp, tcp6) says which uid owns
it. So the console reads the theme of the uid that opened the connection -- nobody
else's, and nothing for a non-loopback peer or root.

The file is read as root out of a user-writable directory, so it is opened O_NOFOLLOW,
must be a regular file OWNED by that uid and at most 64 KiB, and only lines of the form
`--token: value;` with a plain value survive: whatever the file says, the console only
ever emits CSS custom properties. A missing or refused file is an empty stylesheet (the
bundle's own colours), never an error page.

Stdlib only, Python 3.10-compatible.
"""
from __future__ import annotations

import os
import re
import socket
import stat
from pathlib import Path
from typing import Callable, Optional

ROUTE = "/awnix-theme.css"
LINK = b'<link rel="stylesheet" href="/awnix-theme.css">'
THEME_REL = ".config/awnix/theme/awkit.css"
MAX_BYTES = 64 * 1024
_DECL = re.compile(r"^\s*(--[a-z][a-z0-9-]{0,63})\s*:\s*([^;{}<>\\]{1,200});\s*$")
_VALUE = re.compile(r"^[#A-Za-z0-9 ,.()%\"'+-]+$")
HEADER = "/* awnix desktop theme for this user (awnix-theme set) -- tokens only */\n"


def inject(html: bytes) -> bytes:
    """index.html with the theme stylesheet linked LAST in <head> (so its tokens win)."""
    if LINK in html:
        return html
    for anchor in (b"</head>", b"<body"):
        at = html.find(anchor)
        if at >= 0:
            return html[:at] + LINK + html[at:]
    return html + LINK


def sanitize(text: str) -> str:
    """Only `--token: value;` declarations survive, re-emitted inside one :root block."""
    out = []
    for line in text.splitlines():
        m = _DECL.match(line)
        if m and _VALUE.match(m.group(2).strip()):
            out.append("  %s: %s;" % (m.group(1), m.group(2).strip()))
    return HEADER + (":root {\n%s\n}\n" % "\n".join(out) if out else "")


def _decode(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1]
                                                       for i in range(0, 16, 4)))


def _loopback(addr: str) -> bool:
    return addr == "::1" or addr.startswith("127.") or addr.startswith("::ffff:127.")


def peer_uid(peer_port: int, server_port: int,
             proc_net: Optional[Path] = None) -> Optional[int]:
    """The uid owning the loopback socket 127.0.0.1:<peer_port> -> :<server_port>."""
    proc_net = Path(os.environ.get("AWNIX_CONSOLE_PROC_NET", "/proc/net")) \
        if proc_net is None else proc_net
    for name in ("tcp", "tcp6"):
        try:
            rows = (proc_net / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for row in rows.splitlines()[1:]:
            cols = row.split()
            if len(cols) < 8:
                continue
            try:
                laddr, lport = cols[1].split(":")
                raddr, rport = cols[2].split(":")
                if int(lport, 16) != peer_port or int(rport, 16) != server_port:
                    continue
                if not (_loopback(_decode(laddr)) and _loopback(_decode(raddr))):
                    continue
                return int(cols[7])
            except (ValueError, OSError):
                continue
    return None


def _home(uid: int) -> Optional[Path]:
    try:
        import pwd  # noqa: PLC0415 -- POSIX only; the console runs on Linux
    except ImportError:
        return None
    try:
        return Path(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        return None


def read_owned(path: Path, uid: int) -> Optional[str]:
    """The file's text if it is a regular file owned by `uid`, not a symlink, <= 64 KiB."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_size > MAX_BYTES:
            return None
        data = os.read(fd, MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data[:MAX_BYTES].decode("utf-8", "replace")


def css_for(client_ip: str, peer_port: int, server_port: int, *,
            uid_of: Optional[Callable[[int, int], Optional[int]]] = None,
            home_of: Optional[Callable[[int], Optional[Path]]] = None) -> str:
    """The stylesheet for this connection's user; HEADER alone when there is none."""
    if not _loopback(client_ip):
        return HEADER
    uid = (uid_of or peer_uid)(peer_port, server_port)
    if not uid:  # None (unknown) or 0 (root has no desktop theme)
        return HEADER
    home = (home_of or _home)(uid)
    text = read_owned(home / THEME_REL, uid) if home else None
    return sanitize(text) if text else HEADER
