#!/bin/sh
# spark-node-join.sh join PAK_FILE | leave | status -- join the awnix node container to
# AitherMesh (the headscale control plane, hs.aitherium.com) with the real tailscale flow.
#
#   join PAK_FILE  PAK_FILE is a 0600 file holding a ONE-USE, 1h pre-auth key the host
#                  minted (spark_awnix_node.py join does that). The key is copied into the
#                  node at /run/awnix-mesh/pak (tmpfs, 0600), handed to tailscale as
#                  --auth-key=file:..., and both copies are deleted right after. It is never
#                  on an argv, in the environment or in the evidence file.
#   leave          tailscale logout inside the node (the host then deletes the headscale row)
#   status         tailscale status/ip inside the node
#
# tailscaled runs as a transient SYSTEMD unit inside the node (systemd-run), which is also
# the proof the node's systemd is real, in userspace-networking mode: no /dev/net/tun, no
# NET_ADMIN, and no listening port on the Spark.
#
# The tailscale binaries are the Spark host's own (static Go builds, same version as the host
# daemon), copied in with podman cp: no download, reproducible, removable with the node.
# Exit 0 ok; 1 join failed; 2 could not judge (node not running, no tailscale on host).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/spark-lib.sh"
VERB=${1:-status}
LOGIN_SERVER=${LOGIN_SERVER:-https://hs.aitherium.com}
MESH_HOSTNAME=${MESH_HOSTNAME:-awnix-spark-arm64}
TS_SOCK=/run/tailscale/tailscaled.sock
X="podman exec $NODE_NAME"

[ "$(podman inspect -f '{{.State.Running}}' "$NODE_NAME" 2>/dev/null)" = true ] \
  || { ev_write "mesh-$VERB" 2 "podman inspect -f {{.State.Running}} $NODE_NAME" "node not running"; exit 2; }

case "$VERB" in
  join)
    PAK=${2:-}
    [ -n "$PAK" ] && [ -f "$PAK" ] || { echo "usage: $0 join PAK_FILE" >&2; exit 2; }
    TS=$(command -v tailscale); TSD=$(command -v tailscaled || echo /usr/sbin/tailscaled)
    [ -x "$TS" ] && [ -x "$TSD" ] || { ev_write mesh-join 2 "command -v tailscale tailscaled" "no tailscale on host"; exit 2; }
    ev mesh-install-ts sh -c "podman cp '$TS' $NODE_NAME:/usr/local/bin/tailscale && podman cp '$TSD' $NODE_NAME:/usr/local/bin/tailscaled"
    ev mesh-tailscaled $X systemd-run --unit=awnix-tailscaled --property=StateDirectory=tailscale \
      --property=RuntimeDirectory=tailscale \
      /usr/local/bin/tailscaled --tun=userspace-networking \
      --state=/var/lib/tailscale/tailscaled.state --socket=$TS_SOCK || exit 1
    ev mesh-tailscaled-active $X sh -c 'for i in $(seq 1 30); do systemctl is-active --quiet awnix-tailscaled && exit 0; sleep 1; done; exit 1' || exit 1
    # The key file: host 0600 -> node tmpfs 0600 -> deleted. `sh -c` redirects keep it off argv.
    # -i is load-bearing: without it podman closes stdin and cat writes an EMPTY key file.
    podman exec -i "$NODE_NAME" sh -c 'umask 077; mkdir -p /run/awnix-mesh; cat > /run/awnix-mesh/pak' < "$PAK"
    CRC=$?
    rm -f "$PAK"
    # A zero-byte key is a failed stage, not a join attempt: test -s inside the node.
    if [ "$CRC" -eq 0 ]; then $X test -s /run/awnix-mesh/pak || CRC=1; fi
    ev_write mesh-pak-staged "$CRC" "podman exec -i $NODE_NAME sh -c 'cat > /run/awnix-mesh/pak' < <file:0600> && podman exec $NODE_NAME test -s /run/awnix-mesh/pak" "host copy removed"
    [ "$CRC" -eq 0 ] || { $X rm -f /run/awnix-mesh/pak; exit 1; }
    EV_CMD="tailscale --socket=$TS_SOCK up --login-server=$LOGIN_SERVER --auth-key=file:/run/awnix-mesh/pak --hostname=$MESH_HOSTNAME --accept-dns=false --timeout=90s" \
      ev mesh-up $X /usr/local/bin/tailscale --socket=$TS_SOCK up --login-server="$LOGIN_SERVER" \
        --auth-key=file:/run/awnix-mesh/pak --hostname="$MESH_HOSTNAME" --accept-dns=false --timeout=90s
    URC=$?
    $X rm -f /run/awnix-mesh/pak
    ev_write mesh-pak-removed $? "podman exec $NODE_NAME rm -f /run/awnix-mesh/pak" ""
    [ "$URC" -eq 0 ] || exit 1
    IP=$($X /usr/local/bin/tailscale --socket=$TS_SOCK ip -4 2>/dev/null)
    ev_write mesh-ip "$([ -n "$IP" ] && echo 0 || echo 1)" "podman exec $NODE_NAME tailscale ip -4" "ip=$IP"
    [ -n "$IP" ] || exit 1
    ;;
  leave)
    ev mesh-leave $X /usr/local/bin/tailscale --socket=$TS_SOCK logout
    ev mesh-tailscaled-stop $X systemctl stop awnix-tailscaled
    ;;
  status)
    ev mesh-status $X /usr/local/bin/tailscale --socket=$TS_SOCK status --self --peers=false
    ;;
  *) echo "usage: $0 join PAK_FILE | leave | status" >&2; exit 2 ;;
esac
