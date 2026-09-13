#!/bin/bash
# run-upgrade-proof.sh [auth.json] — end-to-end authenticated-upgrade proof on
# the boot-proven appliance disk.
#
#   1. convert the proven r19 disk (qcow2) to raw, inject /etc/ostree/auth.json
#      + a oneshot unit that runs `bootc upgrade` and reports to the serial
#   2. boot the raw disk WITH INTERNET (user-mode net)
#   3. the unit runs bootc upgrade, prints everything to ttyS0, powers off
#   4. parse the serial for the verdict
#
# Run with the dummy auth.json first: the expected, honest result is an auth
# wall at pull (401/denied) — which proves every moving part except the token.
# Drop in the real read:packages token's auth.json and rerun for the real proof.
set -u
AUTH="${1:-/var/tmp/garg-rig/auth.json}"
SRC=/var/tmp/garg-boot-proof-r19/disk.qcow2
RAW=/var/tmp/garg-rig/proof-disk.raw
SER=/var/tmp/garg-rig/upgrade-serial.log
LOOP=""

[ -f "$AUTH" ] || { echo "AUTH-MISSING: $AUTH"; exit 1; }
[ -f "$SRC" ] || { echo "DISK-MISSING: $SRC"; exit 1; }

echo "== converting disk to raw (sparse)"
rm -f "$RAW"
qemu-img convert -O raw -S 1M "$SRC" "$RAW" || { echo CONVERT-FAILED; exit 1; }

echo "== injecting credential + proof unit (into the DEPLOYMENT etc/usr)"
LOOP=$(losetup -Pf --show "$RAW") || { echo LOSETUP-FAILED; exit 1; }
mkdir -p /mnt/proofdisk
mount "${LOOP}p3" /mnt/proofdisk || { losetup -d "$LOOP"; echo MOUNT-FAILED; exit 1; }
# This disk BOOTS the ostree deployment at ostree/deploy/default/deploy/<sha>.0
# — its etc/ IS the booted /etc (firstboot.done lives there) and its usr/ is
# the booted /usr. Writing to the partition's TOP-LEVEL etc/ (the first version
# of this rig) put the unit somewhere the boot never reads: measured — the unit
# never ran and the journal knew nothing about it.
D=$(ls -d /mnt/proofdisk/ostree/deploy/default/deploy/*.0 | head -1)
echo "   deployment: $D"
# Everything under etc/ on purpose: /etc is a REAL writable bind at boot, while
# /usr is composefs-enumerated AT DEPLOYMENT TIME — a file added to the
# deployment's usr/ afterwards is invisible at runtime (measured: the unit
# loaded and died 203/EXEC on /etc/gargbot/garg-upgrade-test.sh).
mkdir -p "$D/etc/ostree" "$D/etc/gargbot" "$D/etc/systemd/system/multi-user.target.wants"
install -m 600 "$AUTH" "$D/etc/ostree/auth.json"
install -m 755 /var/tmp/garg-rig/garg-upgrade-test.sh "$D/etc/gargbot/garg-upgrade-test.sh"
install -m 644 /var/tmp/garg-rig/garg-upgrade-test.service "$D/etc/systemd/system/garg-upgrade-test.service"
ln -sf ../garg-upgrade-test.service "$D/etc/systemd/system/multi-user.target.wants/garg-upgrade-test.service"
# SELinux labels for offline-injected files: the guest boots ENFORCING, and an
# unlabeled unit does not load (measured: the unit never fired, journal knew
# nothing). setfattr writes the raw security.selinux xattr without a loaded
# policy, so the enforcing boot sees the right types.
if command -v setfattr >/dev/null 2>&1; then
    setfattr -n security.selinux -v "system_u:object_r:systemd_unit_file_t:s0" "$D/etc/systemd/system/garg-upgrade-test.service" 2>/dev/null || echo "   (setfattr unit failed — boot adds selinux=0 as the fallback)"
    setfattr -n security.selinux -v "system_u:object_r:bin_t:s0" "$D/etc/gargbot/garg-upgrade-test.sh" 2>/dev/null || true
    setfattr -n security.selinux -v "system_u:object_r:etc_t:s0" "$D/etc/ostree/auth.json" 2>/dev/null || true
else
    echo "   (no setfattr — relying on the selinux=0 boot)"
fi
# BLS: append selinux=0 to every kernel cmdline on /boot — the belt to the
# setfattr braces, and the one that ALWAYS works for an offline-injected rig.
mount "${LOOP}p1" /mnt/proofboot 2>/dev/null || { mkdir -p /mnt/proofboot; mount "${LOOP}p1" /mnt/proofboot; }
for e in /mnt/proofboot/loader/entries/*.conf; do
    [ -f "$e" ] && sed -i -E '/^options /{ /selinux=0/! s/$/ selinux=0/ }' "$e" && echo "   bls edited: $e"
done
umount /mnt/proofboot 2>/dev/null
ls -l "$D/etc/ostree/auth.json" "$D/etc/systemd/system/garg-upgrade-test.service"
umount /mnt/proofdisk
losetup -d "$LOOP"

echo "== booting the raw disk WITH INTERNET; the unit will run bootc upgrade"
rm -f "$SER"
timeout 1500 qemu-system-x86_64 -name garg-upgrade-proof \
  -machine q35,accel=kvm -cpu host -smp 4 -m 4096 \
  -drive file="$RAW",format=raw,cache=unsafe,if=virtio \
  -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
  -serial "file:$SER" -display none -no-reboot || true

echo "== verdict parsing"
if grep -q "GARG-UPGRADE-TEST end" "$SER" 2>/dev/null; then
    sed -n '/GARG-UPGRADE-TEST begin/,/GARG-UPGRADE-TEST end/p' "$SER" | tr -d '\r'
    if sed -n '/GARG-UPGRADE-TEST begin/,/GARG-UPGRADE-TEST end/p' "$SER" | grep -qiE "denied|unauthorized|401|403|authentication required|credential"; then
        echo "VERDICT: AUTH-WALL (the pull was rejected — token/credential is the gap)"
    elif sed -n '/GARG-UPGRADE-TEST begin/,/GARG-UPGRADE-TEST end/p' "$SER" | grep -qiE "Staged|up to date|Up to date|Queued for next boot"; then
        echo "VERDICT: AUTHED-PULL-OK (bootc reached the registry and got/posted the image)"
    else
        echo "VERDICT: UNCLEAR — read the block above"
    fi
else
    echo "VERDICT: NO-RUN (unit never completed) — serial tail:"
    tail -30 "$SER" 2>/dev/null
fi
