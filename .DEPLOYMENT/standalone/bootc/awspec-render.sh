#!/bin/sh
# awspec-render -- resolve this machine's spec layers and apply the render.
#
# Layers, in merge order (awspec sorts by `layer:` rank, then file name):
#   /usr/share/awspec/base.yaml       L0, baked into every awnix image
#   /usr/share/awspec/layers.d/*      the image's product / site-kind layers (leaves)
#   /etc/aither/spec.d/*              this machine's site overlay (mutable /etc)
#   /etc/aither/model-postures.yaml   the posture layer, when the machine has one
# Output: /etc/aither/rendered/<hash>/ + a `current` symlink swap; outputs that
# declare `install:` are copied only when their bytes changed, and a daemon-reload
# is requested (--no-block) only then. Never called from a Before=sysinit unit.
#
# Exit: awspec's own -- 0 applied, 1 spec finding (nothing applied), 2 cannot judge.
set -eu
AWSPEC="${AWSPEC:-/usr/bin/awspec}"
SHARE="${AWSPEC_SHARE:-/usr/share/awspec}"
ETC="${AWSPEC_ETC:-/etc/aither}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
set -- --layer "$SHARE/base.yaml" --from "$SHARE/layers.d"
[ -d "$ETC/spec.d" ] && [ -n "$(ls -A "$ETC/spec.d" 2>/dev/null)" ] && set -- "$@" --from "$ETC/spec.d"
[ -f "$ETC/model-postures.yaml" ] && set -- "$@" --postures "$ETC/model-postures.yaml"
if [ "$DRY" -eq 1 ]; then
    exec "$AWSPEC" render "$@" --dry-run
fi
"$AWSPEC" validate "$@"
exec "$AWSPEC" apply "$@" --out-root "$ETC/rendered" --install --reload
