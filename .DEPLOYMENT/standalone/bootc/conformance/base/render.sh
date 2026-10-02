#!/bin/sh
# conformance/base/render.sh -- every awnix-descended image renders its spec.
#   conformance/base/render.sh <image>
# Runs the image's own awspec self-test and a dry-run render of its baked layers.
# Exit 0 green, 1 red, 2 cannot judge (no podman / image absent).
set -u
IMG="${1:?usage: render.sh <image>}"
command -v podman >/dev/null 2>&1 || { echo "render.sh: CANNOT JUDGE podman absent" >&2; exit 2; }
podman image exists "$IMG" || { echo "render.sh: CANNOT JUDGE image $IMG absent" >&2; exit 2; }
podman run --rm --entrypoint /usr/bin/awspec "$IMG" --self-test || { echo "render.sh: FAIL self-test in $IMG"; exit 1; }
if podman run --rm --entrypoint /bin/sh "$IMG" -c 'test -n "$(ls -A /usr/share/awspec/layers.d 2>/dev/null)"'; then
    podman run --rm --entrypoint /usr/libexec/awspec-render "$IMG" --dry-run || { echo "render.sh: FAIL render in $IMG"; exit 1; }
else
    echo "render.sh: $IMG carries no leaf layers (plain awnix) -- engine self-test only"
fi
echo "render.sh: OK $IMG"
