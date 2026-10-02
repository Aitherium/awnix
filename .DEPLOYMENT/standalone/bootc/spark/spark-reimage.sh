#!/bin/sh
# spark-reimage.sh [--plan|--preflight|--apply] -- DGX Spark bare metal: DGX OS -> awnix.
#
# OWNER-ONLY. This is a B2 action (irreversible, takes production inference down, the
# rollback is NVIDIA's recovery media). No agent runs --apply: it refuses when any agent
# marker is in the environment, and it needs AWNIX_REIMAGE_CONFIRM set to the box's own
# DMI serial, which the owner reads off the machine. Read REIMAGE-PLAN.md first.
#
#   --plan       (default) print the ordered steps and the exact commands. Changes nothing.
#   --preflight  read-only checks on the Spark: arch, serial, image present and arm64, free
#                disk, what is serving right now (the downtime you are about to cause).
#   --apply      run `bootc install to-existing-root` from the awnix arm64 image. Guarded.
#
# Exit 0 ok (plan printed / preflight clean / install finished), 1 refused or preflight
# found a blocker, 2 could not judge (not aarch64, no podman, no image).
set -u
MODE=${1:---plan}
IMAGE=${AWNIX_REIMAGE_IMAGE:-localhost/awnix:arm64}

plan() {
  cat <<'EOF'
DGX Spark reimage plan (OWNER decision; see REIMAGE-PLAN.md)
  0. Decide: the production pool (gemma4-12b :8124, code-embed :8229, ltx-video, the mesh
     agent, secrets replica, dns replica) is DOWN from step 4 until the GB10 driver works
     on awnix, and possibly for good (REIMAGE-PLAN.md "The driver risk").
  1. Back up what is not reproducible: ~/models (weights), ~/aitheros*, docker volumes,
     /etc/tailscale state. `awnix backup` does not exist on DGX OS; use rsync to the NAS.
  2. Prove the image as a container first (spark_awnix_node.py build + run): the node must
     reach `systemctl is-system-running` = running/degraded. Done in the container lane.
  3. Download the NVIDIA DGX Spark recovery image to a USB stick and CHECK IT BOOTS.
     That stick is the only rollback.
  4. sh spark-reimage.sh --preflight        (read-only; prints the serial)
  5. AWNIX_REIMAGE_CONFIRM=<serial> sh spark-reimage.sh --apply
       = podman image scp IMAGE root@localhost::   (rootless store -> root store), then
       = sudo podman run --rm --privileged --pid=host --security-opt label=type:unconfined_t \
           -v /dev:/dev -v /var/lib/containers:/var/lib/containers -v /:/target \
           IMAGE bootc install to-existing-root --acknowledge-destructive
  6. Reboot. First boot must print the awnix greenboot verdict on the console.
  7. On awnix: nvidia driver for GB10 (REIMAGE-PLAN.md), then re-enrol the mesh
     (`awnix mesh join`), then restore the pool with `awmodels use <posture>`.
  Rollback: boot the step-3 USB and reinstall DGX OS; restore from the step-1 backup.
EOF
}

is_agent() {
  # Any of these means a coding agent / automation is driving this shell.
  for v in CLAUDECODE CLAUDE_CODE_ENTRYPOINT AITHER_AGENT AWSH_SESSION CODEX_SANDBOX GITHUB_ACTIONS; do
    eval "[ -n \"\${$v:-}\" ]" && { echo "$v"; return 0; }
  done
  return 1
}

serial() { cat /sys/class/dmi/id/product_serial 2>/dev/null || sudo -n cat /sys/class/dmi/id/product_serial 2>/dev/null; }

preflight() {
  bad=0
  [ "$(uname -m)" = aarch64 ] || { echo "not aarch64"; return 2; }
  command -v podman >/dev/null 2>&1 || { echo "podman absent (owner precondition)"; return 2; }
  echo "serial: $(serial || echo unreadable)"
  a=$(podman image inspect "$IMAGE" --format '{{.Architecture}}' 2>/dev/null) || { echo "image $IMAGE absent"; return 2; }
  [ "$a" = arm64 ] || { echo "image arch $a != arm64"; bad=1; }
  echo "image: $IMAGE arch=$a"
  echo "free on /: $(df -Pk / | awk 'NR==2{print int($4/1048576)}') GiB"
  echo "serving now (goes DOWN at --apply):"
  docker ps --format '  {{.Names}} {{.Status}}' 2>/dev/null
  nvidia-smi --query-compute-apps=process_name,used_memory --format=csv,noheader 2>/dev/null | sed 's/^/  gpu: /'
  return $bad
}

case "$MODE" in
  --plan) plan ;;
  --preflight) preflight; exit $? ;;
  --apply)
    if who=$(is_agent); then
      echo "REFUSED: agent marker $who is set. The reimage is an owner action (B2)." >&2
      exit 1
    fi
    want=$(serial)
    if [ -z "${AWNIX_REIMAGE_CONFIRM:-}" ] || [ -z "$want" ] || [ "$AWNIX_REIMAGE_CONFIRM" != "$want" ]; then
      echo "REFUSED: set AWNIX_REIMAGE_CONFIRM to this machine's serial (spark-reimage.sh --preflight prints it)." >&2
      exit 1
    fi
    preflight || { echo "REFUSED: preflight failed" >&2; exit 1; }
    # The image was built rootless (the build user's store); bootc install runs as root.
    podman image scp "$IMAGE" root@localhost:: || { echo "image copy to root store failed" >&2; exit 1; }
    exec sudo podman run --rm --privileged --pid=host --security-opt label=type:unconfined_t \
      -v /dev:/dev -v /var/lib/containers:/var/lib/containers -v /:/target \
      "$IMAGE" bootc install to-existing-root --acknowledge-destructive
    ;;
  *) echo "usage: $0 [--plan|--preflight|--apply]" >&2; exit 2 ;;
esac
