"""Logic shared by the awnix-setup tty and the awnix-console web setup.

Everything that CHANGES the machine goes through ``run()``: an argv list, never a shell
string. Every path is overridable from the environment so ``--self-test`` can drive the
real functions against a temp tree with a stub runner.

Contracts implemented here (recon.json shared_contracts):
  * setup-state-and-steps: /etc/awnix/setup.json v2 is the ONE completion marker; the
    link bearer lives in /etc/awnix/link.token (0600); steps.d v1 files.
  * seed-volume: awnix-seed.json v1 plus license.lic / setup-answers.json beside it.
  * endpoints-env: process env > /etc/awnix/endpoints.env > /usr/lib/awnix/endpoints.env
    > the literals below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import SEED_SCHEMA, STATE_SCHEMA, STEPS_SCHEMA

UA = "awnix-setup"


# ── paths (every one overridable, so the self-test never touches the real tree) ──────────
def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default))


def state_dir() -> Path:
    return _p("AWNIX_STATE_DIR", "/etc/awnix")


def state_file() -> Path:
    return state_dir() / "setup.json"


def link_token_file() -> Path:
    return state_dir() / "link.token"


def seed_file() -> Path:
    return state_dir() / "seed.json"


def seed_dir() -> Path:
    """Files the installer %post copied off the seed volume (license.lic, answers)."""
    return state_dir() / "seed.d"


def run_dir() -> Path:
    return _p("AWNIX_RUN_DIR", "/run/awnix")


def progress_file() -> Path:
    return run_dir() / "setup-progress.json"


def steps_dir() -> Path:
    return _p("AWNIX_STEPS_DIR", "/usr/share/awnix-setup/steps.d")


def components_lock() -> Path:
    return _p("AWNIX_COMPONENTS_LOCK", "/usr/share/awnix/components.lock.json")


def license_status_file() -> Path:
    return _p("AITHER_LICENSE_STATUS", "/var/lib/aither/license/status.json")


def license_spool() -> Path:
    return _p("AITHER_LICENSE_SPOOL", "/var/lib/aither/license/incoming.lic")


def setup_answers_file() -> Path:
    return _p("AITHER_SETUP_ANSWERS", "/etc/aither/setup-answers.json")


def release_env_file() -> Path:
    return _p("AWNIX_RELEASE_ENV", "/usr/lib/awnix/release.env")


def home_root() -> Path:
    # bootc/ostree: /home is a symlink to /var/home. useradd handles that; we only need
    # the root to find authorized_keys, and the passwd entry is the authority.
    return _p("AWNIX_HOME_ROOT", "/var/home")


def passwd_file() -> Path:
    return _p("AWNIX_PASSWD", "/etc/passwd")


def group_file() -> Path:
    return _p("AWNIX_GROUP", "/etc/group")


def shadow_file() -> Path:
    return _p("AWNIX_SHADOW", "/etc/shadow")


def profile_file() -> Path:
    # Written by `awnix-zero-ports apply --profile airgap|base` (Containerfile.awnix-airgap).
    return _p("AWNIX_PROFILE_FILE", "/usr/lib/awnix/profile")


def airgap() -> bool:
    """True on an image built with the airgap profile (zero open ports, sshd loopback-only)."""
    try:
        return profile_file().read_text(encoding="utf-8").strip() == "airgap"
    except OSError:
        return False


#: Why the airgap profile refuses a key-only administrator. awnix-zero-ports locks root and
#: every shipped account and binds sshd to 127.0.0.1/::1, so a key can never be presented
#: from off the box: the admin's password on the local console is the ONLY login and the
#: only recovery path, and setup never runs again once setup.json exists.
AIRGAP_PASSWORD_WHY = (
    "the airgap profile needs a password for the administrator: sshd listens on "
    "127.0.0.1 only, so a local console login with this password is the only way in "
    "(and the only recovery login)"
)


AWNIX_BIN = os.environ.get("AWNIX_BIN", "/usr/bin/awnix")


# ── the one way anything is executed ─────────────────────────────────────────────────────
def _real_run(
    argv: list[str], input_text: str | None = None, timeout: float | None = 120
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


#: Replaced wholesale by the self-test. Signature: (argv, input_text, timeout).
RUN: Callable[..., tuple[int, str, str]] = _real_run


def run(
    argv: list[str], input_text: str | None = None, timeout: float | None = 120
) -> tuple[int, str, str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("run() takes a non-empty argv list of strings, never a shell string")
    return RUN(argv, input_text, timeout)


def which(name: str) -> str | None:
    return shutil.which(name)


# ── env files (endpoints, release) ───────────────────────────────────────────────────────
def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE, '#' comments, optional surrounding quotes, NO expansion."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export ") :].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if re.match(r"^[A-Z_][A-Z0-9_]*$", k):
            out[k] = v
    return out


#: Today's literals. The vendor file ships these same values; they are the floor.
ENDPOINT_DEFAULTS = {
    "AWNIX_LINK_HOST": "https://mcp.aitherium.com",
    # Where a linked box's agents sign in (adk/awsh identity_url). The platform's tools,
    # agent packs and inference stay on AWNIX_LINK_HOST (/mcp, /v1).
    "AWNIX_IDENTITY_URL": "https://idp.aitherium.com",
    "AWNIX_DEVICE_CODE_URL": "",  # derived from AWNIX_LINK_HOST when empty
    "AWNIX_GITHUB_ORG": "Aitherium",
    "AWNIX_GITHUB_KEYS_URL": "https://github.com/{user}.keys",
}


def load_endpoints(
    environ: dict[str, str] | None = None,
    etc_path: Path | None = None,
    vendor_path: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """{KEY: (value, source)}; precedence process > /etc > /usr/lib > literal."""
    environ = os.environ if environ is None else environ
    etc_path = etc_path or _p("AWNIX_ENDPOINTS_ADMIN", "/etc/awnix/endpoints.env")
    vendor_path = vendor_path or _p("AWNIX_ENDPOINTS_VENDOR", "/usr/lib/awnix/endpoints.env")
    vendor = parse_env_file(vendor_path)
    admin = parse_env_file(etc_path)
    out: dict[str, tuple[str, str]] = {}
    for key, lit in ENDPOINT_DEFAULTS.items():
        if environ.get(key):
            out[key] = (environ[key], "process-env")
        elif admin.get(key):
            out[key] = (admin[key], "admin-env")
        elif vendor.get(key):
            out[key] = (vendor[key], "vendor-env")
        else:
            out[key] = (lit, "default")
    host = out["AWNIX_LINK_HOST"][0].rstrip("/")
    if not out["AWNIX_DEVICE_CODE_URL"][0]:
        out["AWNIX_DEVICE_CODE_URL"] = (host + "/auth/device/code", out["AWNIX_LINK_HOST"][1])
    return out


def endpoint(key: str) -> str:
    return load_endpoints()[key][0]


def device_token_url(code_url: str) -> str:
    base = code_url.rstrip("/")
    return base[: -len("/code")] + "/token" if base.endswith("/code") else base + "/token"


def variant() -> str:
    v = os.environ.get("AWNIX_VARIANT")
    if v:
        return v
    return parse_env_file(release_env_file()).get("AWNIX_VARIANT", "")


# ── state ────────────────────────────────────────────────────────────────────────────────
def _warn(what: str, err: BaseException) -> None:
    """A best-effort step that failed: said on stderr, never silent, never fatal."""
    print(f"awnix-setup: warning: {what}: {err}", file=sys.stderr)


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except BaseException:
        try:
            tmp.unlink()
        except OSError as e:
            _warn(f"could not remove {tmp}", e)
        raise
    try:
        os.chmod(str(tmp), mode)
    except OSError as e:  # os.open already created it no wider than `mode`
        _warn(f"chmod {oct(mode)} {tmp}", e)
    _clear_empty_dir(path)
    os.replace(str(tmp), str(path))


def _clear_empty_dir(path: Path) -> None:
    """Remove an EMPTY directory squatting on a file path before it is written.

    The aither-license.path unit once created the licence spool as a directory
    (MakeDirectory=yes, fixed in #10872); boxes that booted with it keep that directory,
    and os.replace onto it fails with EISDIR. rmdir refuses a non-empty directory, so
    nothing with content is ever removed.
    """
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.rmdir(path)
    except FileNotFoundError:
        return
    except OSError as e:  # not empty, or not ours to remove: os.replace reports it
        _warn(f"could not clear the directory at {path}", e)


def read_state() -> dict:
    """setup.json, v1 or v2. A v1 bearer is moved out on read; it is never returned."""
    try:
        st = json.loads(state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(st, dict):
        return {}
    if "bearer" in st:
        tok = st.pop("bearer")
        if tok and not link_token_file().exists():
            try:
                _atomic_write(link_token_file(), str(tok) + "\n", 0o600)
            except OSError as e:  # the v1 file on disk still holds it; nothing is lost
                _warn("could not move the v1 link bearer to link.token", e)
    return st


def setup_complete() -> bool:
    """THE marker. awnix-setup.service and awnix-console setup mode both key off it."""
    return state_file().exists()


def write_state(state: dict, bearer: str | None = None) -> dict:
    """Write setup.json v2. A bearer goes to link.token (0600), never into setup.json."""
    st = dict(state)
    if "bearer" in st:
        bearer = bearer or st.pop("bearer")
        st.pop("bearer", None)
    st["version"] = STATE_SCHEMA
    if bearer:
        write_link_token(bearer)
    _atomic_write(state_file(), json.dumps(st, indent=2, sort_keys=True) + "\n", 0o644)
    return st


def write_link_token(bearer: str) -> None:
    _atomic_write(link_token_file(), bearer.strip() + "\n", 0o600)
    try:
        provision_platform_client(bearer.strip())
    except Exception as e:  # noqa: BLE001 - the link itself succeeded; say so, never undo it
        _warn("linked, but the admin's adk/awsh platform config was not written", e)


def platform_endpoints() -> dict[str, str]:
    """The platform a LINKED box's agents talk to, from the endpoint chain."""
    host = endpoint("AWNIX_LINK_HOST").rstrip("/")
    return {"api_url": host, "mcp_url": host + "/mcp", "inference_url": host + "/v1",
            "identity_url": endpoint("AWNIX_IDENTITY_URL").rstrip("/")}


def provision_platform_client(bearer: str, user: str | None = None) -> dict:
    """Give the box's admin the same platform sign-in the link just made.

    Before this the link token sat in /etc/awnix/link.token and nothing read it: a box
    linked at setup still had agents (adk, the aither shell) that knew nothing of the
    platform's tools, agent packs or inference. This writes what `adk login --api-key`
    writes -- ~/.aither/config.json (merged, 0600) and ~/.aither/shell.yaml -- without the
    token ever touching a command line. A local model the user already chose stays the
    default backend: the platform's inference lands as gateway_inference_url instead.
    Returns {"user", "config"} or {} when there is no admin to provision.
    """
    name = user or detect_admin()
    if not name or not bearer:
        return {}
    home = _home_of(name)
    d = home / ".aither"
    eps = platform_endpoints()
    cfg_path = d / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except (OSError, ValueError):
        cfg = {}
    upd = dict(eps)
    if cfg.get("default_backend") and cfg.get("inference_url"):
        upd["gateway_inference_url"] = upd.pop("inference_url")
    cfg.update(upd)
    cfg["api_key"] = bearer
    cfg["linked_by"] = "awnix-setup"
    _atomic_write(cfg_path, json.dumps(cfg, indent=2) + "\n", 0o600)
    shell = d / "shell.yaml"
    existing: dict[str, str] = {}
    try:
        for line in shell.read_text(encoding="utf-8").splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                existing[k.strip()] = v.strip()
    except OSError:
        pass
    existing.update({k: eps[k] for k in ("api_url", "mcp_url", "identity_url")})
    _atomic_write(shell, "".join(f"{k}: {v}\n" for k, v in existing.items()), 0o600)
    row = next((r for r in _passwd_rows() if r and r[0] == name and len(r) >= 4), None)
    chown = getattr(os, "chown", None)  # absent on Windows (the self-test host)
    if row and chown:
        for path in (d, cfg_path, shell):
            try:
                chown(str(path), int(row[2]), int(row[3]))
            except (OSError, ValueError):
                pass  # not root (self-test) or a bad row: the files still exist
    return {"user": name, "config": str(cfg_path)}


def read_progress() -> dict:
    try:
        d = json.loads(progress_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_progress(p: dict) -> None:
    _atomic_write(progress_file(), json.dumps(p, indent=2, sort_keys=True) + "\n", 0o600)


def mark_step(step_id: str, status: str, **fields: Any) -> dict:
    p = read_progress()
    steps = p.setdefault("steps", {})
    steps[step_id] = {"status": status, "at": _now()}
    for k, v in fields.items():
        p[k] = v
    write_progress(p)
    return p


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ── hostname ─────────────────────────────────────────────────────────────────────────────
HOSTNAME_RE = re.compile(r"^(?=.{1,63}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def valid_hostname(name: str) -> bool:
    return bool(HOSTNAME_RE.match(name or ""))


def set_hostname(name: str) -> tuple[bool, str]:
    name = (name or "").strip().lower()
    if not valid_hostname(name):
        return False, "hostname must be 1-63 of a-z, 0-9 and '-', not starting or ending with '-'"
    rc, _, err = run(["hostnamectl", "set-hostname", name], timeout=30)
    return rc == 0, (name if rc == 0 else (err.strip() or f"hostnamectl exit {rc}"))


# ── admin user + ssh keys ────────────────────────────────────────────────────────────────
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED_USERS = {
    "root",
    "bin",
    "daemon",
    "adm",
    "lp",
    "sync",
    "shutdown",
    "halt",
    "mail",
    "operator",
    "games",
    "ftp",
    "nobody",
    "systemd-network",
    "sshd",
    "core",
}
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


def valid_username(name: str) -> bool:
    return bool(USER_RE.match(name or "")) and name not in RESERVED_USERS


def looks_private(text: str) -> bool:
    return "PRIVATE KEY" in (text or "") or "BEGIN OPENSSH" in (text or "")


def pubkey_shape_ok(line: str) -> bool:
    parts = (line or "").strip().split()
    return (
        len(parts) >= 2
        and parts[0] in KEY_TYPES
        and re.match(r"^[A-Za-z0-9+/=]+$", parts[1]) is not None
    )


def validate_pubkey(line: str) -> tuple[bool, str]:
    """(ok, fingerprint-or-reason). ssh-keygen is the judge; the shape check is a floor."""
    line = (line or "").strip()
    if looks_private(line):
        return False, "that is a PRIVATE key -- paste the .pub half only"
    if "\n" in line or not pubkey_shape_ok(line):
        return False, "not an OpenSSH public key line (expected 'ssh-ed25519 AAAA... comment')"
    rc, out, err = run(["ssh-keygen", "-l", "-f", "-"], input_text=line + "\n", timeout=15)
    if rc != 0:
        return False, (err.strip() or "ssh-keygen rejected the key")
    parts = out.split()
    fp = next((p for p in parts if p.startswith("SHA256:")), "")
    return (True, fp) if fp else (False, "ssh-keygen gave no fingerprint")


def parse_keys_blob(text: str) -> list[str]:
    return [
        ln.strip()
        for ln in (text or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _fetch_text(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


FETCH: Callable[[str], str] = _fetch_text


def import_github_keys(user: str) -> tuple[list[str], str]:
    """(keys, error). Only well-formed public-key lines survive."""
    if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$", user or ""):
        return [], "not a GitHub username"
    url = endpoint("AWNIX_GITHUB_KEYS_URL").replace("{user}", user)
    try:
        text = FETCH(url)
    except urllib.error.HTTPError as e:
        return [], f"GitHub answered {e.code} for {user}"
    except Exception as e:  # network down is a reason, not a crash
        return [], f"could not reach GitHub ({e})"
    keys = [k for k in parse_keys_blob(text) if pubkey_shape_ok(k)]
    return keys, ("" if keys else f"{user} has no public SSH keys on GitHub")


def _passwd_rows() -> list[list[str]]:
    try:
        return [
            ln.split(":")
            for ln in passwd_file().read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")
        ]
    except OSError:
        return []


def _wheel_members() -> set[str]:
    try:
        for ln in group_file().read_text(encoding="utf-8").splitlines():
            parts = ln.split(":")
            if len(parts) >= 4 and parts[0] == "wheel":
                return {m for m in parts[3].split(",") if m}
    except OSError as e:
        _warn("could not read the group file", e)
    return set()


def user_exists(name: str) -> bool:
    return any(r and r[0] == name for r in _passwd_rows())


def _home_of(name: str) -> Path:
    for r in _passwd_rows():
        if r and r[0] == name and len(r) >= 6 and r[5]:
            home = r[5]
            # /home -> /var/home on ostree; resolve under our (overridable) root.
            if home.startswith("/home/"):
                return home_root() / home[len("/home/") :]
            return Path(home)
    return home_root() / name


def has_password(name: str) -> bool:
    """True when NAME has a usable password hash (not empty, not locked with ! or *)."""
    try:
        rows = shadow_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for ln in rows:
        parts = ln.split(":")
        if parts and parts[0] == name and len(parts) > 1:
            h = parts[1]
            return bool(h) and h[0] not in "!*"
    return False


def detect_admin() -> str | None:
    """An existing wheel member who can log in, else None.

    Can log in = authorized_keys present; on the airgap profile = a usable password,
    because no key can reach a loopback-only sshd from off the box.
    """
    air = airgap()
    for name in sorted(_wheel_members()):
        if name in RESERVED_USERS:
            continue
        if air:
            if has_password(name):
                return name
            continue
        ak = _home_of(name) / ".ssh" / "authorized_keys"
        try:
            if ak.exists() and ak.read_text(encoding="utf-8").strip():
                return name
        except OSError:
            continue
    return None


def hash_password(password: str) -> tuple[bool, str]:
    """openssl passwd -6 -stdin. No crypt module (gone in 3.13, absent in some builds)."""
    if not password:
        return False, "empty password"
    rc, out, err = run(
        ["openssl", "passwd", "-6", "-stdin"], input_text=password + "\n", timeout=15
    )
    h = out.strip()
    if rc != 0 or not h.startswith("$6$"):
        return False, err.strip() or "openssl did not return a SHA-512 crypt hash"
    return True, h


def admin_argv(name: str, password_hash: str | None, exists: bool) -> list[list[str]]:
    """The exact commands create_admin runs. Pure, so the self-test can read them."""
    cmds: list[list[str]] = []
    if not exists:
        cmds.append(["useradd", "-m", "-G", "wheel", "-s", "/bin/bash", name])
    else:
        cmds.append(["usermod", "-aG", "wheel", name])
    if password_hash:
        cmds.append(["usermod", "-p", password_hash, name])
    return cmds


def create_admin(
    name: str, keys: list[str], password: str | None = None, password_hash: str | None = None
) -> tuple[bool, dict]:
    """Create (or extend) the admin. Returns (ok, {admin_user, ssh_key_fingerprints}|{error})."""
    name = (name or "").strip()
    if not valid_username(name):
        return False, {
            "error": "username must be lowercase letters, digits, '_' or '-' "
            "(max 32) and not a system account"
        }
    fps: list[str] = []
    clean: list[str] = []
    for k in keys or []:
        ok, fp = validate_pubkey(k)
        if not ok:
            return False, {"error": f"ssh key rejected: {fp}"}
        fps.append(fp)
        clean.append(" ".join(k.strip().split()))
    if password and not password_hash:
        ok, h = hash_password(password)
        if not ok:
            return False, {"error": h}
        password_hash = h
    if password_hash and not password_hash.startswith("$"):
        return False, {"error": "password_hash must be a crypt(3) hash, never a plaintext"}
    if not clean and not password_hash:
        return False, {
            "error": "give the admin at least one SSH key or a password -- "
            "an account with neither cannot log in"
        }
    if airgap() and not password_hash and not has_password(name):
        return False, {"error": AIRGAP_PASSWORD_WHY}
    for argv in admin_argv(name, password_hash, user_exists(name)):
        rc, _, err = run(argv, timeout=60)
        if rc != 0:
            return False, {"error": f"{argv[0]} failed: {err.strip() or rc}"}
    if clean:
        ssh = _home_of(name) / ".ssh"
        ak = ssh / "authorized_keys"
        existing = parse_keys_blob(ak.read_text(encoding="utf-8")) if ak.exists() else []
        merged = existing + [k for k in clean if k not in existing]
        ssh.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(ssh), 0o700)
        except OSError as e:
            _warn(f"chmod 0700 {ssh}", e)
        _atomic_write(ak, "\n".join(merged) + "\n", 0o600)
        run(["chown", "-R", f"{name}:{name}", str(ssh)], timeout=30)
        run(["restorecon", "-R", str(ssh)], timeout=30)
    if not password_hash and not has_password(name):
        # A key-only admin has no password for sudo to ask for: without this the box is
        # reachable and not administrable (the ec2-user convention; the installer %post
        # writes the same drop-in for a seeded admin).
        drop = _p("AWNIX_SUDOERS_DIR", "/etc/sudoers.d") / "90-awnix-admin"
        lines = parse_keys_blob(drop.read_text(encoding="utf-8")) if drop.exists() else []
        entry = f"{name} ALL=(ALL) NOPASSWD: ALL"
        if entry not in lines:
            _atomic_write(drop, "\n".join(lines + [entry]) + "\n", 0o440)
    return True, {"admin_user": name, "ssh_key_fingerprints": fps}


# ── components (awnix component, never pip) ──────────────────────────────────────────────
def load_lock() -> dict:
    try:
        d = json.loads(components_lock().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def license_status() -> dict:
    try:
        d = json.loads(license_status_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def entitlements() -> set[str]:
    st = license_status()
    if st.get("state") != "valid":
        return set()
    e = st.get("entitlements") or {}
    out = set(e.get("images") or []) | set(e.get("packs") or [])
    if e.get("appliance_tier"):
        out.add(str(e["appliance_tier"]))
    return out


def component_menu(lock: dict | None = None, ents: set[str] | None = None) -> list[dict]:
    """What the components step offers. Hidden: baked, available:false, needs-license."""
    lock = load_lock() if lock is None else lock
    ents = entitlements() if ents is None else ents
    rows = []
    for c in lock.get("components") or []:
        cid = c.get("id")
        if not cid or c.get("kind") == "baked" or c.get("available") is False:
            continue
        if c.get("requires_license") and (c.get("entitlement") or cid) not in ents:
            continue
        rows.append(
            {
                "value": cid,
                "label": cid,
                "detail": (c.get("reason") or c.get("version") or c.get("kind") or ""),
            }
        )
    return rows


def baked_components(lock: dict | None = None) -> list[str]:
    lock = load_lock() if lock is None else lock
    return sorted(
        c["id"] for c in lock.get("components") or [] if c.get("id") and c.get("kind") == "baked"
    )


def install_components(ids: list[str]) -> tuple[bool, dict]:
    offered = {r["value"] for r in component_menu()}
    bad = [i for i in ids if i not in offered]
    if bad:
        return False, {"error": f"not offered: {', '.join(bad)}"}
    if not ids:
        return True, {"installed": []}
    rc, out, err = run([AWNIX_BIN, "component", "install", "--json", *ids], timeout=3600)
    try:
        res = json.loads(out) if out.strip() else {}
    except ValueError:
        res = {}
    if rc == 127:
        return False, {"error": "awnix component is not installed on this image"}
    done = [
        r.get("id") for r in (res.get("results") or []) if r.get("state") in ("installed", "baked")
    ]
    if rc != 0:
        return False, {
            "error": (err.strip() or f"awnix component exit {rc}")[-400:],
            "installed": done,
        }
    return True, {"installed": done or list(ids)}


def parse_selection(raw: str, offered: list[dict]) -> list[str]:
    """`1 3 5`, `1,3,5`, `all`, `none`/empty. Unknown tokens are IGNORED, never guessed."""
    s = (raw or "").strip().lower()
    if s in ("", "none", "n", "skip"):
        return []
    if s in ("all", "a", "*"):
        return [c["value"] for c in offered]
    picked: list[str] = []
    for tok in s.replace(",", " ").split():
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(offered):
                picked.append(offered[i]["value"])
        else:
            for c in offered:
                if str(c["value"]).lower() == tok:
                    picked.append(c["value"])
    seen: set[str] = set()
    return [p for p in picked if not (p in seen or seen.add(p))]


# ── linking (RFC 8628) ───────────────────────────────────────────────────────────────────
def _get_json(url: str, payload: dict | None = None, timeout: float = 20.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET",
        headers={"User-Agent": UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


GET_JSON: Callable[..., Any] = _get_json


def start_device_flow() -> dict:
    return GET_JSON(endpoint("AWNIX_DEVICE_CODE_URL"), {"client_id": "awnix-setup", "scope": "mcp"})


def poll_once(device_code: str) -> tuple[str, str]:
    """One poll: ('linked', token) | ('pending', '') | ('slow_down', '') | ('failed', why)."""
    url = device_token_url(endpoint("AWNIX_DEVICE_CODE_URL"))
    try:
        body = GET_JSON(url, {"device_code": device_code, "client_id": "awnix-setup"})
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            body = {}
    except Exception as exc:
        return "failed", f"network error while polling: {exc}"
    if body.get("access_token"):
        return "linked", body["access_token"]
    err = body.get("error")
    if err == "slow_down":
        return "slow_down", ""
    if err in ("authorization_pending", None):
        return "pending", ""
    return "failed", f"declined or expired ({err})"


def poll_device_flow(
    device_code: str, interval: int, expires_in: int, sleep=time.sleep, now=time.monotonic
) -> tuple[bool, str]:
    """Blocking poll for the tty. pending and slow_down are NORMAL, not failure."""
    deadline = now() + max(30, expires_in)
    wait = max(1, interval)
    while now() < deadline:
        sleep(wait)
        st, val = poll_once(device_code)
        if st == "linked":
            return True, val
        if st == "slow_down":
            wait += 5
            continue
        if st == "pending":
            continue
        return False, val
    return False, "timed out waiting for approval"


# ── steps.d ──────────────────────────────────────────────────────────────────────────────
STEP_KINDS = ("choice", "text", "secret", "toggle", "info", "reveal")
_CMD_KEYS = ("options_cmd", "current_cmd", "apply_cmd", "reveal_cmd")
_SHELLS = {
    "sh",
    "bash",
    "dash",
    "zsh",
    "ash",
    "ksh",
    "fish",
    "busybox",
    "env",
    "python",
    "python3",
    "python3.11",
    "perl",
}
_ALLOWED_KEYS = {
    "schema",
    "id",
    "title",
    "why",
    "kind",
    "options_cmd",
    "current_cmd",
    "apply_cmd",
    "reveal_cmd",
    "secret_dest",
    "required",
    "timeout_s",
    "variants",
    "after",
    "text",
    "options",
    "url",
    "_comment",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def validate_step(
    doc: Any,
    fname: str = "",
    check_path: bool = True,
    which_fn: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Every problem with one steps.d document. Empty list = valid."""
    which_fn = which_fn or which
    errs: list[str] = []
    where = fname or "<step>"
    if not isinstance(doc, dict):
        return [f"{where}: not a JSON object"]
    if doc.get("schema") != STEPS_SCHEMA:
        errs.append(f"{where}: schema must be {STEPS_SCHEMA}")
    sid = doc.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        errs.append(f"{where}: id must match {ID_RE.pattern}")
    elif fname and not re.match(rf"^\d\d-{re.escape(sid)}\.json$", Path(fname).name):
        errs.append(f"{where}: file must be named NN-{sid}.json")
    for k in ("title", "why"):
        if not isinstance(doc.get(k), str) or not doc.get(k).strip():
            errs.append(f"{where}: '{k}' is required text")
    kind = doc.get("kind")
    if kind not in STEP_KINDS:
        errs.append(f"{where}: kind must be one of {'|'.join(STEP_KINDS)}")
    unknown = set(doc) - _ALLOWED_KEYS
    if unknown:
        errs.append(f"{where}: unknown keys {sorted(unknown)}")
    if doc.get("required", False) is not False:
        errs.append(f"{where}: required must be false -- no step may block a box from booting")
    t = doc.get("timeout_s", 60)
    if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 7200:
        errs.append(f"{where}: timeout_s must be an int 1..7200")
    for key in _CMD_KEYS:
        if key not in doc:
            continue
        argv = doc[key]
        if isinstance(argv, str):
            errs.append(f"{where}: {key} is a shell STRING -- it must be an argv list")
            continue
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            errs.append(f"{where}: {key} must be a non-empty list of strings")
            continue
        exe = Path(argv[0]).name
        if exe in _SHELLS:
            errs.append(
                f"{where}: {key} runs '{argv[0]}' -- an interpreter/shell is a shell "
                "string in disguise; call the tool directly"
            )
        for a in argv:
            if "{value}" in a and a != "{value}":
                errs.append(
                    f"{where}: {key} embeds {{value}} inside '{a}' -- it must be one "
                    "whole argv element"
                )
        if key != "apply_cmd" and "{value}" in argv:
            errs.append(f"{where}: only apply_cmd may take {{value}}")
        if check_path and not which_fn(argv[0]):
            errs.append(f"{where}: {key}[0] '{argv[0]}' is not on PATH")
    if kind == "choice" and "options_cmd" not in doc and not isinstance(doc.get("options"), list):
        errs.append(f"{where}: a choice needs options_cmd or options[]")
    if kind == "reveal" and "reveal_cmd" not in doc:
        errs.append(f"{where}: a reveal needs reveal_cmd")
    if kind in ("choice", "text", "toggle") and "apply_cmd" not in doc:
        errs.append(f"{where}: kind {kind} needs apply_cmd")
    if kind == "secret":
        dest = doc.get("secret_dest")
        if not isinstance(dest, str) or not dest.startswith("/"):
            errs.append(f"{where}: a secret needs an absolute secret_dest")
        if "{value}" in (doc.get("apply_cmd") or []):
            errs.append(
                f"{where}: a secret must never be passed on argv (it shows in ps); "
                "it is written to secret_dest"
            )
    elif "secret_dest" in doc:
        errs.append(f"{where}: secret_dest only belongs on kind secret")
    for k in ("variants", "after"):
        if k in doc and (
            not isinstance(doc[k], list) or not all(isinstance(x, str) for x in doc[k])
        ):
            errs.append(f"{where}: {k} must be a list of strings")
    return errs


def load_steps(
    directory: Path | None = None, var: str | None = None, check_path: bool = True
) -> tuple[list[dict], list[str]]:
    """(valid steps for this variant in NN order, problems). Invalid files are SKIPPED at
    runtime (and fail --validate-steps at build time)."""
    d = directory or steps_dir()
    var = variant() if var is None else var
    steps: list[dict] = []
    problems: list[str] = []
    if not d.is_dir():
        return steps, problems
    for f in sorted(d.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(f"{f.name}: not JSON ({e})")
            continue
        errs = validate_step(doc, f.name, check_path=check_path)
        if errs:
            problems.extend(errs)
            continue
        vs = doc.get("variants")
        if vs and var and var not in vs:
            continue
        if vs and not var:
            continue
        doc = dict(doc)
        doc["_file"] = f.name
        steps.append(doc)
    return steps, problems


def substitute(argv: list[str], value: str) -> list[str]:
    """{value} is replaced ONLY where it is a whole element. `; rm -rf /` stays one arg."""
    return [value if a == "{value}" else a for a in argv]


def _host_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET, nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return socket.gethostname()


def step_options(step: dict) -> list[dict]:
    if isinstance(step.get("options"), list):
        return [
            o if isinstance(o, dict) else {"value": str(o), "label": str(o)}
            for o in step["options"]
        ]
    if "options_cmd" not in step:
        return []
    rc, out, _ = run(step["options_cmd"], timeout=min(60, step.get("timeout_s", 60)))
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return [{"value": ln.strip(), "label": ln.strip()} for ln in out.splitlines() if ln.strip()]
    out_rows = []
    for o in data if isinstance(data, list) else []:
        if isinstance(o, dict) and "value" in o:
            out_rows.append(
                {
                    "value": str(o["value"]),
                    "label": str(o.get("label", o["value"])),
                    "detail": str(o.get("detail", "")),
                    "active": bool(o.get("active", False)),
                }
            )
        elif isinstance(o, str):
            out_rows.append({"value": o, "label": o})
    return out_rows


def step_current(step: dict) -> str | None:
    if step.get("kind") == "secret" or "current_cmd" not in step:
        return None
    rc, out, _ = run(step["current_cmd"], timeout=min(30, step.get("timeout_s", 30)))
    return out.strip()[:500] if rc == 0 else None


def render_text(text: str) -> str:
    return (text or "").replace("<host>", _host_ip())


def run_step(step: dict, value: Any) -> tuple[bool, str]:
    """Apply one steps.d step. Returns (ok, message). Secrets never reach argv, state or
    logs: they are written to secret_dest (0600) and the message never echoes them."""
    kind = step.get("kind")
    timeout = int(step.get("timeout_s", 60))
    if kind == "info":
        return True, "noted"
    if kind == "reveal":
        rc, out, err = run(step["reveal_cmd"], timeout=timeout)
        return (rc == 0, out if rc == 0 else (err.strip() or f"exit {rc}"))
    if kind == "toggle":
        if isinstance(value, bool):
            sval = "1" if value else "0"
        elif str(value).lower() in ("1", "0", "on", "off", "true", "false", "yes", "no"):
            sval = "1" if str(value).lower() in ("1", "on", "true", "yes") else "0"
        else:
            return False, "a toggle takes on/off"
    else:
        sval = "" if value is None else str(value)
    if kind == "secret":
        if not sval.strip():
            return False, "empty value"
        if len(sval) > 65536:
            return False, "value too large"
        dest = Path(step["secret_dest"])
        root = os.environ.get("AWNIX_SECRET_ROOT")
        if root:  # self-test only: re-root absolute destinations
            dest = Path(root) / dest.as_posix().lstrip("/")
        try:
            _atomic_write(dest, sval.strip() + "\n", 0o600)
        except OSError as e:
            return False, f"could not write the secret ({e.__class__.__name__})"
        if "apply_cmd" not in step:
            return True, "stored"
        rc, _, err = run(step["apply_cmd"], timeout=timeout)
        # stderr of a verifier could quote its input; never relay it for a secret.
        return (
            rc == 0,
            "stored and applied"
            if rc == 0
            else f"stored, but {Path(step['apply_cmd'][0]).name} exited {rc}",
        )
    if kind == "choice":
        opts = {o["value"] for o in step_options(step)}
        if opts and sval not in opts:
            return False, "not one of the offered choices"
    if kind == "text" and len(sval) > 4096:
        return False, "value too large"
    rc, out, err = run(substitute(step["apply_cmd"], sval), timeout=timeout)
    msg = (out.strip() or "done") if rc == 0 else (err.strip() or out.strip() or f"exit {rc}")
    return rc == 0, msg[-400:]


# ── seed ─────────────────────────────────────────────────────────────────────────────────
def validate_seed(seed: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(seed, dict):
        return ["seed is not a JSON object"]
    if seed.get("schema") != SEED_SCHEMA:
        errs.append(f"schema must be {SEED_SCHEMA}")
    blob = json.dumps(seed)
    if looks_private(blob):
        errs.append("seed contains a PRIVATE key")
    for bad in ("password", "plaintext_password", "update_token", "license_file"):
        if bad in (seed.get("admin") or {}) or bad in seed:
            errs.append(f"field '{bad}' is not allowed (plaintext secret or dropped field)")
    adm = seed.get("admin")
    if adm is not None:
        if not isinstance(adm, dict) or not valid_username(str(adm.get("name", ""))):
            errs.append("admin.name is not a valid username")
        else:
            for k in adm.get("ssh_keys") or []:
                if not pubkey_shape_ok(k):
                    errs.append("admin.ssh_keys has a line that is not a public key")
            ph = adm.get("password_hash")
            if ph is not None and not str(ph).startswith("$"):
                errs.append("admin.password_hash must be a crypt(3) hash")
    if seed.get("hostname") is not None and not valid_hostname(str(seed["hostname"])):
        errs.append("hostname is invalid")
    if seed.get("wipe") and not seed.get("disk"):
        errs.append("wipe:true needs disk")
    if seed.get("setup") is not None and not isinstance(seed.get("setup"), dict):
        errs.append("setup must be an object of {step_id: value}")
    return errs


def apply_seed(path: Path | None = None) -> tuple[int, dict]:
    """Non-interactive first boot from /etc/awnix/seed.json (+ seed.d/ files).

    Returns (exit, report). 0 applied (or nothing to do), 1 a part failed. The seed file
    is DELETED after apply either way so its answers are not re-applied every boot.
    """
    path = path or seed_file()
    report: dict = {"seed": str(path), "applied": [], "failed": [], "finished": False}
    root = path if path.is_dir() else path.parent
    sfile = path / "awnix-seed.json" if path.is_dir() else path
    sdir = root if path.is_dir() else seed_dir()
    if not sfile.exists():
        report["detail"] = "no seed"
        return 0, report
    try:
        seed = json.loads(sfile.read_text(encoding="utf-8"))
    except Exception as e:
        report["failed"].append(f"seed unreadable: {e}")
        _discard_seed(sfile, path)
        return 1, report
    errs = validate_seed(seed)
    if errs:
        report["failed"].extend(errs)
        _discard_seed(sfile, path)
        return 1, report

    adm = seed.get("admin") or {}
    keys = list(adm.get("ssh_keys") or [])
    if adm.get("github_user"):
        gk, why = import_github_keys(adm["github_user"])
        keys += gk
        if why and not gk:
            report["failed"].append(f"github keys: {why}")
    if adm.get("name"):
        ok, res = create_admin(adm["name"], keys, password_hash=adm.get("password_hash"))
        if ok:
            report["applied"].append("admin")
            mark_step(
                "admin",
                "done",
                admin_user=res["admin_user"],
                ssh_key_fingerprints=res["ssh_key_fingerprints"],
            )
        else:
            report["failed"].append(f"admin: {res.get('error')}")
            mark_step("admin", "failed")
    if seed.get("hostname"):
        ok, msg = set_hostname(seed["hostname"])
        (report["applied"] if ok else report["failed"]).append(
            "hostname" if ok else f"hostname: {msg}"
        )
        mark_step("hostname", "done" if ok else "failed", hostname=seed["hostname"])

    lic = sdir / "license.lic"
    if lic.exists():
        body = lic.read_text(encoding="utf-8", errors="replace").strip()
        if body.startswith("AITHER1.") and len(body) <= 16384:
            _atomic_write(license_spool(), body + "\n", 0o600)
            report["applied"].append("license -> spool")
        else:
            report["failed"].append("license.lic is not an AITHER1 envelope")
    ans = sdir / "setup-answers.json"
    if ans.exists():
        try:
            json.loads(ans.read_text(encoding="utf-8"))
            _atomic_write(setup_answers_file(), ans.read_text(encoding="utf-8"), 0o600)
            report["applied"].append("setup-answers")
        except ValueError:
            report["failed"].append("setup-answers.json is not JSON")

    answers = seed.get("setup") or {}
    if answers:
        steps, _ = load_steps()
        by_id = {s["id"]: s for s in steps}
        for sid, val in answers.items():
            if sid in ("components",):
                ok, res = install_components(list(val or []))
                (report["applied"] if ok else report["failed"]).append(
                    "components" if ok else f"components: {res.get('error')}"
                )
                mark_step(
                    "components", "done" if ok else "failed", installed=res.get("installed", [])
                )
                continue
            st = by_id.get(sid)
            if not st:
                report["failed"].append(f"{sid}: no such step on this image")
                continue
            ok, msg = run_step(st, val)
            mark_step(sid, "done" if ok else "failed")
            (report["applied"] if ok else report["failed"]).append(
                sid if ok else f"{sid}: {msg if st.get('kind') != 'secret' else 'failed'}"
            )
    if seed.get("finish_setup"):
        finish_setup()
        report["finished"] = True
    _discard_seed(sfile, path)
    return (0 if not report["failed"] else 1), report


def _discard_seed(sfile: Path, path: Path) -> None:
    if path.is_dir():
        return  # a mounted seed volume is read-only and not ours to delete
    for f in (sfile, seed_dir() / "license.lic", seed_dir() / "setup-answers.json"):
        try:
            f.unlink(missing_ok=True)
        except OSError as e:
            _warn(f"could not delete seed file {f}", e)
    try:
        if seed_dir().is_dir():
            seed_dir().rmdir()
    except OSError as e:
        _warn(f"could not remove {seed_dir()}", e)


# ── finish ───────────────────────────────────────────────────────────────────────────────
def finish_setup(restart_console: bool = True, stop_tty: bool = False) -> dict:
    """Write setup.json v2 from progress, then flip awnix-console into console mode."""
    p = read_progress()
    lock = load_lock()
    st = {
        "version": STATE_SCHEMA,
        "hostname": p.get("hostname") or socket.gethostname(),
        "admin_user": p.get("admin_user") or detect_admin(),
        "ssh_key_fingerprints": p.get("ssh_key_fingerprints") or [],
        "core": baked_components(lock),
        "installed": p.get("installed") or [],
        "linked": bool(p.get("linked")),
        "link_host": endpoint("AWNIX_LINK_HOST") if p.get("linked") else None,
        "steps": p.get("steps") or {},
        "completed_at": _now(),
    }
    written = write_state(st)
    try:
        progress_file().unlink(missing_ok=True)
    except OSError as e:
        _warn("could not remove the progress file", e)
    if restart_console:
        run(["systemctl", "--no-block", "restart", "awnix-console.service"], timeout=30)
    if stop_tty:
        # Finished in the browser: the tty prompt is now answering a question nobody
        # needs to answer. Its ExecStopPost gives tty1 back to getty.
        run(["systemctl", "--no-block", "stop", "awnix-setup.service"], timeout=30)
    return written


def reset_setup() -> bool:
    """Remove the marker (and nothing else): the box asks again on next boot."""
    try:
        state_file().unlink()
        return True
    except FileNotFoundError:
        return False


def redacted_state() -> dict:
    st = read_state()
    st.pop("bearer", None)
    return st
