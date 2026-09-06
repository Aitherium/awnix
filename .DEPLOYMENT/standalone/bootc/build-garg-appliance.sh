#!/bin/bash
# =============================================================================
# Build the garg-appliance bootc image — the ONE command, staging included.
# =============================================================================
# The gargbot backend is baked from the TENANT repo shape (portal_kit_backend
# + lib vendored in by the generator). It cannot be cloned inside the build:
# the tenant repo is PRIVATE and an in-image clone has no credentials
# ("could not read Username for 'https://github.com'", measured 2026-09-01).
# So the backend is staged into the build context from the lane's own clone
# (E:/repos/<tenant> — the same tree the tenant repo ships) BEFORE podman
# build. This script IS that step: run this, never a bare podman build, or
# the COPY dies with "no such file or directory: .gargbot-backend" and the
# error names the wrong thing (the missing dir, not the forgotten stage).
#
#   ./build-garg-appliance.sh              # stage + build
#   TENANT_REPO=~/repos/garg-aitherium ./build-garg-appliance.sh
#   NO_STAGE=1 ./build-garg-appliance.sh   # build only (already staged)
#
# Requires: localhost/awnix-runner-ai:latest (build_awnix_images.py builds
# the chain), the tenant clone under E:/repos, and a working podman.
set -eu
cd "$(dirname "$0")"

TENANT_REPO="${TENANT_REPO:-/mnt/e/repos/garg-aitherium}"
STAGE_DIR=".gargbot-backend"

if [ "${NO_STAGE:-0}" != "1" ]; then
    if [ ! -d "$TENANT_REPO/backend/app" ]; then
        echo "garg-appliance: tenant clone not found at $TENANT_REPO (TENANT_REPO to override)" >&2
        exit 2
    fi
    echo "garg-appliance: staging backend from $TENANT_REPO"
    rm -rf "$STAGE_DIR"
    cp -r "$TENANT_REPO/backend" "$STAGE_DIR"
    test -d "$STAGE_DIR/portal_kit_backend" || {
        echo "garg-appliance: staged backend lacks portal_kit_backend (stale clone?)" >&2
        exit 2
    }
fi

podman build --no-cache -t garg-appliance:latest -f Containerfile.garg-appliance .
echo "garg-appliance: built. Verify: podman run --rm garg-appliance:latest /bin/sh -c 'command -v qdrant && ls /opt/bonsai/models'"
