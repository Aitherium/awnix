#!/bin/sh
# spark-build-awnix.sh CTX_TAR SHA7 -- build the awnix base image NATIVELY on the DGX Spark
# (aarch64), rootless, at the lowest CPU and IO priority, with a memory cap, on the Spark's
# own disk. The production pool is measured before and after; if it got slower, the build
# is recorded as rc=1 and its image removed.
#
#   CTX_TAR  git archive of .DEPLOYMENT/standalone/bootc (the Containerfile's context)
#   SHA7     the develop commit the archive came from; tags localhost/awnix:arm64-SHA7
#
# Exit 0 built and verified arm64; 1 refused (memory/disk gate) or failed, or the pool
# regressed; 2 could not judge (no context, not aarch64, podman absent).
#
# PRECONDITION, OWNER-SIDE: podman on the Spark. It is NOT installed by this script: a
# package install on the production inference host is the owner's call (see
# REIMAGE-PLAN.md "Before any of this"). Absent podman => exit 2, nothing changed.
#
# Reversible: the image lives in the build user's rootless store (`podman rmi` undoes it). No
# docker state, no systemd unit, no GPU device and no pool unit is touched.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/spark-lib.sh"
CTX_TAR=${1:-}
SHA7=${2:-}
[ -n "$CTX_TAR" ] && [ -f "$CTX_TAR" ] && [ -n "$SHA7" ] || { echo "usage: $0 CTX_TAR SHA7" >&2; exit 2; }
TAG="localhost/awnix:arm64-${SHA7}"
BUILD_MEM=${BUILD_MEM:-12g}
LOG="$POOL_DIR/build.log"

if [ "$(uname -m)" != aarch64 ]; then
  ev_write build-arch 2 "uname -m" "not aarch64 ($(uname -m)); this lane is the native arm64 build"
  exit 2
fi
if ! command -v podman >/dev/null 2>&1; then
  ev_write podman-present 2 "command -v podman" "podman absent on the Spark; owner precondition (not installed by agents)"
  exit 2
fi
ev podman-version podman --version || exit 2

gate_memory || exit 1
gate_disk "$AWNIX_SPARK_HOME" || exit 1
pool_health before

CTX="$AWNIX_SPARK_HOME/build/ctx-${SHA7}"
rm -rf "$CTX"; mkdir -p "$CTX"
ev build-context tar -xf "$CTX_TAR" -C "$CTX" || exit 2
# git archive of a subdir keeps the path prefix; find the Containerfile wherever it landed.
CF=$(find "$CTX" -name Containerfile.awnix -type f | head -1)
[ -n "$CF" ] || { ev_write build-context 2 "find Containerfile.awnix" "not in archive"; exit 2; }
CTXDIR=$(dirname "$CF")

START=$(date +%s)
BUILD="nice -n 19 ionice -c 3 podman build --build-arg TARGETARCH=arm64 --memory $BUILD_MEM --cpu-shares 128 --jobs 1 --layers -t $TAG -f Containerfile.awnix ."
EV_CMD="$BUILD" EV_NOTES="native aarch64 on the Spark; rootless; context=git archive $SHA7" \
  ev podman-build sh -c "cd '$CTXDIR' && $BUILD > '$LOG' 2>&1"
BRC=$?
END=$(date +%s)
ev_write build-wall "$BRC" "date +%s (build start/end)" "wall_s=$((END - START))"

pool_health after
if ! pool_regressed; then
  ev_write pool-regressed 1 "compare pool-before.txt pool-after.txt" "production pool slower after build; removing image"
  podman rmi -f "$TAG" >/dev/null 2>&1
  exit 1
fi
[ "$BRC" -eq 0 ] || exit 1

INSPECT=$(podman image inspect "$TAG" --format '{{.Architecture}} {{.Os}} {{.Id}} {{.Size}}')
IRC=$?
ev_write image-inspect "$IRC" "podman image inspect $TAG --format '{{.Architecture}} {{.Os}} {{.Id}} {{.Size}}'" "$INSPECT"
ARCH=$(printf '%s' "$INSPECT" | awk '{print $1}')
if [ "$ARCH" != arm64 ]; then
  ev_write image-arch 1 "podman image inspect --format {{.Architecture}}" "got '$ARCH', want arm64"
  exit 1
fi
ev_write image-arch 0 "podman image inspect --format {{.Architecture}}" "arm64"
ev image-os-release podman run --rm --network=none "$TAG" sh -c 'grep ^PRETTY_NAME /usr/lib/os-release; uname -m; pwsh --version'
