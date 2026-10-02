#!/usr/bin/env bash
# awnix-arm64-lane.sh -- build the awnix chain NATIVELY on aarch64, probe it, and
# optionally make media and boot it. The body of .github/workflows/awnix-arm64.yml;
# kept as a script so the hosted and Spark jobs run the same bytes.
#
# Usage:
#   awnix-arm64-lane.sh --layer awnix|runner|runner-ai|garg --runner-kind hosted|spark
#                       [--iso] [--boot-smoke] [--evidence FILE] [--self-test]
#
# Exit: 0 every requested stage passed, 1 a stage failed, 2 could not judge (wrong
# arch, no podman). The evidence file is written on EVERY exit path that got past
# argument parsing, so a failed run still says which stage failed and with what rc.
#
# What it never does: push, tag for a registry, release, prune, migrate or reset a
# podman store. Publishing is a separate, inputs.publish-guarded workflow step.
# On the Spark (runner-kind spark) it builds ROOTLESS in the store named by
# $CONTAINERS_STORAGE_CONF and refuses --iso/--boot-smoke (bootc-image-builder needs
# rootful --privileged, which does not belong on the inference box).
set -uo pipefail

BIB="quay.io/centos-bootc/bootc-image-builder@sha256:2b52843ea2bfda73b0a08d97e76b734393b1d3a804681b9fabb26723bd3a2f0b"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER=awnix
KIND=hosted
DO_ISO=0
DO_BOOT=0
EVIDENCE="${PWD}/arm64-evidence.json"
SMOKE_TIMEOUT_MIN="${AWNIX_ARM64_SMOKE_TIMEOUT_MIN:-45}"
ISO_MIN_FREE_GB="${AWNIX_ARM64_ISO_MIN_FREE_GB:-30}"
PY=python3; for _c in python3 python; do "$_c" -c "import json" >/dev/null 2>&1 && { PY="$_c"; break; }; done   # the evidence writer (a stub python3 does not count)

die2() { echo "awnix-arm64-lane: CANNOT JUDGE: $*" >&2; exit 2; }
log() { echo "[arm64-lane] $*"; }

# layer -> ordered chain of "tag|containerfile"
chain_for() {
  local base="localhost/awnix-base:latest|Containerfile.awnix"
  local runner="localhost/awnix-runner:latest|Containerfile.awnix-runner"
  local ai="localhost/awnix-runner-ai:latest|Containerfile.awnix-runner-ai"
  local garg="localhost/garg-appliance:latest|Containerfile.garg-appliance"
  case "$1" in
    awnix)     echo "$base" ;;
    runner)    echo "$base $runner" ;;
    runner-ai) echo "$base $runner $ai" ;;
    garg)      echo "$base $runner $ai $garg" ;;
    *) return 1 ;;
  esac
}

# arch-neutral probes, one per layer; each prints <LAYER>_ARM64_OK on success
probe_cmd_for() {
  case "$1" in
    Containerfile.awnix)
      echo 'test "$(uname -m)" = aarch64 && python3.11 -c "import awgit, awgraph, awrelay, awm, awshare, awrecover, awseal" && grep -q "^NAME=.awnix.$" /usr/lib/os-release && test -x /usr/bin/awnix-setup && pwsh -NoLogo -NoProfile -Command "exit 0" && echo BASE_ARM64_OK' ;;
    Containerfile.awnix-runner)
      echo 'test "$(uname -m)" = aarch64 && test -x /opt/actions-runner/config.sh && id runner >/dev/null && test "$(od -An -tx1 -j18 -N2 /opt/actions-runner/bin/Runner.Listener | tr -d " ")" = b700 && ls -d /opt/actions-runner/_work/_tool/Python/3.1*/arm64 && echo RUNNER_ARM64_OK' ;;
    Containerfile.awnix-runner-ai)
      echo 'test "$(uname -m)" = aarch64 && set -- /opt/bonsai/lib/ld-linux-*.so.* && test "$#" -eq 1 && test -x "$1" && BIN=$(find /opt/bonsai/bin -name llama-server -type f | head -1) && "$1" --library-path "/opt/bonsai/lib:$(dirname "$BIN")" "$BIN" --version && echo RUNNER_AI_ARM64_OK' ;;
    Containerfile.garg-appliance)
      echo 'test "$(uname -m)" = aarch64 && test -x /opt/qdrant/qdrant && /opt/qdrant/qdrant --version && test -d /opt/gargbot/backend/portal_kit_backend && test -L /etc/systemd/system/multi-user.target.wants/garg-firstboot.service && echo GARG_ARM64_OK' ;;
  esac
}

# ── evidence (written on every exit) ────────────────────────────────────────────
# Every value reaches Python through the ENVIRONMENT, never spliced into its source:
# a serial marker carries systemd's colour escapes, and a JSON "\u001b" inlined into a
# Python string literal turns back into a raw control byte that json.loads rejects
# (the reviewer's reproduction). A failed write turns a passing run into rc 1, so a
# green job always has its evidence file.
MEDIA_ROOT=/var/tmp
BUILD_EXIT=null PROBE_EXIT=null IMAGE_DIGEST="" ISO_JSON=null BOOT_JSON=null KVM=false
FAILED_STAGE=""
write_evidence() {
  E_LAYER="$LAYER" E_KIND="$KIND" E_DIGEST="$IMAGE_DIGEST" E_BUILD="$BUILD_EXIT" \
  E_PROBE="$PROBE_EXIT" E_ISO="$ISO_JSON" E_BOOT="$BOOT_JSON" E_KVM="$KVM" \
  E_FAILED="$FAILED_STAGE" "$PY" - "$EVIDENCE" <<'PYEOF'
import json, os, platform, sys
e = os.environ
bad = []
def num(k):
    v = e.get(k, "")
    return None if v in ("", "null") else int(v)
def obj(k):
    v = e.get(k, "null")
    try:
        return json.loads(v)
    except ValueError as exc:
        bad.append(k)
        return {"unparsed": v, "error": str(exc)}
doc = {
  "schema": 1,
  "layer": e.get("E_LAYER"),
  "arch": "arm64",
  "image_digest": e.get("E_DIGEST") or None,
  "runner_kind": e.get("E_KIND"),
  "run_id": e.get("GITHUB_RUN_ID"),
  "sha": e.get("GITHUB_SHA"),
  "build_exit": num("E_BUILD"),
  "probe_exit": num("E_PROBE"),
  "iso": obj("E_ISO"),
  "boot_smoke": obj("E_BOOT"),
  "host": {"uname_m": platform.machine(), "kvm": e.get("E_KVM") == "true",
           "failed_stage": e.get("E_FAILED") or None},
}
if bad:
    doc["evidence_errors"] = bad
with open(sys.argv[1], "w") as f:
    f.write(json.dumps(doc, indent=2) + "\n")
print(json.dumps(doc, indent=2))
sys.exit(3 if bad else 0)
PYEOF
}
finish() {
  local rc=$1
  if ! write_evidence; then
    echo "awnix-arm64-lane: evidence write FAILED or a stage JSON did not parse" >&2
    [ "$rc" -eq 0 ] && rc=1
  fi
  exit "$rc"
}
# serial markers -> a JSON list with ANSI escapes and control bytes removed
markers_json() {
  "$PY" -c 'import json,re,sys; r=re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x1f\x7f]"); print(json.dumps(sorted({r.sub("", l).strip() for l in sys.stdin} - {""})))'
}

self_test() {
  local bad=0
  for l in awnix runner runner-ai garg; do
    chain_for "$l" >/dev/null || { echo "  [FAIL] no chain for $l"; bad=1; }
  done
  chain_for bogus >/dev/null 2>&1 && { echo "  [FAIL] unknown layer accepted"; bad=1; }
  [ "$(chain_for runner-ai | wc -w)" -eq 3 ] || { echo "  [FAIL] runner-ai chain is not 3 layers"; bad=1; }
  for cf in Containerfile.awnix Containerfile.awnix-runner Containerfile.awnix-runner-ai Containerfile.garg-appliance; do
    probe_cmd_for "$cf" | grep -q '_ARM64_OK' || { echo "  [FAIL] no probe for $cf"; bad=1; }
    probe_cmd_for "$cf" | grep -q 'uname -m)" = aarch64' || { echo "  [FAIL] $cf probe does not assert aarch64"; bad=1; }
  done
  case "$BIB" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo "  [FAIL] BIB not digest-pinned"; bad=1 ;; esac
  grep -nE 'podman +(system +(prune|migrate|reset)|(image|container|volume) +prune|push)' "${BASH_SOURCE[0]}" \
    | grep -v '^[0-9]*: *#' | grep -v 'grep -nE' >/dev/null && { echo "  [FAIL] a store-wide or push verb is present"; bad=1; }
  # a PASSING boot's colourised marker must round-trip into valid evidence
  local tmpd found; tmpd="$(mktemp -d)"
  found="$(printf '[  OK  ] Reached target \033[0;1;39mMulti-User System\033[0m.\nawnix login: \n' | grep -aoE 'Reached target.*Multi-User|login:' | markers_json)"
  if ( BOOT_JSON="{\"markers\": $found, \"verdict\": \"PASS\"}" EVIDENCE="$tmpd/e.json" write_evidence >/dev/null ) \
      && "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["boot_smoke"]["verdict"]=="PASS" and "Reached target Multi-User" in d["boot_smoke"]["markers"]' "$tmpd/e.json"; then :
  else echo "  [FAIL] an escape-coloured serial marker breaks the evidence writer"; bad=1; fi
  ( BOOT_JSON='{broken' EVIDENCE="$tmpd/f.json" write_evidence >/dev/null ) && { echo "  [FAIL] unparsable stage JSON did not fail the evidence write"; bad=1; }
  rm -rf "$tmpd"
  [ "$bad" -eq 0 ] && echo "self-test: PASS" || echo "self-test: FAIL"
  return "$bad"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --layer) LAYER="${2:-}"; shift 2 ;;
    --runner-kind) KIND="${2:-}"; shift 2 ;;
    --iso) DO_ISO=1; shift ;;
    --boot-smoke) DO_BOOT=1; shift ;;
    --evidence) EVIDENCE="${2:-}"; shift 2 ;;
    --self-test) self_test; exit $? ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die2 "unknown argument: $1" ;;
  esac
done

CHAIN="$(chain_for "$LAYER")" || die2 "unknown --layer '$LAYER' (awnix|runner|runner-ai|garg)"
case "$KIND" in hosted|spark) ;; *) die2 "unknown --runner-kind '$KIND'" ;; esac

# ── preflight: native aarch64 only ──────────────────────────────────────────────
M="$(uname -m)"
[ "$M" = aarch64 ] || { FAILED_STAGE=preflight; echo "REFUSING: this lane builds NATIVELY on aarch64 and this host is '$M' (no qemu-user cross builds: an emulated build proves nothing about the image)"; finish 2; }
[ -e /dev/kvm ] && KVM=true

if [ "$KIND" = spark ]; then
  SUDO=""
  [ -n "${CONTAINERS_STORAGE_CONF:-}" ] && [ -r "$CONTAINERS_STORAGE_CONF" ] || {
    FAILED_STAGE=preflight
    echo "REFUSING: on the Spark the build must use its OWN store; CONTAINERS_STORAGE_CONF='${CONTAINERS_STORAGE_CONF:-}' is unset or unreadable (see register-spark-arm64-runner.sh)"
    finish 2; }
  [ "$(id -u)" != 0 ] || { FAILED_STAGE=preflight; echo "REFUSING: the Spark lane builds rootless; this job is uid 0"; finish 2; }
  if [ "$DO_ISO" = 1 ] || [ "$DO_BOOT" = 1 ]; then
    FAILED_STAGE=preflight
    echo "REFUSING: --iso/--boot-smoke need rootful --privileged bootc-image-builder, which never runs on the inference box. Use runner=hosted."
    finish 2
  fi
  # MemAvailable floor (DGXM001): never build into the pool's headroom.
  AVAIL_KB="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
  [ "${AVAIL_KB:-0}" -ge $((24 * 1024 * 1024)) ] || { FAILED_STAGE=preflight; echo "REFUSING: MemAvailable $((${AVAIL_KB:-0} / 1024 / 1024))G < 24G floor; the inference pool comes first"; finish 2; }
else
  SUDO="sudo"
  command -v podman >/dev/null || { log "installing podman"; sudo apt-get update -qq && sudo apt-get install -y -qq podman >/dev/null; }
  # The hosted image keeps its free space on whichever mount is bigger; the build
  # and bootc-image-builder both use /var/lib/containers/storage, so that path is
  # bind-mounted onto the roomiest filesystem BEFORE podman first touches it.
  sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL 2>/dev/null || true
  best=/; best_kb=$(df -Pk / | awk 'NR==2 {print $4}')
  for m in /mnt /var/tmp; do
    [ -d "$m" ] || continue
    kb=$(df -Pk "$m" | awk 'NR==2 {print $4}')
    [ "${kb:-0}" -gt "$best_kb" ] && { best="$m"; best_kb="$kb"; }
  done
  if [ "$best" != / ] && ! mountpoint -q /var/lib/containers/storage; then
    sudo mkdir -p "$best/awnix-containers-storage" /var/lib/containers/storage
    sudo mount --bind "$best/awnix-containers-storage" /var/lib/containers/storage
  fi
  MEDIA_ROOT="$best"
  log "storage on $best ($((best_kb / 1024 / 1024))G free)"
fi
command -v podman >/dev/null || { FAILED_STAGE=preflight; echo "no podman"; finish 2; }
log "host: $(uname -srm); podman $($SUDO podman --version 2>&1 | awk '{print $NF}'); kvm=$KVM"
df -h / /mnt 2>/dev/null | sed 's/^/  /'

# ── build the chain, natively, one layer at a time ──────────────────────────────
cd "$HERE" || finish 2
BUILD_EXIT=0
TOP_TAG=""
for pair in $CHAIN; do
  tag="${pair%%|*}"; cf="${pair#*|}"
  log "build $cf -> $tag (linux/arm64)"
  $SUDO podman build --no-cache --network=host \
      --platform linux/arm64 --build-arg TARGETARCH=arm64 \
      -t "$tag" -f "$cf" . 2>&1 | tail -n 60
  rc=${PIPESTATUS[0]}
  echo "BUILD $cf rc=$rc"
  if [ "$rc" -ne 0 ]; then BUILD_EXIT=$rc; FAILED_STAGE="build:$cf"; break; fi
  TOP_TAG="$tag"
done
[ "$BUILD_EXIT" -eq 0 ] || finish 1

IMAGE_DIGEST="$($SUDO podman image inspect --format '{{.Digest}}' "$TOP_TAG" 2>/dev/null)"
[ -n "$IMAGE_DIGEST" ] || IMAGE_DIGEST="sha256:$($SUDO podman image inspect --format '{{.Id}}' "$TOP_TAG")"
IMG_ARCH="$($SUDO podman image inspect --format '{{.Architecture}}' "$TOP_TAG")"
log "top image $TOP_TAG digest=$IMAGE_DIGEST arch=$IMG_ARCH"

# ── probe every layer that was built ────────────────────────────────────────────
PROBE_EXIT=0
[ "$IMG_ARCH" = arm64 ] || { PROBE_EXIT=1; FAILED_STAGE="probe:image-arch=$IMG_ARCH"; }
for pair in $CHAIN; do
  [ "$PROBE_EXIT" -eq 0 ] || break
  tag="${pair%%|*}"; cf="${pair#*|}"
  cmd="$(probe_cmd_for "$cf")"
  out="$($SUDO podman run --rm --network=none -i "$tag" bash -s <<<"$cmd" 2>&1)"; rc=$?
  echo "$out" | tail -n 15 | sed 's/^/  /'
  if [ "$rc" -ne 0 ] || ! echo "$out" | grep -q '_ARM64_OK'; then
    PROBE_EXIT=$(( rc == 0 ? 1 : rc )); FAILED_STAGE="probe:$cf"
  fi
  echo "PROBE $cf rc=$rc"
done
[ "$PROBE_EXIT" -eq 0 ] || finish 1

# ── media (hosted only) ─────────────────────────────────────────────────────────
bib() {  # bib <type> <outdir> [config.toml]
  local type=$1 out=$2 cfg=${3:-}
  local extra=()
  [ -n "$cfg" ] && extra=(-v "$cfg":/config.toml:ro)
  sudo mkdir -p "$out"
  sudo podman run --rm --privileged --network=host \
    --security-opt label=type:unconfined_t \
    -v /var/lib/containers/storage:/var/lib/containers/storage \
    -v "$out":/output "${extra[@]}" \
    "$BIB" --type "$type" --local "$TOP_TAG" 2>&1 | tail -n 40
  return "${PIPESTATUS[0]}"
}
free_gb() { df -Pk "$MEDIA_ROOT" | awk 'NR==2 {print int($4/1024/1024)}'; }

RC_FINAL=0
if [ "$DO_ISO" = 1 ]; then
  FG="$(free_gb)"
  if [ "$FG" -lt "$ISO_MIN_FREE_GB" ]; then
    ISO_JSON="{\"built\": false, \"sha256\": null, \"size\": null, \"reason\": \"only ${FG}GB free, need ${ISO_MIN_FREE_GB}\"}"
    FAILED_STAGE="iso:disk"; RC_FINAL=1
  else
    OUT="$MEDIA_ROOT/awnix-arm64-iso"
    bib iso "$OUT"; rc=$?
    ISO="$(sudo find "$OUT" -type f -name '*.iso' | head -1)"
    if [ -n "$ISO" ] && sudo sh -c "dd if='$ISO' bs=1 skip=32769 count=5 2>/dev/null" | grep -q CD001; then
      SUM="$(sudo sha256sum "$ISO" | cut -d' ' -f1)"; SZ="$(sudo stat -c%s "$ISO")"
      # BUILT-UNMEASURED: only the ISO9660 magic is checked; the boot smoke boots a
      # qcow2 of the same image, never this ISO, and the ISO is not retained, so its
      # sha256 names bytes nobody can re-verify after the run.
      ISO_JSON="{\"built\": true, \"sha256\": \"$SUM\", \"size\": $SZ, \"bib_rc\": $rc, \"name\": \"awnix-aarch64.iso\", \"checked\": \"iso9660-magic-only\", \"boot_verified\": false, \"retained\": false}"
      log "ISO sha256=$SUM size=$SZ (bib rc=$rc)"
      sudo rm -rf "$OUT"   # not retained: too large for a hosted runner (see retained:false)
    else
      ISO_JSON="{\"built\": false, \"sha256\": null, \"size\": null, \"bib_rc\": $rc}"
      FAILED_STAGE="iso"; RC_FINAL=1
    fi
  fi
fi

if [ "$DO_BOOT" = 1 ]; then
  command -v qemu-system-aarch64 >/dev/null || sudo apt-get install -y -qq qemu-system-arm qemu-efi-aarch64 qemu-utils >/dev/null
  FW=""
  for f in /usr/share/qemu-efi-aarch64/QEMU_EFI.fd /usr/share/AAVMF/AAVMF_CODE.fd; do [ -r "$f" ] && { FW="$f"; break; }; done
  WORK="$(mktemp -d)"
  printf '[customizations.kernel]\nappend = "console=ttyAMA0,115200"\n' > "$WORK/config.toml"
  OUT="$MEDIA_ROOT/awnix-arm64-qcow2"
  bib qcow2 "$OUT" "$WORK/config.toml"; brc=$?
  DISK="$(sudo find "$OUT" -type f -name '*.qcow2' | head -1)"
  MARKERS='Reached target.*Multi-User|login:'
  if [ -z "$FW" ] || [ -z "$DISK" ]; then
    BOOT_JSON="{\"markers\": [], \"verdict\": \"UNJUDGED\", \"source\": \"qcow2\", \"reason\": \"firmware='$FW' disk='$DISK' bib_rc=$brc\"}"
    FAILED_STAGE="boot_smoke:setup"; RC_FINAL=1
  else
    sudo cp "$DISK" "$WORK/disk.qcow2"; sudo chown "$(id -u)" "$WORK/disk.qcow2"
    ACCEL="-accel tcg -cpu max"; [ "$KVM" = true ] && ACCEL="-accel kvm -cpu host"
    SER="$WORK/serial.log"; : > "$SER"
    # -nic none: nothing listens and nothing leaves (the zero-open-ports seam).
    # shellcheck disable=SC2086
    timeout "${SMOKE_TIMEOUT_MIN}m" qemu-system-aarch64 -machine virt $ACCEL -smp 4 -m 4096 \
      -bios "$FW" -drive file="$WORK/disk.qcow2",if=virtio,format=qcow2 \
      -nic none -display none -serial file:"$SER" -monitor none &
    QPID=$!
    verdict=TIMEOUT
    for _ in $(seq 1 $((SMOKE_TIMEOUT_MIN * 6))); do
      if grep -qa 'login:' "$SER"; then verdict=PASS; break; fi
      if grep -qaE 'Kernel panic|emergency mode' "$SER"; then verdict=FAIL; break; fi
      kill -0 "$QPID" 2>/dev/null || break
      sleep 10
    done
    kill "$QPID" 2>/dev/null; wait "$QPID" 2>/dev/null
    FOUND="$(grep -aoE "$MARKERS" "$SER" | markers_json)"
    BOOT_JSON="{\"markers\": ${FOUND:-[]}, \"verdict\": \"$verdict\", \"source\": \"qcow2\", \"accel\": \"$( [ "$KVM" = true ] && echo kvm || echo tcg )\"}"
    tail -n 30 "$SER" | sed 's/^/  serial: /'
    cp "$SER" "${EVIDENCE%/*}/arm64-serial.log" 2>/dev/null || true
    [ "$verdict" = PASS ] || { FAILED_STAGE="boot_smoke:$verdict"; RC_FINAL=1; }
  fi
  sudo rm -rf "$OUT" "$WORK"
fi

finish "$RC_FINAL"
