#!/usr/bin/python3.11
"""aither-setup -- AitherOS first boot: your user, your Aitherium sign-in, your agent.

Runs once on a fresh AitherOS host (awnix bootc image on bare metal/VM, or the awnix
WSL distro) and again whenever you ask (`--reconfigure`):

    1. user      create the human account (uid 1000, wheel, a password you type) OR
                 adopt a migrated home found under /var/mnt/fleet-src/home/<user>
    2. sign in   RFC 8628 device flow against idp.aitherium.com: the code and URL are
                 shown, the token lands in ~/.aither/auth.json (0600, adk's format)
                 and is never printed
    3. restore   awsettings pull, `adk license sync`, `adk keys pull` (vault key
                 NAMES only), and the awm scope this machine writes memory under
    4. choose    persistent agent daemon (systemd --user unit running `adk up`, with
                 linger), agent packs, capability packs (default customer-core),
                 inference backend (local / DGX-mesh / cloud), awsh at login
    5. marker    /var/lib/aither/setup.done -- the machine is set up; the WSL
                 [user] default and the first-login hook read it

Non-interactive: a seed file (`--seed PATH`, or /etc/aither/setup-seed.json picked up
by `--firstboot`) answers every question; see `--print-seed-example`.

WHAT IT REUSES, NOT REIMPLEMENTS
  * the IdP contract of AitherIdentity `/auth/device/code` + `/auth/device/token`
    (services/security/AitherIdentity.py): pending is HTTP 200
    {"status":"authorization_pending"}, terminal states are 400 {"detail":...}
  * adk's credential file (awdk/adk/auth.py AuthStore: version 1, profiles.portal),
    so `adk`, `awsettings` (profile.portal_token) and awsh see the login unchanged
  * `adk up` as the daemon verb (awnix/Containerfile's awdk-daemon unit),
    `adk install pack:<name>` for agent packs, `adk license sync`, `adk keys pull`
  * AITHER_LLM_BACKEND / AITHER_LLM_BASE_URL / AITHER_DGX_URL / AITHER_CLOUD_MODE,
    the env adk's Config.from_env already reads

Stdlib only and 3.10-compatible: it runs at first boot from /usr/bin on an image that
carries no AitherOS tree, and CI gates on 3.10.

    aither-setup                     interactive (only if not set up yet)
    aither-setup --reconfigure       run again
    aither-setup --seed FILE         non-interactive
    aither-setup --status [--json]   what was chosen (--json adds `caps` for GUIs)
    aither-setup --probe --json      read-only facts a setup GUI shows before asking
    aither-setup --seed FILE --json-progress   NDJSON step events on stdout (the desk)
    aither-setup --renew-token FILE  replace the stored sign-in (FILE may be /dev/stdin)
    aither-setup --self-test         offline proof of the pure logic
    aither-setup --demote-service-accounts --assert-no-human-users   (image build)
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

VERSION = 1
#: What this build can do, for a GUI front end (`--status --json` / `--probe --json`).
#: The desk refuses to half-run against an image that lacks one it needs.
CAPS = ("seed", "progress_json", "probe", "sudo_mode", "login_password", "renew_token",
        "needs_restart", "token_file_shred", "cloud_key_gate")
SUDO_MODES = ("password", "nopasswd")
LOGIN_PASSWORD_MODES = ("keep", "hash", "locked")
#: Where the desk stages the seed and the one-shot token (tmpfs, root-only).
RUN_DIR = "/run/aither-setup"
#: The desk touches this before it applies a seed; aither-setup-wait (the WSL [oobe]
#: command) then tells a terminal to finish in the window instead of prompting.
DESK_PRESENT = f"{RUN_DIR}/desk-present"

IDP_URL = os.environ.get("AITHER_IDP_URL", "https://idp.aitherium.com/identity")
MARKER = "/var/lib/aither/setup.done"
LOCK = "/run/aither-setup.lock"
DEFAULT_SEED = "/etc/aither/setup-seed.json"
CATALOGUE_PATHS = (
    os.environ.get("AITHER_SETUP_CATALOGUE", ""),
    "/usr/share/aither/aither-setup.json",
    str(Path(__file__).resolve().parent / "aither-setup.json"),
)
#: Where the migration attach unit mounts the source distro's disk (aither-attach-
#: fleet-data.sh: AITHER_FLEET_MOUNT_POINT, default /mnt/fleet-src; /mnt is /var/mnt
#: on bootc). A home found here is a person's data; the setup offers to adopt it.
MIGRATED_HOME_ROOTS = ("/var/mnt/fleet-src/home", "/mnt/fleet-src/home")
#: Accounts images create for SERVICES. One of them sitting at uid >= 1000 steals the
#: first human's uid (the live awnix had `bonsai` at 1000 while the migrated home was
#: owned by uid 1000). The setup renumbers them into the system range, never deletes.
SERVICE_ACCOUNTS = ("bonsai", "runner", "awnix", "aither")
HUMAN_UID_MIN, HUMAN_UID_MAX = 1000, 60000
FIRST_HUMAN_UID = 1000
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
INFERENCE_CHOICES = ("local", "mesh", "cloud")
AGENT_UNIT = "aither-agent.service"
AWSH_BEGIN, AWSH_END = "# >>> aither-setup awsh >>>", "# <<< aither-setup awsh <<<"
ENV_BEGIN, ENV_END = "# >>> aither-setup env >>>", "# <<< aither-setup env <<<"


class SetupError(RuntimeError):
    """A step that cannot continue; the message says what to do."""


class _Progress:
    """NDJSON step events for a GUI (`--json-progress`). When on, stdout carries ONLY
    events and human text goes to stderr, so the desk can parse every stdout line."""

    on = False

    @classmethod
    def emit(cls, event: str, **fields: Any) -> None:
        if cls.on:
            sys.stdout.write(json.dumps({"event": event, **fields}, default=str) + "\n")
            sys.stdout.flush()


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr if _Progress.on else sys.stdout, flush=True)


# ── the host, injectable so every step is testable against a fake root ─────────────
Runner = Callable[[list, "str | None"], "tuple[int, str]"]


def _real_run(argv: list, input_text: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except FileNotFoundError as exc:
        return 127, f"{argv[0]}: not found ({exc})"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def detect_wsl() -> bool:
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def open_in_windows_browser(url: str) -> bool:
    """On WSL, open the device-code URL in the Windows default browser via interop.

    Best effort: returns False (the printed URL still works) when interop is off."""
    if not url.startswith("https://"):
        return False
    for exe in ("/mnt/c/Windows/explorer.exe", shutil.which("explorer.exe") or ""):
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,  # noqa: S603
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError:
                continue
    return False


class Host:
    """Filesystem + command surface. `root="/"` is the real machine; anything else is
    a sandbox in which commands go to `runner` and ownership changes are recorded."""

    def __init__(self, root: str | Path = "/", runner: Runner | None = None,
                 is_wsl: bool | None = None, hostname: str | None = None) -> None:
        self.root = Path(root)
        self.real = str(root) == "/"
        self._runner = runner or _real_run
        self.is_wsl = detect_wsl() if is_wsl is None else is_wsl
        self.hostname = hostname or socket.gethostname()
        self.chowns: list[tuple[str, int, int]] = []
        #: Why a distro restart is needed for the result to hold (empty = not needed).
        self.needs_restart: list[str] = []

    def p(self, path: str | Path) -> Path:
        return self.root / str(path).lstrip("/")

    def run(self, argv: list, input_text: str | None = None,
            as_user: "Account | None" = None) -> tuple[int, str]:
        argv = [str(a) for a in argv]
        if as_user is not None:
            argv = ["runuser", "-u", as_user.name, "--", "env", f"HOME={as_user.home}",
                    f"USER={as_user.name}", *argv]
        return self._runner(argv, input_text)

    def which(self, name: str) -> str | None:
        for d in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin"):
            if self.p(f"{d}/{name}").exists():
                return f"{d}/{name}"
        return shutil.which(name) if self.real else None

    def chown(self, path: str | Path, uid: int, gid: int) -> None:
        self.chowns.append((str(path), uid, gid))
        if self.real and hasattr(os, "chown"):
            os.chown(self.p(path), uid, gid)

    def owner_of(self, path: str | Path) -> tuple[int, int]:
        st = self.p(path).stat()
        return st.st_uid, st.st_gid

    def realpath(self, path: str) -> str:
        return os.path.realpath(path) if self.real else path

    def write(self, path: str | Path, text: str, mode: int = 0o644,
              owner: "Account | None" = None) -> None:
        """Create-with-mode (no window where a secret is world-readable), then rename."""
        target = self.p(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".aither-setup.tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, target)
        if owner is not None:
            self.chown(path, owner.uid, owner.gid)

    def mkdir(self, path: str | Path, mode: int = 0o755,
              owner: "Account | None" = None) -> None:
        self.p(path).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.p(path), mode)
        if owner is not None:
            self.chown(path, owner.uid, owner.gid)


# ── accounts ────────────────────────────────────────────────────────────────────────
@dataclass
class Account:
    name: str
    uid: int
    gid: int
    home: str
    shell: str = "/bin/bash"


def read_passwd(host: Host) -> list[Account]:
    out = []
    try:
        lines = host.p("/etc/passwd").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        # Loud: an empty passwd table would make uid 1000 look free.
        raise SetupError(f"cannot read /etc/passwd: {exc}") from None
    for ln in lines:
        f = ln.split(":")
        if len(f) >= 7 and f[2].isdigit() and f[3].isdigit():
            out.append(Account(f[0], int(f[2]), int(f[3]), f[5], f[6]))
    return out


def read_groups(host: Host) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        lines = host.p("/etc/group").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        # Loud: an empty group table would make every gid look free.
        raise SetupError(f"cannot read /etc/group: {exc}") from None
    for ln in lines:
        f = ln.split(":")
        if len(f) >= 3 and f[2].isdigit():
            out[f[0]] = int(f[2])
    return out


def find_account(host: Host, name: str) -> Account | None:
    return next((a for a in read_passwd(host) if a.name == name), None)


def account_has_password(host: Host, name: str) -> bool | None:
    """True = /etc/shadow holds a usable crypt hash for `name`; False = locked or empty
    ('!', '*', '!!', ''); None = shadow unreadable or no entry (could not judge).
    "Keep the current password" with password sudo on an account that has NONE would
    leave the person unable to sudo while Verify shows sudo "asks for a password"."""
    try:
        lines = host.p("/etc/shadow").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for ln in lines:
        f = ln.split(":")
        if len(f) >= 2 and f[0] == name:
            h = f[1]
            return bool(h) and not h.startswith(("!", "*"))
    return None


def human_accounts(host: Host) -> list[Account]:
    return [a for a in read_passwd(host) if HUMAN_UID_MIN <= a.uid < HUMAN_UID_MAX]


def free_system_id(taken: set, lo: int = 201, hi: int = 999) -> int:
    """Highest free id in the system range -- the end `useradd --system` allocates from."""
    for i in range(hi, lo - 1, -1):
        if i not in taken:
            return i
    raise SetupError("no free system uid/gid in 201..999")


def demote_service_accounts(host: Host, names: tuple = SERVICE_ACCOUNTS) -> list[tuple]:
    """Renumber known SERVICE accounts out of the human range. Never deletes one.

    Returns [(name, old_uid, new_uid)]. Files the account owns in its home, /opt/<name>
    and /var/lib/<name> follow it (usermod -u moves the home; the rest by find).
    """
    moved = []
    groups = read_groups(host)
    taken = {a.uid for a in read_passwd(host)} | set(groups.values())
    for acct in read_passwd(host):
        if acct.name not in names or not (HUMAN_UID_MIN <= acct.uid < HUMAN_UID_MAX):
            continue
        new = free_system_id(taken)
        taken.add(new)
        restart = stop_units_of(host, acct)
        rc, out = host.run(["usermod", "-u", str(new), acct.name])
        if rc != 0:
            raise SetupError(f"usermod -u {new} {acct.name} failed: {out.strip()[:200]}")
        own_group = groups.get(acct.name) == acct.gid
        if own_group:
            rc, out = host.run(["groupmod", "-g", str(new), acct.name])
            if rc != 0:
                raise SetupError(f"groupmod -g {new} {acct.name} failed: {out.strip()[:200]}")
        for d in (acct.home, f"/opt/{acct.name}", f"/var/lib/{acct.name}"):
            if d and d != "/" and host.p(d).exists():
                host.run(["find", d, "-xdev", "-uid", str(acct.uid),
                          "-exec", "chown", "-h", str(new), "{}", "+"])
                if own_group:
                    host.run(["find", d, "-xdev", "-gid", str(acct.gid),
                              "-exec", "chgrp", "-h", str(new), "{}", "+"])
        moved.append((acct.name, acct.uid, new))
        for unit in restart:
            host.run(["systemctl", "start", unit])
    return moved


_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:\\-]+\.(service|scope)$")


def stop_units_of(host: Host, acct: Account) -> list[str]:
    """Stop every systemd unit running a process as `acct`, so `usermod -u` neither
    refuses ("currently used by process") nor leaves a process owning stale files.

    Returns the SYSTEM services to start again once the uid has moved (their unit
    files name the user, not the uid). The user's own manager is stopped outright;
    it comes back with linger/login. On an image build nothing runs: a no-op.
    """
    rc, out = host.run(["ps", "-o", "unit=", "-u", str(acct.uid)])
    if rc != 0:
        return []
    units: list[str] = []
    for u in (ln.strip() for ln in out.splitlines()):
        if u and _UNIT_RE.match(u) and u not in units:
            units.append(u)
    if not units:
        return []
    user_mgr = f"user@{acct.uid}.service"
    restart = [u for u in units if u.endswith(".service") and u != user_mgr]
    for u in units:
        host.run(["systemctl", "stop", u])
    host.run(["systemctl", "stop", user_mgr])
    return restart


def migrated_homes(host: Host) -> list[tuple[str, int, int, str]]:
    """(name, uid, gid, path) for each home under a migrated source disk."""
    seen, out = set(), []
    for root in MIGRATED_HOME_ROOTS:
        d = host.p(root)
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if not child.is_dir() or not USERNAME_RE.match(child.name):
                continue
            if child.name in seen:
                continue   # /mnt -> /var/mnt on bootc: the same dir twice
            seen.add(child.name)
            uid, gid = host.owner_of(f"{root}/{child.name}")
            out.append((child.name, uid, gid, f"{root}/{child.name}"))
    return out


def systemd_escape_path(path: str) -> str:
    """`systemd-escape --path` for the character set a validated username allows."""
    parts = [p for p in path.strip("/").split("/") if p]
    esc = []
    for part in parts:
        s = "".join(c if (c.isalnum() or c in "_.") else f"\\x{ord(c):02x}" for c in part)
        if s.startswith("."):
            s = "\\x2e" + s[1:]
        esc.append(s)
    return "-".join(esc) or "-"


def adopt_mount_unit(source: str, where: str) -> str:
    return (
        "[Unit]\n"
        "# Written by aither-setup: the migrated home, bind-mounted where the account\n"
        "# expects it. Ordered after the attach unit that mounts the source disk.\n"
        f"Description=Adopted home {where} from {source}\n"
        "After=aither-attach-fleet-data.service local-fs.target\n"
        "Requires=aither-attach-fleet-data.service\n"
        "Before=systemd-user-sessions.service\n"
        f"ConditionPathIsDirectory={source}\n\n"
        "[Mount]\n"
        f"What={source}\n"
        f"Where={where}\n"
        "Type=none\n"
        "Options=bind\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


@dataclass
class UserPlan:
    name: str
    uid: int = FIRST_HUMAN_UID
    adopt_from: str | None = None
    adopt_gid: int | None = None
    password: str | None = field(default=None, repr=False)        # never persisted
    password_hash: str | None = field(default=None, repr=False)   # seed only
    ssh_keys: list = field(default_factory=list)
    #: password = sudo asks for the account password (default); nopasswd = the
    #: person ticked "Passwordless sudo" in the wizard. Recorded in setup.done.
    sudo: str = "password"
    #: keep = leave an existing account's password alone; hash = set password_hash;
    #: locked = no Linux password at all (WSL logs you in; only with sudo=nopasswd).
    login_password: str = ""


def ensure_wheel_sudo(host: Host) -> str:
    """wheel gets sudo WITH a password (distro policy). NOPASSWD is never written
    here; it is a per-user opt-in (`ensure_user_sudo`, seed `user.sudo: nopasswd`)."""
    pat = re.compile(r"^\s*%wheel\s+ALL\s*=\s*\(ALL(:ALL)?\)\s+ALL\s*$", re.M)
    dropins = host.p("/etc/sudoers.d")
    files = [host.p("/etc/sudoers")] + (sorted(dropins.glob("*")) if dropins.is_dir() else [])
    for f in files:
        with contextlib.suppress(OSError):
            if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                return "wheel already has password sudo"
    host.write("/etc/sudoers.d/10-aither-wheel",
               "# aither-setup: wheel may sudo, with the user's own password.\n"
               "%wheel ALL=(ALL) ALL\n", mode=0o440)
    rc, out = host.run(["visudo", "-cf", "/etc/sudoers.d/10-aither-wheel"])
    if rc not in (0, 127):
        _unlink(host.p("/etc/sudoers.d/10-aither-wheel"))
        raise SetupError(f"visudo rejected the wheel rule: {out.strip()[:200]}")
    return "wrote /etc/sudoers.d/10-aither-wheel (%wheel ALL=(ALL) ALL)"


def _unlink(path: Path) -> None:
    """Remove a 0440 file (sudoers). Root on Linux ignores the mode; the sandbox
    on Windows does not, so make it writable first."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    path.unlink()


def user_sudoers_path(name: str) -> str:
    return f"/etc/sudoers.d/90-aither-{name}"


def ensure_user_sudo(host: Host, name: str, mode: str) -> str:
    """Per-user sudo mode. `nopasswd` only when the seed opts in (the wizard's unticked
    box); validated by `visudo -cf` BEFORE it is renamed into place, so a bad rule can
    never lock sudo. `password` removes a previous opt-in (reconfigure back)."""
    if mode not in SUDO_MODES:
        raise SetupError(f"sudo mode {mode!r} is not one of {SUDO_MODES}")
    path = user_sudoers_path(name)
    if mode == "password":
        target = host.p(path)
        if target.exists():
            _unlink(target)
            return f"sudo asks for {name}'s password (removed {path})"
        return f"sudo asks for {name}'s password"
    staged = f"/etc/sudoers.d/.90-aither-{name}.pending"   # '.' in a name: sudo skips it
    host.write(staged, "# aither-setup: the person opted in to passwordless sudo in the "
                       "setup wizard.\n"
                       f"{name} ALL=(ALL) NOPASSWD: ALL\n", mode=0o440)
    rc, out = host.run(["visudo", "-cf", staged])
    if rc != 0:
        _unlink(host.p(staged))
        raise SetupError(f"visudo rejected the NOPASSWD rule (rc {rc}): {out.strip()[:200]}")
    os.replace(host.p(staged), host.p(path))
    return f"passwordless sudo for {name} ({path}, opted in)"


def adopt_backing_mount(adopt_from: str) -> str:
    """The migrated-disk mount point a home under MIGRATED_HOME_ROOTS lives on."""
    for root in MIGRATED_HOME_ROOTS:
        if adopt_from.startswith(root + "/"):
            return root[: -len("/home")]
    return ""


def create_or_adopt_user(host: Host, plan: UserPlan) -> tuple[Account, list[str]]:
    if not USERNAME_RE.match(plan.name):
        raise SetupError(f"invalid user name {plan.name!r}: use [a-z_][a-z0-9_-]*, max 32")
    notes: list[str] = []
    if plan.adopt_from:
        # Adopting while the data disk is NOT attached would bind an empty directory
        # on the root fs over the home, and the person would log in to nothing.
        mnt = adopt_backing_mount(plan.adopt_from)
        if mnt:
            rc, _out = host.run(["mountpoint", "-q", mnt])
            if rc != 0:
                raise SetupError(
                    f"{mnt} is not mounted, so {plan.adopt_from} is not your migrated home "
                    "yet. Attach the fleet data disk (task AitherOS-AttachFleetData) and "
                    "run the setup again.")
        if not host.p(plan.adopt_from).is_dir():
            raise SetupError(f"adopt_home {plan.adopt_from} does not exist")
    existing = find_account(host, plan.name)
    if existing is not None:
        notes.append(f"user {plan.name} already exists (uid {existing.uid}); reusing it")
        acct = existing
    else:
        holder = next((a for a in read_passwd(host) if a.uid == plan.uid), None)
        if holder is not None:
            if holder.name not in SERVICE_ACCOUNTS:
                raise SetupError(
                    f"uid {plan.uid} already belongs to {holder.name!r}. Choose another uid, "
                    f"or adopt that account instead of creating {plan.name!r}.")
            for n, old, new in demote_service_accounts(host, (holder.name,)):
                notes.append(f"moved service account {n} from uid {old} to {new}")
        groups = read_groups(host)
        gid = plan.adopt_gid if plan.adopt_gid is not None else plan.uid
        if plan.name not in groups:
            if gid in groups.values():
                raise SetupError(f"gid {gid} is taken; free it or pick another uid")
            rc, out = host.run(["groupadd", "-g", str(gid), plan.name])
            if rc != 0:
                raise SetupError(f"groupadd failed: {out.strip()[:200]}")
        home = f"/home/{plan.name}"
        argv = ["useradd", "-u", str(plan.uid), "-g", str(gid), "-G", "wheel",
                "-s", "/bin/bash", "-d", home, "-c", plan.name]
        argv += ["-M"] if plan.adopt_from else ["-m"]
        rc, out = host.run([*argv, plan.name])
        if rc != 0:
            raise SetupError(f"useradd failed: {out.strip()[:200]}")
        acct = Account(plan.name, plan.uid, gid, home)
        notes.append(f"created user {plan.name} (uid {plan.uid}, groups wheel)")
    if plan.adopt_from:
        where = host.realpath(acct.home)
        unit = systemd_escape_path(where) + ".mount"
        host.mkdir(where, 0o755)
        host.write(f"/etc/systemd/system/{unit}", adopt_mount_unit(plan.adopt_from, where))
        wants = host.p("/etc/systemd/system/multi-user.target.wants")
        wants.mkdir(parents=True, exist_ok=True)
        link = wants / unit
        if not link.exists() and not link.is_symlink():
            try:
                link.symlink_to(f"/etc/systemd/system/{unit}")
            except OSError:   # sandbox on a filesystem without symlinks
                link.write_text(f"/etc/systemd/system/{unit}\n", encoding="utf-8")
        host.run(["systemctl", "daemon-reload"])
        rc, out = host.run(["systemctl", "start", unit])
        if rc != 0:
            host.needs_restart.append(f"{unit} did not start live")
        notes.append(f"adopted {plan.adopt_from} as {where} via {unit}"
                     + ("" if rc == 0 else f" (start deferred to next boot: {out.strip()[:120]})"))
    if plan.login_password == "locked":
        rc, out = host.run(["passwd", "-l", acct.name])
        if rc != 0:
            raise SetupError(f"locking the password failed: {out.strip()[:200]}")
        notes.append("no Linux password (locked); WSL logs you in")
    elif plan.login_password == "keep" and existing is not None:
        if plan.sudo == "password" and account_has_password(host, acct.name) is False:
            raise SetupError(f"{acct.name} has no Linux password to keep, so sudo could "
                             "never succeed: set a password, or choose passwordless sudo")
        notes.append("password kept")
    elif plan.login_password == "keep" and plan.sudo == "password":
        # A NEW account has no password to keep: with password sudo it could never
        # sudo. The desk offers "keep" only for an account that already exists.
        raise SetupError(f"{acct.name} is a new account: set a password (sudo asks for it) "
                         "or choose passwordless sudo")
    elif plan.password:
        rc, out = host.run(["chpasswd"], input_text=f"{acct.name}:{plan.password}\n")
        if rc != 0:
            raise SetupError(f"setting the password failed: {out.strip()[:200]}")
        notes.append("password set (sudo asks for it)")
    elif plan.password_hash:
        rc, out = host.run(["chpasswd", "-e"], input_text=f"{acct.name}:{plan.password_hash}\n")
        if rc != 0:
            raise SetupError(f"setting the password hash failed: {out.strip()[:200]}")
        notes.append("password hash set from seed")
    elif existing is None:
        notes.append("no password set: the account is locked for password login; "
                     "use an SSH key, or `wsl -u root` then `passwd " + acct.name + "`")
    if plan.ssh_keys:
        host.mkdir(f"{acct.home}/.ssh", 0o700, owner=acct)
        host.write(f"{acct.home}/.ssh/authorized_keys", "\n".join(plan.ssh_keys) + "\n",
                   mode=0o600, owner=acct)
        notes.append(f"{len(plan.ssh_keys)} SSH key(s) authorised")
    notes.append(ensure_wheel_sudo(host))
    notes.append(ensure_user_sudo(host, acct.name, plan.sudo))
    return acct, notes


# ── sign in: RFC 8628 device flow against AitherIdentity ────────────────────────────
def _check_idp_url(base: str) -> str:
    u = urllib.parse.urlparse(base)
    if u.scheme == "https":
        return base.rstrip("/")
    host = u.hostname or ""
    loopback = host == "localhost"
    with contextlib.suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    if u.scheme == "http" and loopback:
        return base.rstrip("/")
    raise SetupError(f"refusing IdP URL {base!r}: a bearer is only fetched over https")


def _post_json(url: str, payload: dict, timeout: float = 20.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "aither-setup"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 -- scheme checked
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return e.code, {}


class DeviceLogin:
    def __init__(self, base: str = IDP_URL, post: Callable = _post_json,
                 sleep: Callable = time.sleep, now: Callable = time.monotonic) -> None:
        self.base = _check_idp_url(base)
        self._post, self._sleep, self._now = post, sleep, now

    def start(self, client_name: str, email: str = "") -> dict:
        payload: dict[str, Any] = {"client_name": client_name[:80]}
        if email:
            payload["email"] = email   # the IdP emails a one-tap approve link
        status, body = self._post(f"{self.base}/auth/device/code", payload)
        if status != 200 or not body.get("device_code") or not body.get("user_code"):
            raise SetupError(f"device-code start failed at {self.base}: HTTP {status} "
                             f"{body.get('detail') or ''}".rstrip())
        return body

    def poll(self, challenge: dict) -> dict:
        """Returns the token response. Accepts Identity's 200-pending AND RFC 400-pending."""
        interval = max(1, int(challenge.get("interval") or 5))
        deadline = self._now() + max(30, int(challenge.get("expires_in") or 900))
        while self._now() < deadline:
            self._sleep(interval)
            try:
                status, body = self._post(f"{self.base}/auth/device/token",
                                          {"device_code": challenge["device_code"]})
            except (urllib.error.URLError, OSError) as exc:
                raise SetupError(f"network error while waiting for approval: {exc}") from None
            if status == 200 and body.get("access_token"):
                return body
            state = body.get("error") or body.get("status") or body.get("detail") or ""
            if state == "authorization_pending" or (status == 200 and state == "pending"):
                interval = max(interval, int(body.get("interval") or interval))
                continue
            if state == "slow_down":
                interval += 5
                continue
            raise SetupError(f"sign-in did not complete: HTTP {status} {state}".rstrip())
        raise SetupError("sign-in timed out waiting for approval")


def store_login(host: Host, acct: Account, body: dict, endpoint: str) -> dict:
    """Write the token into adk's ~/.aither/auth.json (profile `portal`), 0600.

    Returns the NON-secret facts (user, tier, packs) for the summary."""
    user = body.get("user") or {}
    profile = {
        "endpoint": endpoint,
        "genesis_url": user.get("genesis_url", ""),
        "token_type": body.get("token_type", "bearer"),
        "access_token": body["access_token"],
        "expires_at": body.get("expires_at", ""),
        "user": user,
    }
    path = f"{acct.home}/.aither/auth.json"
    store: dict = {}
    with contextlib.suppress(OSError, ValueError):
        store = json.loads(host.p(path).read_text(encoding="utf-8"))
    if not isinstance(store, dict) or store.get("version") != 1:
        store = {"version": 1, "profiles": {}}
    store.setdefault("profiles", {})["portal"] = profile
    store["active_profile"] = "portal"
    host.mkdir(f"{acct.home}/.aither", 0o700, owner=acct)
    host.write(path, json.dumps(store, indent=2) + "\n", mode=0o600, owner=acct)
    return {"username": user.get("username", ""), "email": user.get("email", ""),
            "tenant_slug": user.get("tenant_slug", ""), "tier": body.get("tier", ""),
            "entitled_packs": list(body.get("packs") or [])}


def shred_file(host: Host, path: str) -> bool:
    """Overwrite then unlink a one-shot secret file. True when it is gone."""
    p = host.p(path)
    try:
        size = p.stat().st_size
        with open(p, "r+b") as fh:
            fh.write(b"\0" * size)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        p.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        with contextlib.suppress(OSError):
            p.unlink()
    return not p.exists()


def parse_key_names(text: str) -> list[str]:
    """Provider NAMES with a key in the vault, from `adk keys pull` ("[+] OpenAI").
    Never values: the command prints none, and this reads only the marked lines."""
    out: list[str] = []
    for ln in text.splitlines():
        m = re.match(r"^\s*\[\+\]\s+(.+?)(\s+\(.*\))?\s*$", ln)
        if m and m.group(1).strip() not in out:
            out.append(m.group(1).strip())
    return out


def mesh_url_guess(host: Host) -> str:
    """The DGX/mesh endpoint from the environment the image or the desk provides
    (AITHER_DGX_URL / AITHER_MESH_URL in /etc/aither/mesh.env). Empty = unknown."""
    for k in ("AITHER_MESH_URL", "AITHER_DGX_URL"):
        v = os.environ.get(k, "") if host.real else ""
        if re.match(r"^https?://[^\s'\"]+$", v):
            return v
    with contextlib.suppress(OSError):
        for ln in host.p("/etc/aither/mesh.env").read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition("=")
            v = v.strip().strip('"')
            if k.strip() in ("AITHER_MESH_URL", "AITHER_DGX_URL") \
                    and re.match(r"^https?://[^\s'\"]+$", v):
                return v
    return ""


# ── restore, packs, daemon, inference, awsh ─────────────────────────────────────────
@dataclass
class StepResult:
    step: str
    ok: bool | None          # None = could not judge / skipped with a reason
    note: str


class Results(list):
    """The step list; every append is also a progress event (a no-op when off)."""

    def append(self, r: StepResult) -> None:   # type: ignore[override]
        super().append(r)
        _Progress.emit("step", step=r.step, ok=r.ok, note=r.note)

    def extend(self, rs) -> None:   # type: ignore[override]
        for r in rs:
            self.append(r)

    def __iadd__(self, rs):   # type: ignore[override]
        self.extend(rs)
        return self


def awm_scope(identity: dict, hostname: str) -> str:
    def clean(s: str, fallback: str) -> str:
        s = re.sub(r"[^a-z0-9_-]", "-", (s or "").lower()).strip("-")
        return s or fallback
    return ":".join((clean(identity.get("tenant_slug", ""), "aitherium"),
                     clean(identity.get("username", ""), "me"),
                     clean(hostname.split(".")[0], "host")))


def restore(host: Host, acct: Account, identity: dict, scope: str,
            facts: dict | None = None) -> list[StepResult]:
    out: list[StepResult] = []
    facts = facts if facts is not None else {}
    tools = [("awsettings pull", ["awsettings", "--quiet", "pull"], "settings pulled"),
             ("adk license sync", ["adk", "license", "sync"], "license synced"),
             ("adk keys pull", ["adk", "keys", "pull"],
              "vault key names listed (values stay in the vault)")]
    for label, argv, ok_note in tools:
        tool = argv[0]
        if not host.which(tool):
            out.append(StepResult(label, None, f"{tool} is not installed on this image"))
            continue
        rc, text = host.run(argv, as_user=acct)
        if rc == 0 and label == "adk keys pull":
            facts["provider_keys"] = parse_key_names(text)
        if rc == 0:
            out.append(StepResult(label, True, ok_note))
        elif rc == 2:
            out.append(StepResult(label, None, "could not reach the service; local state kept"))
        else:
            out.append(StepResult(label, False, f"exit {rc}: {text.strip()[-160:]}"))
    if host.which("awm"):
        rc, text = host.run(["awm", "recall", "--scope", scope, "--limit", "3"], as_user=acct)
        out.append(StepResult("awm scope", rc == 0,
                              f"{scope} ({'readable' if rc == 0 else text.strip()[-120:]})"))
    else:
        out.append(StepResult("awm scope", None, f"{scope} recorded; awm not installed"))
    return out


def load_catalogue() -> dict:
    for p in CATALOGUE_PATHS:
        if p and Path(p).is_file():
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            if not doc.get("capability_profiles"):
                raise SetupError(f"{p} lists no capability profiles")
            return doc
    raise SetupError("aither-setup.json (capability profiles) is missing from the image; "
                     "refusing to offer a built-in list that would drift")


def resolve_packs(catalogue: dict, profiles: list) -> list[str]:
    known = catalogue["capability_profiles"]
    bad = [p for p in profiles if p not in known]
    if bad:
        raise SetupError(f"unknown capability profile(s): {', '.join(bad)}; "
                         f"known: {', '.join(sorted(known))}")
    packs: list[str] = []
    for p in profiles:
        for pid in known[p]["packs"]:
            if pid not in packs:
                packs.append(pid)
    return packs


def bundled_agent_packs(host: Host) -> list[str]:
    """Agent packs bundled with the installed adk (the set `adk install pack:` accepts)."""
    py = host.which("python3.11") or host.which("python3")
    if not py or not host.which("adk"):
        return []
    rc, out = host.run([py, "-c", "import adk, pathlib; p = pathlib.Path(adk.__file__)"
                        ".parent / 'packs'; print('\\n'.join(sorted(d.name for d in "
                        "p.iterdir() if (d / 'agent.yaml').is_file())))"])
    return [ln.strip() for ln in out.splitlines() if rc == 0 and USERNAME_RE.match(ln.strip())]


def apply_packs(host: Host, acct: Account, catalogue: dict, profiles: list,
                agent_packs: list, identity: dict) -> list[StepResult]:
    out = []
    packs = resolve_packs(catalogue, profiles)
    entitled = identity.get("entitled_packs") or []
    doc = {"version": 1, "source": "aither-setup", "capability_profiles": profiles,
           "packs": packs, "agent_packs": agent_packs, "entitled_packs": entitled}
    host.mkdir(f"{acct.home}/.aither", 0o700, owner=acct)
    host.write(f"{acct.home}/.aither/packs.json", json.dumps(doc, indent=2) + "\n",
               owner=acct)
    out.append(StepResult("capability packs", True,
                          f"{', '.join(profiles)} -> {len(packs)} pack(s) in ~/.aither/packs.json"
                          " (entitlement enforced by adk.licensing)"))
    for name in agent_packs:
        rc, text = host.run(["adk", "install", f"pack:{name}"], as_user=acct)
        out.append(StepResult(f"agent pack {name}", rc == 0,
                              "installed" if rc == 0 else f"exit {rc}: {text.strip()[-160:]}"))
    return out


def agent_env(inference: dict, scope: str, idp: str) -> str:
    env = {"AITHERIDENTITY_URL": idp, "AWM_SCOPE": scope}
    kind = inference.get("backend", "local")
    if kind == "local":
        env["AITHER_LLM_BACKEND"] = "auto"
    elif kind == "mesh":
        url = inference.get("url", "")
        if not re.match(r"^https?://[^\s'\"]+$", url):
            raise SetupError("mesh inference needs an http(s) URL (the DGX/mesh endpoint)")
        env.update({"AITHER_LLM_BACKEND": "openai", "AITHER_LLM_BASE_URL": url,
                    "AITHER_DGX_URL": url})
    elif kind == "cloud":
        env.update({"AITHER_LLM_BACKEND": "auto", "AITHER_CLOUD_MODE": "cloud"})
    else:
        raise SetupError(f"unknown inference backend {kind!r}; choose {INFERENCE_CHOICES}")
    lines = ["# Written by aither-setup. Read by aither-agent.service and login shells."]
    lines += [f"{k}={v}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def agent_unit(adk: str, identity: str, offline: bool) -> str:
    argv = [adk, "up", "--foreground", "--yes", "--no-persist", "--identity", identity]
    if offline:
        argv.append("--offline")
    return (
        "[Unit]\n"
        "Description=Aither persistent agent (adk up), written by aither-setup\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "EnvironmentFile=-%h/.config/aither/agent.env\n"
        f"ExecStart={' '.join(argv)}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def enable_daemon(host: Host, acct: Account, identity: str, offline: bool) -> StepResult:
    adk = host.which("adk")
    if not adk:
        return StepResult("agent daemon", False, "adk is not installed; no unit written")
    unit_dir = f"{acct.home}/.config/systemd/user"
    for d in (f"{acct.home}/.config", unit_dir, f"{unit_dir}/default.target.wants"):
        host.mkdir(d, 0o755, owner=acct)
    host.write(f"{unit_dir}/{AGENT_UNIT}", agent_unit(adk, identity, offline), owner=acct)
    link = host.p(f"{unit_dir}/default.target.wants/{AGENT_UNIT}")
    if not link.exists() and not link.is_symlink():
        try:
            link.symlink_to(f"{acct.home}/.config/systemd/user/{AGENT_UNIT}")
        except OSError:
            link.write_text(f"../{AGENT_UNIT}\n", encoding="utf-8")
    # Linger = the user manager (and so the agent) runs at boot with nobody logged in.
    host.mkdir("/var/lib/systemd/linger", 0o755)
    host.p(f"/var/lib/systemd/linger/{acct.name}").touch()
    host.run(["loginctl", "enable-linger", acct.name])
    rc, out = host.run(["systemctl", "--user", f"--machine={acct.name}@", "daemon-reload"])
    if rc == 0:
        rc, out = host.run(["systemctl", "--user", f"--machine={acct.name}@",
                            "start", AGENT_UNIT])
    return StepResult("agent daemon", True,
                      f"{AGENT_UNIT} enabled with linger"
                      + ("; started" if rc == 0 else "; starts at next boot/login"))


def _replace_block(text: str, begin: str, end: str, body: str) -> str:
    block = f"{begin}\n{body.rstrip()}\n{end}\n"
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
    if pat.search(text):
        return pat.sub(lambda _m: block, text)
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block


def write_shell_profile(host: Host, acct: Account, awsh: bool) -> StepResult:
    path = f"{acct.home}/.bash_profile"
    try:
        text = host.p(path).read_text(encoding="utf-8")
    except OSError:
        text = "[ -f ~/.bashrc ] && . ~/.bashrc\n"
    text = _replace_block(text, ENV_BEGIN, ENV_END,
                          "if [ -r ~/.config/aither/agent.env ]; then\n"
                          "  set -a; . ~/.config/aither/agent.env; set +a\n"
                          "fi")
    if awsh:
        text = _replace_block(text, AWSH_BEGIN, AWSH_END,
                              "# Interactive logins open awsh; `exit` returns to bash.\n"
                              "# AITHER_NO_AWSH=1 skips it for one login.\n"
                              "if [ -t 0 ] && [ -t 1 ] \\\n"
                              "   && [ -z \"${AITHER_AWSH_STARTED:-}\" ] \\\n"
                              "   && [ -z \"${AITHER_NO_AWSH:-}\" ] \\\n"
                              "   && command -v awsh >/dev/null 2>&1; then\n"
                              "  export AITHER_AWSH_STARTED=1\n"
                              "  awsh\n"
                              "fi")
    else:
        text = re.sub(re.escape(AWSH_BEGIN) + r".*?" + re.escape(AWSH_END) + r"\n?", "",
                      text, flags=re.S)
    host.write(path, text, owner=acct)
    note = "awsh opens at login" if awsh else "plain bash at login"
    if awsh and not host.which("awsh"):
        note += " (awsh not installed yet; the hook waits for it)"
    return StepResult("login shell", True, note)


def set_ini_key(text: str, section: str, key: str, value: str) -> str:
    """Set [section] key=value, keeping every other line (wsl.conf is hand-edited)."""
    lines = text.splitlines()
    out, in_sec, done, seen_sec = [], False, False, False
    for ln in lines:
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", ln)
        if m:
            if in_sec and not done:
                out.append(f"{key}={value}")
                done = True
            in_sec = m.group(1).strip() == section
            seen_sec = seen_sec or in_sec
        elif in_sec and re.match(rf"^\s*{re.escape(key)}\s*=", ln):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(ln)
    if in_sec and not done:
        out.append(f"{key}={value}")
        done = True
    if not seen_sec:
        if out and out[-1].strip():
            out.append("")
        out += [f"[{section}]", f"{key}={value}"]
    return "\n".join(out) + "\n"


def set_wsl_default_user(host: Host, name: str) -> StepResult:
    path = host.p("/etc/wsl.conf")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = "[boot]\nsystemd=true\n"
    new = set_ini_key(text, "user", "default", name)
    if new != text:
        host.needs_restart.append("wsl.conf [user] default changed")
    host.write("/etc/wsl.conf", new)
    return StepResult("wsl default user", True,
                      f"/etc/wsl.conf [user] default={name} (takes effect after "
                      f"`wsl --terminate <distro>`; `wsl -u root` still works)")


# ── plan: from prompts or from a seed ───────────────────────────────────────────────
@dataclass
class Plan:
    user: UserPlan
    login: str = "device"                 # device | token_file | skip
    login_email: str = ""
    token_file: str = ""
    restore: bool = True
    daemon: bool = True
    daemon_identity: str = "aither"
    capability_profiles: list = field(default_factory=lambda: ["customer-core"])
    agent_packs: list = field(default_factory=list)
    inference: dict = field(default_factory=lambda: {"backend": "local"})
    awsh_login: bool = True
    hostname: str = ""
    wsl_default_user: bool = True


def plan_from_seed(seed: dict, catalogue: dict) -> Plan:
    u = seed.get("user") or {}
    if not u.get("name"):
        raise SetupError("seed: user.name is required")
    if u.get("password"):
        raise SetupError("seed: plaintext user.password is refused; use user.password_hash "
                         "(openssl passwd -6) or ssh_authorized_keys")
    login = seed.get("login") or {}
    inf = seed.get("inference") or {"backend": "local"}
    daemon = seed.get("daemon") or {}
    sudo = u.get("sudo") or "password"
    if sudo not in SUDO_MODES:
        raise SetupError(f"seed: user.sudo must be one of {SUDO_MODES}")
    lp = u.get("login_password") or ""
    if lp and lp not in LOGIN_PASSWORD_MODES:
        raise SetupError(f"seed: user.login_password must be one of {LOGIN_PASSWORD_MODES}")
    ph = u.get("password_hash") or None
    if lp == "hash" and not ph:
        raise SetupError("seed: user.login_password=hash needs user.password_hash")
    if lp == "locked" and sudo != "nopasswd":
        raise SetupError("seed: a locked password needs user.sudo=nopasswd, or sudo could "
                         "never be used")
    if ph and not re.match(r"^\$(6|5|y|7|2[aby])\$", ph):
        raise SetupError("seed: user.password_hash is not a crypt(3) hash "
                         "(openssl passwd -6); plaintext is refused")
    plan = Plan(
        user=UserPlan(name=u["name"], uid=int(u.get("uid", FIRST_HUMAN_UID)),
                      adopt_from=u.get("adopt_home") or None,
                      adopt_gid=(int(u["adopt_gid"]) if u.get("adopt_gid") is not None
                                 else None),
                      password_hash=ph if lp in ("", "hash") else None,
                      ssh_keys=list(u.get("ssh_authorized_keys") or []),
                      sudo=sudo, login_password=lp),
        login=login.get("mode", "device"), login_email=login.get("email", ""),
        token_file=login.get("token_file", ""),
        restore=bool(seed.get("restore", True)),
        daemon=bool(daemon.get("enable", True)),
        daemon_identity=daemon.get("identity", "aither"),
        capability_profiles=list(seed.get("capability_packs")
                                 or catalogue.get("default_capability_profiles")
                                 or ["customer-core"]),
        agent_packs=list(seed.get("agent_packs") or []),
        inference=dict(inf), awsh_login=bool(seed.get("awsh_login", True)),
        hostname=seed.get("hostname", ""),
        wsl_default_user=bool(seed.get("wsl_default_user", True)))
    if plan.login not in ("device", "token_file", "skip"):
        raise SetupError(f"seed: login.mode {plan.login!r} is not device|token_file|skip")
    resolve_packs(catalogue, plan.capability_profiles)
    if plan.inference.get("backend", "local") not in INFERENCE_CHOICES:
        raise SetupError(f"seed: inference.backend must be one of {INFERENCE_CHOICES}")
    return plan


SEED_EXAMPLE = {
    "user": {"name": "alice", "uid": 1000, "password_hash": "$6$...openssl passwd -6...",
             "sudo": "password", "login_password": "hash",
             "ssh_authorized_keys": ["ssh-ed25519 AAAA... alice@laptop"],
             "adopt_home": None},
    "login": {"mode": "device", "email": "alice@example.com"},
    "restore": True,
    "daemon": {"enable": True, "identity": "aither"},
    "capability_packs": ["customer-core"],
    "agent_packs": [],
    "inference": {"backend": "local"},
    "awsh_login": True,
    "hostname": "",
}


class Prompter:
    def __init__(self, ask: Callable = input, secret: Callable = getpass.getpass) -> None:
        self._ask, self._secret = ask, secret

    def text(self, q: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            v = self._ask(f"  {q}{suffix}: ").strip()
        except EOFError:
            raise SetupError("no input (stdin closed); use --seed for unattended setup") from None
        return v or default

    def yes(self, q: str, default: bool = True) -> bool:
        v = self.text(f"{q} ({'Y/n' if default else 'y/N'})", "").lower()
        return default if not v else v.startswith("y")

    def password(self, name: str) -> str:
        for _ in range(3):
            a = self._secret(f"  password for {name} (sudo asks for it): ")
            if len(a) < 8:
                say("  at least 8 characters, please")
                continue
            if a == self._secret("  again: "):
                return a
            say("  they did not match")
        raise SetupError("password not confirmed")


def plan_interactive(host: Host, pr: Prompter, catalogue: dict) -> Plan:
    say("\n  1. Your account")
    homes = migrated_homes(host)
    user: UserPlan | None = None
    for name, uid, gid, path in homes:
        if pr.yes(f"A migrated home was found: {path} (owner uid {uid}). Adopt it as user "
                  f"{name!r}?", True):
            user = UserPlan(name=name, uid=uid, adopt_from=path, adopt_gid=gid)
            break
    if user is None:
        default = next((a.name for a in human_accounts(host)), "")
        while True:
            name = pr.text("user name", default)
            if USERNAME_RE.match(name):
                break
            say("  lowercase letters, digits, - and _; start with a letter")
        user = UserPlan(name=name)
    if find_account(host, user.name) is None or pr.yes("Set a new password?", False):
        user.password = pr.password(user.name)
    if not host.is_wsl:
        key = pr.text("an SSH public key to authorise (blank to skip)", "")
        if key:
            user.ssh_keys = [key]
    plan = Plan(user=user)
    say("\n  2. Sign in to Aitherium (idp.aitherium.com)")
    plan.login = "device" if pr.yes("Sign in now with a device code?", True) else "skip"
    if plan.login == "device":
        plan.login_email = pr.text("email to send a one-tap approve link to (blank: none)", "")
        plan.restore = pr.yes("Restore settings, license, vault key names and memory scope "
                              "after sign-in?", True)
    say("\n  3. Your agent")
    plan.daemon = pr.yes("Run a persistent agent daemon (adk up) at boot?", True)
    profiles = catalogue["capability_profiles"]
    defaults = catalogue.get("default_capability_profiles") or ["customer-core"]
    say("  Capability packs:")
    names = list(profiles)
    for i, n in enumerate(names, 1):
        mark = "*" if n in defaults else " "
        say(f"   {mark}{i:2d}. {n:14s} {profiles[n]['title']}")
    raw = pr.text("numbers or names, comma-separated (Enter = starred)", "")
    plan.capability_profiles = _pick(raw, names) or list(defaults)
    agent_packs = bundled_agent_packs(host)
    if agent_packs:
        say(f"  Agent packs bundled with adk: {', '.join(agent_packs)}")
        plan.agent_packs = _pick(pr.text("agent packs to install (blank: none)", ""),
                                 agent_packs)
    offered = ["local", "mesh"] + (["cloud"] if plan.login != "skip" else [])
    backend = ""
    while backend not in offered:
        backend = pr.text(f"inference backend ({'/'.join(offered)})", "local")
    plan.inference = {"backend": backend}
    if backend == "mesh":
        plan.inference["url"] = pr.text("DGX / mesh OpenAI-compatible URL "
                                        "(e.g. http://dgx.local:8000/v1)", "")
    plan.awsh_login = pr.yes("Open awsh when you log in?", True)
    return plan


def _pick(raw: str, names: list) -> list:
    out = []
    for tok in [t.strip() for t in raw.replace(" ", ",").split(",") if t.strip()]:
        if tok.isdigit() and 1 <= int(tok) <= len(names):
            tok = names[int(tok) - 1]
        if tok in names and tok not in out:
            out.append(tok)
        elif tok not in names:
            say(f"  (ignored unknown choice {tok!r})")
    return out


# ── the run ─────────────────────────────────────────────────────────────────────────
def execute(host: Host, plan: Plan, catalogue: dict, idp: DeviceLogin) -> dict:
    results: list[StepResult] = Results()
    _Progress.emit("phase", phase="account")
    acct, notes = create_or_adopt_user(host, plan.user)
    results += [StepResult("user", True, n) for n in notes]
    if host.is_wsl and plan.wsl_default_user:
        results.append(set_wsl_default_user(host, acct.name))
    elif plan.hostname:
        rc, out = host.run(["hostnamectl", "set-hostname", plan.hostname])
        results.append(StepResult("hostname", rc == 0, plan.hostname if rc == 0 else out[-120:]))
        host.hostname = plan.hostname if rc == 0 else host.hostname

    identity: dict = {}
    _Progress.emit("phase", phase="sign-in")
    try:
        identity = sign_in(host, acct, plan, idp, results)
    except SetupError as exc:
        # Never fatal: the machine is usable signed-out, and on WSL a non-zero OOBE
        # exit would re-run the whole setup at every launch.
        results.append(StepResult("sign in", False, f"{exc}; run `adk login` later"))
    return finish(host, acct, plan, catalogue, idp, identity, results)


def sign_in(host: Host, acct: Account, plan: Plan, idp: DeviceLogin,
            results: list) -> dict:
    """Device flow (or a seed token file). Prints the code and URL, never the token."""
    identity: dict = {}
    if plan.login == "device":
        ch = idp.start(f"aither-setup ({host.hostname})", plan.login_email)
        say("")
        say("  To sign in, open:  " + ch.get("verification_uri", ""))
        say("  and enter code:    " + ch["user_code"])
        if ch.get("verification_uri_complete"):
            say("  (or open directly: " + ch["verification_uri_complete"] + ")")
        if plan.login_email:
            say(f"  An approve link was also emailed to {plan.login_email}.")
        if host.real and detect_wsl() and open_in_windows_browser(
                ch.get("verification_uri_complete") or ch.get("verification_uri", "")):
            say("  Opened the sign-in page in your Windows browser.")
        say("  Waiting for approval...")
        body = idp.poll(ch)
        identity = store_login(host, acct, body, idp.base)
        results.append(StepResult("sign in", True,
                                  f"signed in as {identity['username'] or identity['email']}"
                                  " -- token in ~/.aither/auth.json (0600)"))
    elif plan.login == "token_file":
        tf = os.path.abspath(plan.token_file) if host.real else plan.token_file
        try:
            tok = host.p(tf).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SetupError(f"login.token_file unreadable: {exc}") from None
        if not tok:
            raise SetupError(f"login.token_file {plan.token_file} is empty")
        body: dict = {"access_token": tok}
        if tok.startswith("{"):
            # The desk hands the whole device-token response (user, tier, expiry).
            try:
                body = json.loads(tok)
            except ValueError:
                raise SetupError("login.token_file is neither a token nor JSON") from None
            if not isinstance(body, dict) or not body.get("access_token"):
                raise SetupError("login.token_file JSON has no access_token")
        identity = store_login(host, acct, body, idp.base)
        gone = shred_file(host, tf)
        who = identity["username"] or identity["email"] or "you"
        results.append(StepResult("sign in", True,
                                  f"signed in as {who} -- token in ~/.aither/auth.json (0600); "
                                  "the one-shot token file "
                                  + ("was shredded" if gone else "COULD NOT be removed")))
    else:
        results.append(StepResult("sign in", None, "skipped; run `adk login` later"))
    return identity


def finish(host: Host, acct: Account, plan: Plan, catalogue: dict, idp: DeviceLogin,
           identity: dict, results: list) -> dict:
    """Restore, env, packs, daemon, shell, then the marker (last, so a crash re-asks)."""
    scope = awm_scope(identity, host.hostname)
    facts: dict = {}
    _Progress.emit("phase", phase="restore")
    if plan.restore and identity:
        results += restore(host, acct, identity, scope, facts)
    host.mkdir(f"{acct.home}/.config/aither", 0o700, owner=acct)
    _Progress.emit("phase", phase="agent")
    inference = dict(plan.inference)
    if inference.get("backend") == "cloud":
        # Cloud is gated on a provider key NAME in the vault, not on being signed in:
        # signed in with no key, "cloud" would be a backend that answers nothing.
        if identity and "provider_keys" not in facts and host.which("adk"):
            rc, text = host.run(["adk", "keys", "pull"], as_user=acct)
            facts["provider_keys"] = parse_key_names(text) if rc == 0 else []
        if not identity:
            results.append(StepResult("inference", None, "cloud needs a sign-in; using local"))
            inference = {"backend": "local"}
        elif not facts.get("provider_keys"):
            results.append(StepResult("inference", None,
                                      "cloud needs a provider key in your vault (`adk keys "
                                      "pull` lists none); using local"))
            inference = {"backend": "local"}
    if inference.get("backend") == "mesh" and not inference.get("url"):
        inference["url"] = mesh_url_guess(host)
        if not inference["url"]:
            results.append(StepResult("inference", None,
                                      "no mesh URL given or discoverable; using local"))
            inference = {"backend": "local"}
    host.write(f"{acct.home}/.config/aither/agent.env",
               agent_env(inference, scope, idp.base), mode=0o600, owner=acct)
    results.append(StepResult("inference", True, f"{inference['backend']} "
                              "(~/.config/aither/agent.env)"))
    results += apply_packs(host, acct, catalogue, plan.capability_profiles,
                           plan.agent_packs, identity)
    if plan.daemon:
        results.append(enable_daemon(host, acct, plan.daemon_identity,
                                     offline=not identity))
    results.append(write_shell_profile(host, acct, plan.awsh_login))

    state = {
        "version": VERSION, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user": acct.name, "uid": acct.uid, "adopted_home": plan.user.adopt_from,
        "idp": idp.base, "signed_in": bool(identity),
        "identity": {k: identity.get(k) for k in ("username", "tenant_slug", "tier")},
        "awm_scope": scope, "daemon": plan.daemon, "inference": inference["backend"],
        "capability_profiles": plan.capability_profiles, "agent_packs": plan.agent_packs,
        "awsh_login": plan.awsh_login, "wsl": host.is_wsl,
        "sudo_mode": plan.user.sudo,
        "login_password": plan.user.login_password or (
            "hash" if (plan.user.password_hash or plan.user.password) else "keep"),
        "provider_key_names": facts.get("provider_keys", []),
        "needs_restart": bool(host.needs_restart), "restart_reasons": list(host.needs_restart),
        "results": [r.__dict__ for r in results],
    }
    text = json.dumps(state, indent=2) + "\n"
    host.write(f"{acct.home}/.aither/setup.json", text, owner=acct)
    host.mkdir("/var/lib/aither", 0o755)
    host.write(MARKER, text, mode=0o644)
    return state


def print_summary(state: dict) -> None:
    say("\n  " + "=" * 64)
    say(f"  AitherOS is set up for {state['user']}.")
    for r in state["results"]:
        mark = {True: "ok ", False: "ERR", None: "-- "}[r["ok"]]
        say(f"   [{mark}] {r['step']:<22} {r['note']}")
    say("  Re-run any time: sudo aither-setup --reconfigure")
    say("  " + "=" * 64)


@contextlib.contextmanager
def single_instance(host: Host):
    if not host.real:
        yield
        return
    import fcntl
    Path(LOCK).parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+")   # noqa: SIM115 -- held for the whole run
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SetupError("another aither-setup is running (perhaps on the console, tty1). "
                         "Stop it with `systemctl stop aither-firstboot` and re-run.") from None
    try:
        yield
    finally:
        fh.close()


def load_seed(host: Host, path: str) -> dict:
    p = host.p(path)
    if host.real:
        st = p.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise SetupError(f"seed {path} must be owned by root and not group/world-"
                             "writable: it can create a sudo-capable user")
    return json.loads(p.read_text(encoding="utf-8"))


def consume_seed(host: Host, path: str) -> None:
    p = host.p(path)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)
        os.replace(p, p.with_name(p.name + ".consumed"))


# ── GUI surface: probe, status, token renewal ───────────────────────────────────────
def probe(host: Host) -> dict:
    """Read-only facts the setup wizard shows before it asks anything. Never writes,
    never prompts; a fact that cannot be read is None, not a guess."""
    rc, out = host.run(["systemctl", "is-system-running"])
    lines = out.strip().splitlines()
    systemd = lines[-1].strip() if lines and rc not in (124, 127) else None
    data_mount = None
    for root in MIGRATED_HOME_ROOTS:
        mnt = root[: -len("/home")]
        if host.run(["mountpoint", "-q", mnt])[0] == 0:
            data_mount = mnt
            break
    passwd = read_passwd(host)
    uid1000 = next((a for a in passwd if a.uid == FIRST_HUMAN_UID), None)
    homes = []
    for name, uid, gid, path in migrated_homes(host):
        acct = next((a for a in passwd if a.name == name), None)
        homes.append({"name": name, "uid": uid, "gid": gid, "path": path,
                      "account_exists": acct is not None,
                      "account_uid": acct.uid if acct else None,
                      "has_password": account_has_password(host, name) if acct else None})
    gpu = bool(host.p("/dev/dxg").exists() or host.p("/dev/nvidia0").exists())
    marker = host.p(MARKER)
    state = None
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(marker.read_text(encoding="utf-8"))
    suggestion: dict = {"mode": "create", "name": ""}
    adoptable = [h for h in homes if data_mount and h["path"].startswith(data_mount + "/")]
    if adoptable:
        h = adoptable[0]
        suggestion = {"mode": "adopt", "name": h["name"], "adopt_home": h["path"],
                      "uid": h["uid"], "gid": h["gid"]}
    return {
        "version": VERSION, "caps": list(CAPS), "configured": marker.exists(),
        "wsl": host.is_wsl, "hostname": host.hostname, "systemd": systemd,
        "data_mount": data_mount,
        "uid1000": ({"name": uid1000.name, "service_account": uid1000.name in SERVICE_ACCOUNTS}
                    if uid1000 else None),
        "migrated_homes": homes, "gpu": gpu,
        "tools": {t: bool(host.which(t)) for t in ("adk", "awsh", "awsettings", "awm",
                                                   "openssl", "visudo")},
        "suggestion": suggestion,
        "catalogue": catalogue_summary(),
        "mesh_url": mesh_url_guess(host),
        "state_user_has_password": (account_has_password(host, str(state.get("user") or ""))
                                    if isinstance(state, dict) and state.get("user") else None),
        "state": ({k: state.get(k) for k in ("user", "uid", "sudo_mode", "inference",
                                             "capability_profiles", "agent_packs", "daemon",
                                             "awsh_login", "signed_in", "completed_at")}
                  if isinstance(state, dict) else None),
    }


def catalogue_summary() -> dict | None:
    """Profile ids + titles for the wizard's pack cards (None if the image lacks it)."""
    try:
        cat = load_catalogue()
    except (SetupError, OSError, ValueError):
        return None
    return {"default": list(cat.get("default_capability_profiles") or ["customer-core"]),
            "profiles": [{"id": k, "title": v.get("title", k)}
                         for k, v in cat["capability_profiles"].items()]}


def status_json(host: Host) -> dict:
    marker = host.p(MARKER)
    state = None
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(marker.read_text(encoding="utf-8"))
    return {"version": VERSION, "caps": list(CAPS), "configured": marker.exists(),
            "state": state}


def renew_token(host: Host, source: str, idp_base: str) -> dict:
    """Replace the stored sign-in of the configured user. The token arrives in a file
    or on stdin (never argv) and is never printed; returns the non-secret identity."""
    try:
        state = json.loads(host.p(MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SetupError("not set up yet: nothing to renew (run the setup first)") from None
    acct = find_account(host, state.get("user") or "")
    if acct is None:
        raise SetupError(f"the configured user {state.get('user')!r} no longer exists")
    stdin = source in ("-", "/dev/stdin")
    raw = (sys.stdin.read() if stdin else host.p(source).read_text(encoding="utf-8")).strip()
    if not raw:
        raise SetupError("renew: no token on input")
    body: Any = {"access_token": raw}
    if raw.startswith("{"):
        try:
            body = json.loads(raw)
        except ValueError:
            raise SetupError("renew: input is neither a token nor JSON") from None
    if not isinstance(body, dict) or not body.get("access_token"):
        raise SetupError("renew: no access_token in the input")
    ident = store_login(host, acct, body, idp_base)
    if not stdin:
        shred_file(host, source)
    return {"user": acct.name, "username": ident.get("username", ""),
            "expires_at": body.get("expires_at", "")}


# ── self-test (runs in the image build) ─────────────────────────────────────────────
def self_test() -> int:
    fails = []

    def chk(ok: bool, what: str) -> None:
        say(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails.append(what)

    conf = "[boot]\nsystemd=true\n\n[user]\ndefault=root\n\n[interop]\nappendWindowsPath=false\n"
    new = set_ini_key(conf, "user", "default", "david")
    chk("default=david" in new and "default=root" not in new
        and "appendWindowsPath=false" in new, "wsl.conf [user] default rewritten in place")
    chk("[user]\ndefault=x" in set_ini_key("[boot]\nsystemd=true\n", "user", "default", "x"),
        "wsl.conf [user] section added when absent")
    chk(systemd_escape_path("/var/home/a-b") == "var-home-a\\x2db",
        "mount unit name escapes '-' like systemd-escape --path")
    chk(awm_scope({"username": "David", "tenant_slug": ""}, "awnix.local")
        == "aitherium:david:awnix", "awm scope derived from identity + host")
    try:
        _check_idp_url("http://idp.example.com")
        chk(False, "plain-http IdP refused")
    except SetupError:
        chk(True, "plain-http IdP refused")
    chk(free_system_id({999, 998}) == 997, "system uid allocated from the top of 201..999")
    try:
        cat = load_catalogue()
        chk("customer-core" in cat["capability_profiles"], "catalogue has customer-core")
        plan = plan_from_seed(SEED_EXAMPLE, cat)
        chk(plan.capability_profiles == ["customer-core"], "seed example parses")
    except SetupError as exc:
        chk(False, f"catalogue/seed: {exc}")
    try:
        plan_from_seed({"user": {"name": "a", "password": "x"}}, {"capability_profiles": {}})
        chk(False, "plaintext seed password refused")
    except SetupError:
        chk(True, "plaintext seed password refused")
    chk("--no-persist" in agent_unit("/usr/local/bin/adk", "aither", False)
        and "--foreground" in agent_unit("/usr/local/bin/adk", "aither", False),
        "daemon unit runs adk up in the foreground without its own autostart")
    chk(parse_key_names("  [+] OpenAI            (workspace)\n  [-] Groq\n") == ["OpenAI"],
        "vault key NAMES parsed from `adk keys pull`, unset ones ignored")
    chk(adopt_backing_mount("/var/mnt/fleet-src/home/david") == "/var/mnt/fleet-src",
        "adopt refuses unless the data disk the home lives on is mounted")
    for bad in ({"user": {"name": "a", "sudo": "maybe"}},
                {"user": {"name": "a", "login_password": "locked"}},
                {"user": {"name": "a", "password_hash": "hunter2"}}):
        try:
            plan_from_seed(bad, {"capability_profiles": {"customer-core": {"packs": []}}})
            chk(False, f"bad seed refused: {bad}")
        except SetupError:
            chk(True, f"bad seed refused: {sorted(bad['user'])}")
    say(f"\nSELF-TEST: {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


# ── main ────────────────────────────────────────────────────────────────────────────
def main(argv: list | None = None, host: Host | None = None,
         prompter: Prompter | None = None, idp: DeviceLogin | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aither-setup", description=__doc__.split("\n")[0])
    ap.add_argument("--reconfigure", action="store_true", help="run even if already set up")
    ap.add_argument("--seed", help="non-interactive: answers from this JSON file")
    ap.add_argument("--firstboot", action="store_true",
                    help="systemd entry: seed if present, else interactive on a TTY")
    ap.add_argument("--oobe", action="store_true", help="WSL OOBE entry (wsl-distribution.conf)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true", help="--status/--probe: machine-readable")
    ap.add_argument("--probe", action="store_true",
                    help="read-only facts for a setup GUI (with --json)")
    ap.add_argument("--json-progress", action="store_true",
                    help="NDJSON step events on stdout; human text to stderr")
    ap.add_argument("--renew-token", metavar="FILE",
                    help="replace the stored sign-in from FILE (or /dev/stdin); never argv")
    ap.add_argument("--idp", default=IDP_URL, help=f"identity provider (default {IDP_URL})")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--print-seed-example", action="store_true")
    ap.add_argument("--demote-service-accounts", action="store_true",
                    help="image build: move bonsai/runner/awnix/aither out of uid >= 1000")
    ap.add_argument("--assert-no-human-users", action="store_true",
                    help="image build: exit 1 if any uid in 1000..59999 exists")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.print_seed_example:
        say(json.dumps(SEED_EXAMPLE, indent=2))
        return 0
    host = host or Host()
    if a.demote_service_accounts or a.assert_no_human_users:
        try:
            if a.demote_service_accounts:
                for n, old, new in demote_service_accounts(host):
                    say(f"aither-setup: service account {n}: uid {old} -> {new}")
        except SetupError as exc:
            say(f"aither-setup: {exc}")
            return 1
        if a.assert_no_human_users:
            humans = human_accounts(host)
            if humans:
                say("aither-setup: an image must bake no human account; found: "
                    + ", ".join(f"{h.name}({h.uid})" for h in humans))
                return 1
            say("aither-setup: no account in uid 1000..59999 -- the first human gets 1000")
        return 0

    marker = host.p(MARKER)
    if a.status and a.json:
        print(json.dumps(status_json(host)))
        return 0
    if a.status:
        say(marker.read_text(encoding="utf-8") if marker.exists() else "not set up yet")
        return 0
    if a.probe:
        try:
            print(json.dumps(probe(host)) if a.json else json.dumps(probe(host), indent=2))
        except SetupError as exc:
            print(json.dumps({"error": str(exc), "caps": list(CAPS)}))
            return 2
        return 0
    if host.real and hasattr(os, "geteuid") and os.geteuid() != 0 and (
            a.renew_token or a.seed or a.reconfigure or not marker.exists()):
        say("aither-setup: run as root (sudo aither-setup)")
        return 1
    if a.renew_token:
        try:
            with single_instance(host):
                r = renew_token(host, a.renew_token, _check_idp_url(a.idp))
        except (SetupError, OSError) as exc:
            say(f"aither-setup: {exc}")
            return 1
        print(json.dumps({"renewed": True, **r}) if a.json else
              f"aither-setup: sign-in renewed for {r['user']}")
        return 0
    _Progress.on = bool(a.json_progress)
    if marker.exists() and not a.reconfigure:
        if not (a.firstboot or a.oobe):
            say("aither-setup: already set up (see --status); use --reconfigure to change it")
        return 0
    seed_path = a.seed or (DEFAULT_SEED if (a.firstboot or a.oobe)
                           and host.p(DEFAULT_SEED).exists() else "")
    if seed_path and host.real:
        seed_path = os.path.abspath(seed_path)
    try:
        catalogue = load_catalogue()
        idp = idp or DeviceLogin(a.idp)
        with single_instance(host):
            if seed_path:
                plan = plan_from_seed(load_seed(host, seed_path), catalogue)
                say(f"aither-setup: unattended, from {seed_path}")
            else:
                if a.firstboot and not sys.stdin.isatty() and prompter is None:
                    say("aither-setup: no seed and no terminal; the first interactive "
                        "login will offer the setup")
                    return 0
                say("\n  " + "=" * 64)
                say("  AitherOS -- first boot. Every answer can be changed later with")
                say("  `aither-setup --reconfigure`.")
                say("  " + "=" * 64)
                plan = plan_interactive(host, prompter or Prompter(), catalogue)
            state = execute(host, plan, catalogue, idp)
            if seed_path:
                consume_seed(host, seed_path)
    except SetupError as exc:
        say(f"\naither-setup: {exc}")
        _Progress.emit("error", message=str(exc))
        return 1
    except KeyboardInterrupt:
        say("\naither-setup: cancelled; nothing marked done -- run it again any time")
        return 130
    print_summary(state)
    _Progress.emit("done", user=state["user"], uid=state["uid"], signed_in=state["signed_in"],
                   sudo_mode=state["sudo_mode"], inference=state["inference"],
                   needs_restart=state["needs_restart"],
                   restart_reasons=state["restart_reasons"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
