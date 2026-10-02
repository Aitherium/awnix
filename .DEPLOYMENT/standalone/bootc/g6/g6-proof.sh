#!/usr/bin/env bash
# g6-proof.sh -- credential-free proof of a real bootc upgrade, a manual rollback and a
# greenboot health-gated automatic fallback, in one KVM guest (AFRL proof plan G6,
# demo Steps 4-5).
#
#   1. build N, N+1, N+2 from Containerfile.g6 (public centos-bootc, or --base IMG)
#   2. serve them from a LOCAL registry:2 bound to 127.0.0.1:5000 -- no auth.json
#   3. `bootc install to-disk --via-loopback` N, tracking 10.0.2.2:5000/g6/awnix:stable
#   4. boot it under qemu+KVM (-no-reboot: one launch per boot) until the in-guest
#      agent prints G6-END; retag :stable whenever the agent asks (G6-WANT)
#   5. g6_verdict.py turns the serial log into verdict.json
#
# Usage (root, on a Linux host with podman, skopeo, qemu-system-x86_64, python3):
#   g6-proof.sh [--base IMG] [--registry 10.0.2.2:5000] [--work DIR]
#               [--only upgrade|rollback|greenboot] [--mode vm|container]
#               [--inject none|no-greenboot] [--max-launches N]
#
# Exit: 0 PASS · 1 FAIL (UPGRADE-FAILED, ROLLBACK-FAILED, NO-AUTO-FALLBACK, STATE-LOST)
#       2 UNJUDGED or CONTAINER-ONLY. The last stdout line is `G6-VERDICT: <V>`.
#
# Never run on a workstation that matters: it builds three OS images and boots a VM
# up to a dozen times. It is meant for .github/workflows/awnix-g6-proof.yml (hosted
# ubuntu runner with /dev/kvm) or a disposable metal box.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CTX=$(cd "$HERE/.." && pwd)             # .DEPLOYMENT/standalone/bootc
BASE=quay.io/centos-bootc/centos-bootc:stream9
GUEST_REG=10.0.2.2:5000
HOST_PORT=5000
WORK=/var/tmp/awnix-g6
ONLY=all
MODE=vm
INJECT=none
MAX_LAUNCHES=16
LAUNCH_TIMEOUT=900
REG_IMAGE=docker.io/library/registry:2
REG_NAME=g6-registry

die2() { echo "G6: $*"; echo "G6-VERDICT: UNJUDGED"; exit 2; }
usage() { sed -n '2,27p' "$0"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --registry) GUEST_REG="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        --only) ONLY="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --inject) INJECT="$2"; shift 2 ;;
        --max-launches) MAX_LAUNCHES="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die2 "unknown argument: $1" ;;
    esac
done

case "$ONLY" in
    all) PLAN=upgrade,rollback,fallback ;;
    upgrade) PLAN=upgrade ;;
    rollback) PLAN=upgrade,rollback ;;      # something must be installed before it can be rolled back
    greenboot) PLAN=fallback ;;
    *) die2 "--only must be upgrade, rollback or greenboot" ;;
esac
case "$MODE" in vm|container) ;; *) die2 "--mode must be vm or container" ;; esac
case "$INJECT" in none|no-greenboot) ;; *) die2 "--inject must be none or no-greenboot" ;; esac
case "$GUEST_REG" in *:"$HOST_PORT") ;; *) die2 "--registry must end in :$HOST_PORT (the host port the local registry binds)" ;; esac

[ "$(id -u)" -eq 0 ] || die2 "run as root (bootc install and loop devices need it)"
for t in podman python3; do command -v "$t" >/dev/null 2>&1 || die2 "missing tool: $t"; done

KVM=false
if [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then KVM=true; fi
if [ "$MODE" = vm ] && [ "$KVM" != true ]; then
    echo "G6: /dev/kvm is not usable here -- falling back to CONTAINER-ONLY (never MEASURED)"
    MODE=container
fi
if [ "$MODE" = vm ]; then
    command -v qemu-system-x86_64 >/dev/null 2>&1 || die2 "missing tool: qemu-system-x86_64"
fi

mkdir -p "$WORK"
SERIAL="$WORK/serial.log"
DIG="$WORK/digests.json"
: > "$WORK/harness.log"
log() { echo "$*" | tee -a "$WORK/harness.log"; }
STARTED=$(date -u +%FT%TZ)

cleanup() { podman rm -f "$REG_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ── 1. build ─────────────────────────────────────────────────────────────────────────
for role in n n1 n2; do
    inj="$INJECT"   # the negative control must disarm the image that STAGES n2 (N), not only n2
    log "== build $role (base $BASE, plan $PLAN, inject $inj)"
    podman build --pull=missing -f "$HERE/Containerfile.g6" \
        --build-arg BASE="$BASE" --build-arg G6_PLAN="$PLAN" \
        --build-arg G6_ROLE="$role" --build-arg G6_INJECT="$inj" \
        -t "localhost/g6/awnix:$role" "$CTX" >> "$WORK/build-$role.log" 2>&1 \
        || { tail -20 "$WORK/build-$role.log"; die2 "build of $role failed"; }
done

# ── 2. local registry: 127.0.0.1 ONLY ─────────────────────────────────────────────────
log "== registry on 127.0.0.1:$HOST_PORT"
podman rm -f "$REG_NAME" >/dev/null 2>&1 || true
podman run -d --name "$REG_NAME" -p "127.0.0.1:$HOST_PORT:5000" "$REG_IMAGE" >/dev/null \
    || die2 "could not start the local registry"
for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$HOST_PORT/v2/" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "http://127.0.0.1:$HOST_PORT/v2/" >/dev/null 2>&1 || die2 "local registry never answered"

HREG="127.0.0.1:$HOST_PORT/g6/awnix"
declare -A D
for role in n n1 n2; do
    podman push --tls-verify=false --digestfile "$WORK/digest-$role" \
        "localhost/g6/awnix:$role" "docker://$HREG:$role" >> "$WORK/push.log" 2>&1 \
        || die2 "push of $role failed"
    D[$role]=$(cat "$WORK/digest-$role")
    log "   $role -> ${D[$role]}"
done

retag() {   # point :stable at a role's manifest, digest preserved
    if command -v skopeo >/dev/null 2>&1; then
        skopeo copy --preserve-digests --src-tls-verify=false --dest-tls-verify=false \
            "docker://$HREG:$1" "docker://$HREG:stable" >> "$WORK/push.log" 2>&1
    else
        podman push --tls-verify=false "localhost/g6/awnix:$1" "docker://$HREG:stable" >> "$WORK/push.log" 2>&1
    fi
}
retag n || die2 "could not tag :stable"
STABLE=n

GB_MAX=$(podman run --rm "localhost/g6/awnix:n" sh -c \
    "sed -n 's/^GREENBOOT_MAX_BOOT_ATTEMPTS=\([0-9]*\).*/\1/p' /etc/greenboot/greenboot.conf | tail -1" 2>/dev/null)
GRUB_COUNTING=$(podman run --rm "localhost/g6/awnix:n" cat /usr/lib/g6/grub_counting 2>/dev/null)

write_digests() {
    python3 - "$DIG" <<PY
import json, sys
json.dump({
  "schema": 1, "n_registry": "${D[n]}", "n1": "${D[n1]}", "n2": "${D[n2]}",
  "base_image": "$BASE", "kvm": "$KVM" == "true", "mode": "$MODE", "inject": "$INJECT",
  "plan": "$PLAN", "greenboot_max": int("${GB_MAX:-3}" or 3), "grub_counting": "${GRUB_COUNTING:-unknown}",
  "runner": "${RUNNER_NAME:-$(hostname)} (${RUNNER_ENVIRONMENT:-local})",
  "gh_run_id": "${GITHUB_RUN_ID:-}" or None,
  "started_at": "$STARTED", "ended_at": "$(date -u +%FT%TZ)",
}, open(sys.argv[1], "w"), indent=2)
PY
}

if [ "$MODE" = container ]; then
    for role in n n1 n2; do
        podman run --rm "localhost/g6/awnix:$role" bootc container lint >> "$WORK/lint.log" 2>&1 \
            && log "   lint $role ok" || log "   lint $role FAILED (see lint.log)"
    done
    write_digests
    python3 "$HERE/g6_verdict.py" --container-only --digests "$DIG" --out "$WORK/verdict.json"
    exit $?
fi

# ── 3. install N to a raw disk ─────────────────────────────────────────────────────
DISK="$WORK/disk.raw"
rm -f "$DISK"; truncate -s 20G "$DISK"
FSARG=""
podman run --rm "localhost/g6/awnix:n" sh -c 'cat /usr/lib/bootc/install/*.toml 2>/dev/null' \
    | grep -qE '^[[:space:]]*type[[:space:]]*=' || FSARG="--filesystem xfs"
log "== bootc install to-disk (target $GUEST_REG/g6/awnix:stable) $FSARG"
# shellcheck disable=SC2086
podman run --rm --privileged --pid=host --security-opt label=type:unconfined_t \
    -v /var/lib/containers:/var/lib/containers -v /dev:/dev -v "$WORK":/output \
    "localhost/g6/awnix:n" \
    bootc install to-disk --via-loopback --generic-image --skip-fetch-check $FSARG \
        --target-imgref "$GUEST_REG/g6/awnix:stable" /output/disk.raw \
    > "$WORK/install.log" 2>&1 \
    || { tail -30 "$WORK/install.log"; die2 "bootc install to-disk failed"; }

# Record whether the INSTALLED grub.cfg can count boots (diagnostic; the verdict
# decides on what booted, not on this).
LOOP=$(losetup -Pf --show "$DISK" 2>/dev/null) && {
    mkdir -p "$WORK/mnt"
    for p in "${LOOP}"p*; do
        mount -o ro "$p" "$WORK/mnt" 2>/dev/null || continue
        if [ -f "$WORK/mnt/grub2/grub.cfg" ]; then
            if grep -rqs boot_counter "$WORK/mnt/grub2"; then log "   installed grub: boot_counter logic PRESENT"
            else log "   installed grub: boot_counter logic ABSENT"; fi
        fi
        umount "$WORK/mnt"
    done
    losetup -d "$LOOP"
}

# ── 4. boot until the agent closes the run ────────────────────────────────────────
: > "$SERIAL"
for launch in $(seq 1 "$MAX_LAUNCHES"); do
    log "== launch $launch (:stable -> $STABLE)"
    timeout "$LAUNCH_TIMEOUT" qemu-system-x86_64 -name g6-proof \
        -machine q35,accel=kvm -cpu host -smp 2 -m 3072 \
        -drive file="$DISK",format=raw,if=virtio,cache=unsafe \
        -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
        -chardev file,id=s0,path="$SERIAL",append=on -serial chardev:s0 \
        -display none -monitor none -no-reboot >> "$WORK/qemu.log" 2>&1
    log "   launch $launch exited rc=$?"
    grep -q "G6-END" "$SERIAL" && break
    want=$(grep -ao 'G6-WANT: stable=[a-z0-9]*' "$SERIAL" | tail -1 | sed 's/.*stable=//')
    if [ -n "$want" ] && [ "$want" != "$STABLE" ]; then
        retag "$want" || die2 "could not retag :stable -> $want"
        STABLE=$want
        log "   :stable -> $want (${D[$want]})"
    fi
done

# The guest's own copy of the agent log, in case getty ate the serial tail.
LOOP=$(losetup -Pf --show "$DISK" 2>/dev/null) && {
    for p in "${LOOP}"p*; do
        mount -o ro "$p" "$WORK/mnt" 2>/dev/null || mount -o ro,norecovery "$p" "$WORK/mnt" 2>/dev/null || continue
        f=$(ls "$WORK"/mnt/ostree/deploy/*/var/lib/awnix-g6/agent.log 2>/dev/null | head -1)
        [ -n "$f" ] && cp "$f" "$WORK/guest-agent.log"
        umount "$WORK/mnt"
    done
    losetup -d "$LOOP"
}

write_digests
log "== verdict"
python3 "$HERE/g6_verdict.py" --serial "$SERIAL" --guest-log "$WORK/guest-agent.log" \
    --digests "$DIG" --out "$WORK/verdict.json"
exit $?
