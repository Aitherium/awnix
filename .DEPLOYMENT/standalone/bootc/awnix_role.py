#!/usr/bin/python3
"""awnix role -- add, remove and configure OS role bundles on an awnix box.

    awnix role list
    awnix role status [<role>]
    awnix role add <role> [--set KEY=VALUE ...] [--apply]
    awnix role remove <role> [--apply]
    awnix role configure <role> --set KEY=VALUE [...] [--apply]
    awnix role hook <role> backup|restore|replicate [--param NAME=VALUE ...] [--apply]

The Windows Server "Add Roles and Features" shape: a ROLE is a bundle of
platform services (identity, storage, inference, mail, web-portal, backup,
replication-partner) installed and removed as one thing, with its requirements
resolved, its licence checked and its health gated.

DRY-RUN IS THE DEFAULT. Without `--apply` every mutating step (file writes,
unit generation, systemctl, hook calls) is printed as `WOULD:` and nothing
changes; read-only probes still run so `status` is real. Every command goes
through ONE runner (`Runner.run`), which is how the tests assert the exact
command list with no side effects.

The catalog is JSON compiled from AitherOS/config/os_roles.yaml by
check_os_role_catalog.py --write-catalog; nothing on the box parses YAML.

Units come from the monorepo generator when the box has it
(/opt/aitheros/.../generate_os_roles.py), else from the unit TEMPLATES the image
ships (/usr/share/aither/roles/units/<role>, rendered by
check_os_role_catalog.py --write-templates). With neither, `add` refuses before
changing anything.

Hooks (backup/restore/replicate) call a member's HTTPS route with the fleet
internal key from a 0600 header file, run a shipped script, or podman-export the
role's named volumes. `remove` runs the backup hook first and REFUSES when it
fails; a hook declared `none` is a documented no-op.

This file is ALSO the licence gate. `/usr/libexec/aither/role-entitlement-check`
imports `entitlement_main` from here; the decision is `decide()`. See its
docstring for the fail-open / fail-closed rule.

stdlib only, Python 3.9 (CentOS Stream 9's /usr/bin/python3).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── paths (every one overridable, so tests never touch the real box) ────────

def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, "").strip() or default)


def catalog_path() -> Path:
    env = os.environ.get("AWNIX_ROLE_CATALOG", "").strip()
    if env:
        return Path(env)
    installed = Path("/usr/share/aither/roles/catalog.json")
    if installed.is_file():
        return installed
    return Path(__file__).resolve().with_name("awnix-role-catalog.json")   # repo checkout


STATE_DEFAULT = "/etc/awnix/roles.json"
ENV_DIR_DEFAULT = "/etc/aither/roles"
QUADLET_DIR_DEFAULT = "/etc/containers/systemd"
UNIT_DIR_DEFAULT = "/etc/systemd/system"
STAGING_DEFAULT = "/var/lib/awnix/roles"
GENERATOR_DEFAULT = "/opt/aitheros/AitherOS/dev/tools/generate_os_roles.py"
BACKUP_DIR_DEFAULT = "/var/lib/awnix/backups"
SECRET_ENV_DIR_DEFAULT = "/etc/aither"
PODMAN = "/usr/bin/podman"


def templates_dir() -> Path:
    env = os.environ.get("AWNIX_ROLE_TEMPLATES", "").strip()
    if env:
        return Path(env)
    installed = Path("/usr/share/aither/roles/units")
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().with_name("awnix-role-units")          # repo checkout
LICENCE_DEFAULT = "/etc/aither/appliance.lic"
LICENCE_STATUS_DEFAULT = "/var/lib/aither/license/status.json"

#: Licence states (APPLIANCE-LICENSING-SPEC.md) under which an add-on may NOT start.
DENY_STATES = {"invalid", "revoked", "lapsed"}
#: Classes that are the free floor. `entry`/`lite` are the retired spellings
#: appliance_entitlements.yaml legacy_entitlement_map maps to core.
OPEN_CLASSES = {"core", "internal", "entry", "lite"}


# ── the licence decision ──────────────────────────────────────────────────────

def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(mapping, '') or (None, why)."""
    if not path.is_file():
        return None, "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"unreadable ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, ""


def granted_packs(licence: Dict[str, Any], policy: Dict[str, Any]) -> Tuple[set, str]:
    """Add-on packs a licence grants: its explicit lists + its tier's bundle.

    The tier may be spelled in any of the three live vocabularies: an appliance
    tier (free|standard|professional|sovereign), a SaaS plan (via saas_tier_map)
    or a factory tier (lite|entry|pro|pro-reasoning|full, via factory_tier_map).
    """
    packs = set()
    for key in ("addons", "entitlements", "packs", "features"):
        val = licence.get(key)
        items = val if isinstance(val, list) else (
            [k for k, v in val.items() if v] if isinstance(val, dict) else [])
        for item in items:
            s = str(item).strip()
            packs.add(s[len("addon:"):] if s.startswith("addon:") else s)
    tier = str(licence.get("tier") or "").strip()
    tiers = policy.get("tiers") or {}
    note = ""
    if tier:
        resolved = tier if tier in tiers else (
            (policy.get("saas_tier_map") or {}).get(tier)
            or (policy.get("factory_tier_map") or {}).get(tier))
        if resolved in tiers:
            packs.update(str(a) for a in tiers[resolved])
            note = f"tier {tier}" + (f" -> {resolved}" if resolved != tier else "")
        else:
            note = f"tier {tier!r} is in no known vocabulary (grants no add-ons)"
    return packs, note


def decide(service: str, cls: str, policy: Dict[str, Any],
           licence_file: Path, status_file: Path) -> Tuple[bool, str]:
    """May `service` (licence class `cls`) start on this box?

    The rule, and why:

    * core / internal (and the retired `lite`/`entry`): ALLOW, licence or not.
      FAIL-OPEN on purpose. appliance_entitlements.yaml defines core as "present
      in every appliance including an unlicensed one", and the licensing spec
      says a platform outage must never brick an appliance -- a missing or
      unreadable licence file is exactly that outage. Gating identity or storage
      on a file would turn a lost licence into a box that cannot log anyone in
      to fix it.
    * addon:<pack>: ALLOW only when a licence file exists, parses, is not
      marked invalid/revoked/lapsed by the validator (status.json), and grants
      the pack. FAIL-CLOSED on purpose: an add-on is the paid thing, and "no
      licence" must never read as "licensed". The core floor stays up, so a
      denied add-on degrades the box instead of breaking it.
    * platform-only: allowed only when the licence names `platform-only`
      explicitly (the owner's own nodes); never for a customer tier.
    * anything else (unset, a retired paid class with no pack, a typo): DENY.
      An unknown class is not a free class.
    """
    cls = (cls or "unset").strip()
    if cls in OPEN_CLASSES:
        return True, f"{service}: class {cls} is the free core floor (runs unlicensed)"
    licence, why = read_json(licence_file)
    if licence is None:
        return False, (f"{service}: class {cls} needs a licence and {licence_file} is {why} "
                       f"(add-ons fail closed; core roles keep running)")
    status, _ = read_json(status_file)
    state = str((status or {}).get("status") or "").strip()
    if state in DENY_STATES:
        return False, f"{service}: licence status is {state!r} ({status_file})"
    packs, note = granted_packs(licence, policy)
    if cls == "platform-only":
        ok = "platform-only" in packs
        return ok, f"{service}: platform-only {'granted' if ok else 'not granted'} by the licence"
    if cls.startswith("addon:"):
        pack = cls.split(":", 1)[1]
        if pack in packs:
            return True, f"{service}: add-on {pack} granted ({note or 'explicit list'})"
        return False, (f"{service}: add-on {pack} NOT granted by the licence "
                       f"({note or 'no tier'}; grants: {', '.join(sorted(packs)) or 'none'})")
    return False, f"{service}: unknown licence class {cls!r} (fails closed)"


def load_catalog(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or catalog_path()
    data, why = read_json(p)
    if data is None:
        raise SystemExit(f"awnix role: catalog {p} is {why}")
    return data


def entitlement_main(argv: Sequence[str]) -> int:
    """`role-entitlement-check <Service> [<class>]` -- exit 0 allow, 1 deny.

    The class normally comes from the unit (generate_os_roles writes it), so the
    gate decides with no catalog on the box. A unit from an older generator
    passes only the name; the class is then looked up in the catalog, and a
    service found nowhere is `unset` -> denied.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: role-entitlement-check <Service> [<class>]", file=sys.stderr)
        return 2
    service = argv[0]
    cls = argv[1] if len(argv) > 1 else ""
    cat, _ = read_json(catalog_path())
    if not cls:
        cls = str(((cat or {}).get("services") or {}).get(service, {}).get("entitlement") or "unset")
    ok, reason = decide(service, cls, (cat or {}).get("policy") or {},
                        _env_path("AITHER_APPLIANCE_LICENSE", LICENCE_DEFAULT),
                        _env_path("AITHER_LICENSE_STATUS", LICENCE_STATUS_DEFAULT))
    print(("role-entitlement-check: ALLOW " if ok else "role-entitlement-check: DENY ") + reason,
          file=sys.stderr)
    return 0 if ok else 1


# ── the one runner ────────────────────────────────────────────────────────────

class Runner:
    """Every side effect goes through here.

    `run(argv, mutating=True)` executes only under `apply`; otherwise it prints
    `WOULD: <argv>` and reports success. Read-only probes (`mutating=False`)
    always execute. `write_file` and `sleep` follow the same rule. Tests
    subclass this and record `calls`.
    """

    def __init__(self, apply: bool = False, out: Any = None) -> None:
        self.apply = apply
        self.out = out or sys.stdout

    def say(self, msg: str) -> None:
        print(msg, file=self.out)

    def run(self, argv: List[str], mutating: bool = True, timeout: int = 600) -> Tuple[int, str]:
        if mutating and not self.apply:
            self.say("WOULD: " + " ".join(argv))
            return 0, ""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, f"{type(e).__name__}: {e}"
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def write_file(self, path: Path, content: str, mode: int = 0o644) -> None:
        if not self.apply:
            self.say(f"WOULD: write {path} ({len(content)} bytes)")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def make_private_dir(self, path: Path) -> None:
        """mkdir -p `path` as 0700 (missing parents too). Backups carry key
        material and the DIT's password hashes, so no level is world-readable."""
        if not self.apply:
            self.say(f"WOULD: mkdir -m 0700 -p {path}")
            return
        missing = []
        cur = Path(path)
        while not cur.exists() and cur != cur.parent:
            missing.append(cur)
            cur = cur.parent
        for d in reversed(missing):
            d.mkdir(mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        os.chmod(path, 0o700)

    def sleep(self, seconds: float) -> None:
        if self.apply:
            time.sleep(seconds)


# ── state ─────────────────────────────────────────────────────────────────────

class Box:
    """Paths + catalog + state for one invocation."""

    def __init__(self, catalog: Dict[str, Any], state_path: Path) -> None:
        self.catalog = catalog
        self.roles: Dict[str, Dict[str, Any]] = catalog.get("roles") or {}
        self.services: Dict[str, Dict[str, Any]] = catalog.get("services") or {}
        self.policy: Dict[str, Any] = catalog.get("policy") or {}
        self.state_path = state_path
        self.env_dir = _env_path("AWNIX_ROLE_ENV_DIR", ENV_DIR_DEFAULT)
        self.quadlet_dir = _env_path("AWNIX_QUADLET_DIR", QUADLET_DIR_DEFAULT)
        self.unit_dir = _env_path("AWNIX_UNIT_DIR", UNIT_DIR_DEFAULT)
        self.staging = _env_path("AWNIX_ROLE_STAGING", STAGING_DEFAULT)
        self.generator = _env_path("AWNIX_ROLE_GENERATOR", GENERATOR_DEFAULT)
        self.templates = templates_dir()
        self.backup_dir = _env_path("AWNIX_BACKUP_DIR", BACKUP_DIR_DEFAULT)
        self.secret_env_dir = _env_path("AWNIX_SECRET_ENV_DIR", SECRET_ENV_DIR_DEFAULT)
        self.python = os.environ.get("AWNIX_ROLE_PYTHON", "").strip() or sys.executable or "python3"
        self.licence = _env_path("AITHER_APPLIANCE_LICENSE", LICENCE_DEFAULT)
        self.licence_status = _env_path("AITHER_LICENSE_STATUS", LICENCE_STATUS_DEFAULT)
        self.ca_bundle = os.environ.get("AWNIX_CA_BUNDLE", "").strip()
        self.health_attempts = int(os.environ.get("AWNIX_HEALTH_ATTEMPTS", "12") or 12)
        data, _ = read_json(state_path)
        self.state: Dict[str, Any] = data if data is not None else {"version": 1, "roles": {}}
        self.state.setdefault("roles", {})

    # graph
    def closure(self, rid: str) -> List[str]:
        order: List[str] = []
        seen: Dict[str, int] = {}

        def visit(x: str, path: List[str]) -> None:
            if x not in self.roles:
                raise SystemExit(f"awnix role: unknown role {x!r} (known: {', '.join(sorted(self.roles))})")
            if seen.get(x) == 2:
                return
            if seen.get(x) == 1:
                raise SystemExit("awnix role: requires cycle " + " -> ".join(path + [x]))
            seen[x] = 1
            for d in self.roles[x].get("requires_roles") or []:
                visit(str(d), path + [x])
            seen[x] = 2
            order.append(x)

        visit(rid, [])
        return order

    def installed(self) -> Dict[str, Any]:
        return self.state["roles"]

    def installed_dependents(self, rid: str) -> List[str]:
        return sorted(r for r in self.installed()
                      if r != rid and r in self.roles and rid in self.closure(r)[:-1])

    def units(self, rid: str) -> List[str]:
        return [self.services.get(m, {}).get("unit", "aither-" + m.lower()) + ".service"
                for m in self.roles[rid].get("deploy") or []]

    def entitled(self, rid: str) -> Tuple[bool, List[str]]:
        reasons = []
        ok = True
        for cls in self.roles[rid].get("entitlement") or ["unset"]:
            allowed, why = decide(rid, cls, self.policy, self.licence, self.licence_status)
            ok = ok and allowed
            reasons.append(why)
        return ok, reasons

    def save_state(self, runner: Runner) -> None:
        runner.write_file(self.state_path, json.dumps(self.state, indent=2, sort_keys=True) + "\n")


# ── config ────────────────────────────────────────────────────────────────────

_KV = re.compile(r"^([A-Z][A-Z0-9_]{1,63})=(.*)$", re.S)


def parse_sets(pairs: Sequence[str]) -> Dict[str, str]:
    out = {}
    for p in pairs or []:
        m = _KV.match(p)
        if not m:
            raise SystemExit(f"awnix role: --set wants KEY=VALUE with an ENV_KEY, got {p!r}")
        out[m.group(1)] = m.group(2)
    return out


def resolve_config(rid: str, role: Dict[str, Any], prior: Dict[str, str],
                   sets: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """(values to write, errors). Secrets are refused: they live in the vault."""
    schema = role.get("config") or {}
    errors: List[str] = []
    values = dict(prior)
    for k, v in sets.items():
        sch = schema.get(k)
        if not isinstance(sch, dict):
            errors.append(f"{rid}: {k} is not a config key of this role "
                          f"(keys: {', '.join(sorted(schema)) or 'none'})")
            continue
        typ = sch.get("type")
        if typ == "secret":
            errors.append(f"{rid}: {k} is a secret -- set vault key {sch.get('secret_ref')!r} "
                          f"through the secrets plane; awnix never writes secret values")
            continue
        if typ == "int" and not re.match(r"^-?\d+$", v):
            errors.append(f"{rid}: {k} must be an int, got {v!r}")
            continue
        if typ == "bool" and v.lower() not in ("true", "false", "1", "0", "yes", "no"):
            errors.append(f"{rid}: {k} must be a bool, got {v!r}")
            continue
        if typ == "enum" and v not in (sch.get("values") or []):
            errors.append(f"{rid}: {k} must be one of {sch.get('values')}, got {v!r}")
            continue
        if "\n" in v:
            errors.append(f"{rid}: {k} may not contain a newline")
            continue
        values[k] = v
    for k, sch in schema.items():
        if not isinstance(sch, dict) or sch.get("type") == "secret":
            continue
        if k not in values and "default" in sch:
            values[k] = str(sch["default"]).lower() if isinstance(sch["default"], bool) else str(sch["default"])
        if sch.get("required") and not values.get(k):
            errors.append(f"{rid}: {k} is required (--set {k}=...)")
    return values, errors


#: The file aither-setup writes the box's own identity into (mode, domain).
BOX_ENV_FILE = "aitheros.env"
#: What a role unit must be told so it runs as THIS box. Unset, the services
#: run as the vendor's platform: OIDC redirect URIs, connector callbacks, session
#: cookies and login links on the vendor's hosts (lib.security.sovereign_idp).
BOX_MODE_ENV = "AITHER_DEPLOYMENT_MODE"
BOX_DOMAIN_ENV = "AITHER_SOVEREIGN_DOMAIN"


def box_identity(box: "Box") -> Dict[str, str]:
    """{env name: value} every role env file carries: whose stack this is.

    Read from the box's own /etc/aither/aitheros.env when aither-setup wrote
    one. A box with no monorepo generator installs from the shipped templates
    -- a product box -- and is `sovereign` unless it says otherwise; with no
    domain configured the identity plane then names no public host at all,
    never the vendor's.
    """
    found: Dict[str, str] = {}
    try:
        text = (box.secret_env_dir / BOX_ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name in (BOX_MODE_ENV, BOX_DOMAIN_ENV, "AITHER_DOMAIN"):
            found[name] = value.strip().strip('"').strip("'")
    out: Dict[str, str] = {}
    mode = found.get(BOX_MODE_ENV) or ("" if box.generator.is_file() else "sovereign")
    if mode:
        out[BOX_MODE_ENV] = mode
    domain = found.get(BOX_DOMAIN_ENV) or found.get("AITHER_DOMAIN") or ""
    if domain and domain.lower() != "localhost":
        out[BOX_DOMAIN_ENV] = domain
    return out


def render_env(rid: str, role: Dict[str, Any], values: Dict[str, str],
               identity: Optional[Dict[str, str]] = None) -> str:
    lines = [f"# awnix role {rid} -- written by `awnix role`; edit with `awnix role configure`.",
             "# Secret values are NEVER written here; the services read them from the vault."]
    for k in sorted(values):
        lines.append(f"{k}={values[k]}")
    extra = {k: v for k, v in sorted((identity or {}).items()) if k not in values}
    if extra:
        lines.append("# whose stack this is (from this box, never from the shipped unit)")
        lines += [f"{k}={v}" for k, v in extra.items()]
    for k, sch in sorted((role.get("config") or {}).items()):
        if isinstance(sch, dict) and sch.get("type") == "secret":
            lines.append(f"# {k}: vault key {sch.get('secret_ref')}")
    return "\n".join(lines) + "\n"


# ── health + hooks ────────────────────────────────────────────────────────────

def _curl(box: Box, url: str, method: str = "GET") -> List[str]:
    argv = ["curl", "--fail", "--silent", "--show-error", "--max-time", "10"]
    if box.ca_bundle:
        argv += ["--cacert", box.ca_bundle]
    if method != "GET":
        argv += ["-X", method]
    return argv + [url]


def health_gate(box: Box, rid: str, runner: Runner) -> List[str]:
    """Probe every member's health endpoint; return the members that never answered."""
    failed = []
    for m in box.roles[rid].get("services") or []:
        facts = box.services.get(m) or {}
        if not facts.get("port"):
            runner.say(f"  health: {m} has no port in the catalog -- not probed")
            continue
        url = f"https://127.0.0.1:{facts['port']}{facts.get('health') or '/health'}"
        ok = False
        for attempt in range(box.health_attempts):
            rc, _ = runner.run(_curl(box, url), mutating=True)
            if rc == 0:
                ok = True
                break
            if attempt + 1 < box.health_attempts:
                runner.sleep(5)
        runner.say(f"  health: {m} {url} -> {'ok' if ok else 'NO ANSWER'}")
        if not ok:
            failed.append(m)
    return failed


def run_hook(box: Box, rid: str, kind: str, runner: Runner,
             params: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Run one role hook. (ok, what happened).

    `none` is a DECLARED no-op and succeeds; a leftover `todo` (an old catalog)
    FAILS -- an undeclared backup is not a backup, and `remove` refuses on it.
    """
    hook = (box.roles[rid].get("hooks") or {}).get(kind) or {}
    if hook.get("none"):
        return True, f"no {kind} for {rid} by design: {hook['none']}"
    if hook.get("todo"):
        return False, f"{rid}.{kind} is undeclared (TODO: {hook['todo']})"
    ctx = HookContext(box, rid, params or {})
    # Every file a hook writes (curl --output, podman volume export) is created
    # by a child that inherits this umask: 0600 files, 0700 dirs. Under the
    # default 022 a DIT snapshot (password/API-key hashes) was world-readable.
    old_umask = os.umask(0o077)
    try:
        if hook.get("volumes"):
            return _volumes_hook(box, rid, kind, str(hook["volumes"]), runner, ctx)
        steps = hook.get("steps") if isinstance(hook.get("steps"), list) else [hook]
        msgs = []
        for i, step in enumerate(steps):
            ok, msg = _run_action(box, rid, kind, step if isinstance(step, dict) else {},
                                  runner, ctx)
            msgs.append(msg if len(steps) == 1 else f"[{i + 1}/{len(steps)}] {msg}")
            if not ok:
                return False, "; ".join(msgs)
        return True, "; ".join(msgs)
    finally:
        os.umask(old_umask)


_PH = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class HookContext:
    """Placeholder values for one hook run: {ts} {role} {backup_dir} + --param."""

    def __init__(self, box: Box, rid: str, params: Dict[str, str]) -> None:
        self.values = dict(params)
        self.values.update(
            ts=_dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            role=rid, backup_dir=str(box.backup_dir))

    def fill(self, text: str, url: bool = False) -> str:
        """Substitute placeholders. `url=True` percent-encodes EVERY value
        (safe=''), so `--param backup_id=../../x` stays one path segment and
        cannot walk the route or smuggle a query string."""
        missing = sorted({m for m in _PH.findall(text) if m not in self.values})
        if missing:
            raise KeyError(", ".join(missing))
        if url:
            return _PH.sub(lambda m: urllib.parse.quote(self.values[m.group(1)], safe=""), text)
        return _PH.sub(lambda m: self.values[m.group(1)], text)

    def fill_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.fill(obj)
        if isinstance(obj, list):
            return [self.fill_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.fill_obj(v) for k, v in obj.items()}
        return obj


def internal_key(box: Box) -> str:
    """The fleet internal secret the hook authenticates with ('' = none found).

    AITHER_INTERNAL_SECRET in the environment, else the first
    `AITHER_INTERNAL_SECRET=` line in the /etc/aither/*.env files the generated
    units already load (generate_os_roles moves every credential there). The
    value is never printed and never placed on a command line.
    """
    env = os.environ.get("AITHER_INTERNAL_SECRET", "").strip()
    if env:
        return env
    if not box.secret_env_dir.is_dir():
        return ""
    for f in sorted(box.secret_env_dir.glob("*.env")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("AITHER_INTERNAL_SECRET="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except OSError:
            continue
    return ""


def _header_file(box: Box, auth: str, runner: Runner) -> Tuple[Optional[str], str]:
    """(path of a 0600 curl header file, '') or (None, why). Dry-run: a placeholder."""
    if not runner.apply:
        return "<internal-auth-headers>", ""
    key = internal_key(box)
    if not key:
        return None, (f"no internal key (AITHER_INTERNAL_SECRET unset and none in "
                      f"{box.secret_env_dir}/*.env) -- refusing an unauthenticated hook call")
    lines = [f"X-API-Key: {key}"] if auth == "api-key" else \
        [f"X-Internal-Key: {key}", "X-Caller-Type: platform"]
    fd, path = tempfile.mkstemp(prefix="awnix-hook-", suffix=".hdr")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path, ""


def _run_action(box: Box, rid: str, kind: str, a: Dict[str, Any], runner: Runner,
                ctx: HookContext) -> Tuple[bool, str]:
    try:
        if a.get("script"):
            argv = [ctx.fill(str(a["script"]))] + [ctx.fill(str(x)) for x in a.get("args") or []]
            rc, out = runner.run(argv)
            return rc == 0, f"{kind} script rc={rc} {out.strip()[:200]}"
        if not a.get("http"):
            return False, f"{rid}.{kind}: hook is malformed"
        method, path = str(a["http"]).split(" ", 1)
        path = ctx.fill(path, url=True)
        body = ctx.fill_obj(a.get("body", {} if method in ("POST", "PUT") else None))
        save_to = ctx.fill(str(a["save_to"])) if a.get("save_to") else ""
    except KeyError as e:
        return False, (f"{rid}.{kind} needs --param {e.args[0].replace(', ', ' --param ')}"
                       f" (value)")
    facts = box.services.get(str(a.get("service"))) or {}
    if not facts.get("port"):
        return False, f"{kind} hook service {a.get('service')} has no port"
    hdr, why = _header_file(box, str(a.get("auth") or "internal"), runner)
    if hdr is None:
        return False, f"{kind} {method} {path}: {why}"
    # --max-time 600: a backup is not a health probe. Headers come from a 0600
    # file (`-H @file`) so the key is never on a command line or in `ps`.
    # --globoff: a URL is taken literally; curl's own [] {} globbing never applies.
    argv = ["curl", "--fail", "--silent", "--show-error", "--globoff", "--max-time", "600"]
    if box.ca_bundle:
        argv += ["--cacert", box.ca_bundle]
    argv += ["-H", f"@{hdr}"]
    if body is not None:
        argv += ["-H", "Content-Type: application/json", "--data", json.dumps(body, sort_keys=True)]
    if save_to:
        runner.make_private_dir(Path(save_to).parent)
        argv += ["--output", save_to]
    if method != "GET":
        argv += ["-X", method]
    argv.append(f"https://127.0.0.1:{facts['port']}{path}")
    try:
        rc, out = runner.run(argv, timeout=660)
    finally:
        if runner.apply and hdr and os.path.isfile(hdr):
            os.unlink(hdr)
    msg = f"{kind} {method} {path} rc={rc}" + (f" -> {save_to}" if save_to else "")
    if rc != 0:
        return False, f"{msg} {out.strip()[:200]}"
    expect = a.get("expect") or {}
    if expect and runner.apply:
        try:
            reply = json.loads(out)
        except ValueError:
            return False, f"{msg}: reply is not JSON, cannot check {expect}"
        for k, want in expect.items():
            got = reply.get(k) if isinstance(reply, dict) else None
            if got not in (want if isinstance(want, list) else [want]):
                return False, f"{msg}: {k}={got!r}, expected {want!r}"
    return True, msg


def role_volumes(box: Box, rid: str) -> List[str]:
    """The named volumes that hold this role's state ON THIS BOX: the catalog's
    data_volumes, plus the state volumes the shipped template mounts in place of
    a writable monorepo bind (recorded at install; a generator box binds a host
    directory there instead and has no such volume)."""
    vols = [str(v) for v in box.roles[rid].get("data_volumes") or [] if "/" not in str(v)]
    for v in (box.installed().get(rid) or {}).get("state_volumes") or []:
        if str(v) not in vols and "/" not in str(v):
            vols.append(str(v))
    return vols


def _volumes_hook(box: Box, rid: str, kind: str, action: str, runner: Runner,
                  ctx: HookContext) -> Tuple[bool, str]:
    """podman volume export/import of the role's named volumes."""
    vols = role_volumes(box, rid)
    if not vols:
        return False, f"{rid}.{kind}: volumes hook but no named data_volumes"
    if action == "export":
        dest = Path(ctx.fill("{backup_dir}/{role}/{ts}"))
        try:
            runner.make_private_dir(dest)
        except OSError as e:
            return False, f"{kind}: mkdir {dest}: {e}"
        for v in vols:
            rc, out = runner.run([PODMAN, "volume", "export", v,
                                  "--output", str(dest / f"{v}.tar")])
            if rc != 0:
                return False, f"{kind}: podman volume export {v} rc={rc} {out.strip()[:200]}"
        return True, f"{kind}: exported {', '.join(vols)} -> {dest}"
    if action == "import":
        raw = ctx.values.get("from", "")
        if not raw:
            return False, (f"{rid}.{kind} needs --param from=<dir> (a {box.backup_dir}/{rid}/<ts> "
                           f"directory written by the backup hook)")
        # Only an archive this CLI wrote may be imported into a role's volume:
        # resolve symlinks and `..` first, then require it under backup_dir.
        root = Path(os.path.realpath(box.backup_dir))
        src = Path(os.path.realpath(raw))
        if src == root or root not in src.parents:
            return False, (f"{rid}.{kind}: --param from={raw} resolves to {src}, which is not "
                           f"under the backup dir {root} -- refusing")
        for v in vols:
            rc, out = runner.run([PODMAN, "volume", "create", "--ignore", v])
            if rc != 0:
                return False, f"{kind}: podman volume create {v} rc={rc} {out.strip()[:200]}"
            rc, out = runner.run([PODMAN, "volume", "import", v, str(Path(src) / f"{v}.tar")])
            if rc != 0:
                return False, f"{kind}: podman volume import {v} rc={rc} {out.strip()[:200]}"
        return True, f"{kind}: imported {', '.join(vols)} <- {src}"
    return False, f"{rid}.{kind}: unknown volumes action {action!r}"


# ── verbs ─────────────────────────────────────────────────────────────────────

def cmd_list(box: Box, runner: Runner) -> int:
    runner.say(f"{'ROLE':<20} {'RING':<9} {'STATE':<10} {'LICENCE':<8} {'ENTITLEMENT':<30} SERVICES")
    for rid in sorted(box.roles):
        r = box.roles[rid]
        ok, _ = box.entitled(rid)
        state = "installed" if rid in box.installed() else "available"
        runner.say(f"{rid:<20} {r.get('ring', ''):<9} {state:<10} {'ok' if ok else 'DENIED':<8} "
                   f"{','.join(r.get('entitlement') or []):<30} {', '.join(r.get('services') or [])}")
    return 0


def cmd_status(box: Box, runner: Runner, rid: Optional[str]) -> int:
    ids = [rid] if rid else sorted(box.roles)
    worst = 0
    for x in ids:
        if x not in box.roles:
            raise SystemExit(f"awnix role: unknown role {x!r}")
        inst = box.installed().get(x)
        runner.say(f"{x}: {'installed ' + str(inst.get('installed_at')) if inst else 'not installed'}")
        for u in box.units(x):
            rc, out = runner.run(["systemctl", "is-active", u], mutating=False)
            active = (out.strip().splitlines() or ["unknown"])[0]
            runner.say(f"  {u:<44} {active}")
            if inst and rc != 0:
                worst = 1
        ok, why = box.entitled(x)
        runner.say("  licence: " + ("ok" if ok else "DENIED") + " -- " + "; ".join(why))
    return worst


def cmd_add(box: Box, runner: Runner, rid: str, sets: Dict[str, str]) -> int:
    plan = box.closure(rid)
    todo = [x for x in plan if x not in box.installed()]
    if not todo:
        runner.say(f"{rid} is already installed")
        return 0
    runner.say(f"add {rid}: install order {' -> '.join(todo)}"
               + (f" (already installed: {', '.join(x for x in plan if x not in todo)})"
                  if len(todo) < len(plan) else ""))
    # Refuse BEFORE touching anything: licence for every role in the closure,
    # config for the requested role.
    errors: List[str] = []
    configs: Dict[str, Dict[str, str]] = {}
    for x in todo:
        ok, why = box.entitled(x)
        if not ok:
            errors += why
        prior = (box.installed().get(x) or {}).get("config") or {}
        vals, errs = resolve_config(x, box.roles[x], prior, sets if x == rid else {})
        configs[x] = vals
        errors += errs
    use_generator = box.generator.is_file()
    for x in todo:
        if box.roles[x].get("deploy") and not use_generator \
                and not (box.templates / x / "manifest.json").is_file():
            errors.append(f"{x}: no unit source -- neither the generator ({box.generator}) nor "
                          f"shipped templates ({box.templates / x}) exist on this box")
    if errors:
        for e in errors:
            runner.say("REFUSED: " + e)
        return 1
    for x in todo:
        role = box.roles[x]
        deploy = [str(m) for m in role.get("deploy") or []]
        state_volumes: List[str] = []
        env_file = box.env_dir / f"{x}.env"
        runner.write_file(env_file, render_env(x, role, configs[x], box_identity(box)), mode=0o640)
        if deploy:
            if use_generator:
                stage = box.staging / x
                rc, out = runner.run([box.python, str(box.generator), "--for", ",".join(deploy),
                                      "--out", str(stage), "--entitle"])
                if rc != 0:
                    runner.say(f"FAILED: unit generation for {x} rc={rc}\n{out[-2000:]}")
                    return 1
            else:
                stage = box.templates / x
                manifest, why = read_json(stage / "manifest.json")
                if manifest is None:
                    runner.say(f"FAILED: {stage / 'manifest.json'} is {why}")
                    return 1
                runner.say(f"  {x}: units from shipped templates {stage} "
                           f"(no generator on this box)")
                state_volumes = [str(v) for v in manifest.get("state_volumes") or []]
                dirs = [str(d) for d in manifest.get("host_dirs") or []]
                if dirs:
                    # the data binds the units mount; podman refuses a missing source
                    runner.run(["mkdir", "-p"] + dirs)
            runner.run(["cp", "-rf", f"{stage}/containers/.", f"{box.quadlet_dir}/"])
            runner.run(["cp", "-rf", f"{stage}/system/.", f"{box.unit_dir}/"])
            for m in deploy:
                unit = box.services.get(m, {}).get("unit", "aither-" + m.lower())
                runner.write_file(box.quadlet_dir / f"{unit}.container.d" / "50-awnix-role.conf",
                                  f"[Container]\nEnvironmentFile={env_file}\n")
            units = box.units(x)
            runner.run(["systemctl", "unmask"] + units)
            runner.run(["systemctl", "daemon-reload"])
            rc, out = runner.run(["systemctl", "start"] + units)
            if rc != 0:
                runner.say(f"FAILED: start {x} rc={rc} {out.strip()[:400]}")
                return 1
        else:
            runner.say(f"  {x}: every member is carried by a required role's process -- "
                       f"no units to start, health-gating only")
        bad = health_gate(box, x, runner)
        if bad:
            runner.say(f"FAILED: {x} members never became healthy: {', '.join(bad)} "
                       f"(units left running for diagnosis; `awnix role status {x}`)")
            return 1
        box.installed()[x] = {
            "installed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "requested": x == rid,
            "config": configs[x],
            "units": box.units(x),
            # backed up and restored with the role's data_volumes (role_volumes)
            "state_volumes": state_volumes,
        }
        box.save_state(runner)
        runner.say(f"  {x}: installed")
    if not runner.apply:
        runner.say("dry run -- nothing changed. Re-run with --apply to act.")
    return 0


def cmd_remove(box: Box, runner: Runner, rid: str) -> int:
    if rid not in box.roles:
        raise SystemExit(f"awnix role: unknown role {rid!r}")
    role = box.roles[rid]
    if not role.get("removable", False):
        runner.say(f"REFUSED: {rid} is ring {role.get('ring')} -- not removable")
        return 1
    if rid not in box.installed():
        runner.say(f"{rid} is not installed")
        return 0
    deps = box.installed_dependents(rid)
    if deps:
        runner.say(f"REFUSED: installed role(s) {', '.join(deps)} require {rid}; remove them first")
        return 1
    ok, msg = run_hook(box, rid, "backup", runner)
    runner.say("  backup: " + msg)
    if not ok:
        runner.say(f"REFUSED: the backup hook failed -- {rid} left running")
        return 1
    units = box.units(rid)
    if units:
        runner.run(["systemctl", "stop"] + units)
        # Persistent: a mask survives reboot and daemon-reload, and beats the
        # quadlet generator's output. `disable` alone does not stop a Wants= pull.
        runner.run(["systemctl", "mask"] + units)
        runner.run(["systemctl", "daemon-reload"])
    box.installed().pop(rid, None)
    box.save_state(runner)
    runner.say(f"  {rid}: removed (config kept in {box.env_dir / (rid + '.env')}, data volumes kept: "
               f"{', '.join(role.get('data_volumes') or []) or 'none'})")
    if not runner.apply:
        runner.say("dry run -- nothing changed. Re-run with --apply to act.")
    return 0


def cmd_configure(box: Box, runner: Runner, rid: str, sets: Dict[str, str]) -> int:
    if rid not in box.roles:
        raise SystemExit(f"awnix role: unknown role {rid!r}")
    if not sets:
        runner.say("nothing to configure: pass --set KEY=VALUE")
        return 1
    inst = box.installed().get(rid)
    prior = (inst or {}).get("config") or {}
    values, errors = resolve_config(rid, box.roles[rid], prior, sets)
    if errors:
        for e in errors:
            runner.say("REFUSED: " + e)
        return 1
    runner.write_file(box.env_dir / f"{rid}.env",
                      render_env(rid, box.roles[rid], values, box_identity(box)), mode=0o640)
    if inst:
        inst["config"] = values
        box.save_state(runner)
        units = box.units(rid)
        if units:
            runner.run(["systemctl", "restart"] + units)
    else:
        runner.say(f"  {rid} is not installed; the values apply on `awnix role add {rid}`")
    if not runner.apply:
        runner.say("dry run -- nothing changed. Re-run with --apply to act.")
    return 0


def cmd_hook(box: Box, runner: Runner, rid: str, kind: str, params: Dict[str, str]) -> int:
    """Run one declared hook on demand (restore/replicate take --param)."""
    if rid not in box.roles:
        raise SystemExit(f"awnix role: unknown role {rid!r}")
    if kind not in ("backup", "restore", "replicate"):
        runner.say(f"awnix role hook: kind must be backup|restore|replicate, got {kind!r}")
        return 2
    hook = (box.roles[rid].get("hooks") or {}).get(kind) or {}
    if hook.get("note"):
        runner.say(f"  note: {hook['note']}")
    if rid not in box.installed():
        runner.say(f"  {rid} is not recorded as installed by `awnix role`; running anyway")
    ok, msg = run_hook(box, rid, kind, runner, params)
    runner.say(("  " if ok else "FAILED: ") + msg)
    if ok and not runner.apply:
        runner.say("dry run -- nothing changed. Re-run with --apply to act.")
    return 0 if ok else 1


def parse_params(pairs: Sequence[str]) -> Dict[str, str]:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep or not re.match(r"^[a-z_][a-z0-9_]*$", k):
            raise SystemExit(f"awnix role: --param wants name=value (lower_snake name), got {p!r}")
        out[k] = v
    return out


def role_main(argv: Sequence[str], runner: Optional[Runner] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix role", description="Add and remove awnix OS roles.")
    ap.add_argument("verb", choices=["list", "status", "add", "remove", "configure", "hook"])
    ap.add_argument("role", nargs="?")
    ap.add_argument("kind", nargs="?", help="hook: backup|restore|replicate")
    ap.add_argument("--param", dest="params", action="append", default=[], metavar="NAME=VALUE",
                    help="hook placeholder value (restore/replicate), e.g. backup_id=...")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--apply", action="store_true", help="act (default is a dry run)")
    ap.add_argument("--state", default="", help=f"state file (default {STATE_DEFAULT})")
    ap.add_argument("--catalog", default="", help="catalog JSON (default: the installed one)")
    args = ap.parse_args(list(argv))
    r = runner or Runner(apply=args.apply)
    r.apply = args.apply
    box = Box(load_catalog(Path(args.catalog) if args.catalog else None),
              Path(args.state or os.environ.get("AWNIX_ROLES_STATE", "") or STATE_DEFAULT))
    if args.verb in ("add", "remove", "configure", "hook") and not args.role:
        ap.error(f"{args.verb} needs a role")
    if args.verb == "hook":
        if not args.kind:
            ap.error("hook needs a kind: backup|restore|replicate")
        return cmd_hook(box, r, args.role, args.kind, parse_params(args.params))
    if args.verb == "list":
        return cmd_list(box, r)
    if args.verb == "status":
        return cmd_status(box, r, args.role)
    if args.verb == "add":
        return cmd_add(box, r, args.role, parse_sets(args.sets))
    if args.verb == "remove":
        return cmd_remove(box, r, args.role)
    return cmd_configure(box, r, args.role, parse_sets(args.sets))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`awnix role ...` (installed as /usr/bin/awnix)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.split("\n\n")[1] if __doc__ else "awnix role ...")
        return 0 if args else 2
    if args[0] != "role":
        print(f"awnix: unknown command {args[0]!r} (only `role` is implemented here)", file=sys.stderr)
        return 2
    return role_main(args[1:])


if __name__ == "__main__":
    sys.exit(main())
