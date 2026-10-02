#!/usr/bin/env bash
# rollback-drill.sh -- prove greenboot rolls a failing awnix update back, in a real VM.
#
#   N    = the image under test + a oneshot drill unit + an oci-archive of N+1
#   N+1  = N plus a required greenboot check that always fails
#
# First boot of N: `bootc switch` to N+1, reboot. N+1 fails its health check on every
# boot; greenboot must count the failures and fall back to N. The drill unit logs every
# boot to the serial console and powers off with DRILL-RESULT: ROLLED-BACK, or with
# DRILL-RESULT: NO-ROLLBACK after too many boots on N+1.
#
# Run as root on a podman host with /dev/kvm (the hosted awnix-update-proof lane runs it
# with sudo on ubuntu-latest):
#   bash rollback-drill.sh [base-image]      # default localhost/awnix-greenboot-proof:latest
#   bash rollback-drill.sh ghcr.io/aitherium/awnix:beta   # a registry ref is pulled if absent
# Env (defaults unchanged): AWNIX_DRILL_WORK, AWNIX_DRILL_STORAGE (the containers storage
# BIB reads), AWNIX_DRILL_BIB (the bootc-image-builder image).
# Exit: 0 rolled back · 1 did not roll back · 2 could not judge (build/boot failed).
set -uo pipefail

BASE="${1:-localhost/awnix-greenboot-proof:latest}"
WORK="${AWNIX_DRILL_WORK:-/var/tmp/awnix-rollback-drill}"
STORAGE="${AWNIX_DRILL_STORAGE:-/var/lib/containers/storage}"
BIB="${AWNIX_DRILL_BIB:-quay.io/centos-bootc/bootc-image-builder:latest}"
SERIAL="$WORK/serial.log"
rm -rf "$WORK"; mkdir -p "$WORK/n1" "$WORK/n" "$WORK/out"

if ! podman image exists "$BASE"; then
  case "$BASE" in
    localhost/*) echo "DRILL-DEAD: base image $BASE not present"; exit 2 ;;
    *) podman pull "$BASE" >/dev/null || { echo "DRILL-DEAD: could not pull $BASE"; exit 2; } ;;
  esac
fi

# ── N+1: a health check that always fails ──────────────────────────────────────────
cat > "$WORK/n1/Containerfile" <<EOF
FROM $BASE
RUN printf '#!/bin/bash\necho "drill: this image is broken on purpose"\nexit 1\n' \
      > /etc/greenboot/check/required.d/99-drill-fail.sh \
    && chmod 0755 /etc/greenboot/check/required.d/99-drill-fail.sh
COPY n1-mark.service /usr/lib/systemd/system/awnix-drill-n1-mark.service
RUN systemctl enable awnix-drill-n1-mark.service
LABEL awnix.drill=n1
EOF
# N+1 announces itself BEFORE greenboot can reboot it: the only proof it ever booted.
cat > "$WORK/n1/n1-mark.service" <<'UNIT'
[Unit]
Description=awnix drill: this boot is N+1
DefaultDependencies=no
After=local-fs.target
Before=greenboot-healthcheck.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'mkdir -p /var/lib/awnix-drill; date >> /var/lib/awnix-drill/n1_seen; echo "DRILL: N+1 BOOTED" > /dev/ttyS0'
[Install]
WantedBy=sysinit.target
UNIT
podman build -q -t localhost/awnix-drill-n1:latest "$WORK/n1" >/dev/null \
  || { echo "DRILL-DEAD: N+1 build failed"; exit 2; }
podman save --format oci-archive -o "$WORK/n/n1.tar" localhost/awnix-drill-n1:latest \
  || { echo "DRILL-DEAD: could not save N+1"; exit 2; }

# ── N: the drill unit + N+1 on board ───────────────────────────────────────────────
cat > "$WORK/n/drill.sh" <<'EOF'
#!/bin/bash
M=/var/lib/awnix-drill; mkdir -p "$M"
[ -f "$M/log" ] && { echo '--- DRILL-REPLAY begin'; sed 's/^/REPLAY: /' "$M/log"; echo '--- DRILL-REPLAY end'; } > /dev/ttyS0 2>&1
exec > >(tee -a "$M/log" > /dev/ttyS0) 2>&1
img=$(bootc status --format=json 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin); b=(d.get("status") or {}).get("booted") or {}
i=((b.get("image") or {}).get("image") or {}); print(i.get("transport","?"), i.get("image","?"))' 2>/dev/null)
n=$(( $(cat "$M/boots" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$M/boots"
echo "DRILL: boot $n booted=[$img]"
if [ ! -f "$M/switched" ]; then
  touch "$M/switched"
  echo "DRILL: on N, switching to the failing image"
  bootc switch --transport oci-archive /usr/share/awnix-drill/n1.tar; rc=$?
  sync
  echo "DRILL: switch rc=$rc"; bootc status 2>&1 | head -30
  [ "$rc" = 0 ] && { systemctl reboot; exit 0; }
  echo "DRILL-RESULT: SWITCH-FAILED"; systemctl poweroff; exit 0
fi
case "$img" in
  oci-archive*) echo "DRILL: on N+1 (expected to fail health checks)"
                [ "$n" -ge 8 ] && { echo "DRILL-RESULT: NO-ROLLBACK"; systemctl poweroff; } ;;
  *)            if [ -s "$M/n1_seen" ]; then
                  echo "DRILL-RESULT: ROLLED-BACK after $n boots ($(wc -l < "$M/n1_seen") N+1 boot(s) seen)"
                else
                  echo "DRILL-RESULT: N1-NEVER-BOOTED"
                fi
                bootc status 2>&1 | head -30; systemctl poweroff ;;
esac
EOF
cat > "$WORK/n/awnix-drill.service" <<'EOF'
[Unit]
Description=awnix rollback drill
After=greenboot-healthcheck.service multi-user.target
Wants=greenboot-healthcheck.service
[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=/usr/libexec/awnix-drill.sh
[Install]
WantedBy=multi-user.target
EOF
# awnix-setup.service is the INTERACTIVE first-boot prompt on tty1; headless it waits
# forever and multi-user.target (which the drill unit orders after) is never reached.
cat > "$WORK/n/Containerfile" <<EOF
FROM $BASE
COPY n1.tar /usr/share/awnix-drill/n1.tar
COPY drill.sh /usr/libexec/awnix-drill.sh
COPY awnix-drill.service /usr/lib/systemd/system/awnix-drill.service
RUN chmod 0755 /usr/libexec/awnix-drill.sh && systemctl enable awnix-drill.service \
    && (systemctl mask awnix-setup.service || true) \
    && mkdir -p /usr/lib/bootc/kargs.d \
    && printf 'kargs = ["console=ttyS0,115200n8"]\n' > /usr/lib/bootc/kargs.d/10-serial.toml \
    && mkdir -p /usr/lib/bootc/install \
    && printf '[install.filesystem.root]\ntype = "xfs"\n' > /usr/lib/bootc/install/00-drill.toml
LABEL awnix.drill=n
EOF
podman build -q -t localhost/awnix-drill-n:latest "$WORK/n" >/dev/null \
  || { echo "DRILL-DEAD: N build failed"; exit 2; }

# ── disk ───────────────────────────────────────────────────────────────────────────
podman run --rm --privileged --network=host --security-opt label=type:unconfined_t \
  -v "$STORAGE":/var/lib/containers/storage -v "$WORK/out":/output \
  "$BIB" --type qcow2 --local localhost/awnix-drill-n:latest > "$WORK/bib.log" 2>&1
DISK=$(find "$WORK/out" -name '*.qcow2' | head -1)
[ -n "$DISK" ] || { echo "DRILL-DEAD: no qcow2 (see $WORK/bib.log)"; tail -5 "$WORK/bib.log"; exit 2; }

# ── boot until the drill decides ───────────────────────────────────────────────────
# -no-reboot turns every guest reboot into a QEMU exit, so each boot is one launch and
# the loop relaunches until the drill writes a verdict. One serial log, appended.
: > "$SERIAL"
for launch in $(seq 1 12); do
  timeout 900 qemu-system-x86_64 -enable-kvm -m 4096 -smp 4 -cpu host     -drive file="$DISK",if=virtio,format=qcow2 -display none     -chardev file,id=s0,path="$SERIAL",append=on -serial chardev:s0     -monitor none -no-reboot -net none >> "$WORK/qemu.log" 2>&1
  echo "launch $launch exited rc=$?" >> "$WORK/qemu.log"
  grep -q "DRILL-RESULT:" "$SERIAL" && break
done

grep -E "DRILL|greenboot|redboot|boot_counter" "$SERIAL" | tail -40
if grep -q "DRILL-RESULT: ROLLED-BACK" "$SERIAL"; then echo "VERDICT: ROLLED-BACK"; exit 0; fi
if grep -q "DRILL-RESULT: NO-ROLLBACK" "$SERIAL"; then echo "VERDICT: NO-ROLLBACK"; exit 1; fi
grep -qE "DRILL-RESULT: (N1-NEVER-BOOTED|SWITCH-FAILED)" "$SERIAL" && { echo "VERDICT: UNJUDGED (N+1 never ran)"; exit 2; }
echo "VERDICT: UNJUDGED (no DRILL-RESULT in the serial log)"; exit 2
