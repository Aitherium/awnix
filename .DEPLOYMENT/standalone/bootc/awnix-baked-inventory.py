#!/usr/bin/python3.11
"""awnix-baked-inventory -- write the components.d manifest of what THIS image baked.

WHY THIS EXISTS

`awnix component list|install|rollback` can only tell the truth about the box if the
box carries an inventory of the bricks baked into it. Until 2026-11-21 no image did:
`grep -rn components.d Containerfile*` matched only awnix-hypr and garg-appliance, so
on a base/full/ai-full/dev box every baked brick was reported from the LOCK alone --
at a version OLDER than what pip actually installed (`awgit 1.12.1` when the image
carried `awgit 1.14.1`) -- and could never appear as `baked`. awsh, which the base
definitely ships, was reported `unavailable` because the lock only knew `pip install
awsh` and awsh is npm. The man page promised a `baked` state that could not exist.

WHAT IT DOES

Runs at BUILD time, after every aw* install, and writes a flat `components:` list in
exactly the shape awnix-component.parse_baked_yaml + load_baked read. Every row is a
first-party (aw*/aither*/adk*) distribution found by importlib.metadata on THIS box,
plus an npm probe for @aitherium/awsh (the family's one npm brick). The version and
the provided commands are the ones actually installed, not the ones a lock predicted,
so `list` shows real `baked` rows and an install of an already-baked brick is refused
as baked instead of misreported as available.

Only first-party names are listed. A baked row claims "this image ships it" and must
never shadow a component the lock can still install, so a third-party dependency is
deliberately absent.

Stdlib only (PyYAML is not assumed on the base). Python 3.10 compatible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# A name is "first-party" exactly when awnix-component's own refusal logic treats it as
# ours: the aw* family plus the aither*/adk runtimes. Same prefix set, kept in sync with
# check_awnix_variants.py's _FIRST_PARTY_RE.
_FIRST_PARTY = re.compile(r"^(aw|aither|adk)", re.I)

# npm global roots, in install order. The base installs @aitherium/awsh into
# /usr/local/lib/node_modules; desktop-open stages it under /usr/lib/awsh-npm.
_NPM_ROOTS = (
    "/usr/local/lib/node_modules",
    "/usr/lib/node_modules",
    "/usr/lib/awsh-npm/node_modules",
)


def _pypi_rows() -> list[dict]:
    from importlib import metadata as md

    rows: dict[str, dict] = {}
    for dist in md.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name or not _FIRST_PARTY.match(name):
            continue
        version = (dist.version or "").strip()
        provides: set[str] = set()
        try:
            for ep in dist.entry_points:
                if ep.group == "console_scripts" and ep.name:
                    provides.add(ep.name)
        except Exception as e:  # a broken dist must not abort the sweep, but say so
            print(f"awnix-baked-inventory: {name}: cannot read entry points: {e}",
                  file=sys.stderr)
        # Top-level import names, so a shim cannot be shadowed by a module name.
        try:
            text = dist.read_text("top_level.txt") or ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    provides.add(line)
        except Exception as e:  # top_level.txt is optional; a read failure is not fatal
            print(f"awnix-baked-inventory: {name}: no top_level.txt ({e})", file=sys.stderr)
        cid = name.lower()
        row = {
            "id": cid,
            "name": name,
            "version": version,
            "source": f"pypi:{name}=={version}",
            "provides": sorted(provides),
        }
        # Duplicate ids can appear across site dirs; keep the newest version.
        prev = rows.get(cid)
        if prev is None or _newer(version, prev["version"]):
            rows[cid] = row
    return list(rows.values())


def _newer(a: str, b: str) -> bool:
    def key(v: str) -> tuple:
        return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", v))

    try:
        return key(a) > key(b)
    except TypeError:
        return a > b


def _npm_rows() -> list[dict]:
    rows = []
    for root in _NPM_ROOTS:
        pkg = os.path.join(root, "@aitherium", "awsh", "package.json")
        if not os.path.isfile(pkg):
            continue
        try:
            with open(pkg, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        version = str(doc.get("version") or "").strip()
        if not version:
            continue
        bins = doc.get("bin") or {}
        provides = list(bins) if isinstance(bins, dict) else ["awsh"]
        rows.append(
            {
                "id": "awsh",
                "name": "@aitherium/awsh",
                "version": version,
                "source": f"npm:@aitherium/awsh@{version}",
                "provides": sorted(set(provides) | {"awsh"}),
            }
        )
        break  # one awsh on a box
    return rows


def _emit(rows: list[dict], variant: str, out: str) -> None:
    rows.sort(key=lambda r: r["id"])
    lines = [
        "# awnix baked inventory -- GENERATED at build time by awnix-baked-inventory.",
        "# What THIS image actually baked (name, version, provided commands). Read by",
        "# `awnix component list|install|rollback` as kind=baked. Do not edit by hand:",
        "# re-run the generator after changing what the Containerfile installs.",
        f"# variant: {variant}",
        "",
        "components:",
    ]
    for r in rows:
        provides = ", ".join(r["provides"])
        lines.append(f"  - id: {r['id']}")
        lines.append(f"    name: {r['name']}")
        lines.append(f"    version: {r['version']}")
        lines.append(f"    source: {r['source']}")
        lines.append(f"    provides: [{provides}]")
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awnix-baked-inventory",
        description="Write the components.d manifest of what this image baked.",
    )
    ap.add_argument("--variant", required=True, help="image variant name (for the header)")
    ap.add_argument(
        "--out",
        default="/usr/share/awnix/components.d/baked.yaml",
        help="output manifest path (default: the image's components.d)",
    )
    ap.add_argument(
        "--println",
        action="store_true",
        help="print the manifest instead of writing it (inspection / CI)",
    )
    args = ap.parse_args(argv)

    rows = _pypi_rows() + _npm_rows()
    if not rows:
        print(
            "awnix-baked-inventory: found NO first-party distributions -- the image "
            "would ship an empty inventory, which reads as 'nothing is baked'",
            file=sys.stderr,
        )
        return 1
    if args.println:
        import tempfile

        with tempfile.NamedTemporaryFile("r", suffix=".yaml", delete=False) as t:
            tmp = t.name
        try:
            _emit(rows, args.variant, tmp)
            with open(tmp, encoding="utf-8") as f:
                sys.stdout.write(f.read())
        finally:
            os.unlink(tmp)
        return 0
    _emit(rows, args.variant, args.out)
    print(f"awnix-baked-inventory: {len(rows)} baked row(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
