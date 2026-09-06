#!/usr/bin/env python3
# Rewrite the GENERATED quadlets for the offline fleet bake.
#
# The generated quadlets reference ghcr.io/aitherium/aitheros-base:latest
# (a PRIVATE ghcr package; an unauthenticated pull is 401) plus docker.io
# minio/redis. The build's svcimg context carries compressed oci-archives
# tagged with the LOCALHOST refs mapped below; aither-load-images.service
# (first-boot oneshot) podman-loads them BEFORE any quadlet starts, so the
# appliance runs with zero network and zero registry creds.
#
# This script MUST be invoked as `RUN python3 <path>` — never as a heredoc
# (`RUN python3 - <<'PY'`). Heredocs are BuildKit syntax; podman/buildah
# parse each body line as a Containerfile INSTRUCTION and die on the first
# one (the error names a fake instruction, not the heredoc). Same trap as
# Containerfile.awnix-full.
#
# A ref with no mapping FAILS the build: it would boot with nothing to run
# and the error would live nowhere near the failure.
from pathlib import Path
import sys

MAP = {
    "ghcr.io/aitherium/aitheros-base:latest": "localhost/aitheros-sovereign:latest",
    "docker.io/minio/minio:latest": "localhost/minio:latest",
    "docker.io/library/redis:7-alpine": "localhost/redis:7-alpine",
}

changed = 0
for p in sorted(Path("/etc/containers/systemd").glob("*.container")):
    lines = p.read_text().splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("Image="):
            old = line.split("=", 1)[1]
            if old not in MAP:
                print(f"FATAL: {p.name} references unbaked image {old} — "
                      f"add it to the fleet bake MAP in Containerfile.aitheros", file=sys.stderr)
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
print(f"quadlets baked offline: {changed} Image= refs -> localhost + Pull=never + After=aither-load-images")
