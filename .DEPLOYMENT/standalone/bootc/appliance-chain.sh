#!/usr/bin/env bash
# Build the garg appliance image, its ISO, and prove it boots — in ONE
# uninterrupted sequence, with the lost-tag hazard handled.
#
# WHY THIS EXISTS. Running the three steps by hand failed four times on this
# host, always the same way: the podman API service restart-storms under load
# ("Failed to spawn executor: Device or resource busy", then shutdown.Stop()
# every 10-25s), which can kill a COMMIT's tag write. The image survives as an
# UNTAGGED layer set — i.e. dangling — and the next routine `podman image
# prune -f` removes it as garbage, entirely correctly. The build log says
# "Successfully tagged"; minutes later the image "has vanished".
#
# So this script does two things a hand-run cannot:
#   1. no human-sized gap between build and ISO (the window the storm needs);
#   2. it captures the image ID from the build and RE-TAGS from that ID if the
#      tag is missing when the ISO step starts — recovering a lost tag write
#      instead of rebuilding 11.7 GB.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="localhost/garg-appliance:latest"
OUT="${OUT:-/var/tmp/awnix-iso-garg}"
LOG="${LOG:-/var/tmp/appliance-chain.log}"
SKIP_PROOF="${SKIP_PROOF:-0}"

say() { echo "[chain $(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
die() { say "FAILED: $*"; exit 1; }

: > "$LOG"
say "== step 1/4: build image"
"$HERE/build-garg-appliance.sh" >>"$LOG" 2>&1
rc=$?
# The build's own rc is unreliable under a storm; the TAG (or the ID) decides.
IMG_ID="$(grep -oE '^--> [0-9a-f]{12}' "$LOG" | tail -1 | awk '{print $2}')"
say "build rc=$rc image_id=${IMG_ID:-none}"
[ -n "$IMG_ID" ] || die "no image id in the build log — the build never COMMITted"

say "== step 2/4: assert the tag (re-tag from id if the storm ate it)"
if ! podman image exists "$IMAGE"; then
  say "tag MISSING right after a successful COMMIT — the known storm hazard"
  podman tag "$IMG_ID" "$IMAGE" || die "cannot re-tag $IMG_ID (the layers are gone too)"
  say "re-tagged $IMG_ID -> $IMAGE"
fi
podman image exists "$IMAGE" || die "$IMAGE still absent"
say "tag present"

say "== step 3/4: build ISO (immediately, no gap)"
rm -rf "$OUT/bootiso"
"$HERE/build-awnix-iso.sh" --image "$IMAGE" --out "$OUT" >>"$LOG" 2>&1
iso_rc=$?
ISO="$OUT/bootiso/install.iso"
[ -s "$ISO" ] || die "no ISO produced (builder rc=$iso_rc)"
say "ISO: $(du -h "$ISO" | cut -f1) at $ISO"

if [ "$SKIP_PROOF" = "1" ]; then say "== step 4/4 SKIPPED (SKIP_PROOF=1)"; exit 0; fi

# NOTE ON STEP 4 AND THE WSL HOP. qemu here is a CHILD of this script. When
# the chain is launched across the Windows->WSL boundary and the launching
# shell goes away, the whole process tree goes with it and qemu dies mid-
# install — measured 2026-09-01: the ISO finished clean, stage 1 started, and
# the tree was reaped seconds later with a 75 KB disk.qcow2 to show for it.
# Run the chain with SKIP_PROOF=1 from such a launcher and start
# boot-proof-garg.sh as its own long-lived job.
say "== step 4/4: boot proof"
rm -rf /var/tmp/garg-boot-proof && mkdir -p /var/tmp/garg-boot-proof
cd /var/tmp/garg-boot-proof || die "cannot enter the proof workdir"
bash "$HERE/boot-proof-garg.sh" "$ISO" 2>&1 | tee -a "$LOG" | tail -20
say "chain done"
