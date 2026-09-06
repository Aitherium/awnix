#!/usr/bin/env bash
# boot-proof-garg.sh — Stage 5 of the garg appliance plan: THE BOOT PROOF.
#
# Replicates boot-smoke-standalone.yml's two-stage QEMU mechanics exactly
# (stage 1: unattended install from the ISO to a 20G qcow2, -no-reboot, 90m;
# stage 2: boot the INSTALLED DISK, 20m) against the LOCAL pre-publish ISO.
# PASS = the installed system reaches multi-user or speaks a first-boot marker
# on serial. Serial logs are saved for the flip commit's evidence.
#
#   ./boot-proof-garg.sh <install.iso> [workdir]
#   verdict=PASS | INSTALL_TIMEOUT | INSTALL_FAIL | FAIL | TIMEOUT
set -u
ISO="${1:?usage: boot-proof-garg.sh <install.iso> [workdir]}"
WORK="${2:-/var/tmp/garg-boot-proof}"
MARKERS='garg-firstboot|Gargbot appliance first boot|Reached target.*Multi-User|login:'
mkdir -p "$WORK"
cd "$WORK" || exit 2

echo "== stage 1: unattended install from ISO (90m cap)"
qemu-img create -f qcow2 -o preallocation=metadata ./disk.qcow2 20G
timeout 90m qemu-system-x86_64 \
    -name "garg-smoke-install" \
    -machine q35,accel=kvm \
    -cpu host \
    -smp 4 \
    -m 4096 \
    -drive file="$ISO",media=cdrom,readonly=on \
    -drive file="./disk.qcow2",format=qcow2,cache=unsafe,if=virtio \
    -netdev user,id=net0,restrict=off \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56 \
    -serial file:/var/tmp/garg-serial-install.log \
    -nographic \
    -no-reboot \
    || { rc=$?; test $rc -eq 124 && { echo "verdict=INSTALL_TIMEOUT"; exit 3; }; echo "verdict=INSTALL_FAIL rc=$rc"; exit 1; }
if grep -qE "Kernel panic|Anaconda.*Error|Installation failed|Pane is dead" /var/tmp/garg-serial-install.log; then
    echo "verdict=INSTALL_FAIL (installer reported a fatal error)"
    exit 1
fi
echo "== stage 1 done — installer requested reboot; disk populated"

echo "== stage 2: first boot of the installed disk (20m cap)"
timeout 20m qemu-system-x86_64 \
    -name "garg-smoke-firstboot" \
    -machine q35,accel=kvm \
    -cpu host \
    -smp 4 \
    -m 4096 \
    -drive file="./disk.qcow2",format=qcow2,cache=unsafe,if=virtio \
    -netdev user,id=net0,restrict=off \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56 \
    -serial file:/var/tmp/garg-serial.log \
    -nographic \
    -no-reboot \
    || test $? -eq 124

if grep -qiE "$MARKERS" /var/tmp/garg-serial.log; then
    echo "verdict=PASS"
    echo "== marker lines:"
    grep -iE "$MARKERS" /var/tmp/garg-serial.log | head -5
    echo "== serial logs saved: /var/tmp/garg-serial-install.log /var/tmp/garg-serial.log"
    exit 0
fi
if grep -qE "Kernel panic|Unable to mount|BUG:|OOPS:|emergency mode|Failed to start" /var/tmp/garg-serial.log; then
    echo "verdict=FAIL (first boot hit a critical error)"
    exit 1
fi
echo "verdict=TIMEOUT (installed system never reached multi-user/login in 20m)"
exit 3
