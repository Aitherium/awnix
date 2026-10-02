#!/usr/bin/python3.11
"""awnix-wheelhouse -- the image's vendored Python wheels, verified offline.

Installed as /usr/libexec/awnix/awnix-wheelhouse, so `awnix wheelhouse <verb>`
reaches it through the dispatcher.

Every awnix image carries the wheels its layers were installed from:

    /usr/share/awnix/wheelhouse/<layer>/*.whl + SHA256SUMS
    /usr/share/awnix/wheels/<layer>.lock.txt       (name==ver --hash=sha256:...)
    /usr/share/awnix/wheels/<layer>.constraints.txt

They live under /usr, so they are part of the bootc image and roll back WITH
it: `bootc rollback` returns the wheels the previous image was built from, and
a component reinstall after a rollback resolves offline against those.

Verbs:
  verify [--layer L] [--json] [--quiet]
        every wheel matches SHA256SUMS, every SHA256SUMS entry exists, no
        unlisted wheel, every lock row agrees with SHA256SUMS, and every lock
        shipped in the wheels dir has its wheelhouse directory (a lock with no
        wheels is a forgotten COPY, exit 1).
        exit 0 ok, 1 bad hash / missing / unlisted, 2 no wheelhouse.
  list [--layer L] [--json]
        name, version, layer and file of every vendored wheel.
  resolve <name>==<version> [--json]
        {found, path, sha256, layer}; the file is re-hashed first.
        exit 0 found, 1 not vendored (or tampered), 2 no wheelhouse.
  pip-args [--json] [--airgap]
        the flags a runtime pip caller prepends: --find-links=<dir> per layer,
        plus --no-index when /etc/pip.conf says no-index or the profile is
        airgap. exit 0, 2 when there is no wheelhouse (then only --no-index
        is printed on an air-gapped box, so the caller still cannot reach PyPI).
  --self-test   hermetic (tempdir), includes the flipped-byte tamper case.
  --list-verbs  one verb per line.

Environment (tests and non-standard roots): AWNIX_WHEELHOUSE_ROOT,
AWNIX_WHEELS_DIR, AWNIX_PIP_CONF, AWNIX_PROFILE_FILE.

Stdlib only; Python 3.10 compatible.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

VERBS = ("verify", "list", "resolve", "pip-args")
ROW_RE = re.compile(
    r"^([a-z0-9][a-z0-9._-]*)==(\S+) --hash=sha256:([0-9a-f]{64})"
    r"(?:\s+#.*?file=(\S+\.whl))?"
)


def _root() -> Path:
    return Path(os.environ.get("AWNIX_WHEELHOUSE_ROOT", "/usr/share/awnix/wheelhouse"))


def _wheels_dir() -> Path:
    return Path(os.environ.get("AWNIX_WHEELS_DIR", "/usr/share/awnix/wheels"))


def _pip_conf() -> Path:
    return Path(os.environ.get("AWNIX_PIP_CONF", "/etc/pip.conf"))


def _profile_file() -> Path:
    return Path(os.environ.get("AWNIX_PROFILE_FILE", "/usr/lib/awnix/profile"))


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wheel_name_version(filename: str) -> tuple[str, str]:
    parts = filename[:-4].split("-") if filename.endswith(".whl") else []
    if len(parts) < 5:
        return "", ""
    return normalize(parts[0]), parts[1]


def read_sums(layer_dir: Path) -> dict[str, str] | None:
    p = layer_dir / "SHA256SUMS"
    if not p.is_file():
        return None
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(\S+)$", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


def read_lock(layer: str) -> list[tuple[str, str, str, str]]:
    p = _wheels_dir() / f"{layer}.lock.txt"
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows.append((m.group(1), m.group(2), m.group(3), m.group(4) or ""))
    return rows


def layers(only: str | None = None) -> list[Path]:
    root = _root()
    if not root.is_dir():
        return []
    # An empty directory is a placeholder (pip.conf names every layer dir so pip
    # never warns about a missing find-links path), not a layer.
    dirs = sorted(
        d
        for d in root.iterdir()
        if d.is_dir() and ((d / "SHA256SUMS").is_file() or any(d.glob("*.whl")))
    )
    if only:
        dirs = [d for d in dirs if d.name == only]
    return dirs


def verify_layer(d: Path) -> dict:
    res = {"layer": d.name, "wheels": 0, "mismatched": [], "missing": [], "unlisted": []}
    sums = read_sums(d)
    wheels = sorted(p.name for p in d.glob("*.whl"))
    res["wheels"] = len(wheels)
    if sums is None:
        res["missing"].append("SHA256SUMS")
        sums = {}
    for fn, want in sorted(sums.items()):
        p = d / fn
        if not p.is_file():
            res["missing"].append(fn)
        elif sha256_file(p) != want:
            res["mismatched"].append(fn)
    res["unlisted"] = [w for w in wheels if w not in sums]
    for name, ver, sha, fn in read_lock(d.name):
        if fn and fn not in sums:
            res["missing"].append(fn)
        elif fn and sums.get(fn) != sha:
            res["mismatched"].append(f"{fn} (lock)")
        elif not fn and sha not in sums.values():
            res["missing"].append(f"{name}=={ver}")
    res["ok"] = not (res["mismatched"] or res["missing"] or res["unlisted"])
    return res


def locked_layers(only: str | None = None) -> list[str]:
    """Layers whose lock ships in the wheels dir with at least one row.

    A shipped lock is the image's claim that the layer's wheels are vendored; a
    forgotten COPY or a dropped layer leaves the lock with no wheel directory, and
    `verify` must fail on exactly that. Locks of ship_wheelhouse:false layers
    (garg-backend) are never copied into the image, so they are never judged.
    """
    wd = _wheels_dir()
    if not wd.is_dir():
        return []
    names = sorted(p.name[: -len(".lock.txt")] for p in wd.glob("*.lock.txt"))
    names = [n for n in names if read_lock(n)]
    return [n for n in names if n == only] if only else names


def cmd_verify(a: argparse.Namespace) -> int:
    dirs = layers(a.layer)
    have = {d.name for d in dirs}
    orphans = [n for n in locked_layers(a.layer) if n not in have]
    if not dirs and not orphans:
        out = {
            "ok": False,
            "layers": [],
            "error": f"no wheelhouse at {_root()}" + (f" for layer {a.layer}" if a.layer else ""),
        }
        rc = 2
    else:
        res = [verify_layer(d) for d in dirs]
        for n in orphans:
            res.append(
                {
                    "layer": n,
                    "wheels": 0,
                    "mismatched": [],
                    "missing": [f"wheelhouse/{n} ({len(read_lock(n))} locked wheel(s))"],
                    "unlisted": [],
                    "ok": False,
                }
            )
        res.sort(key=lambda r: r["layer"])
        out = {"ok": all(r["ok"] for r in res), "layers": res}
        rc = 0 if out["ok"] else 1
    if a.json:
        print(json.dumps(out, indent=2))
    elif not a.quiet or rc:
        if "error" in out:
            print(f"awnix-wheelhouse: {out['error']}", file=sys.stderr)
        for r in out["layers"]:
            bad = r["mismatched"] + r["missing"] + r["unlisted"]
            print(
                f"{r['layer']}: {r['wheels']} wheel(s) "
                + (
                    "ok"
                    if not bad
                    else f"BAD mismatched={r['mismatched']} missing={r['missing']} "
                    f"unlisted={r['unlisted']}"
                )
            )
    return rc


def cmd_list(a: argparse.Namespace) -> int:
    dirs = layers(a.layer)
    if not dirs:
        print(f"awnix-wheelhouse: no wheelhouse at {_root()}", file=sys.stderr)
        return 2
    rows = []
    for d in dirs:
        for p in sorted(d.glob("*.whl")):
            n, v = wheel_name_version(p.name)
            rows.append({"name": n, "version": v, "layer": d.name, "file": p.name})
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['layer']:14} {r['name']}=={r['version']}")
    return 0


def resolve(spec: str) -> tuple[int, dict]:
    m = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)\s*", spec)
    if not m:
        return 1, {"found": False, "reason": f"expected name==version, got {spec!r}"}
    name, ver = normalize(m.group(1)), m.group(2)
    dirs = layers()
    if not dirs:
        return 2, {"found": False, "reason": f"no wheelhouse at {_root()}"}
    for d in dirs:
        sums = read_sums(d) or {}
        for p in sorted(d.glob("*.whl")):
            if wheel_name_version(p.name) != (name, ver):
                continue
            got = sha256_file(p)
            if sums.get(p.name) != got:
                return 1, {
                    "found": False,
                    "path": str(p),
                    "layer": d.name,
                    "reason": "tampered: sha256 does not match SHA256SUMS",
                }
            return 0, {
                "found": True,
                "path": str(p),
                "sha256": got,
                "layer": d.name,
                "dir": str(d),
            }
    return 1, {"found": False, "reason": "not vendored"}


def cmd_resolve(a: argparse.Namespace) -> int:
    rc, out = resolve(a.spec)
    if a.json:
        print(json.dumps(out))
    else:
        print(out.get("path") if out.get("found") else f"not found: {out.get('reason')}")
    return rc


def airgapped() -> bool:
    p = _pip_conf()
    if p.is_file():
        cp = configparser.ConfigParser()
        try:
            cp.read(p, encoding="utf-8")
            for sec in ("global", "install"):
                if cp.has_option(sec, "no-index") and cp.get(sec, "no-index").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                ):
                    return True
        except configparser.Error as e:
            # An unreadable pip.conf is not proof the box is online: say so and fall
            # through to the profile check, which still forces --no-index on airgap.
            print(f"awnix-wheelhouse: cannot parse {p}: {e}", file=sys.stderr)
    pf = _profile_file()
    if pf.is_file() and "airgap" in pf.read_text(encoding="utf-8", errors="replace"):
        return True
    return os.environ.get("AWNIX_PROFILE", "") == "airgap"


def pip_args(force_airgap: bool = False) -> tuple[int, list[str]]:
    dirs = layers()
    args = [f"--find-links={d}" for d in dirs]
    if force_airgap or airgapped():
        args.append("--no-index")
    return (0 if dirs else 2), args


def cmd_pip_args(a: argparse.Namespace) -> int:
    rc, args = pip_args(a.airgap)
    print(json.dumps(args) if a.json else " ".join(args))
    return rc


# ----------------------------------------------------------------- self-test ---


def self_test() -> int:
    fails: list[str] = []

    def expect(cond: bool, label: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        if not cond:
            fails.append(label)

    saved = {
        k: os.environ.get(k)
        for k in (
            "AWNIX_WHEELHOUSE_ROOT",
            "AWNIX_WHEELS_DIR",
            "AWNIX_PIP_CONF",
            "AWNIX_PROFILE_FILE",
            "AWNIX_PROFILE",
        )
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            os.environ["AWNIX_WHEELHOUSE_ROOT"] = str(t / "wh")
            os.environ["AWNIX_WHEELS_DIR"] = str(t / "wheels")
            os.environ["AWNIX_PIP_CONF"] = str(t / "pip.conf")
            os.environ["AWNIX_PROFILE_FILE"] = str(t / "profile")
            os.environ.pop("AWNIX_PROFILE", None)
            ns = argparse.Namespace(layer=None, json=False, quiet=True)
            expect(cmd_verify(ns) == 2, "no wheelhouse -> verify exit 2")
            expect(resolve("awgit==1.0")[0] == 2, "no wheelhouse -> resolve exit 2")
            (t / "wh" / "components").mkdir(parents=True)
            expect(cmd_verify(ns) == 2, "only an empty placeholder dir -> still no wheelhouse (2)")
            d = t / "wh" / "base"
            d.mkdir(parents=True)
            (t / "wheels").mkdir()
            sums, lock = [], ["# awnix-wheels schema=1 layer=base"]
            for fn in ("awgit-1.0-py3-none-any.whl", "httpx-0.28.1-py3-none-any.whl"):
                (d / fn).write_bytes(b"PK\x03\x04 fake wheel " + fn.encode())
                h = sha256_file(d / fn)
                sums.append(f"{h}  {fn}")
                n, v = wheel_name_version(fn)
                lock.append(f"{n}=={v} --hash=sha256:{h}  # file={fn}")
            (d / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
            (t / "wheels" / "base.lock.txt").write_text(
                "\n".join(lock) + "\n", encoding="utf-8", newline="\n"
            )
            expect(cmd_verify(ns) == 0, "clean wheelhouse -> verify exit 0")
            rc, r = resolve("awgit==1.0")
            expect(rc == 0 and r["found"] and r["layer"] == "base", "resolve finds a vendored pin")
            expect(resolve("Awgit==2.0")[0] == 1, "resolve refuses an unvendored version")
            rc, args = pip_args()
            expect(
                rc == 0 and args == [f"--find-links={d}"],
                "pip-args on a base box: find-links only",
            )
            (t / "pip.conf").write_text(
                "[global]\nno-index = true\nfind-links = /x\n", encoding="utf-8", newline="\n"
            )
            expect(
                pip_args()[1][-1] == "--no-index", "pip.conf no-index -> pip-args adds --no-index"
            )
            (t / "pip.conf").unlink()
            (t / "profile").write_text("airgap\n", encoding="utf-8", newline="\n")
            expect("--no-index" in pip_args()[1], "airgap profile -> --no-index")
            (t / "profile").unlink()
            # tamper: flip one byte of a vendored wheel
            p = d / "awgit-1.0-py3-none-any.whl"
            b = bytearray(p.read_bytes())
            b[-1] ^= 0x01
            p.write_bytes(bytes(b))
            expect(cmd_verify(ns) == 1, "flipped byte -> verify exit 1")
            expect(resolve("awgit==1.0")[0] == 1, "flipped byte -> resolve refuses (tampered)")
            p.unlink()
            expect(cmd_verify(ns) == 1, "deleted wheel -> verify exit 1")
            (d / "evil-1.0-py3-none-any.whl").write_bytes(b"x")
            res = verify_layer(d)
            expect("evil-1.0-py3-none-any.whl" in res["unlisted"], "unlisted wheel is reported")
            ns_l = argparse.Namespace(layer="nope", json=False, quiet=True)
            expect(cmd_verify(ns_l) == 2, "unknown --layer -> exit 2")
            # a lock that ships with no wheelhouse dir: a forgotten COPY must fail
            (d / "evil-1.0-py3-none-any.whl").unlink()
            (t / "wheels" / "garg.lock.txt").write_text(
                "# awnix-wheels schema=1 layer=garg\n"
                f"awdk==3.8.27 --hash=sha256:{'c' * 64}  # file=awdk-3.8.27-py3-none-any.whl\n",
                encoding="utf-8",
                newline="\n",
            )
            (t / "wh" / "garg").mkdir()
            # judged per layer: base is already broken above, garg must fail on its own
            ns_g = argparse.Namespace(layer="garg", json=False, quiet=True)
            expect(cmd_verify(ns_g) == 1, "lock shipped, wheelhouse dir empty -> exit 1, not 2")
            (t / "wh" / "garg").rmdir()
            expect(cmd_verify(ns_g) == 1, "lock shipped, wheelhouse dir absent -> exit 1, not 2")
            expect(cmd_verify(ns) == 1, "whole-box verify fails with the orphan lock")
            (t / "wheels" / "garg.lock.txt").write_text(
                "# awnix-wheels schema=1 layer=garg\n", encoding="utf-8", newline="\n"
            )
            expect(
                "garg" not in locked_layers(), "a lock with no rows claims no wheels (not judged)"
            )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(
        f"awnix-wheelhouse self-test: {'PASS' if not fails else 'FAIL'} ({len(fails)} failure(s))"
    )
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv[:1] == ["--list-verbs"]:
        print("\n".join(VERBS))
        return 0
    ap = argparse.ArgumentParser(prog="awnix-wheelhouse", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--layer")
    v.add_argument("--json", action="store_true")
    v.add_argument("--quiet", action="store_true")
    v.set_defaults(fn=cmd_verify)
    li = sub.add_parser("list")
    li.add_argument("--layer")
    li.add_argument("--json", action="store_true")
    li.set_defaults(fn=cmd_list)
    r = sub.add_parser("resolve")
    r.add_argument("spec")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_resolve)
    pa = sub.add_parser("pip-args")
    pa.add_argument("--json", action="store_true")
    pa.add_argument("--airgap", action="store_true")
    pa.set_defaults(fn=cmd_pip_args)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except OSError as e:
        print(f"awnix-wheelhouse: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
