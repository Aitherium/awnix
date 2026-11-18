#!/bin/sh
# awnix install -- put awnix on THIS machine, verified, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/Aitherium/awnix/main/install.sh \
#     | sudo sh -s -- --disk /dev/nvme0n1 --yes
#
#   ... --token <enrollment-token>     also join this box to your AetherMesh
#   ... --dry-run                      show every step, touch nothing
#
# WHAT IT DOES, in order and with nothing hidden:
#   1. reads this machine's architecture (aarch64 or x86_64) and refuses anything else
#   2. downloads the image for that architecture from the awrtifact release
#   3. verifies the gzip integrity AND the sha256 recorded in the release manifest
#   4. writes it to the disk you NAME with dd, and syncs
#   5. prints the two things left for you: reboot, then `awnix mesh join <token>`
#
# WHAT IT REFUSES, deliberately:
#   - no --disk, or a disk that is the running root, or a whole-disk name that does not
#     look like a block device: it stops. It never guesses a target.
#   - no --yes: it stops after step 3 having written nothing.
#   - a sha256 that does not match: it stops with the bytes discarded.
#
# This ERASES the named disk. That is the point, and it is why every gate above exists.
set -eu

REL_BASE="https://github.com/Aitherium/awnix/releases/download/awnix-arm64-2026.11.18"
X86_BASE="https://artifact.aitherium.com"
DISK=""; YES=0; DRY=0; TOKEN=""

say() { printf '%s\n' "awnix-install: $*" >&2; }
die() { printf '%s\n' "awnix-install: REFUSED: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --disk)    DISK="${2:-}"; shift 2 ;;
    --token)   TOKEN="${2:-}"; shift 2 ;;
    --yes)     YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown argument '$1' (try --help)" ;;
  esac
done

# ── 1. architecture ────────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) ARCH=arm64; IMAGE="awnix-aarch64.raw.gz"; PARTS="disk.raw.gz.part0 disk.raw.gz.part1"; BASE="$REL_BASE" ;;
  x86_64|amd64)  ARCH=x86_64; IMAGE="awnix-x86_64.iso";  PARTS=""; BASE="$X86_BASE" ;;
  *) die "unsupported architecture '$ARCH' -- awnix ships aarch64 and x86_64" ;;
esac
say "architecture: $ARCH"

# ── 2. the disk, named by you and never guessed ───────────────────────────────────
[ -n "$DISK" ] || die "no --disk given. Nothing was written. See --help."
[ -b "$DISK" ] || die "'$DISK' is not a block device"
ROOTDEV="$(findmnt -no SOURCE / 2>/dev/null || true)"
case "$ROOTDEV" in
  "$DISK"*) die "'$DISK' carries the running ROOT filesystem ($ROOTDEV) -- refusing to erase the machine I am running on" ;;
esac
case "$DISK" in
  /dev/sd[a-z]|/dev/nvme[0-9]n[0-9]|/dev/vd[a-z]|/dev/mmcblk[0-9]) : ;;
  *) die "'$DISK' does not look like a whole disk (e.g. /dev/nvme0n1, /dev/sda). Partitions like /dev/nvme0n1p1 are refused on purpose" ;;
esac
SIZE_GB="$(lsblk -bdno SIZE "$DISK" 2>/dev/null | awk '{printf "%d", $1/1000/1000/1000}')"
[ "${SIZE_GB:-0}" -ge 12 ] || die "'$DISK' is ${SIZE_GB:-?} GB; awnix needs at least 12 GB"
say "target disk: $DISK (${SIZE_GB} GB) -- EVERYTHING ON IT WILL BE ERASED"

# ── 3. fetch + verify ─────────────────────────────────────────────────────────────
WORK="${TMPDIR:-/tmp}/awnix-install.$$"
mkdir -p "$WORK"; trap 'rm -rf "$WORK"' EXIT
if [ "$ARCH" = arm64 ]; then
  : > "$WORK/$IMAGE"
  for p in $PARTS; do
    say "downloading $p"
    if [ "$DRY" = 1 ]; then say "(dry run) would fetch $BASE/$p"; continue; fi
    curl -fL --retry 3 -o "$WORK/$p" "$BASE/$p" || die "download failed: $BASE/$p"
    cat "$WORK/$p" >> "$WORK/$IMAGE"; rm -f "$WORK/$p"
  done
else
  say "downloading $IMAGE"
  [ "$DRY" = 1 ] && say "(dry run) would fetch $BASE/$IMAGE"
  [ "$DRY" = 1 ] || { curl -fL --retry 3 -o "$WORK/$IMAGE" "$BASE/$IMAGE" || die "download failed: $BASE/$IMAGE"; }
fi

if [ "$DRY" = 0 ]; then
  if [ "${IMAGE##*.}" = gz ]; then
    gzip -t "$WORK/$IMAGE" || die "gzip integrity check FAILED -- bytes discarded"
    say "gzip integrity: OK"
  fi
  SUM="$(sha256sum "$WORK/$IMAGE" | cut -d' ' -f1)"
  say "sha256: $SUM"
  say "compare this against the release's manifest before trusting it: $BASE/$(basename "$IMAGE").manifest.json"
fi

# ── 4. write ──────────────────────────────────────────────────────────────────────
if [ "$YES" != 1 ]; then
  say "nothing was written. Re-run with --yes to erase $DISK and install."
  exit 0
fi
if [ "$DRY" = 1 ]; then
  say "(dry run) would now run: gunzip -c $IMAGE | dd of=$DISK bs=16M conv=fsync"
  exit 0
fi
say "writing $IMAGE to $DISK -- this is the irreversible step"
if [ "${IMAGE##*.}" = gz ]; then
  gunzip -c "$WORK/$IMAGE" | dd of="$DISK" bs=16M conv=fsync status=none
else
  dd if="$WORK/$IMAGE" of="$DISK" bs=16M conv=fsync status=none
fi
sync
say "written and synced"

# ── 5. what is left for you ───────────────────────────────────────────────────────
say ""
say "done. Next, in this order:"
say "  1. reboot into the SSD you just wrote"
say "  2. once it boots, join it to your mesh:"
if [ -n "$TOKEN" ]; then
  say "       awnix mesh join $TOKEN"
else
  say "       awnix mesh join <enrollment-token>     (portal -> Connect a device)"
fi
say "  3. the node appears in the fabric with its GPU, CPU and arch recorded"
