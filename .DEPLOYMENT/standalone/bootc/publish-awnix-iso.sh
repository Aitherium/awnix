#!/usr/bin/env bash
# Publish a built awnix ISO as a GitHub release asset set.
#
# Done by hand once (awnix-iso-2026.08.21) and about to be done a second time, which is
# the tell that it should not be hand-typed again. Every step below was a decision made
# live, and two of them are not obvious:
#
#   * A GitHub release asset is capped at 2 GiB and these images are 2.7-3.0 GB, so the
#     ISO ships SPLIT. That is not a preference, it is the only way it fits.
#   * The split is VERIFIED by streaming the parts back through sha256sum before anything
#     is uploaded. A download that cannot be rejoined is worse than no download, and the
#     failure would land on a stranger's machine after a 3 GB transfer.
#
#   ./publish-awnix-iso.sh --iso /var/tmp/awnix-iso-ai/bootiso/install.iso \
#       --tag awnix-iso-ai-2026.08.21 --title "awnix AI ISO" --notes notes.md
#   ./publish-awnix-iso.sh --self-test
#
# Exit: 0 published, 1 a step failed, 2 could not judge.
set -uo pipefail

# Absolute, because this script cd's to the ISO's directory before uploading --
# a relative path to the helpers would resolve against the wrong place there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="Aitherium/awnix"
PART_MB=1400          # comfortably under the 2 GiB cap, and a round number in the UI
ISO=""; TAG=""; TITLE=""; NOTES=""; DRY=0

die() { echo "publish-awnix-iso: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --iso)   ISO="${2:-}";   shift 2 ;;
    --tag)   TAG="${2:-}";   shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --notes) NOTES="${2:-}"; shift 2 ;;
    --repo)  REPO="${2:-}";  shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [ "${SELFTEST:-0}" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }

  # The join must reproduce the original EXACTLY. Built from real bytes, because this is
  # the assertion the whole procedure rests on.
  t=$(mktemp -d)
  head -c 3000000 /dev/urandom > "$t/src.bin"
  ( cd "$t" && split -b 1M -d --additional-suffix=.part src.bin piece. )
  orig=$(sha256sum "$t/src.bin" | cut -d' ' -f1)
  join=$(cat "$t"/piece.*.part | sha256sum | cut -d' ' -f1)
  chk "$join" "$orig" "split parts rejoin byte-identically"
  # ...and a MISSING slice must be caught, not silently produce a short file.
  rm -f "$t"/piece.01.part
  bad=$(cat "$t"/piece.*.part | sha256sum | cut -d' ' -f1)
  chk "$([ "$bad" != "$orig" ] && echo differs || echo same)" "differs" \
      "a dropped slice changes the hash (so the check can catch it)"
  rm -rf "$t"

  need_iso() { [ -n "$1" ] && echo ok || echo refuse; }
  chk "$(need_iso '')" "refuse" "refuses with no --iso"
  chk "$(need_iso /x/y.iso)" "ok" "accepts an --iso path"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ -n "$ISO" ] || die "--iso is required"
[ -s "$ISO" ] || die "not a file, or empty: $ISO"
[ -n "$TAG" ] || die "--tag is required"
# Credentials are checked only when we are actually going to publish. A --dry-run
# splits and verifies the join and touches no API, so demanding auth for it turns the
# one cheap way to validate an ISO into something you can only do on the box that
# happens to hold the token.
if [ "$DRY" != "1" ]; then
  command -v gh >/dev/null 2>&1 || die "gh not on PATH"
  [ -n "${GH_TOKEN:-}" ] || gh auth status >/dev/null 2>&1     || die "gh is not authenticated (and this is not a --dry-run)"
fi

DIR=$(dirname "$ISO")
STEM="awnix-x86_64.iso"
cd "$DIR" || die "cannot cd to $DIR"

echo "publish-awnix-iso"
echo "  iso  : $ISO ($(du -h "$ISO" | cut -f1))"
echo "  repo : $REPO"
echo "  tag  : $TAG"

# Fresh every run: a stale part from a previous, differently-sized build would upload
# cleanly and corrupt the join.
rm -f "$STEM".*.part SHA256SUMS
split -b "${PART_MB}M" -d --additional-suffix=.part "$(basename "$ISO")" "$STEM."
sha256sum "$(basename "$ISO")" "$STEM".*.part > SHA256SUMS
echo "  parts: $(ls "$STEM".*.part | wc -l)"

# VERIFY THE JOIN BEFORE PUBLISHING. Streamed, so it costs no extra disk.
ORIG=$(grep " $(basename "$ISO")$" SHA256SUMS | cut -d' ' -f1)
JOIN=$(cat "$STEM".*.part | sha256sum | cut -d' ' -f1)
[ "$JOIN" = "$ORIG" ] || die "the parts do NOT rejoin to the original — refusing to publish"
echo "  join : verified byte-identical ($ORIG)"

if [ "$DRY" = "1" ]; then
  echo "  dry-run: not creating a release"
  exit 0
fi

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "  release $TAG exists — uploading assets with --clobber"
else
  args=(--repo "$REPO" --title "${TITLE:-$TAG}")
  [ -n "$NOTES" ] && args+=(--notes-file "$NOTES") || args+=(--notes "awnix ISO $TAG")
  gh release create "$TAG" "${args[@]}" || die "release create failed"
fi

# PARTS FIRST, checksums last. SHA256SUMS is ~300 bytes and lands instantly, so
# uploading it in the same batch means it can sit on the release describing parts that
# are absent -- which is exactly what a user runs `sha256sum -c` against.
#
# 🚨 --clobber DELETES BEFORE IT UPLOADS. Measured 2026-08-21: replacing a published ISO,
# it removed both existing parts and the transfer was then interrupted, leaving the
# release holding only SHA256SUMS -- strictly worse than before the attempt. The flag is
# still correct (a re-run must be able to replace a bad asset) but the operator should
# hear it, because the alternative is discovering it from a broken download. GitHub
# offers no transactional asset swap, so this narrows the window rather than closing it.
echo "  NOTE: --clobber removes the existing assets before uploading. If this is"
echo "        interrupted the release is left INCOMPLETE -- re-run to finish."
for part in "$STEM".*.part; do
  echo "  uploading $part"
  gh release upload "$TAG" --repo "$REPO" --clobber "$part" \
    || die "upload failed on $part -- the release is now INCOMPLETE, re-run to finish"
done
# 🚨 An ISO records the container ref it was built FROM, and `bootc upgrade` pulls from
# that exact ref on the installed machine. An ISO built from `localhost/awnix-base:latest`
# boots, installs and runs perfectly -- and can NEVER update, because localhost resolves
# to the user's own empty store. The failure surfaces days later, on someone else's
# hardware, naming a registry they never configured.
#
# This is not hypothetical: the first awnix ISO shipped exactly that way and had to be
# rebuilt against ghcr.io and re-uploaded (a ~2.8 GB republish). Refusing here costs a
# re-run; publishing costs a release cycle and every machine installed from the bad media.
#
# Scanning the ISO rather than trusting the --local tag passed to the builder, because the
# tag is what a person types and the recorded ref is what the machine will actually use.
echo "  checking the recorded image reference"
_refs="$(grep -a -o -E '(localhost|ghcr\.io|quay\.io|docker\.io)/[A-Za-z0-9._/-]+' "$ISO" \
         2>/dev/null | sort -u | head -20)"
if [ -z "$_refs" ]; then
  die "could not read ANY image reference out of $ISO -- refusing to publish media whose
     upgrade path could not be verified. That is not the same as 'it is fine'."
fi
if printf '%s\n' "$_refs" | grep -q '^localhost/'; then
  echo "  refs found:" >&2
  printf '%s\n' "$_refs" | sed 's/^/       /' >&2
  die "this ISO records a localhost/ image reference, so \`bootc upgrade\` on every
     machine installed from it will try to pull from the user's own empty store and fail.
     Rebuild against the published registry ref (build-awnix-iso.sh --image
     ghcr.io/aitherium/<repo>:latest) and publish that instead."
fi
printf '%s\n' "$_refs" | sed 's/^/    ref: /'

# The assembler ships WITH the parts. A release page of *.part files and no *.iso is
# indistinguishable from a broken upload to the person looking at it, and telling them
# to run `cat` in a blog post is not shipping a product -- it is shipping homework, and
# it is precisely where the two traps live (numeric vs lexical part order, and a
# SHA256SUMS that describes the ASSEMBLED image rather than any part).
for helper in assemble-awnix-iso.sh assemble-awnix-iso.ps1; do
  if [ -f "$HERE/$helper" ]; then
    echo "  uploading $helper"
    gh release upload "$TAG" --repo "$REPO" --clobber "$HERE/$helper" \
      || die "upload failed on $helper -- the release has parts nobody can join, re-run"
  else
    die "$helper not found next to this script -- refusing to publish parts with no
     way to reassemble them. That is the whole failure this step exists to prevent."
  fi
done

gh release upload "$TAG" --repo "$REPO" --clobber SHA256SUMS \
  || die "parts uploaded but SHA256SUMS did not -- re-run"

# Trust the API, and count the EXPECTED parts -- not 'at least two'. Measured
# 2026-08-21: an upload was interrupted mid-transfer (the distro holding the files
# went down), gh still exited 0, and the release ended up with SHA256SUMS plus ONE
# of three parts. A `-ge 2` check blesses that, publishing a download nobody can
# reassemble -- the very failure the join verification above exists to prevent,
# re-introduced one step later. The strictest check was followed by the weakest.
WANT=$(ls "$STEM".*.part 2>/dev/null | wc -l)
GOT=$(gh release view "$TAG" --repo "$REPO" --json assets \
      -q "[.assets[] | select(.size > 0) | select(.name | endswith(\".part\"))] | length" 2>/dev/null || echo 0)
if [ "${GOT:-0}" -ne "${WANT:-0}" ]; then
  die "uploaded ${GOT} of ${WANT} parts -- the release is INCOMPLETE and unusable.
  Re-run to retry: gh exits 0 on a partial upload, so this count is the only thing
  between that and a published download that cannot be rejoined."
fi
echo "  verified: ${GOT}/${WANT} parts present on $TAG"
exit 0
