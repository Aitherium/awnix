#!/bin/sh
# awnix install -- put awnix on THIS machine, verified, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/Aitherium/awnix/main/install.sh \
#     | sudo sh -s -- --disk /dev/nvme0n1 --yes
#
#   ... --token <enrollment-token>     also join this box to your AetherMesh
#   ... --dry-run                      show every step, touch nothing
#   ... --sha256 <hex>                 expect THIS digest (for an image other than the pinned one)
#
# WHAT IT DOES, in order and with nothing hidden:
#   1. reads this machine's architecture (aarch64 or x86_64) and refuses anything else
#   2. downloads the image for that architecture from its release
#   3. verifies the gzip integrity AND the sha256 against the digest this script carries,
#      after checking that digest against the one the release publishes (when reachable)
#   4. writes it to the disk you NAME with dd, and syncs
#   5. prints the two things left for you: reboot, then `awnix mesh join <token>`
#
# WHAT IT REFUSES, deliberately:
#   - no --disk, or a disk that is the running root, or a whole-disk name that does not
#     look like a block device: it stops. It never guesses a target.
#   - no --yes: it stops after step 3 having written nothing.
#   - a sha256 that does not match: it stops with the bytes discarded.
#   - a release whose published digest disagrees with the one pinned here: it stops.
#
# Download locations are overridable for a mirror: AWNIX_ARM64_BASE, AWNIX_X86_BASE,
# AWNIX_X86_SUMS. The pinned digests are not: a mirror must serve the same bytes.
#
# This ERASES the named disk. That is the point, and it is why every gate above exists.
set -eu

REL_BASE="${AWNIX_ARM64_BASE:-https://github.com/Aitherium/awnix/releases/download/awnix-arm64-2026.11.18}"
X86_BASE="${AWNIX_X86_BASE:-https://artifact.aitherium.com}"
X86_SUMS="${AWNIX_X86_SUMS:-https://github.com/Aitherium/awnix/releases/download/awnix-iso-2026.11.22/SHA256SUMS}"
# The assembled image each release published (manifest / SHA256SUMS of those tags).
# The base ISO served at artifact.aitherium.com/awnix-x86_64.iso is PROXIED live from
# this release's parts, so this pin must move WITH the awrtifact spec pin
# (.DEPLOYMENT/workers/awrtifact/awrtifact.yaml, id awnix-iso-base). AW011 fails when
# either lags the newest awnix-iso-<date> release; a mismatch here makes this script
# refuse ("the release now publishes X but this script pins Y") -- loudly, by design.
ARM64_SHA256="4acdf336945018a32e82c8bbd179a4737970e2898bd484dda09fe096e5ed4e15"
X86_SHA256="9c6c1a37feb196184cd2c8f18fe03a6f4caa3fe335609d35a70000c9c1f010a6"
DISK=""; YES=0; DRY=0; TOKEN=""; WANT_SHA=""

say() { printf '%s\n' "awnix-install: $*" >&2; }
die() { printf '%s\n' "awnix-install: REFUSED: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --disk)    DISK="${2:-}"; shift 2 ;;
    --token)   TOKEN="${2:-}"; shift 2 ;;
    --yes)     YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --sha256)  WANT_SHA="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) die "unknown argument '$1' (try --help)" ;;
  esac
done

# ── 1. architecture ────────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) ARCH=arm64; IMAGE="awnix-aarch64.raw.gz"; PARTS="disk.raw.gz.part0 disk.raw.gz.part1"; BASE="$REL_BASE"; PINNED="$ARM64_SHA256" ;;
  x86_64|amd64)  ARCH=x86_64; IMAGE="awnix-x86_64.iso";  PARTS=""; BASE="$X86_BASE"; PINNED="$X86_SHA256" ;;
  *) die "unsupported architecture '$ARCH' -- awnix ships aarch64 and x86_64" ;;
esac
say "architecture: $ARCH"
if [ -n "$WANT_SHA" ]; then
  printf '%s' "$WANT_SHA" | grep -Eq '^[0-9a-f]{64}$' || die "--sha256 must be 64 lowercase hex characters"
  EXPECT="$WANT_SHA"
else
  EXPECT="$PINNED"
fi
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is not installed -- refusing to write an image it cannot verify"

# The digest the release itself publishes: the arm64 manifest's top-level sha256, the
# x86 SHA256SUMS line. Empty when it cannot be fetched (the pinned digest still holds).
published_sha() {
  case "$ARCH" in
    arm64)  curl -fsSL --max-time 30 "$BASE/$IMAGE.manifest.json" 2>/dev/null \
              | sed -n 's/^  "sha256": "\([0-9a-f]\{64\}\)".*/\1/p' | head -n 1 ;;
    x86_64) curl -fsSL --max-time 30 "$X86_SUMS" 2>/dev/null \
              | awk '$2 !~ /\.part[0-9]+$/ { print $1; exit }' ;;
  esac
}

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

if [ -z "$WANT_SHA" ]; then
  PUB="$(published_sha || true)"
  if [ -n "$PUB" ] && [ "$PUB" != "$EXPECT" ]; then
    die "the release now publishes sha256 $PUB but this script pins $EXPECT -- refusing until they agree"
  fi
  [ -n "$PUB" ] && say "release digest agrees with the pinned one" \
    || say "release digest not reachable; verifying against the pinned digest only"
fi
if [ "$DRY" = 0 ]; then
  if [ "${IMAGE##*.}" = gz ]; then
    gzip -t "$WORK/$IMAGE" || die "gzip integrity check FAILED -- bytes discarded"
    say "gzip integrity: OK"
  fi
  # From stdin: with a file NAME, sha256sum escapes a backslash in the path and prefixes
  # the line with one, and the digest no longer compares equal.
  SUM="$(sha256sum < "$WORK/$IMAGE" | cut -d' ' -f1)"
  [ "$SUM" = "$EXPECT" ] || die "sha256 MISMATCH: got $SUM, expected $EXPECT -- bytes discarded, nothing written"
  say "sha256: $SUM (matches)"
else
  say "(dry run) would require sha256 = $EXPECT"
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
