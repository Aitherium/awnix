#!/usr/bin/python3.11
"""aither-eula -- licence acceptance at first boot for the proprietary awnix images.

Installed as /usr/bin/aither-eula in the garg-appliance, aitheros and aitheros-cloud
images ONLY. The public awnix images ship none of this (check_eula_firstboot EUL005).

What it does
  * `show`    prints the licence document for this image variant (from the manifest
              /usr/share/aither/eula/eula.json), with its version and sha256.
  * `accept accept:<sha256>` records acceptance of EXACTLY that text. A sha that is not
              the sha of the installed text is refused, so a stale browser tab or seed can
              never accept a document the operator did not see.
  * `accept decline` records a decline. Setup stays pending (exit 1).
  * `gate`    is the systemd ExecCondition of every licence-gated product unit: exit 0
              lets the unit start; exit 1 makes systemd SKIP the unit. Boot is never held.
  * `release` starts the gated units (--no-block) once acceptance is recorded; it is run
              by aither-eula-release.service, triggered by aither-eula.path.

State (never secrets)
  /var/lib/aither/eula/acceptance.json  {schema:1,id,version,sha256,accepted_at,via,by,
                                         peer,boot_id,image_digest?}
  /var/lib/aither/eula/history.jsonl    append-only, one line per accept/decline
  /var/lib/aither/eula/declined.json    present while the latest decision is a decline

Exit codes: 0 accepted / grandfathered / not-required / ok, 1 pending / declined / stale
needing re-acceptance / refused, 2 could-not-judge (manifest or text missing on a
proprietary variant, text sha mismatch, unwritable state). Fail closed: a present
manifest means a proprietary image, so an unset, public or unknown AWNIX_VARIANT next to
a manifest is 'invalid' (exit 2, gate skips), never 'not-required'.

Channels (`via` in the record): the caller names its channel in AWNIX_SETUP_VIA
(tty|web|seed|answers|cli). When no caller names one, an interactive terminal records
'cli' and anything else records 'unattributed' -- never a guessed channel. `gate`
accepts from the answers file /etc/aither/setup-answers.json key "eula_accept"
("accept:<sha256>" or the bare sha256) and records via=answers.

Existing installs: a document may list `grandfather_if` paths. When no decision is
recorded and one of them exists (a box that completed first boot before this plane
shipped), the state is 'grandfathered' (exit 0): the product keeps running and setup
still offers the terms. A decline always wins over grandfathering.

Test seam: AITHER_EULA_ROOT prefixes every absolute path; AITHER_EULA_SYSTEMCTL names the
systemctl binary `release` runs. Stdlib only, Python 3.10 compatible.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def _wt(path, text: str) -> None:
    """Write UTF-8 with LF on every platform (never the locale codec or CRLF)."""
    path.write_bytes(text.encode("utf-8"))

SCHEMA = 1
VERBS = ("show", "status", "options", "accept", "gate", "release", "hash")

#: Variants that MUST carry a licence document. A proprietary variant whose manifest or
#: text is missing fails closed (exit 2) instead of reading as "not required".
PROPRIETARY_VARIANTS = frozenset({"garg-appliance", "aitheros", "aitheros-cloud"})

VIA_CHANNELS = ("tty", "web", "seed", "answers", "cli")
UNATTRIBUTED = "unattributed"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PRINTABLE = re.compile(r"[^\x20-\x7e]")


# ── paths ───────────────────────────────────────────────────────────────────────────
def root() -> Path:
    return Path(os.environ.get("AITHER_EULA_ROOT") or "/")


def _at(path: str) -> Path:
    return root() / path.lstrip("/")


def manifest_path() -> Path:
    return _at("/usr/share/aither/eula/eula.json")


def state_dir() -> Path:
    return _at("/var/lib/aither/eula")


def acceptance_path() -> Path:
    return state_dir() / "acceptance.json"


def history_path() -> Path:
    return state_dir() / "history.jsonl"


def declined_path() -> Path:
    return state_dir() / "declined.json"


def release_env_path() -> Path:
    return _at("/usr/lib/awnix/release.env")


def answers_path() -> Path:
    return _at("/etc/aither/setup-answers.json")


# ── helpers ─────────────────────────────────────────────────────────────────────────
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_env_file(path: Path) -> dict[str, str]:
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
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def variant() -> str:
    return os.environ.get("AWNIX_VARIANT") or parse_env_file(release_env_path()).get(
        "AWNIX_VARIANT", "")


def major(version: str) -> int | None:
    m = re.match(r"^\s*v?(\d+)", str(version or ""))
    return int(m.group(1)) if m else None


def _clean(value: str | None, limit: int = 128) -> str:
    return _PRINTABLE.sub("", str(value or ""))[:limit]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            print(f"aither-eula: could not remove {tmp}: {e}", file=sys.stderr)
        raise


def _append_history(entry: dict[str, Any]) -> None:
    p = history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ── resolution ──────────────────────────────────────────────────────────────────────
class Resolved:
    """What this image variant must accept. state is 'ok', 'not-required' or 'invalid'."""

    def __init__(self, state: str, var: str, doc: dict | None = None,
                 text: str = "", detail: str = "") -> None:
        self.state = state
        self.variant = var
        self.doc = doc or {}
        self.text = text
        self.detail = detail

    @property
    def sha256(self) -> str:
        return str(self.doc.get("sha256", ""))


def resolve() -> Resolved:
    var = variant()
    proprietary = var in PROPRIETARY_VARIANTS
    mpath = manifest_path()
    if not mpath.is_file():
        if proprietary:
            return Resolved("invalid", var, detail=f"licence manifest missing: {mpath}")
        return Resolved("not-required", var, detail="no licence manifest on this image")
    # From here the manifest is present. It ships ONLY on proprietary images
    # (check_eula_firstboot EUL005), so every path below fails closed: an unset, public
    # or unknown variant is a broken image (release.env missing or not overridden by the
    # leaf Containerfile, EUL010), not a licence-free one.
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Resolved("invalid", var,
                        detail=f"licence manifest unreadable ({e.__class__.__name__})")
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA \
            or not isinstance(manifest.get("documents"), list):
        return Resolved("invalid", var, detail="licence manifest is not schema 1")
    doc = next((d for d in manifest["documents"]
                if isinstance(d, dict) and var and var in (d.get("variants") or [])), None)
    if doc is None:
        if not var:
            return Resolved("invalid", var, detail="licence manifest present but AWNIX_VARIANT "
                            "is unset (no /usr/lib/awnix/release.env)")
        return Resolved("invalid", var, detail=f"licence manifest present but variant {var} "
                        "has no licence document")
    for key in ("id", "title", "version", "file", "sha256"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            return Resolved("invalid", var, doc, detail=f"licence document lacks '{key}'")
    base = mpath.parent.resolve()
    tpath = (mpath.parent / doc["file"]).resolve()
    if base not in tpath.parents:
        return Resolved("invalid", var, doc, detail="licence file escapes the manifest dir")
    try:
        data = tpath.read_bytes()
    except OSError:
        return Resolved("invalid", var, doc, detail=f"licence text missing: {doc['file']}")
    got = sha256_bytes(data)
    if got != doc["sha256"]:
        return Resolved("invalid", var, doc,
                        detail=f"licence text sha256 {got[:12]} != manifest {doc['sha256'][:12]}")
    return Resolved("ok", var, doc, text=data.decode("utf-8", errors="replace"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "corrupt"


def status() -> tuple[int, dict[str, Any]]:
    r = resolve()
    out: dict[str, Any] = {"schema": SCHEMA, "state": r.state, "variant": r.variant,
                           "id": r.doc.get("id"), "version": r.doc.get("version"),
                           "sha256": r.doc.get("sha256"), "accepted": None,
                           "reaccept_required": False}
    if r.state == "not-required":
        out["detail"] = r.detail
        return 0, out
    if r.state == "invalid":
        out["detail"] = r.detail
        return 2, out
    rec = _read_json(acceptance_path())
    declined = _read_json(declined_path())
    if rec == "corrupt" or (rec is not None and not isinstance(rec, dict)):
        out.update(state="pending", detail="acceptance record unreadable; accept again")
        return 1, out
    if declined not in (None, "corrupt") and isinstance(declined, dict) \
            and declined.get("sha256") == r.sha256:
        out.update(state="declined", detail="the licence terms were declined")
        return 1, out
    if not rec:
        held = _grandfathered_by(r.doc)
        if held:
            out.update(state="grandfathered",
                       detail=f"installed before the licence gate ({held}); the terms are "
                              "still offered in setup")
            return 0, out
        out.update(state="pending", detail="the licence terms have not been accepted")
        return 1, out
    out["accepted"] = {k: rec.get(k) for k in ("id", "version", "sha256", "accepted_at",
                                              "via", "by")}
    if rec.get("id") == r.doc.get("id") and rec.get("sha256") == r.sha256:
        out["state"] = "accepted"
        return 0, out
    out["state"] = "stale"
    reaccept = r.doc.get("reaccept", "major")
    if rec.get("id") != r.doc.get("id"):
        need = True
    elif reaccept == "never":
        need = False
    else:
        old, new = major(str(rec.get("version", ""))), major(str(r.doc.get("version", "")))
        need = old is None or new is None or old != new
    out["reaccept_required"] = need
    out["detail"] = ("the licence changed; accept the new version" if need
                     else "the licence text changed within the accepted version line")
    return (1 if need else 0), out


def _grandfathered_by(doc: dict) -> str:
    """The first existing grandfather_if path, or ''. Only absolute paths count."""
    for p in doc.get("grandfather_if") or []:
        if isinstance(p, str) and p.startswith("/") and _at(p).exists():
            return p
    return ""


def marker(code: int, st: dict[str, Any]) -> str:
    """The serial/boot-proof marker: `aither-eula: <state> [sha=<12>] [via=<ch>]`."""
    parts = [f"aither-eula: {st.get('state')}"]
    sha = (st.get("accepted") or {}).get("sha256") or st.get("sha256")
    if sha:
        parts.append(f"sha={str(sha)[:12]}")
    via = (st.get("accepted") or {}).get("via")
    if via and st.get("state") in ("accepted", "stale"):
        parts.append(f"via={via}")
    if st.get("variant"):
        parts.append(f"variant={st['variant']}")
    return " ".join(parts)


# ── verbs ───────────────────────────────────────────────────────────────────────────
def cmd_show(args: list[str]) -> int:
    r = resolve()
    as_json = "--json" in args
    if r.state != "ok":
        body = {"schema": SCHEMA, "state": r.state, "variant": r.variant, "detail": r.detail}
        print(json.dumps(body) if as_json else f"aither-eula: {r.state}: {r.detail}")
        return 0 if r.state == "not-required" else 2
    if as_json:
        print(json.dumps({"schema": SCHEMA, "id": r.doc["id"], "title": r.doc["title"],
                          "version": r.doc["version"], "sha256": r.sha256, "text": r.text}))
    else:
        print(f"{r.doc['title']}  (version {r.doc['version']}, sha256 {r.sha256[:12]})")
        print()
        sys.stdout.write(r.text if r.text.endswith("\n") else r.text + "\n")
    return 0


def cmd_status(args: list[str]) -> int:
    code, st = status()
    print(json.dumps(st) if "--json" in args else marker(code, st))
    return code


def cmd_options(args: list[str]) -> int:
    r = resolve()
    if r.state == "not-required":
        opts: list[dict[str, str]] = []
    elif r.state != "ok":
        print(json.dumps([]) if "--json" in args else f"aither-eula: {r.detail}",
              file=sys.stdout)
        return 2
    else:
        opts = [
            {"value": f"accept:{r.sha256}",
             "label": f"I accept {r.doc['title']} (version {r.doc['version']})",
             "detail": f"sha256 {r.sha256[:12]}"},
            {"value": "decline", "label": "I do not accept",
             "detail": "setup stays pending and the product does not start"},
        ]
    if "--json" in args:
        print(json.dumps(opts))
    else:
        for o in opts:
            print(o["value"])
    return 0


def _interactive() -> bool:
    """A person at a shell: stdin AND stdout are terminals. A setup UI captures stdout, so
    a wizard that forgot AWNIX_SETUP_VIA reads as unattributed, never as 'cli'."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _actor() -> tuple[str, str, str]:
    """(via, by, peer). via is what the caller declared in AWNIX_SETUP_VIA; with no
    declaration it is 'cli' only on an interactive terminal, else 'unattributed'."""
    via = os.environ.get("AWNIX_SETUP_VIA", "")
    if via not in VIA_CHANNELS:
        via = "cli" if _interactive() else UNATTRIBUTED
    by = os.environ.get("AWNIX_SETUP_ACTOR") or os.environ.get("SUDO_USER") \
        or os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
    return via, _clean(by, 64), _clean(os.environ.get("AWNIX_SETUP_PEER", ""), 128)


def _boot_id() -> str:
    p = _at("/proc/sys/kernel/random/boot_id")
    try:
        return _clean(p.read_text(encoding="utf-8").strip(), 64)
    except OSError:
        return ""


def cmd_accept(args: list[str]) -> int:
    pos = [a for a in args if not a.startswith("--")]
    if len(pos) != 1:
        print("usage: aither-eula accept <accept:SHA256|decline>", file=sys.stderr)
        return 2
    value = pos[0].strip()
    r = resolve()
    if r.state == "not-required":
        print(f"aither-eula: not-required ({r.detail})")
        return 0
    if r.state != "ok":
        print(f"aither-eula: cannot judge: {r.detail}", file=sys.stderr)
        return 2
    return _record(r, value, *_actor())


def _record(r: Resolved, value: str, via: str, by: str, peer: str) -> int:
    entry: dict[str, Any] = {"schema": SCHEMA, "id": r.doc["id"], "version": r.doc["version"],
                             "sha256": r.sha256, "at": _now(), "via": via, "by": by,
                             "peer": peer, "boot_id": _boot_id()}
    try:
        if value == "decline":
            entry["decision"] = "decline"
            _append_history(entry)
            _atomic_write(declined_path(), json.dumps(entry, indent=2, sort_keys=True) + "\n")
            print("aither-eula: declined; setup stays pending and the product does not start")
            return 1
        if not value.startswith("accept:"):
            print("aither-eula: value must be accept:<sha256> or decline", file=sys.stderr)
            return 1
        sha = value[len("accept:"):].strip().lower()
        if not SHA_RE.match(sha):
            print("aither-eula: accept needs the 64-hex sha256 of the licence text shown",
                  file=sys.stderr)
            return 1
        if sha != r.sha256:
            print(f"aither-eula: refused: sha {sha[:12]} is not the installed licence text "
                  f"({r.sha256[:12]}); show it again and accept what is shown",
                  file=sys.stderr)
            return 1
        record: dict[str, Any] = {"schema": SCHEMA, "id": r.doc["id"],
                                  "version": r.doc["version"], "sha256": r.sha256,
                                  "accepted_at": entry["at"], "via": via, "by": by,
                                  "peer": peer, "boot_id": entry["boot_id"]}
        digest = _clean(os.environ.get("AWNIX_IMAGE_DIGEST")
                        or parse_env_file(release_env_path()).get("AWNIX_IMAGE_DIGEST", ""), 80)
        if digest:
            record["image_digest"] = digest
        entry["decision"] = "accept"
        _append_history(entry)
        _atomic_write(acceptance_path(), json.dumps(record, indent=2, sort_keys=True) + "\n")
        declined_path().unlink(missing_ok=True)
    except OSError as e:
        print(f"aither-eula: cannot record the decision ({e.__class__.__name__}: "
              f"{state_dir()})", file=sys.stderr)
        return 2
    print(f"aither-eula: accepted sha={r.sha256[:12]} via={via}")
    return 0


def _accept_from_answers() -> None:
    """The answers channel: /etc/aither/setup-answers.json {"eula_accept": "accept:<sha>"}
    (or the bare sha). Recorded only while no decision exists and only for the exact
    installed sha; anything else is ignored and the gate stays shut."""
    _, st = status()
    if st.get("state") not in ("pending", "grandfathered"):
        return
    ans = _read_json(answers_path())
    if not isinstance(ans, dict):
        return
    raw = str(ans.get("eula_accept") or "").strip().lower()
    if not raw:
        return
    value = raw if raw.startswith("accept:") else f"accept:{raw}"
    r = resolve()
    if r.state == "ok":
        _record(r, value, "answers", "answers-file", "")


def cmd_gate(args: list[str]) -> int:
    """ExecCondition: 0 = start the unit, 1 = skip it. Never 2 -- 255 would be a failure."""
    try:
        _accept_from_answers()
    except OSError as e:  # never let the answers channel turn a skip into a failure
        print(f"aither-eula: answers file not applied ({e.__class__.__name__})",
              file=sys.stderr)
    code, st = status()
    line = marker(code, st)
    print(line, file=sys.stderr)
    return 0 if code == 0 else 1


def _systemctl() -> str:
    return os.environ.get("AITHER_EULA_SYSTEMCTL", "systemctl")


def cmd_release(args: list[str]) -> int:
    code, st = status()
    if code != 0:
        print(f"{marker(code, st)} -- nothing released")
        return 1
    if st.get("state") == "not-required":
        print("aither-eula: not-required -- nothing gated")
        return 0
    units = [u for u in (resolve().doc.get("gated_units") or []) if isinstance(u, str)]
    failed = []
    for unit in units:
        argv = [_systemctl(), "start", "--no-block", unit]
        try:
            rc = subprocess.run(argv, capture_output=True, timeout=60).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = 127
        print(f"aither-eula: release {unit} rc={rc}")
        if rc != 0:
            failed.append(unit)
    return 2 if failed else 0


def cmd_hash(args: list[str]) -> int:
    pos = [a for a in args if not a.startswith("--")]
    if len(pos) != 1:
        print("usage: aither-eula hash <file>", file=sys.stderr)
        return 2
    try:
        print(sha256_bytes(Path(pos[0]).read_bytes()))
    except OSError as e:
        print(f"aither-eula: {e}", file=sys.stderr)
        return 2
    return 0


# ── self-test ───────────────────────────────────────────────────────────────────────
def self_test() -> int:
    import contextlib
    import io

    failures: list[str] = []
    saved = {k: os.environ.get(k) for k in ("AITHER_EULA_ROOT", "AWNIX_VARIANT",
                                              "AWNIX_SETUP_VIA", "AITHER_EULA_SYSTEMCTL")}

    def run(argv: list[str]) -> tuple[int, str]:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, buf.getvalue() + err.getvalue()

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            os.environ["AITHER_EULA_ROOT"] = str(base)
            text = b"TEST LICENCE\nterms\n"
            sha = sha256_bytes(text)
            d = base / "usr/share/aither/eula"
            (d / "t").mkdir(parents=True)
            (d / "t/LICENSE.txt").write_bytes(text)
            doc = {"id": "t-eula", "title": "Test", "version": "1.0", "variants": ["aitheros"],
                   "file": "t/LICENSE.txt", "sha256": sha, "reaccept": "major",
                   "gated_units": ["x.service"]}
            os.environ["AWNIX_VARIANT"] = "awnix"
            check("public image (no manifest) not-required", run(["status"])[0] == 0)
            check("public image gate passes", run(["gate"])[0] == 0)
            _wt((d / "eula.json"), json.dumps({"schema": 1, "documents": [doc]}))
            # fail closed: a manifest means a proprietary image, whatever the variant says
            check("manifest + public variant is invalid", run(["status"])[0] == 2)
            check("manifest + public variant gate skips", run(["gate"])[0] == 1)
            os.environ.pop("AWNIX_VARIANT", None)
            rc, out = run(["status"])
            check("manifest + unset variant is invalid", rc == 2 and "invalid" in out)
            check("manifest + unset variant gate skips", run(["gate"])[0] == 1)
            (base / "usr/lib/awnix").mkdir(parents=True)
            _wt(base / "usr/lib/awnix/release.env", "AWNIX_VARIANT=aitheros\n")
            check("variant read from release.env", run(["status"])[0] == 1)
            os.environ["AWNIX_VARIANT"] = "aitheros"
            rc, out = run(["status"])
            check("pending exit 1", rc == 1 and "pending" in out)
            check("gate skips while pending", run(["gate"])[0] == 1)
            check("show json", json.loads(run(["show", "--json"])[1])["sha256"] == sha)
            check("wrong sha refused", run(["accept", "accept:" + "0" * 64])[0] == 1)
            check("garbage refused", run(["accept", "yes"])[0] == 1)
            # grandfathering: an existing install keeps running, a decline still wins
            gf = base / "etc/gargbot/firstboot.done"
            gf.parent.mkdir(parents=True)
            gf.write_bytes(b"")
            _wt((d / "eula.json"), json.dumps({"schema": 1, "documents": [
                dict(doc, grandfather_if=["/etc/gargbot/firstboot.done"])]}))
            rc, out = run(["status"])
            check("grandfathered exit 0", rc == 0 and "grandfathered" in out)
            check("grandfathered gate passes", run(["gate"])[0] == 0)
            gf.unlink()
            check("no marker = pending again", run(["gate"])[0] == 1)
            # the answers channel records via=answers for the exact sha only
            ans = base / "etc/aither/setup-answers.json"
            ans.parent.mkdir(parents=True)
            _wt(ans, json.dumps({"eula_accept": "0" * 64}))
            check("answers with a wrong sha stays shut", run(["gate"])[0] == 1)
            _wt(ans, json.dumps({"eula_accept": sha}))
            check("answers with the sha opens the gate", run(["gate"])[0] == 0)
            rec = json.loads((base / "var/lib/aither/eula/acceptance.json").read_text())
            check("answers recorded via=answers", rec["via"] == "answers")
            ans.unlink()
            for f in ("acceptance.json", "history.jsonl"):
                (base / "var/lib/aither/eula" / f).unlink()
            # no declared channel off a terminal is unattributed, never a guessed 'cli'
            os.environ.pop("AWNIX_SETUP_VIA", None)
            if not _interactive():
                run(["accept", f"accept:{sha}"])
                rec = json.loads((base / "var/lib/aither/eula/acceptance.json").read_text())
                check("undeclared channel is unattributed", rec["via"] == UNATTRIBUTED)
                for f in ("acceptance.json", "history.jsonl"):
                    (base / "var/lib/aither/eula" / f).unlink()
            os.environ["AWNIX_SETUP_VIA"] = "tty"
            check("decline exit 1", run(["accept", "decline"])[0] == 1)
            check("declined state", "declined" in run(["status"])[1])
            rc, out = run(["accept", f"accept:{sha}"])
            check("accept exit 0", rc == 0 and "via=tty" in out)
            rec = json.loads((base / "var/lib/aither/eula/acceptance.json").read_text())
            check("record binds sha+time+via", rec["sha256"] == sha and rec["via"] == "tty"
                  and rec["accepted_at"].endswith("Z"))
            check("history has 2 lines", len((base / "var/lib/aither/eula/history.jsonl")
                                             .read_text().splitlines()) == 2)
            check("accepted gate passes", run(["gate"])[0] == 0)
            os.environ["AITHER_EULA_SYSTEMCTL"] = sys.executable  # `python start ...` fails
            check("release reports a failed start", run(["release"])[0] == 2)
            # minor edit, same major: stale but not re-accept
            text2 = text + b"typo fixed\n"
            (d / "t/LICENSE.txt").write_bytes(text2)
            doc2 = dict(doc, sha256=sha256_bytes(text2), version="1.1")
            _wt((d / "eula.json"), json.dumps({"schema": 1, "documents": [doc2]}))
            rc, out = run(["status", "--json"])
            check("minor stale passes", rc == 0 and json.loads(out)["state"] == "stale")
            doc3 = dict(doc2, version="2.0")
            _wt((d / "eula.json"), json.dumps({"schema": 1, "documents": [doc3]}))
            check("major stale needs re-accept", run(["gate"])[0] == 1)
            # tamper: text changed under the manifest
            (d / "t/LICENSE.txt").write_bytes(b"tampered\n")
            check("tamper exit 2", run(["status"])[0] == 2)
            check("tamper gate skips", run(["gate"])[0] == 1)
            (d / "eula.json").unlink()
            check("proprietary w/o manifest exit 2", run(["status"])[0] == 2)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for f in failures:
        print(f"SELF-TEST FAIL: {f}")
    print(f"aither-eula self-test: {'FAIL' if failures else 'ok'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "--list-verbs":
        print("\n".join(VERBS))
        return 0
    if argv[0] == "--self-test":
        return self_test()
    verb, rest = argv[0], argv[1:]
    handler = {"show": cmd_show, "status": cmd_status, "options": cmd_options,
               "accept": cmd_accept, "gate": cmd_gate, "release": cmd_release,
               "hash": cmd_hash}.get(verb)
    if handler is None:
        print(f"aither-eula: unknown verb {verb!r} (try --list-verbs)", file=sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main())
