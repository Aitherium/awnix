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
#   ./publish-awnix-images.sh --include-private   # also the private appliance
#   ./publish-awnix-images.sh --dry-run
#   ./publish-awnix-images.sh --self-test
#
# Exit: 0 pushed, 1 a push failed, 2 could not judge.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$HERE/awnix-variants.yaml"
DATE_TAG="$(date +%Y.%m.%d)"
ONLY=""; DRY=0; PRIVATE=0

die() { echo "publish-awnix-images: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --variant) ONLY="${2:-}"; shift 2 ;;
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --tag) DATE_TAG="${2:-}"; shift 2 ;;
    --include-private) PRIVATE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
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
    print(f"{name}\t{img}\t{reg.rstrip('/')}/{repo}")
PY
}

if [ "${SELFTEST:-0}" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }
  [ -f "$MANIFEST" ] || { echo "SELF-TEST DEAD: no manifest at $MANIFEST"; exit 2; }

  pub=$(read_variants public | wc -l)
  priv=$(read_variants private | wc -l)
  chk "$([ "$pub" -ge 1 ] && echo yes || echo no)" "yes" "reads at least one public variant"
  chk "$priv" "1" "reads exactly one private variant"
  # The private one must NOT appear in the public set -- that is the whole safety
  # property, and the read is where it would be lost.
  chk "$(read_variants public | grep -c aitheros)" "0" \
      "the private appliance is absent from the public set"
  chk "$(read_variants private | grep -c 'ghcr.io/aitherium/aitheros-bootc')" "1" \
      "the private variant carries its full destination"
  # A destination must be registry-qualified or the push goes to docker.io by default.
  chk "$(read_variants public | awk -F'\t' '$3 !~ /\// {print}' | wc -l)" "0" \
      "every public destination is registry-qualified"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
command -v podman >/dev/null 2>&1 || die "podman not on PATH -- run inside the podman host"

SETS="public"
[ "$PRIVATE" = "1" ] && SETS="public private"

TOTAL=0; FAILED=0
for set_name in $SETS; do
  while IFS="$(printf '\t')" read -r name img dest; do
    [ -n "${name:-}" ] || continue
    [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue

    if ! podman image exists "$img"; then
      echo "  SKIP  $name -- $img is not built"
      continue
    fi

    for tag in latest "$DATE_TAG"; do
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
      if podman push "$target" >/tmp/awnix-push.log 2>&1; then
        echo "  ok    $target"
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
