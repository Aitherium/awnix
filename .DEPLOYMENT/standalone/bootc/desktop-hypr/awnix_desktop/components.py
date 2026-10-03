"""Bake aw* bricks into the awnix desktop image FROM the component lock.

    python3.11 -m awnix_desktop.components bake <id>... [--out FILE]
    python3.11 -m awnix_desktop.components --self-test

Run once, by Containerfile.awnix-hypr. It does not install anything itself: it runs
`awnix-component install <id>...` -- the tool that builds each brick's venv with
`pip --require-hashes --no-deps` from the lock's hashed closure -- with its paths moved
into the immutable image:

    venvs   /usr/lib/awnix/components/<id>/<pin12>/   (a `current` symlink beside them)
    shims   /usr/bin/<command>                        (exec through `current`)

and then writes the components.d rows that make `awnix component list` call them what
they now are: baked into this image, at exactly the lock's version and pin, not
removable, replaced by the next image. Writing those rows FROM the state the install
recorded (never by hand) is what keeps them true after a lock refresh.

Two traps it closes. awnix-component returns "baked" and installs NOTHING for an id a
components.d row already names, so the install runs with an empty components.d. And a
brick the lock marks unavailable or license-gated must fail the build, not quietly bake
nothing.

Exit: 0 baked * 1 an install failed or a row is not installable * 2 could not judge.
Stdlib only, Python 3.10-compatible.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

STATE_DIR = "/usr/lib/awnix/components"
SHIM_DIR = "/usr/bin"
LOCK = "/usr/share/awnix/components.lock.json"
OUT = "/usr/share/awnix/components.d/aw-family.yaml"
TOOL = "/usr/libexec/awnix/awnix-component"

Runner = Callable[[List[str], Dict[str, str]], int]


class CannotJudgeError(RuntimeError):
    """Exit 2."""


def _run(argv: List[str], env: Dict[str, str]) -> int:
    return subprocess.run(argv, env=env, check=False).returncode


def lock_rows(lock_path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CannotJudgeError("cannot read the component lock %s: %s" % (lock_path, exc)) from None
    return {str(r.get("id")): r for r in lock.get("components") or [] if isinstance(r, dict)}


def installable(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why a lock row cannot be baked into a public image, or None."""
    if row is None:
        return "not in the lock"
    if not row.get("available"):
        return "unavailable in the lock (%s)" % (row.get("reason") or "no reason given")
    if row.get("requires_license"):
        return "license-gated: a public image may not bake it"
    if row.get("kind") not in ("pypi", "git"):
        return "kind %r is not a python brick" % row.get("kind")
    return None


def render_rows(ids: Sequence[str], state: Dict[str, Any], rows: Dict[str, Dict[str, Any]],
                state_dir: str) -> str:
    """The components.d YAML (the flat shape awnix-component parses without PyYAML)."""
    out = ["# GENERATED at image build by awnix_desktop.components from the component lock.",
           "# Each row is a brick baked into THIS image: its venv lives under",
           "# %s/<id>, its commands are shims in /usr/bin, and the next image" % state_dir,
           "# replaces it. Not removable: remove it by building an image without it.",
           "schema: 1", "components:"]
    comps = state.get("components") or {}
    for cid in ids:
        st = comps.get(cid) or {}
        if st.get("status") != "installed" or st.get("pin") != rows[cid].get("pin"):
            raise CannotJudgeError("%s is not installed at the lock's pin" % cid)
        provides = (st.get("meta") or {}).get(st["pin"], {}).get("provides") or []
        out += ["  - id: %s" % cid,
                "    kind: baked",
                '    version: "%s"' % st.get("version", ""),
                '    source: "component lock %s, venv %s/%s"' % (st["pin"], state_dir, cid),
                "    provides: [%s]" % ", ".join(provides),
                "    removable: false"]
    return "\n".join(out) + "\n"


def bake(ids: Sequence[str], *, lock: Path, out: Path, state_dir: str = STATE_DIR,
         shim_dir: str = SHIM_DIR, tool: str = TOOL, run: Runner = _run,
         err: Any = None) -> int:
    err = sys.stderr if err is None else err
    rows = lock_rows(lock)
    bad = {cid: installable(rows.get(cid)) for cid in ids}
    bad = {k: why for k, why in bad.items() if why}
    for cid, why in sorted(bad.items()):
        err.write("awnix-desktop bake: %s: %s\n" % (cid, why))
    if bad or not ids:
        return 1
    with tempfile.TemporaryDirectory() as empty:
        env = {**os.environ,
               "AWNIX_COMPONENT_LOCK": str(lock),
               "AWNIX_COMPONENT_STATE_DIR": state_dir,
               "AWNIX_SHIM_DIR": shim_dir,
               "AWNIX_COMPONENTS_D": empty,
               "AWNIX_COMPONENT_LOG": str(Path(empty) / "components.log")}
        rc = run([tool, "install", *ids], env)
        if rc != 0:
            log = Path(empty) / "components.log"
            if log.is_file():
                err.write(log.read_text(encoding="utf-8", errors="replace")[-4000:])
            err.write("awnix-desktop bake: awnix-component install exited %d\n" % rc)
            return 1
    try:
        state = json.loads((Path(state_dir) / "state.json").read_text(encoding="utf-8"))
        text = render_rows(ids, state, rows, state_dir)
    except (OSError, ValueError, CannotJudgeError) as exc:
        err.write("awnix-desktop bake: %s\n" % exc)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return 0


def self_test() -> int:
    fails = 0

    def chk(cond: bool, label: str) -> None:
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        if not cond:
            fails += 1

    pin = "sha256:" + "a" * 64
    lock_doc = {"schema": 1, "components": [
        {"id": "awm", "kind": "pypi", "version": "0.6.0", "pin": pin, "available": True,
         "requires_license": False, "provides": ["awm"]},
        {"id": "gone", "kind": "pypi", "available": False, "reason": "not published"},
        {"id": "paid", "kind": "pypi", "available": True, "requires_license": True, "pin": pin},
    ]}
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        lock = t / "lock.json"
        lock.write_text(json.dumps(lock_doc), encoding="utf-8")
        seen: Dict[str, Any] = {}

        def fake(argv: List[str], env: Dict[str, str]) -> int:
            seen["argv"], seen["env"] = argv, env
            sd = Path(env["AWNIX_COMPONENT_STATE_DIR"])
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "state.json").write_text(json.dumps({"components": {"awm": {
                "status": "installed", "pin": pin, "version": "0.6.0",
                "meta": {pin: {"provides": ["awm"]}}}}}), encoding="utf-8")
            return 0

        out = t / "d" / "aw-family.yaml"
        rc = bake(["awm"], lock=lock, out=out, state_dir=str(t / "sd"), run=fake,
                  err=open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
        text = out.read_text(encoding="utf-8") if out.is_file() else ""
        chk(rc == 0, "a lock row installs and bakes")
        chk(seen.get("argv", [])[1:] == ["install", "awm"], "it runs awnix-component install")
        env = seen.get("env") or {}
        chk(env.get("AWNIX_COMPONENTS_D") != "/usr/share/awnix/components.d"
            and env.get("AWNIX_SHIM_DIR") == SHIM_DIR,
            "the install sees an empty components.d and shims into /usr/bin")
        chk("kind: baked" in text and pin in text and 'version: "0.6.0"' in text
            and "provides: [awm]" in text, "the row carries the lock's version and pin")
        null = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        chk(bake(["gone"], lock=lock, out=out, run=fake, err=null) == 1,
            "an unavailable lock row fails the build")
        chk(bake(["paid"], lock=lock, out=out, run=fake, err=null) == 1,
            "a license-gated row fails the build")
        chk(bake(["nope"], lock=lock, out=out, run=fake, err=null) == 1,
            "an id the lock does not carry fails the build")
        chk(bake(["awm"], lock=lock, out=out, state_dir=str(t / "sd2"),
                 run=lambda a, e: 1, err=null) == 1, "a failed install fails the build")

        def wrong_pin(argv: List[str], env: Dict[str, str]) -> int:
            fake(argv, env)
            p = Path(env["AWNIX_COMPONENT_STATE_DIR"]) / "state.json"
            p.write_text(p.read_text(encoding="utf-8").replace("a" * 64, "b" * 64),
                         encoding="utf-8")
            return 0

        chk(bake(["awm"], lock=lock, out=out, state_dir=str(t / "sd3"), run=wrong_pin,
                 err=null) == 1, "a venv at another pin than the lock's fails the build")
        null.close()
        dead = False
        try:
            lock_rows(t / "missing.json")
        except CannotJudgeError:
            dead = True
        chk(dead, "an unreadable lock is could-not-judge")
    print("awnix-desktop components self-test: %s" % ("PASS" if not fails else "FAIL (%d)" % fails))
    return 0 if not fails else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix_desktop.components",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("verb", nargs="?", choices=["bake"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--lock", default=LOCK)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.verb != "bake":
        ap.print_help()
        return 2
    try:
        return bake(a.ids, lock=Path(a.lock), out=Path(a.out))
    except CannotJudgeError as exc:
        print("awnix-desktop bake: could not judge: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
