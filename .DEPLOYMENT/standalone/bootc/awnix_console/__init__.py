"""awnix-console -- the ONE on-box web server of an awnix appliance.

Contract `appliance-web-api` (program plan, console-surfaces gap). One URL for the
life of the box, https://<host>:9443, one self-signed ECDSA cert, one auth scheme:

  * setup mode    while /etc/awnix/setup.json is absent. The login code lives in
                  /run/awnix/setup-code and IS printed on the tty/serial banner.
  * console mode  afterwards. The login code is /run/awnix-console/token and is NOT
                  printed; `sudo awnix console code` reads it.

The server is a privileged broker with a literal argv allowlist (actions.py): it never
runs a shell, never passes a user string anywhere but a validated whole argv element or
stdin, and maps the owning CLI's exit code onto HTTP (0 200, 1 409, 2 503, 3 402, binary
absent 501). It is stdlib-only and Python 3.10-compatible because it runs under the
image's /usr/bin/python3.11 and is self-tested in the Containerfile RUN.

Every filesystem location is a Settings field so the self-test and the pytest suite run
the real server against a temp tree; nothing in here reads a path that is not in
Settings.
"""
from __future__ import annotations

import dataclasses
import glob
import os
from pathlib import Path
from typing import Dict, List, Optional

VERSION = "1.0.0"
DEFAULT_PORT = 9443

# The repo directory this package sits in (.DEPLOYMENT/standalone/bootc). Used ONLY as
# a dev fallback for files that are installed elsewhere on a real box.
_HERE = Path(__file__).resolve().parent
BOOTC_DIR = _HERE.parent


@dataclasses.dataclass
class Settings:
    """Every path and knob the console reads. Defaults are the installed layout."""

    setup_marker: str = "/etc/awnix/setup.json"
    setup_code: str = "/run/awnix/setup-code"
    console_token: str = "/run/awnix-console/token"
    tls_dir: str = "/var/lib/awnix-console/tls"
    static_dir: str = "/usr/share/awnix-console"
    guide_json: str = "/usr/share/doc/awnix/guide.json"
    surfaces_file: str = "/usr/share/awnix/surfaces.yaml"
    release_env: str = "/usr/lib/awnix/release.env"
    components_lock: str = "/usr/share/awnix/components.lock.json"
    license_status: str = "/var/lib/aither/license/status.json"
    update_status: str = "/var/lib/awnix/update-status.json"
    vendor_conf: str = "/usr/lib/awnix/console.conf"
    vendor_conf_d: str = "/usr/lib/awnix/console.d"
    admin_conf: str = "/etc/awnix/console.conf"
    cloud_id: str = "/run/cloud-init/cloud-id"
    issue_helper: str = "/usr/libexec/awnix/awnix-console-issue.sh"
    setup_api_path: str = "/usr/lib/awnix-setup"
    # Where allowlisted binaries are looked up, in order. The dispatcher rule:
    # /usr/libexec/awnix/<name> first, then /usr/bin/<name>.
    tool_path: str = "/usr/libexec/awnix:/usr/bin:/usr/sbin"
    bind: str = "auto"
    port: int = DEFAULT_PORT
    profile: str = "awnix"
    brand: str = "awnix"
    tls: bool = True
    # Extra environment handed to child CLIs (tests only; production is scrubbed).
    child_env: Dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Settings":
        """Installed defaults, overridden by AWNIX_CONSOLE_<FIELD> (upper-case)."""
        env = dict(os.environ if environ is None else environ)
        s = cls()
        for f in dataclasses.fields(cls):
            if f.name == "child_env":
                continue
            key = "AWNIX_CONSOLE_" + f.name.upper()
            if key not in env:
                continue
            val = env[key]
            if f.type in ("int", int):
                setattr(s, f.name, int(val))
            elif f.type in ("bool", bool):
                setattr(s, f.name, val.strip().lower() not in ("0", "false", "no", "off", ""))
            else:
                setattr(s, f.name, val)
        return s

    # -- derived ---------------------------------------------------------------------

    def tool_dirs(self) -> List[str]:
        # os.pathsep, except that the POSIX-style default still splits on ':' when a
        # dev box running Windows reads it (a drive path never appears there).
        sep = os.pathsep
        if os.name == "nt" and ";" not in self.tool_path and not (
                len(self.tool_path) > 2 and self.tool_path[1] == ":"):
            sep = ":"
        return [d for d in self.tool_path.split(sep) if d]

    def mode(self) -> str:
        return "console" if os.path.isfile(self.setup_marker) else "setup"

    def code_path(self, mode: Optional[str] = None) -> str:
        return self.setup_code if (mode or self.mode()) == "setup" else self.console_token


def parse_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE, `#` comments, optional surrounding quotes, NO expansion."""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_conf(settings: Settings, environ: Optional[Dict[str, str]] = None) -> Settings:
    """Apply /usr/lib/awnix/console.conf < console.d/*.conf < /etc/awnix/console.conf
    < process env onto `settings` (bind, port, profile, brand)."""
    merged: Dict[str, str] = {}
    merged.update(parse_env_file(settings.vendor_conf))
    for p in sorted(glob.glob(os.path.join(settings.vendor_conf_d, "*.conf"))):
        merged.update(parse_env_file(p))
    merged.update(parse_env_file(settings.admin_conf))
    env = os.environ if environ is None else environ
    for k in ("AWNIX_CONSOLE_BIND", "AWNIX_CONSOLE_PORT", "AWNIX_CONSOLE_PROFILE",
              "AWNIX_CONSOLE_BRAND"):
        if k in env:
            merged[k] = env[k]
    if merged.get("AWNIX_CONSOLE_BIND"):
        settings.bind = merged["AWNIX_CONSOLE_BIND"]
    if merged.get("AWNIX_CONSOLE_PORT"):
        settings.port = int(merged["AWNIX_CONSOLE_PORT"])
    if merged.get("AWNIX_CONSOLE_PROFILE"):
        settings.profile = merged["AWNIX_CONSOLE_PROFILE"]
    if merged.get("AWNIX_CONSOLE_BRAND"):
        settings.brand = merged["AWNIX_CONSOLE_BRAND"]
    return settings


# cloud-init datasources on which a LAN bind is the right default: a box a person
# installed from media. Anything else (aws, azure, gce, openstack, ...) is a cloud VM
# whose public interface must not expose a root broker by default.
LAN_DATASOURCES = frozenset({"", "nocloud", "none", "nocloud-net", "fallback"})


def resolve_bind(settings: Settings) -> str:
    """The contract default: 0.0.0.0, except 127.0.0.1 on a real cloud datasource."""
    b = (settings.bind or "auto").strip()
    if b not in ("", "auto"):
        return b
    try:
        with open(settings.cloud_id, encoding="utf-8") as fh:
            ds = fh.read().strip().lower()
    except OSError:
        ds = ""
    return "0.0.0.0" if ds in LAN_DATASOURCES else "127.0.0.1"


def read_release(settings: Settings) -> Dict[str, str]:
    rel = parse_env_file(settings.release_env)
    rel.setdefault("AWNIX_VARIANT", "awnix")
    return rel
