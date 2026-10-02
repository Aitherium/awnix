#!/usr/bin/env python3
"""Rewrite the GENERATED quadlets at appliance build time -- baked or pull mode.

Extracted from a `RUN python3 - <<'PY'` heredoc in Containerfile.aitheros on
2026-09-09. Containerfile heredocs need buildah 1.34 / **podman 5.0**; the AWS
build pool is Ubuntu 24.04, whose newest podman is **4.9.3** (backports has
nothing newer). On 4.9.3 the parser reads the heredoc BODY as Dockerfile
instructions and fails the whole build with

    Error: FROM requires either one argument, or three

-- an error naming FROM, on line 1 of a file whose FROM is fine, for a heredoc
200 lines below. It cost three ISO builds and ~an hour to find by bisection.

--mode baked (the default, unchanged behaviour): Image= -> the localhost ref,
Pull=never, AutoUpdate dropped, and every unit ordered After= the first-boot image
load (After= only -- a Wants= would re-trigger the oneshot on every Restart= cycle,
restarting a multi-GB podman-load that never finishes). StartLimitIntervalSec=0
lets Restart=always retry through the multi-minute first-boot load. A ref with no
mapping FAILS the build: it would boot with nothing to run and the error would live
nowhere near the failure.

--mode pull --lock images.lock.json (the customer build): Image= -> the
digest-pinned ref from the CI-generated lock, Pull=missing, AutoUpdate dropped (the
OS image digest pins the services; bootc rollback rolls them back too), and every
unit ordered After=/Wants=aither-license.service -- the ONLY writer of the registry
credential (contract registry-credential), which rootful podman reads through the
/run/containers/0/auth.json symlink. A ref missing from the lock, or a lock value
that is not @sha256-pinned, FAILS the build.

Exit: 0 rewritten . 1 an unmapped/unpinned ref . 2 could not judge (no lock, no dir).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

MAP = {
    "ghcr.io/aitherium/aitheros-base:latest": "localhost/aitheros-sovereign:latest",
    "quay.io/minio/minio:latest": "localhost/minio:latest",
    "docker.io/library/redis:7-alpine": "localhost/redis:7-alpine",
}

QUADLET_DIR = Path("/etc/containers/systemd")
LICENSE_UNIT = "aither-license.service"


class UnmappedError(Exception):
    pass


def _rewrite(p: Path, mode: str, lock: Dict[str, str]) -> int:
    changed = 0
    out: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("Image="):
            old = line.split("=", 1)[1]
            if mode == "baked":
                if old not in MAP:
                    raise UnmappedError(f"{p.name} references unbaked image {old} - "
                                   f"add it to MAP in bake-quadlets-offline.py")
                out += [f"Image={MAP[old]}", "Pull=never"]
            else:
                pinned = lock.get(old) or (old if "@sha256:" in old else "")
                if not pinned:
                    raise UnmappedError(f"{p.name} references {old}, which is not in the lock")
                if "@sha256:" not in pinned:
                    raise UnmappedError(f"{p.name}: lock value for {old} is not digest-pinned")
                out += [f"Image={pinned}", "Pull=missing"]
            changed += 1
            continue
        if line.startswith("AutoUpdate=") or line.startswith("Pull="):
            continue
        if line == "[Unit]":
            out.append(line)
            if mode == "baked":
                out += ["After=aither-load-images.service", "StartLimitIntervalSec=0"]
            else:
                out += [f"After={LICENSE_UNIT}", f"Wants={LICENSE_UNIT}",
                        "StartLimitIntervalSec=0"]
            continue
        out.append(line)
    p.write_bytes(("\n".join(out) + "\n").encode("utf-8"))
    return changed


def bake(quadlet_dir: Path = QUADLET_DIR, mode: str = "baked",
         lock: Optional[Dict[str, str]] = None) -> int:
    """Returns the number of Image= refs rewritten. Raises UnmappedError on a bad ref."""
    changed = 0
    for p in sorted(quadlet_dir.glob("*.container")):
        changed += _rewrite(p, mode, lock or {})
    return changed


def self_test() -> int:
    fails: List[str] = []

    def check(cond: bool, what: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            fails.append(what)

    unit = ("[Unit]\nDescription=x\n\n[Container]\nImage=ghcr.io/aitherium/aitheros-base:latest\n"
            "AutoUpdate=registry\n\n[Service]\nRestart=on-failure\n")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "a.container").write_text(unit, encoding="utf-8")
        n = bake(d, "baked")
        t = (d / "a.container").read_text()
        check(n == 1 and "Image=localhost/aitheros-sovereign:latest" in t and "Pull=never" in t
              and "After=aither-load-images.service" in t and "AutoUpdate" not in t,
              "baked: localhost ref + Pull=never + After=aither-load-images")
        (d / "b.container").write_text(
            unit.replace("aitheros-base:latest", "mystery:1"), encoding="utf-8"
        )
        try:
            bake(d, "baked")
            check(False, "baked: an unmapped ref fails")
        except UnmappedError:
            check(True, "baked: an unmapped ref fails")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "a.container").write_text(unit, encoding="utf-8")
        lock = {"ghcr.io/aitherium/aitheros-base:latest":
                "ghcr.io/aitherium/aitheros-base@sha256:" + "a" * 64}
        n = bake(d, "pull", lock)
        t = (d / "a.container").read_text()
        check(n == 1 and f"Image={lock['ghcr.io/aitherium/aitheros-base:latest']}" in t
              and "Pull=missing" in t and "AutoUpdate" not in t,
              "pull: digest-pinned ref + Pull=missing, AutoUpdate dropped")
        check(f"After={LICENSE_UNIT}" in t and f"Wants={LICENSE_UNIT}" in t
              and "aither-load-images" not in t,
              "pull: ordered After=/Wants=aither-license.service, not the image loader")
        (d / "b.container").write_text(
            unit.replace("aitheros-base:latest", "mystery:1"), encoding="utf-8"
        )
        try:
            bake(d, "pull", lock)
            check(False, "pull: a ref missing from the lock fails")
        except UnmappedError:
            check(True, "pull: a ref missing from the lock fails")
        (d / "b.container").unlink()
        (d / "c.container").write_text(
            unit.replace("aitheros-base:latest", "other:2"), encoding="utf-8"
        )
        try:
            bake(d, "pull", {"ghcr.io/aitherium/other:2": "ghcr.io/aitherium/other:2"})
            check(False, "pull: an unpinned lock value fails")
        except UnmappedError:
            check(True, "pull: an unpinned lock value fails")
    print(f"bake-quadlets-offline self-test: {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("baked", "pull"), default="baked")
    ap.add_argument("--lock", help="images.lock.json (required with --mode pull)")
    ap.add_argument("--dir", default=str(QUADLET_DIR), help="quadlet directory")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    qdir = Path(args.dir)
    if not qdir.is_dir():
        print(f"FATAL: no quadlet dir {qdir}", file=sys.stderr)
        return 2
    lock: Dict[str, str] = {}
    if args.mode == "pull":
        if not args.lock:
            print("FATAL: --mode pull needs --lock images.lock.json", file=sys.stderr)
            return 2
        try:
            lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FATAL: cannot read the lock {args.lock}: {exc}", file=sys.stderr)
            return 2
    try:
        changed = bake(qdir, args.mode, lock)
    except UnmappedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    if args.mode == "baked":
        print(f"quadlets baked offline: {changed} Image= refs -> localhost + "
              f"Pull=never + After=aither-load-images")
    else:
        print(f"quadlets pinned for pull: {changed} Image= refs -> @sha256 + "
              f"Pull=missing + After={LICENSE_UNIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
