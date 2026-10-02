#!/bin/sh
# spark-node-run.sh up|status|down [SHA7] -- the awnix arm64 image as a systemd NODE
# container on the DGX Spark. Rootless podman, CPU only, no published port.
#
#   up SHA7   start localhost/awnix:arm64-SHA7 as awnix-node-arm64 with systemd as PID 1,
#             wait for `systemctl is-system-running --wait` (running or degraded = booted),
#             record the container's PortBindings/NetworkMode so SPK002 can judge it
#   status    one evidence line: state, systemd verdict, port bindings
#   down      podman rm -f the node (and --image: the image too). Fully reversible.
#
# The AFRL seam: ZERO published ports. There is no -p/--publish and no --network=host here,
# and check_spark_awnix_node.py SPK002 fails the evidence if PortBindings is ever non-empty.
# No CDI/--device nvidia either: the GB10's memory is unified and the production pool owns
# it; this node is a CPU node by contract (--memory 6g --cpus 4).
# Exit 0 ok; 1 failed to boot / bound a port; 2 could not judge (no podman, no image).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/spark-lib.sh"
VERB=${1:-status}
SHA7=${2:-}
TAG="localhost/awnix:arm64-${SHA7}"
command -v podman >/dev/null 2>&1 || { ev_write "node-$VERB" 2 "command -v podman" "podman absent"; exit 2; }

record_ports() {
  _pb=$(podman inspect "$NODE_NAME" --format '{{json .HostConfig.PortBindings}}|{{.HostConfig.NetworkMode}}' 2>/dev/null)
  _rc=$?
  ev_write node-inspect "$_rc" "podman inspect $NODE_NAME --format '{{json .HostConfig.PortBindings}}|{{.HostConfig.NetworkMode}}'" "PortBindings=${_pb%%|*} NetworkMode=${_pb#*|}"
  # A failed inspect (or an empty answer) is COULD-NOT-JUDGE, never "no ports" (SPK002).
  if [ "$_rc" -ne 0 ] || [ -z "${_pb%%|*}" ]; then
    ev_write node-ports 2 "podman inspect PortBindings" "inspect rc=$_rc; ports not judged"; return 2
  fi
  case "${_pb%%|*}" in
    '{}'|'null') return 0 ;;
    *) ev_write node-ports 1 "podman inspect PortBindings" "published ports present: ${_pb%%|*}"; return 1 ;;
  esac
}

case "$VERB" in
  up)
    [ -n "$SHA7" ] || { echo "usage: $0 up SHA7" >&2; exit 2; }
    podman image exists "$TAG" || { ev_write node-up 2 "podman image exists $TAG" "image not built"; exit 2; }
    gate_memory || exit 1
    podman rm -f "$NODE_NAME" >/dev/null 2>&1
    # tailscale runs in userspace-networking mode inside the node, so no /dev/net/tun and
    # no NET_ADMIN are needed; the node reaches out through rootless slirp/pasta only.
    ev node-up podman run -d --name "$NODE_NAME" --hostname "$NODE_NAME" \
      --systemd=always --memory 6g --cpus 4 --pids-limit 4096 \
      --label awnix.lane=spark-awnix-node "$TAG" /sbin/init || exit 1
    EV_CMD="podman exec $NODE_NAME timeout 180 systemctl is-system-running --wait" \
      ev_sh node-systemd "v=\$(podman exec $NODE_NAME timeout 180 systemctl is-system-running --wait); echo \"\$v\"; [ \"\$v\" = running ] || [ \"\$v\" = degraded ]"
    SRC=$?
    ev node-failed-units podman exec "$NODE_NAME" systemctl --failed --no-legend --plain
    record_ports; PRC=$?
    [ "$PRC" -eq 0 ] || exit "$PRC"
    exit "$SRC"
    ;;
  status)
    ev node-status podman ps -a --filter "name=^${NODE_NAME}\$" --format '{{.Names}} {{.Status}} {{.Image}}'
    record_ports
    ;;
  down)
    ev node-down podman rm -f "$NODE_NAME"
    if [ "${3:-}" = --image ] && [ -n "$SHA7" ]; then ev node-rmi podman rmi -f "$TAG"; fi
    ;;
  *) echo "usage: $0 up SHA7 | status | down [SHA7 --image]" >&2; exit 2 ;;
esac
