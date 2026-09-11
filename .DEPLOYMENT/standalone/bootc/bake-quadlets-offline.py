#!/usr/bin/env python3
"""Rewrite the GENERATED quadlets for the offline bake, at appliance build time.

Extracted from a `RUN python3 - <<'PY'` heredoc in Containerfile.aitheros on
2026-09-09. Containerfile heredocs need buildah 1.34 / **podman 5.0**; the AWS
build pool is Ubuntu 24.04, whose newest podman is **4.9.3** (backports has
nothing newer). On 4.9.3 the parser reads the heredoc BODY as Dockerfile
instructions and fails the whole build with

    Error: FROM requires either one argument, or three

-- an error naming FROM, on line 1 of a file whose FROM is fine, for a heredoc
200 lines below. It cost three ISO builds and ~an hour to find by bisection.

Keeping the logic in a real file is better than the heredoc regardless of
podman: it is lintable, diffable and testable, which a string embedded in a
Containerfile never is.

What it does: Image= -> the localhost ref, Pull=never, AutoUpdate dropped, and
every unit ordered After= the first-boot image load (After= only -- a Wants=
would re-trigger the oneshot on every Restart= cycle, restarting a multi-GB
podman-load that never finishes). StartLimitIntervalSec=0 lets Restart=always
retry through the multi-minute first-boot load. A ref with no mapping FAILS the
build: it would boot with nothing to run and the error would live nowhere near
the failure.
"""

import sys
from pathlib import Path

MAP = {
    "ghcr.io/aitherium/aitheros-base:latest": "localhost/aitheros-sovereign:latest",
    "docker.io/minio/minio:latest": "localhost/minio:latest",
    "docker.io/library/redis:7-alpine": "localhost/redis:7-alpine",
}

QUADLET_DIR = Path("/etc/containers/systemd")


def bake(quadlet_dir: Path = QUADLET_DIR) -> int:
    """Returns the number of Image= refs rewritten. Exits 1 on an unbaked ref."""
    changed = 0
    for p in sorted(quadlet_dir.glob("*.container")):
        lines = p.read_text().splitlines()
        out: "list[str]" = []
        for line in lines:
            if line.startswith("Image="):
                old = line.split("=", 1)[1]
                if old not in MAP:
                    print(f"FATAL: {p.name} references unbaked image {old} - "
                          f"add it to MAP in bake-quadlets-offline.py",
                          file=sys.stderr)
                    sys.exit(1)
                out.append(f"Image={MAP[old]}")
                out.append("Pull=never")
                changed += 1
                continue
            if line == "AutoUpdate=registry":
                continue
            if line == "[Unit]":
                out.append(line)
                out.append("After=aither-load-images.service")
                out.append("StartLimitIntervalSec=0")
                continue
            out.append(line)
        p.write_text("\n".join(out) + "\n")
    return changed


def main() -> int:
    changed = bake()
    print(f"quadlets baked offline: {changed} Image= refs -> localhost + "
          f"Pull=never + After=aither-load-images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
