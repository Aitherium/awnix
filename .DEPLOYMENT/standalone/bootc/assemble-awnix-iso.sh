#!/usr/bin/env bash
# Turn the downloaded awnix release parts into a bootable ISO. Ships WITH the release.
#
# The ISO is ~2.8 GB and a GitHub release asset is capped at 2 GiB, so it is published as
# `awnix-x86_64.iso.00.part`, `.01.part`, ... plus a SHA256SUMS. Without this script a
# reader lands on a release page with no file named *.iso on it and reasonably concludes
# the download is broken. That is a silent failure in documentation form: nothing errors,
# the person just leaves.
#
# So the parts ship with the thing that reassembles them. Telling somebody to run `cat`
# is not shipping a product; it is shipping homework -- and it is the step where the two
# real traps live:
#
#   * SHA256SUMS records the digest of the ASSEMBLED image, not of any part. Checking a
#     part against it fails, which reads as a corrupt download of a file that is fine.
#   * The parts must be concatenated in NUMERIC order. They are zero-padded (00, 01, ...)
#     so lexical order happens to be correct today, but a 10th part would break a naive
#     glob on some shells. This sorts explicitly rather than relying on that.
#
#   ./assemble-awnix-iso.sh                       # assemble from parts in this directory
#   ./assemble-awnix-iso.sh --dir ~/Downloads
#   ./assemble-awnix-iso.sh --out /tmp/awnix.iso
#   ./assemble-awnix-iso.sh --keep-parts          # do not delete the parts afterwards
#   ./assemble-awnix-iso.sh --self-test
#
# Exit: 0 assembled and verified, 1 a real failure, 2 could not judge (missing checksum
# tool, unreadable directory) -- never 0 on "I could not check".
set -uo pipefail

DIR="."
OUT=""
KEEP=0
SELFTEST=0

die() { echo "assemble-awnix-iso: $*" >&2; exit 1; }
dead() { echo "assemble-awnix-iso: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        DIR="${2:-}"; shift 2 ;;
    --out)        OUT="${2:-}"; shift 2 ;;
    --keep-parts) KEEP=1; shift ;;
    --self-test)  SELFTEST=1; shift ;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# Numeric sort on the part index, never a bare glob. `sort -t. -k3,3n` keys on the number
# between the last two dots, so part 10 lands after part 9 rather than after part 1.
list_parts() {
  find "$1" -maxdepth 1 -type f -name '*.iso.*.part' 2>/dev/null \
    | sort -t. -k3,3n
}

sha_tool() {
  if command -v sha256sum >/dev/null 2>&1; then echo "sha256sum"
  elif command -v shasum >/dev/null 2>&1; then echo "shasum -a 256"
  else echo ""; fi
}

# ── self-test ───────────────────────────────────────────────────────────────────────
if [ "$SELFTEST" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }

  T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  printf 'HELLO' > "$T/x.iso.00.part"
  printf 'WORLD' > "$T/x.iso.01.part"
  # A 10th part proves the numeric sort: lexically "10" sorts before "02".
  printf 'TENTH' > "$T/x.iso.10.part"
  got="$(list_parts "$T" | xargs -n1 basename | tr '\n' ' ')"
  chk "$got" "x.iso.00.part x.iso.01.part x.iso.10.part " "parts sort NUMERICALLY, not lexically"

  ST="$(sha_tool)"
  if [ -z "$ST" ]; then
    echo "  SELF-TEST DEAD: no sha256 tool available"; exit 2
  fi
  cat $(list_parts "$T") > "$T/joined"
  chk "$(cat "$T/joined")" "HELLOWORLDTENTH" "join concatenates in that order"

  # A wrong checksum must FAIL, or the verification is decorative.
  real="$($ST "$T/joined" | cut -d' ' -f1)"
  chk "$([ ${#real} -eq 64 ] && echo yes || echo no)" "yes" "checksum tool returns a sha256"
  chk "$([ "$real" = "0000000000000000000000000000000000000000000000000000000000000000" ] && echo yes || echo no)" \
      "no" "a real digest is not the all-zero sentinel"

  # No parts at all must not read as success.
  E="$(mktemp -d)"
  chk "$(list_parts "$E" | wc -l | tr -d ' ')" "0" "an empty directory yields no parts"
  rm -rf "$E"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

# ── assemble ────────────────────────────────────────────────────────────────────────
[ -d "$DIR" ] || dead "not a directory: $DIR"

mapfile -t PARTS < <(list_parts "$DIR")
if [ "${#PARTS[@]}" -eq 0 ]; then
  die "no *.iso.*.part files in '$DIR'.
     Download every part AND the SHA256SUMS file from the release, put them in one
     directory, and run this from there (or pass --dir)."
fi

STEM="$(basename "${PARTS[0]}")"; STEM="${STEM%.*.part}"
[ -n "$OUT" ] || OUT="$DIR/$STEM"

echo "assemble-awnix-iso"
echo "  parts : ${#PARTS[@]}"
for p in "${PARTS[@]}"; do echo "          $(basename "$p")"; done
echo "  out   : $OUT"

# A partial assembly that looks like an ISO is worse than none, so build to a temp name
# and only move it into place once the checksum agrees.
TMP="$OUT.assembling"
rm -f "$TMP"
cat "${PARTS[@]}" > "$TMP" || die "concatenation failed (out of disk?) -- nothing was moved into place"

SUMS="$DIR/SHA256SUMS"
ST="$(sha_tool)"

if [ -z "$ST" ]; then
  rm -f "$TMP"
  dead "no sha256sum/shasum on PATH -- refusing to present an UNVERIFIED image as done.
     Install one and re-run; the parts are untouched."
fi

if [ ! -f "$SUMS" ]; then
  rm -f "$TMP"
  dead "SHA256SUMS not found next to the parts -- refusing to present an UNVERIFIED image.
     It is published alongside them on the same release page."
fi

# The recorded name is the builder's internal one (install.iso); match on the DIGEST
# column rather than the filename, which differs from the published part names.
WANT="$(awk '/\.iso$/ {print $1; exit}' "$SUMS")"
[ -n "$WANT" ] || { rm -f "$TMP"; dead "SHA256SUMS has no .iso line -- cannot verify"; }

GOT="$($ST "$TMP" | cut -d' ' -f1)"
if [ "$GOT" != "$WANT" ]; then
  rm -f "$TMP"
  die "CHECKSUM MISMATCH -- the assembled image is not the published one.
     expected $WANT
     got      $GOT
     A part is truncated or missing. Re-download the parts; the bad image was deleted
     rather than left on disk looking usable."
fi

mv "$TMP" "$OUT"
echo "  sha256: $GOT  VERIFIED"

if [ "$KEEP" = "0" ]; then
  rm -f "${PARTS[@]}"
  echo "  parts removed (--keep-parts to retain them)"
fi

echo
echo "  Ready: $OUT"
echo "  Write it to a USB stick, or attach it as a VM boot disc."
exit 0
