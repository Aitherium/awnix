#!/usr/bin/env bash
# Publish an awnix image as a PREBUILT WSL root filesystem (release assets on Aitherium/awnix).
#
# Why: install-awnix-wsl.ps1 made the rootfs ON THE CUSTOMER'S MACHINE, by exporting the
# container image through Docker Desktop or podman inside another WSL distro. A laptop
# that has never seen a terminal has neither, so the one-line install died on step one.
# Exporting it HERE, once per release, turns the Windows side into download + import:
# install-aither-windows.ps1 reads the manifest this script writes and needs nothing but
# wsl.exe.
#
#   publish-awnix-wsl-rootfs.sh --image ghcr.io/aitherium/awnix:latest --layer base \
#       --tag awnix-wsl-base-2026.11.15 [--repo Aitherium/awnix] [--out DIR] [--dry-run]
#   publish-awnix-wsl-rootfs.sh --from-tar FILE.tar.gz --layer base --tag ... --dry-run
#   publish-awnix-wsl-rootfs.sh --self-test
#
# What lands on the release (tag awnix-wsl-<layer>-<YYYY.MM.DD>, never "Latest" -- AUC006
# keeps /releases/latest the base ISO):
#   awnix-wsl-<layer>.rootfs.tar.gz.partNN   GitHub caps an asset at 2 GiB, so it ships split
#   awnix-wsl.json                           the awrtifact manifest shape (name, part_size,
#                                            parts[{name,size,sha256}], sha256, total) plus
#                                            schema/kind/layer/tag/image, read by the installer
#   SHA256SUMS                               the WHOLE tarball and every part
# The parts are rejoined and re-hashed BEFORE anything is uploaded; parts go up first,
# the manifest last, and the part count on the release is read back from the API.
#
# `wsl --import` reads a gzip tarball directly, so no decompression step is needed on
# Windows. The tarball must hold a systemd root (usr/lib/systemd/systemd) or it is refused.
#
# Exit: 0 published (or dry-run clean), 1 a step failed, 2 could not judge.
set -uo pipefail

REPO="Aitherium/awnix"
PART_BYTES=$((1400 * 1024 * 1024))   # under the 2 GiB cap; same size as the ISO parts
IMAGE=""; LAYER=""; TAG=""; OUT=""; FROM_TAR=""; DRY=0; SELFTEST=0
MIN_BYTES=$((50 * 1024 * 1024))

die() { echo "publish-awnix-wsl-rootfs: $*" >&2; exit 1; }
PY="${PYTHON:-$(command -v python3 || command -v python || true)}"

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --layer) LAYER="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --from-tar) FROM_TAR="$2"; shift 2 ;;
    --part-bytes) PART_BYTES="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "publish-awnix-wsl-rootfs: unknown argument $1" >&2; exit 2 ;;
  esac
done

[ -n "$PY" ] || { echo "publish-awnix-wsl-rootfs: python3 is required for the manifest" >&2; exit 2; }

stem_for() { echo "awnix-wsl-$1.rootfs.tar.gz"; }

# Only the public awnix rungs get a public rootfs; a private image never does.
layer_allowed() {  # layer_allowed REPO LAYER -> ok | refuse:<why>
  case "$1" in Aitherium/awnix) : ;; *) echo "refuse:repo $1 is not Aitherium/awnix"; return ;; esac
  case "$2" in base|full|ai-full) echo ok ;; *) echo "refuse:layer $2 is not a public WSL edition (base, full, ai-full)" ;; esac
}

tag_ok() {  # tag_ok TAG LAYER -> ok | refuse
  case "$1" in "awnix-wsl-$2-"[0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]*) echo ok ;; *) echo refuse ;; esac
}

# write_manifest OUT_JSON TARBALL LAYER TAG IMAGE PART... (parts in order)
write_manifest() {
  "$PY" - "$@" <<'PY'
import hashlib, json, os, sys
out, tar, layer, tag, image, *parts = sys.argv[1:]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()
rows = [{"name": os.path.basename(p), "size": os.path.getsize(p), "sha256": sha(p)} for p in parts]
doc = {"schema": 1, "kind": "awnix-wsl-rootfs", "layer": layer, "tag": tag, "image": image,
       "name": os.path.basename(tar), "part_size": rows[0]["size"] if rows else 0,
       "parts": rows, "sha256": sha(tar), "total": os.path.getsize(tar)}
if sum(r["size"] for r in rows) != doc["total"]:
    sys.exit("parts do not add up to the tarball")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(doc, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}

# The parts, rejoined in manifest order, hash to the manifest's whole sha256.
verify_join() {  # verify_join MANIFEST DIR -> exit 0/1
  "$PY" - "$1" "$2" <<'PY'
import hashlib, json, os, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
h = hashlib.sha256(); n = 0
for p in m["parts"]:
    with open(os.path.join(sys.argv[2], p["name"]), "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b); n += len(b)
sys.exit(0 if (h.hexdigest() == m["sha256"] and n == m["total"]) else 1)
PY
}

is_systemd_root() {  # is_systemd_root TARBALL -> exit 0 when it carries usr/lib/systemd/systemd
  tar -tzf - < "$1" 2>/dev/null | grep -qE '^(\./)?usr/lib/systemd/systemd$'
}

split_parts() {  # split_parts TARBALL STEM DIR
  ( cd "$3" && rm -f "$2".part[0-9][0-9] && split -b "$PART_BYTES" -d -a 2 "$1" "$2.part" )
}

if [ "$SELFTEST" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1', want '$2')"; fail=$((fail+1)); fi; }
  T="$(mktemp -d)"; trap 'rm -rf -- "$T"' EXIT
  chk "$(layer_allowed Aitherium/awnix base)" ok "base publishes on the public repo"
  chk "$(layer_allowed Aitherium/awnix ai-full)" ok "ai-full publishes on the public repo"
  chk "$(layer_allowed Aitherium/awnix garg | cut -d: -f1)" refuse "a private layer never gets a public rootfs"
  chk "$(layer_allowed Aitherium/garg-aitherium base | cut -d: -f1)" refuse "another repo is refused"
  chk "$(tag_ok awnix-wsl-base-2026.11.15 base)" ok "dated tag accepted"
  chk "$(tag_ok awnix-iso-2026.11.15 base)" refuse "an ISO tag is refused"
  chk "$(tag_ok awnix-wsl-full-2026.11.15 base)" refuse "a tag for another layer is refused"
  chk "$(stem_for ai-full)" "awnix-wsl-ai-full.rootfs.tar.gz" "asset stem names the layer"

  mkdir -p "$T/root/usr/lib/systemd" "$T/root/etc" "$T/out"
  printf 'x' > "$T/root/usr/lib/systemd/systemd"
  head -c 300000 /dev/urandom > "$T/root/etc/blob"
  ( cd "$T/root" && tar -czf "$T/out/fake.tar.gz" . )
  is_systemd_root "$T/out/fake.tar.gz"; chk "$?" 0 "a tarball with systemd is a systemd root"
  mkdir -p "$T/bare/etc"; printf 'y' > "$T/bare/etc/x"
  ( cd "$T/bare" && tar -czf "$T/bare.tar.gz" . )
  is_systemd_root "$T/bare.tar.gz"; chk "$?" 1 "a tarball without systemd is refused"

  PART_BYTES=100000
  STEM="$(stem_for base)"; cp "$T/out/fake.tar.gz" "$T/out/$STEM"
  split_parts "$STEM" "$STEM" "$T/out"
  NP=$(ls "$T/out/$STEM".part[0-9][0-9] | wc -l | tr -d ' ')
  [ "$NP" -ge 3 ] && chk ok ok "the tarball splits into $NP parts" || chk "$NP" ">=3" "the tarball splits"
  write_manifest "$T/out/awnix-wsl.json" "$T/out/$STEM" base awnix-wsl-base-2026.11.15 img $(ls "$T/out/$STEM".part[0-9][0-9] | sort)
  chk "$?" 0 "manifest written"
  verify_join "$T/out/awnix-wsl.json" "$T/out"; chk "$?" 0 "parts rejoin to the whole sha256"
  chk "$("$PY" -c 'import json,sys;m=json.load(open(sys.argv[1]));print(m["kind"], m["layer"], len(m["parts"])==int(sys.argv[2]), sum(p["size"] for p in m["parts"])==m["total"])' "$T/out/awnix-wsl.json" "$NP")" \
      "awnix-wsl-rootfs base True True" "manifest carries kind, layer, every part, sizes that add up"
  printf 'Z' | dd of="$T/out/$STEM.part01" bs=1 seek=10 conv=notrunc 2>/dev/null
  verify_join "$T/out/awnix-wsl.json" "$T/out"; chk "$?" 1 "a corrupted part breaks the join"
  [ "$fail" = 0 ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED ($fail)"; exit 1; }
fi

[ -n "$LAYER" ] || die "--layer is required"
[ -n "$TAG" ] || die "--tag is required"
_g="$(layer_allowed "$REPO" "$LAYER")"; [ "$_g" = ok ] || die "${_g#refuse:}"
[ "$(tag_ok "$TAG" "$LAYER")" = ok ] || die "tag $TAG is not awnix-wsl-$LAYER-YYYY.MM.DD"
[ -n "$IMAGE" ] || [ -n "$FROM_TAR" ] || die "--image or --from-tar is required"
OUT="${OUT:-/var/tmp/awnix-wsl-$LAYER}"
mkdir -p "$OUT" || die "cannot create $OUT"
STEM="$(stem_for "$LAYER")"
TARBALL="$OUT/$STEM"
rm -f -- "$TARBALL" "$OUT/$STEM".part[0-9][0-9] "$OUT/awnix-wsl.json" "$OUT/SHA256SUMS"

if [ -n "$FROM_TAR" ]; then
  cp -- "$FROM_TAR" "$TARBALL" || die "cannot copy $FROM_TAR"
else
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
  GZ="gzip -9"; command -v pigz >/dev/null 2>&1 && GZ="pigz -9"
  echo "  exporting $IMAGE ($GZ)"
  CID="$($SUDO podman create --entrypoint /bin/true "$IMAGE")" || die "podman create failed for $IMAGE"
  $SUDO podman export "$CID" | $GZ > "$TARBALL"
  rc=("${PIPESTATUS[@]}")
  $SUDO podman rm "$CID" >/dev/null 2>&1 || true
  [ "${rc[0]}" = 0 ] && [ "${rc[1]}" = 0 ] || die "export failed (podman ${rc[0]}, gzip ${rc[1]})"
fi
SIZE=$(wc -c < "$TARBALL" | tr -d ' ')
[ "$SIZE" -ge "$MIN_BYTES" ] || [ -n "$FROM_TAR" ] || die "the tarball is only $SIZE bytes -- not a rootfs"
is_systemd_root "$TARBALL" || die "the tarball has no usr/lib/systemd/systemd -- WSL would boot no services"
echo "  rootfs: $TARBALL ($SIZE bytes)"

split_parts "$STEM" "$STEM" "$OUT" || die "split failed"
PARTS=$(cd "$OUT" && ls "$STEM".part[0-9][0-9] | sort)
write_manifest "$OUT/awnix-wsl.json" "$TARBALL" "$LAYER" "$TAG" "${IMAGE:-from-tar}" \
  $(for p in $PARTS; do echo "$OUT/$p"; done) || die "could not write awnix-wsl.json"
verify_join "$OUT/awnix-wsl.json" "$OUT" || die "the parts do NOT rejoin to the tarball -- refusing to publish"
( cd "$OUT" && sha256sum "$STEM" $PARTS > SHA256SUMS ) || die "sha256sum failed"
echo "  parts: $(echo $PARTS | wc -w | tr -d ' '), join verified"

if [ "$DRY" = 1 ]; then echo "  dry-run: not creating a release ($OUT)"; exit 0; fi
command -v gh >/dev/null 2>&1 || die "gh not on PATH"
[ -n "${GH_TOKEN:-}" ] || gh auth status >/dev/null 2>&1 || die "gh is not authenticated"

if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release create "$TAG" --repo "$REPO" --latest=false \
    --title "awnix for Windows (WSL) -- $LAYER -- ${TAG##*-}" \
    --notes "Prebuilt awnix root filesystem for Windows. Install: run install-aither-windows.ps1 (it picks the newest of these). Parts rejoin to awnix-wsl.json's sha256." \
    || die "release create failed"
fi
for p in $PARTS; do
  echo "  uploading $p"
  gh release upload "$TAG" --repo "$REPO" --clobber "$OUT/$p" || die "upload failed on $p -- re-run to finish"
done
gh release upload "$TAG" --repo "$REPO" --clobber "$OUT/SHA256SUMS" "$OUT/awnix-wsl.json" \
  || die "parts uploaded but the manifest did not -- re-run"
WANT=$(echo $PARTS | wc -w | tr -d ' ')
GOT=$(gh release view "$TAG" --repo "$REPO" --json assets \
      -q "[.assets[] | select(.size > 0) | select(.name | test(\"[.]part[0-9][0-9]\$\"))] | length" 2>/dev/null || echo 0)
[ "${GOT:-0}" -eq "$WANT" ] || die "uploaded $GOT of $WANT parts -- the release is INCOMPLETE, re-run"
echo "  verified: $GOT/$WANT parts + awnix-wsl.json on $REPO $TAG"
rm -f -- "$TARBALL" "$OUT/$STEM".part[0-9][0-9]
exit 0
