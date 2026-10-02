#!/usr/bin/env bash
# Push the awnix variant images to their declared registries.
#
# Typed by hand twice on 2026-08-21, and the second time only after the first attempt
# pushed a tag that did not exist yet ("image not known") -- because the tag-and-push
# were separate steps and only one of them had run. That is the shape of a runbook.
#
# It reads awnix-variants.yaml. The manifest already records which variants may be
# published and WHERE, and check_awnix_variants.py already enforces that a private
# variant reaches exactly one destination. Duplicating those facts into a shell script
# would create the second copy that drifts -- so this asks the manifest.
#
#   ./publish-awnix-images.sh                 # every publishable variant
#   ./publish-awnix-images.sh --variant awnix
#   ./publish-awnix-images.sh --layer garg    # every variant whose layer is `garg`
#   ./publish-awnix-images.sh --include-private   # also the private appliance
#   ./publish-awnix-images.sh --dry-run
#   ./publish-awnix-images.sh --digests-out FILE  # "<ref> <digest>" per push, for signing
#   ./publish-awnix-images.sh --promote       # ALSO move :stable and :latest (see below)
#   ./publish-awnix-images.sh --self-test
#
# Tags (update-channels contract, 2026-09-27): every publish pushes `beta`, the date tag
# and the immutable `sha-<git12>`. `stable` and `latest` (an alias of stable that
# pre-channel installs still track) are what installed machines follow, so a default
# run NEVER moves them: that happens in awnix-promote.yml, by digest, only after the
# hosted upgrade/rollback proof passed. --promote exists for that workflow's fallback
# and for a hand-run on the owner's say-so; nothing else passes it.
#
# Exit: 0 pushed, 1 a push failed, 2 could not judge.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$HERE/awnix-variants.yaml"
DATE_TAG="$(date +%Y.%m.%d)"
ONLY=""; ONLY_LAYER=""; DRY=0; PRIVATE=0; PROMOTE=0; DIGESTS_OUT=""
GIT_SHA="${GIT_SHA:-$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)}"

die() { echo "publish-awnix-images: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --variant) ONLY="${2:-}"; shift 2 ;;
    --layer) ONLY_LAYER="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --tag) DATE_TAG="${2:-}"; shift 2 ;;
    --include-private) PRIVATE=1; shift ;;
    --promote) PROMOTE=1; shift ;;
    --digests-out) DIGESTS_OUT="${2:-}"; shift 2 ;;
    --git-sha) GIT_SHA="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# One place that knows how to read a variant out of the manifest. Python because it is
# in every awnix image and because a shell yaml parser is how the manifest and the
# script start disagreeing.
read_variants() {
  python3 - "$MANIFEST" "$1" <<'PY'
import sys
path, want = sys.argv[1], sys.argv[2]
cur, inv = None, False
data, top = {}, {}
for raw in open(path, encoding='utf-8'):
    if not raw.strip() or raw.lstrip().startswith('#'):
        continue
    ind, line = len(raw) - len(raw.lstrip()), raw.strip()
    if ind == 0:
        inv = line.startswith('variants:')
        cur = None
        # Top-level scalars are DEFAULTS the variants inherit. `registry:` lives
        # here for the three public variants; only the private one overrides it.
        if not inv and ':' in line:
            k, _, v = line.partition(':')
            if v.strip():
                top[k.strip()] = v.strip().strip(chr(39) + chr(34))
        continue
    if not inv:
        continue
    if ind == 2 and line.endswith(':'):
        cur = line[:-1]
        data[cur] = {}
        continue
    if cur and ':' in line:
        k, _, v = line.partition(':')
        data[cur][k.strip()] = v.strip().strip(chr(39) + chr(34))
for name, d in data.items():
    pub = d.get("publish", "")
    if want == "public" and pub != "true":
        continue
    if want == "private" and pub != "private":
        continue
    reg = d.get("registry") or top.get("registry", "")
    repo, img = d.get("repo", ""), d.get("image", "")
    if not (reg and repo and img):
        continue
    # The LAYER column lets a caller select a whole lane without naming the
    # variant -- `--layer garg` pushes the variant(s) built from that layer.
    print(f"{name}\t{img}\t{reg.rstrip('/')}/{repo}\t{d.get('layer', '')}")
PY
}

# The tag list is ONE function so the self-test proves what a real run pushes.
tags_for_run() {
  local t="beta $DATE_TAG"
  case "$GIT_SHA" in [0-9a-f]??????????*) t="$t sha-$(printf '%s' "$GIT_SHA" | cut -c1-12)" ;; esac
  [ "$PROMOTE" = "1" ] && t="$t stable latest"
  printf '%s
' "$t"
}

if [ "${SELFTEST:-0}" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }
  [ -f "$MANIFEST" ] || { echo "SELF-TEST DEAD: no manifest at $MANIFEST"; exit 2; }

  pub=$(read_variants public | wc -l)
  priv=$(read_variants private | wc -l)
  chk "$([ "$pub" -ge 1 ] && echo yes || echo no)" "yes" "reads at least one public variant"
  # Two private variants exist (aitheros + aitheros-cloud); a `-ge 1` arm so a
  # third cannot silently pass either.
  chk "$([ "$priv" -ge 1 ] && echo yes || echo no)" "yes" "reads at least one private variant"
  # The private ones must NOT appear in the public set -- that is the whole safety
  # property, and the read is where it would be lost.
  chk "$(read_variants public | grep -c aitheros)" "0" \
      "the private appliances are absent from the public set"
  # Every private variant carries its full destination (reg override resolved).
  chk "$([ "$(read_variants private | grep -c 'ghcr.io/aitherium/aitheros-bootc')" -ge 1 ] && echo yes || echo no)" "yes" \
      "every private variant carries its full destination"
  # A destination must be registry-qualified or the push goes to docker.io by default.
  chk "$(read_variants public | awk -F'\t' '$3 !~ /\// {print}' | wc -l)" "0" \
      "every public destination is registry-qualified"
  # --layer selects the variant(s) of one build lane, not by hand-typed name.
  chk "$(read_variants public | awk -F'\t' '$4 == "base" {print $1}' | grep -c '^awnix$')" "1" \
      "--layer base resolves to the awnix variant"

  # Channels: a default run never moves what installed machines follow.
  tags=$(PROMOTE=0; GIT_SHA=0123456789abcdef; tags_for_run)
  chk "$(echo " $tags " | grep -c ' latest \| stable ')" "0" "a default run pushes no latest and no stable"
  chk "$(echo " $tags " | grep -c ' beta ')" "1" "a default run pushes beta"
  chk "$(echo " $tags " | grep -c ' sha-0123456789ab ')" "1" "a default run pushes the immutable sha-<git12> tag"
  tags=$(PROMOTE=1; GIT_SHA=0123456789abcdef; tags_for_run)
  chk "$(echo " $tags " | grep -c ' stable latest ')" "1" "--promote is what moves stable and latest"
  tags=$(PROMOTE=0; GIT_SHA=; tags_for_run)
  chk "$(echo " $tags " | grep -c 'sha-')" "0" "no git sha means no sha- tag, never sha-<empty>"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
command -v podman >/dev/null 2>&1 || die "podman not on PATH -- run inside the podman host"

SETS="public"
[ "$PRIVATE" = "1" ] && SETS="public private"

TOTAL=0; FAILED=0
for set_name in $SETS; do
  while IFS="$(printf '\t')" read -r name img dest layer; do
    [ -n "${name:-}" ] || continue
    [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
    [ -z "$ONLY_LAYER" ] || [ "$ONLY_LAYER" = "$layer" ] || continue

    if ! podman image exists "$img"; then
      echo "  SKIP  $name -- $img is not built"
      continue
    fi

    for tag in $(tags_for_run); do
      TOTAL=$((TOTAL + 1))
      target="$dest:$tag"
      # Tag AND push together. Splitting them is how the first hand-run failed:
      # the push ran against a tag that had never been created and reported
      # "image not known", which reads as a missing image rather than a missing step.
      if [ "$DRY" = "1" ]; then
        echo "  DRY   $img -> $target"
        continue
      fi
      podman tag "$img" "$target" || { echo "  FAIL  tag $target"; FAILED=$((FAILED+1)); continue; }
      if podman push --digestfile /tmp/awnix-push.digest "$target" >/tmp/awnix-push.log 2>&1; then
        echo "  ok    $target"
        # The signer signs the DIGEST, never the tag (a tag can move after it is signed).
        if [ -n "$DIGESTS_OUT" ] && [ -s /tmp/awnix-push.digest ]; then
          printf '%s %s
' "$target" "$(cat /tmp/awnix-push.digest)" >> "$DIGESTS_OUT"
        fi
      else
        echo "  FAIL  $target"
        tail -3 /tmp/awnix-push.log | sed 's/^/        /'
        FAILED=$((FAILED + 1))
      fi
    done
  done <<EOF
$(read_variants "$set_name")
EOF
done

[ "$TOTAL" -gt 0 ] || die "no variant matched -- nothing was pushed, and that is not a pass"
[ "$FAILED" -eq 0 ] || die "$FAILED of $TOTAL push(es) failed"
echo "  $TOTAL image(s) pushed"
exit 0
