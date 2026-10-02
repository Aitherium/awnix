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
#   ./publish-awnix-iso.sh ... --mirror-artifact   # also emit the one-file URL entry
#
# EVERY release also carries awnix-iso.json (schema 1):
#   {variant, tag, iso_name, size, sha256, parts:[{name,size,sha256}],
#    urls:{release, single|null}}
# so assemble-awnix-iso.{sh,ps1} and write-awnix-usb.sh can name a corrupt PART instead
# of failing the whole 3 GB join. A signing step may ADD {sig, cert, bundle}; it must not
# rename these fields.
#
# --mirror-artifact is for the PUBLIC awnix variants only: it records the single-file
# URL (artifact.aitherium.com stitches the parts) and writes awrtifact-entry.yaml for the
# spec. It REFUSES any repo but Aitherium/awnix and any tag naming garg, appliance or
# aitheros: those images are private and never get a public URL. It deploys nothing --
# regenerating and deploying the worker is an owner step.
#
# Exit: 0 published, 1 a step failed, 2 could not judge.
set -uo pipefail

# Absolute, because this script cd's to the ISO's directory before uploading --
# a relative path to the helpers would resolve against the wrong place there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="Aitherium/awnix"
PART_MB=1400          # comfortably under the 2 GiB cap, and a round number in the UI
ISO=""; TAG=""; TITLE=""; NOTES=""; DRY=0; VARIANT=""; MIRROR=0; CALLER_LATEST=""
ARTIFACT_HOST="https://artifact.aitherium.com"

die() { echo "publish-awnix-iso: $*" >&2; exit 1; }

# awnix-iso-ai-2026.09.27 -> ai ; awnix-iso-2026.09.27 -> base
variant_from_tag() {
  local v="${1#awnix-iso}"
  v="${v%-[0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]*}"
  v="${v#-}"
  echo "${v:-base}"
}

# The name artifact.aitherium.com serves the stitched ISO under (matches the awrtifact
# spec's existing awnix-iso-* entries).
served_name() { if [ "$1" = "base" ]; then echo "awnix-x86_64.iso"; else echo "awnix-$1-x86_64.iso"; fi; }

# THE GUARD (AIN010): only public awnix media may get a public single-file URL.
mirror_allowed() {  # mirror_allowed REPO TAG -> ok | refuse:<why>
  case "$1" in
    Aitherium/awnix) : ;;
    *) echo "refuse:repo $1 is not Aitherium/awnix"; return ;;
  esac
  case "$(printf '%s' "$2" | tr 'A-Z' 'a-z')" in
    *garg*|*appliance*|*aitheros*) echo "refuse:tag $2 names a private image"; return ;;
  esac
  echo ok
}

# THE LATEST RULE (AUC006): only the BASE layer's plain dated release on Aitherium/awnix
# may be GitHub's "Latest" -- every other layer, every suffixed tag and every other repo
# is published --latest=false, so /releases/latest always names the base ISO.
#   A caller may pass --latest-flag for ANOTHER repo (the private tenant repo keeps its
#   own rule: an unsuffixed appliance-YYYYMMDD is its Latest); on Aitherium/awnix the
#   rule below always wins, whatever the caller asked for.
latest_flag() {  # latest_flag REPO LAYER TAG [CALLER_FLAG] -> --latest | --latest=false
  local LATEST_FLAG=--latest=false LAYER="$2"
  if [ "$1" = Aitherium/awnix ]; then
    if [ "$LAYER" = base ]; then
      case "$3" in
        awnix-iso-[0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]) LATEST_FLAG=--latest ;;
      esac
    fi
  elif [ "${4:-}" = --latest ]; then
    LATEST_FLAG=--latest
  fi
  echo "$LATEST_FLAG"
}

# THE ANONYMOUS-PULL RULE (AUC007): public media records a `bootc switch` ref that every
# stranger's box will pull. Before the release exists, prove that ref answers an
# ANONYMOUS pull (the GHCR anonymous token, then a manifest HEAD). An org member's
# credential reads `internal` packages too, so any authenticated check says yes here.
anon_pull_code() {  # anon_pull_code REF -> the HTTP status of an anonymous manifest HEAD
  local ref="$1" name tag tok
  name="${ref#ghcr.io/}"; tag="${name##*:}"; name="${name%:*}"
  [ "$tag" != "$name" ] || tag=latest
  case "$name" in *@sha256*) tag="${name#*@}"; name="${name%@*}" ;; esac
  tok=$(curl -s --max-time 20 "https://ghcr.io/token?scope=repository:${name}:pull&service=ghcr.io" \
        | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 -I \
    -H "Authorization: Bearer ${tok}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/${name}/manifests/${tag}" 2>/dev/null || echo 000
}

# One JSON line per part, then the manifest. Values are names, hex, ints and URLs, so
# printf is exact; no jq on the path that ships the download.
write_manifest() {  # write_manifest OUT VARIANT TAG ISO_FILE STEM RELEASE_URL SINGLE_OR_EMPTY
  local out="$1" variant="$2" tag="$3" iso="$4" stem="$5" rel="$6" single="$7"
  local size sha parts="" sep="" p psz psha
  size=$(wc -c < "$iso" | tr -d ' ')
  sha=$(sha256sum "$iso" | cut -d' ' -f1)
  for p in $(ls "$(dirname "$iso")/$stem".*.part 2>/dev/null | sort -t. -k3,3n); do
    psz=$(wc -c < "$p" | tr -d ' ')
    psha=$(sha256sum "$p" | cut -d' ' -f1)
    parts="${parts}${sep}{\"name\": \"$(basename "$p")\", \"size\": ${psz}, \"sha256\": \"${psha}\"}"
    sep=", "
  done
  if [ -n "$single" ]; then single="\"$single\""; else single="null"; fi
  printf '{"schema": 1, "variant": "%s", "tag": "%s", "iso_name": "%s", "size": %s, "sha256": "%s", "parts": [%s], "urls": {"release": "%s", "single": %s}}\n' \
    "$variant" "$tag" "$stem" "$size" "$sha" "$parts" "$rel" "$single" > "$out"
}

# The sum of the parts' sizes, read back from a manifest (the self-test's cross-check).
manifest_part_sum() { grep -o '"size": [0-9]*' "$1" | tail -n +2 | awk '{s+=$2} END {print s+0}'; }
manifest_size() { grep -o '"size": [0-9]*' "$1" | head -1 | awk '{print $2}'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --iso)   ISO="${2:-}";   shift 2 ;;
    --tag)   TAG="${2:-}";   shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --notes) NOTES="${2:-}"; shift 2 ;;
    --repo)  REPO="${2:-}";  shift 2 ;;
    --variant) VARIANT="${2:-}"; shift 2 ;;
    --latest-flag) CALLER_LATEST="${2:-}"; shift 2 ;;
    --mirror-artifact) MIRROR=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
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

  # awnix-iso.json: the parts' sizes sum to the whole and each part carries its own sum.
  t=$(mktemp -d)
  head -c 2500000 /dev/urandom > "$t/install.iso"
  ( cd "$t" && split -b 1M -d --additional-suffix=.part install.iso awnix-x86_64.iso. )
  write_manifest "$t/awnix-iso.json" ai awnix-iso-ai-2026.09.27 "$t/install.iso" awnix-x86_64.iso \
    "https://github.com/Aitherium/awnix/releases/tag/awnix-iso-ai-2026.09.27" ""
  chk "$(manifest_part_sum "$t/awnix-iso.json")" "$(manifest_size "$t/awnix-iso.json")" "manifest part sizes sum to the whole"
  chk "$(grep -o '"sha256": "[0-9a-f]\{64\}"' "$t/awnix-iso.json" | wc -l | tr -d ' ')" "4" "the whole and each of 3 parts carry a sha256"
  chk "$(grep -c '"single": null' "$t/awnix-iso.json")" "1" "no --mirror-artifact means single:null"
  PY=""; for c in python3 python; do command -v "$c" >/dev/null 2>&1 && "$c" -c pass 2>/dev/null && { PY=$c; break; }; done
  if [ -n "$PY" ]; then
    chk "$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["schema"], len(d["parts"]), d["urls"]["single"])' "$t/awnix-iso.json")" \
        "1 3 None" "the manifest is valid JSON with the schema-1 fields"
  fi
  rm -rf "$t"

  chk "$(variant_from_tag awnix-iso-ai-2026.09.27)" "ai" "variant from an ai tag"
  chk "$(variant_from_tag awnix-iso-2026.09.27)" "base" "the base tag is variant base"
  chk "$(variant_from_tag awnix-iso-desktop-open-2026.09.27)" "desktop-open" "a hyphenated variant survives"
  chk "$(served_name base)" "awnix-x86_64.iso" "base serves as awnix-x86_64.iso"
  chk "$(served_name ai)" "awnix-ai-x86_64.iso" "a variant serves under its own name"
  chk "$(mirror_allowed Aitherium/awnix awnix-iso-ai-2026.09.27)" "ok" "a public awnix ISO may be mirrored"
  chk "$(mirror_allowed Aitherium/garg-aitherium awnix-iso-2026.09.27 | cut -d: -f1)" "refuse" "--mirror-artifact REFUSES a non-awnix repo"
  chk "$(mirror_allowed Aitherium/awnix garg-appliance-iso-2026.09.27 | cut -d: -f1)" "refuse" "--mirror-artifact REFUSES a garg tag"
  chk "$(mirror_allowed Aitherium/awnix awnix-iso-Appliance-2026.09.27 | cut -d: -f1)" "refuse" "...and an appliance tag (any case)"
  chk "$(mirror_allowed Aitherium/awnix aitheros-iso-2026.09.27 | cut -d: -f1)" "refuse" "...and an aitheros tag"

  chk "$(latest_flag Aitherium/awnix base awnix-iso-2026.09.28)" "--latest" "the base dated release on Aitherium/awnix is Latest"
  chk "$(latest_flag Aitherium/awnix ai awnix-iso-ai-2026.09.28)" "--latest=false" "another layer is never Latest"
  chk "$(latest_flag Aitherium/awnix base awnix-iso-2026.09.28-rc1)" "--latest=false" "a suffixed base tag is never Latest"
  chk "$(latest_flag Aitherium/AitherOS base awnix-iso-2026.09.28)" "--latest=false" "another repo is not Latest by default"
  chk "$(latest_flag Aitherium/awnix ai awnix-iso-ai-2026.09.28 --latest)" "--latest=false" "a caller cannot make a non-base awnix layer Latest"
  chk "$(latest_flag Aitherium/garg-aitherium garg appliance-20260928 --latest)" "--latest" "the tenant repo keeps its own Latest rule"

  need_iso() { [ -n "$1" ] && echo ok || echo refuse; }
  chk "$(need_iso '')" "refuse" "refuses with no --iso"
  chk "$(need_iso /x/y.iso)" "ok" "accepts an --iso path"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ -n "$ISO" ] || die "--iso is required"
[ -s "$ISO" ] || die "not a file, or empty: $ISO"
[ -n "$TAG" ] || die "--tag is required"
[ -n "$VARIANT" ] || VARIANT="$(variant_from_tag "$TAG")"
SINGLE=""
if [ "$MIRROR" = "1" ]; then
  _g="$(mirror_allowed "$REPO" "$TAG")"
  [ "$_g" = "ok" ] || die "--mirror-artifact ${_g#refuse:} -- private media never gets a public URL"
  SINGLE="$ARTIFACT_HOST/$(served_name "$VARIANT")"
fi
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

write_manifest awnix-iso.json "$VARIANT" "$TAG" "$(basename "$ISO")" "$STEM" \
  "https://github.com/$REPO/releases/tag/$TAG" "$SINGLE" || die "could not write awnix-iso.json"
echo "  manifest: awnix-iso.json (variant $VARIANT, single ${SINGLE:-none})"
if [ -n "$SINGLE" ]; then
  # The spec entry for the stitched one-file URL. Merging it into the awrtifact spec,
  # regenerating (awrtifact serve-spec) and deploying the worker are owner steps.
  {
    echo "- id: awnix-iso-$VARIANT"
    echo "  name: $(served_name "$VARIANT")"
    echo "  source_url: https://github.com/$REPO/releases/tag/$TAG"
    echo "  total: $(wc -c < "$(basename "$ISO")" | tr -d ' ')"
    echo "  repo: $REPO"
    echo "  release: $TAG"
    echo "  parts:"
    for p in $(ls "$STEM".*.part | sort -t. -k3,3n); do echo "  - $(wc -c < "$p" | tr -d ' ')"; done
    echo "  part_names:"
    for p in $(ls "$STEM".*.part | sort -t. -k3,3n); do echo "  - $p"; done
  } > awrtifact-entry.yaml
  echo "  awrtifact-entry.yaml written (owner: merge into the spec, serve-spec, deploy)"
fi

if [ "$DRY" = "1" ]; then
  echo "  dry-run: not creating a release"
  exit 0
fi

# AUC007: on the PUBLIC repo, the recorded upgrade ref must answer an anonymous pull
# BEFORE anything is released -- a public ISO whose image strangers cannot pull installs
# fine and can never update.
if [ "$REPO" = Aitherium/awnix ]; then
  _pre_ref="$(grep -a -o -E 'bootc switch [^"]{0,160}' "$(basename "$ISO")" 2>/dev/null \
              | grep -o -E 'ghcr\.io/[A-Za-z0-9._/:@-]+' | head -1)"
  [ -n "$_pre_ref" ] || die "cannot read the ghcr bootc switch ref out of the ISO -- refusing to publish public media whose upgrade path is unproven"
  _code="$(anon_pull_code "$_pre_ref")"
  [ "$_code" = "200" ] || die "the upgrade ref $_pre_ref answers $_code to an ANONYMOUS pull (want 200) -- a stranger's box could never update; fix the package visibility first"
  echo "  anon pull: $_pre_ref -> 200"
fi

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "  release $TAG exists — uploading assets with --clobber"
else
  args=(--repo "$REPO" --title "${TITLE:-$TAG}" "$(latest_flag "$REPO" "$VARIANT" "$TAG" "$CALLER_LATEST")")
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
# Judge the OPERATIVE ref, not every matching string in 7.7 GB of ISO.
#
# The origin `bootc upgrade` reads is set by exactly one thing: the kickstart's
#   bootc switch --mutate-in-place --transport registry <REF>
# Everything else that looks like an image reference in here is metadata --
# notably `org.opencontainers.image.base.name`, which records the LINEAGE the
# image was built on and is `localhost/awnix-runner-ai:latest` for every awnix
# image ever built, by construction.
#
# The first version of this check grepped the whole ISO for any `localhost/`
# and died on that annotation. Measured 2026-09-02: an ISO whose switch ref was
# correctly `ghcr.io/aitherium/garg-appliance:latest` was refused because three
# base-lineage annotations mentioned localhost. That version could never pass
# for ANY image built on a local base -- i.e. all of them -- so the guard would
# have been deleted or bypassed the first time it mattered, which is how a real
# check becomes decoration.
_switch_ref="$(grep -a -o -E 'bootc switch [^"]{0,160}' "$ISO" 2>/dev/null \
               | grep -o -E '(localhost|ghcr\.io|quay\.io|docker\.io)/[A-Za-z0-9._/:-]+' \
               | head -1)"
_refs="$(grep -a -o -E '(localhost|ghcr\.io|quay\.io|docker\.io)/[A-Za-z0-9._/-]+' "$ISO" \
         2>/dev/null | sort -u | head -20)"
if [ -z "$_switch_ref" ]; then
  die "could not read the bootc switch reference out of $ISO -- refusing to publish media
     whose upgrade path could not be verified. That is not the same as 'it is fine'."
fi
case "$_switch_ref" in
  localhost/*)
    echo "  refs found:" >&2
    printf '%s\n' "$_refs" | sed 's/^/       /' >&2
    die "this ISO's bootc switch ref is '$_switch_ref', so \`bootc upgrade\` on every
     machine installed from it will try to pull from the user's own empty store and fail.
     Rebuild against the published registry ref (build-awnix-iso.sh --image
     ghcr.io/aitherium/<repo>:latest) and publish that instead."
    ;;
esac
echo "    upgrade ref: $_switch_ref   (the one bootc actually uses)"
printf '%s\n' "$_refs" | sed 's/^/    seen: /'

# The assembler ships WITH the parts. A release page of *.part files and no *.iso is
# indistinguishable from a broken upload to the person looking at it, and telling them
# to run `cat` in a blog post is not shipping a product -- it is shipping homework, and
# it is precisely where the two traps live (numeric vs lexical part order, and a
# SHA256SUMS that describes the ASSEMBLED image rather than any part).
for helper in assemble-awnix-iso.sh assemble-awnix-iso.ps1 write-awnix-usb.sh; do
  if [ -f "$HERE/$helper" ]; then
    echo "  uploading $helper"
    gh release upload "$TAG" --repo "$REPO" --clobber "$HERE/$helper" \
      || die "upload failed on $helper -- the release has parts nobody can join, re-run"
  else
    die "$helper not found next to this script -- refusing to publish parts with no
     way to reassemble them. That is the whole failure this step exists to prevent."
  fi
done

gh release upload "$TAG" --repo "$REPO" --clobber SHA256SUMS awnix-iso.json \
  || die "parts uploaded but SHA256SUMS/awnix-iso.json did not -- re-run"

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
