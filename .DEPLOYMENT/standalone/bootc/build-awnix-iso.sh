#!/usr/bin/env bash
# Turn an awnix bootc image into bootable media (ISO / qcow2 / raw).
#
# `build_awnix_images.py` builds the three CONTAINER layers. Nothing turned them into
# something a person can boot, which is the gap between "awnix builds" and "awnix is a
# thing you can download". This is that step.
#
#   ./build-awnix-iso.sh                          # ISO from localhost/awnix:latest
#   ./build-awnix-iso.sh --image localhost/awnix-runner-ai:latest
#   ./build-awnix-iso.sh --type qcow2 --out /var/tmp/awnix-iso
#
# Run it INSIDE the podman host (the Debian WSL2 distro here), as root:
#   wsl -d Debian -u root /mnt/c/AitherOS-Fresh/.DEPLOYMENT/standalone/bootc/build-awnix-iso.sh
#
# WHY THE DISK GUARD IS NOT OPTIONAL. bootc-image-builder writes several GB of
# intermediate osbuild artifacts before it writes the ISO, into the SAME filesystem the
# whole podman fleet lives on. Filling that root has taken Postgres down on this host
# before -- the database does not degrade, it PANICs. So this refuses to start unless
# there is real headroom, and says how much it wanted. A build that kills the fleet to
# produce an ISO is not a successful build.
set -uo pipefail

IMAGE="localhost/awnix:latest"
TYPE="iso"
OUT="/var/tmp/awnix-iso"
MIN_FREE_GB=40
BIB="quay.io/centos-bootc/bootc-image-builder:latest"

# The image types bootc-image-builder can emit that we support. ONE list: it was
# previously written twice -- in the self-test and in the runtime guard -- so the
# self-test could pass a type the real path refused.
#
# iso/qcow2/raw/vmdk are local media and VMs. ami/vhd/gce are AWS, Azure and Google
# Cloud, which is how the same chain that builds the installer builds cloud images.
#
# Space-separated, NOT a case pattern: `case $x in $VAR)` does not treat "a|b" from a
# variable as alternation (the | is parsed before expansion), so it would match only the
# literal string and refuse everything.
AWNIX_TYPES="iso qcow2 raw vmdk ami vhd gce"

valid_type() {
  local _t
  for _t in $AWNIX_TYPES; do
    [ "$1" = "$_t" ] && { echo yes; return 0; }
  done
  echo no
}

die() { echo "build-awnix-iso: $*" >&2; exit 1; }

# Is this file actually a bootable ISO, or merely a file whose name ends in .iso?
#
# "An artifact exists" and "an artifact boots" are different claims, and only the second
# one matters for something people download. A truncated osbuild run, a 0-byte file, or a
# non-hybrid image all satisfy the first. Two structural facts settle it without mounting
# anything or trusting `file` to be installed:
#   * ISO 9660 writes the literal bytes CD001 at offset 0x8001 (sector 16 + 1).
#   * A bootable CD carries an El Torito boot record in sector 17.
# Echoes ok / notiso / noboot / empty -- never silence, because an unreadable image must
# not read the same as a good one.
iso_bootable() {
  _f="${1:-}"
  [ -s "$_f" ] || { echo empty; return; }
  # grep the PIPE rather than capturing into a variable. Command substitution on
  # binary makes bash warn about ignored NUL bytes on every single run, and a build
  # script that cries wolf on a good run trains people past the run that matters.
  # Escaping NULs for `tr` was tried first and is how this got quietly broken: the
  # sequence survived one hop and arrived as `tr -d ' '`, deleting SPACES -- which
  # the self-test happily passed, because CD001 contains no spaces. No substitution,
  # no escaping, nothing to mangle.
  if ! dd if="$_f" bs=1 skip=32769 count=5 2>/dev/null | grep -qa "CD001"; then echo notiso; return; fi
  if dd if="$_f" bs=2048 skip=17 count=1 2>/dev/null | grep -qa "EL TORITO"; then
    echo ok
  else
    echo noboot
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:-}"; shift 2 ;;
    --type)  TYPE="${2:-}";  shift 2 ;;
    --out)   OUT="${2:-}";   shift 2 ;;
    --min-free-gb) MIN_FREE_GB="${2:-}"; shift 2 ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# --self-test proves the guards can refuse, without building anything. A build script
# whose refusals have never been seen to fire is a script that will happily fill the disk.
if [ "${SELFTEST:-0}" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }

  # free_gb parses the number it is given, and a garbled df must not read as "plenty".
  free_gb_from() { awk 'NR==1 {print int($1/1024/1024)}' <<<"${1:-}"; }
  chk "$(free_gb_from 209715200)" "200" "parses kB into GB"

  enough() { [ -n "$1" ] && [ "$1" -ge "$2" ] 2>/dev/null && echo yes || echo no; }
  chk "$(enough 200 40)" "yes" "200GB clears a 40GB floor"
  chk "$(enough 12 40)"  "no"  "12GB is refused"
  chk "$(enough '' 40)"  "no"  "unknown free space is refused, not assumed"
  # The property that matters is not what the parser RETURNS for garbage, it is that
  # garbage ends in a refusal. The first version asserted the return value, and awk
  # answers '0' for empty input rather than '' -- so the test failed while the code was
  # correct. Assert the decision, not the intermediate.
  chk "$(enough "$(free_gb_from '')" 40)" "no" "a garbled df ends in a refusal"
  chk "$(enough "$(free_gb_from 1048576)" 40)" "no" "1GB free is refused"

  # The verdict rule, both directions. Born from a real false negative: rc=127 with a
  # 2.7GB bootable ISO on disk, reported as "nothing usable was produced".
  verdict() { if [ -n "$1" ]; then echo ok; else echo fail; fi; }
  chk "$(verdict /out/install.iso)" "ok"   "an artifact means success even when rc != 0"
  chk "$(verdict '')"               "fail" "no artifact is a failure even when rc == 0"

  # iso_bootable, against bytes built here -- an empty file, a file with no CD001, and a
  # synthetic ISO 9660 that carries no boot record. The last one is the case that a
  # name-and-size check cannot tell from a good image.
  _t="$(mktemp -d)"
  : > "$_t/empty.iso"
  chk "$(iso_bootable "$_t/empty.iso")" "empty" "an empty file is not a bootable ISO"
  head -c 40000 /dev/zero > "$_t/zeros.iso"
  chk "$(iso_bootable "$_t/zeros.iso")" "notiso" "40KB of zeros is not an ISO"
  head -c 40000 /dev/zero > "$_t/noboot.iso"
  printf 'CD001' | dd of="$_t/noboot.iso" bs=1 seek=32769 conv=notrunc 2>/dev/null
  chk "$(iso_bootable "$_t/noboot.iso")" "noboot" "ISO 9660 with no El Torito is refused"
  printf 'EL TORITO SPECIFICATION' | dd of="$_t/noboot.iso" bs=1 seek=34816 conv=notrunc 2>/dev/null
  chk "$(iso_bootable "$_t/noboot.iso")" "ok" "...and accepted once the boot record is there"
  chk "$(iso_bootable "$_t/nope.iso")" "empty" "a missing file is not a pass"
  rm -rf "$_t"

  # valid_type is defined ONCE at the top and shared with the runtime guard, so this
  # cannot pass a type the real path would refuse.
  chk "$(valid_type iso)"   "yes" "iso is a type"
  chk "$(valid_type qcow2)" "yes" "qcow2 is a type"
  chk "$(valid_type ami)"   "yes" "ami is a type (AWS)"
  chk "$(valid_type vhd)"   "yes" "vhd is a type (Azure)"
  chk "$(valid_type gce)"   "yes" "gce is a type (Google Cloud)"
  chk "$(valid_type sandwich)" "no" "an unknown type is refused"
  chk "$(valid_type "iso qcow2")" "no" "the whole list is not itself a valid type"

  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ "$(valid_type "$TYPE")" = "yes" ] || die "unsupported --type '$TYPE' (one of: $AWNIX_TYPES)"
command -v podman >/dev/null 2>&1 || die "podman not on PATH -- run this inside the podman host"
[ "$(id -u)" -eq 0 ] || die "must run as root (bootc-image-builder needs --privileged)"

podman image exists "$IMAGE" || die "image not in local storage: $IMAGE
  build it first:  python AitherOS/dev/tools/build_awnix_images.py"

mkdir -p "$OUT" || die "cannot create $OUT"

FREE_KB="$(df -Pk "$OUT" | awk 'NR==2 {print $4}')"
FREE_GB="$(awk -v k="${FREE_KB:-0}" 'BEGIN {print int(k/1024/1024)}')"
if [ -z "$FREE_KB" ] || [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
  die "only ${FREE_GB}GB free on $OUT, need >= ${MIN_FREE_GB}GB.
  bootc-image-builder writes GBs of osbuild intermediates before the image appears, on the
  same filesystem the fleet runs on. Free space or pass --min-free-gb if you know better."
fi

echo "build-awnix-iso"
echo "  image : $IMAGE"
echo "  type  : $TYPE"
echo "  out   : $OUT  (${FREE_GB}GB free)"
echo

START=$(date +%s)
# --network=host: bootc-image-builder DEPSOLVES against the CentOS mirrors, so the
# media step needs a working resolver just as much as the layer build does -- and
# for the same reason it does not have one by default. On the AWS builder this runs
# as ROOTFUL podman inside a container; a rootless build inherits the surrounding
# container's networking, a rootful one builds its own netns with no usable
# /etc/resolv.conf. Measured 2026-08-22 on run 32547979438: the image layer built
# clean (it already carries --network=host) and bib died 254s later with
#   Curl error (6): Could not resolve host: mirrors.centos.org
# which reads as a mirror outage rather than as this container having no DNS.
#
# The failure is expensive rather than loud: it arrives four minutes in, after the
# 20-minute layer build has already succeeded, and it names a hostname owned by
# somebody else.
podman run --rm --privileged \
  --network=host \
  --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v "$OUT":/output \
  "$BIB" \
  --type "$TYPE" --local "$IMAGE"
rc=$?
ELAPSED=$(( $(date +%s) - START ))

# THE EXIT CODE IS NOT THE VERDICT -- IN EITHER DIRECTION.
#
# The first version of this script failed the build on a non-zero rc before looking at
# the disk, and the very first real run proved that wrong: osbuild printed "Build
# complete!", wrote a 2.7GB bootable ISO, and bootc-image-builder still exited 127. The
# script reported "nothing usable was produced" about a finished, verified-bootable
# image (ISO 9660, CD001, El Torito, CentOS-Stream-9 boot sector).
#
# So the artifact decides, and the rc is reported alongside it:
#   artifact + rc 0    -> success
#   artifact + rc != 0 -> success WITH a warning naming the code, because something in
#                         the builder's teardown failed and that is worth knowing
#   no artifact        -> failure, whatever the rc said
# I had already written the "exit 0 having written nothing" half of this rule and only
# applied it in one direction.
ART="$(find "$OUT" -type f \( -name '*.iso' -o -name '*.qcow2' -o -name '*.raw' -o -name '*.vmdk' \) -newermt "-${ELAPSED} seconds" 2>/dev/null | head -1)"
[ -n "$ART" ] || ART="$(find "$OUT" -type f \( -name '*.iso' -o -name '*.qcow2' -o -name '*.raw' \) | head -1)"
if [ -z "$ART" ]; then
  die "no image file under $OUT after ${ELAPSED}s (builder exit ${rc}) -- FAILED build"
fi

SIZE="$(du -h "$ART" | cut -f1)"
echo
echo "  artifact : $ART"
echo "  size     : $SIZE"
echo "  built in : ${ELAPSED}s"

# Only an ISO can be checked this way; a qcow2/raw is a different shape and is reported
# as unverified rather than silently blessed.
if [ "$TYPE" = "iso" ]; then
  case "$(iso_bootable "$ART")" in
    ok)     echo "  boot     : ISO 9660 + El Torito present -- structurally bootable" ;;
    empty)  die "the produced file is EMPTY -- FAILED build" ;;
    notiso) die "no CD001 magic at 0x8001 -- this is not an ISO 9660 image, FAILED build" ;;
    noboot) die "ISO 9660 but NO El Torito boot record -- it would not boot, FAILED build" ;;
    *)      die "could not judge the image structure -- refusing to call this a success" ;;
  esac
else
  echo "  boot     : not checked (structural check is ISO-only)"
fi
if [ "$rc" -ne 0 ]; then
  echo
  echo "  NOTE: the builder exited ${rc} but produced a usable image. Something in its"
  echo "        teardown failed; the artifact above is real. Verify it before shipping:"
  echo "        file \"$ART\"   # expect: ISO 9660 ... (bootable)"
fi
exit 0
