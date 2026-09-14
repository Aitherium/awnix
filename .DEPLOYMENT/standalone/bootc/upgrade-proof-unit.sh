#!/bin/bash
# One-shot: prove the AUTHENTICATED update path on the installed appliance.
# Everything goes to the serial console so the host captures it.
# Log to /var (survives the boot; the rig reads it off the disk) AND the serial.
# Measured 2026-09-13: getty re-took ttyS0 mid-run and the serial lost the tail,
# so a serial-only verdict read a completed run as NO-RUN.
mkdir -p /var/log
exec > >(tee -a /var/log/garg-upgrade-test.log >/dev/ttyS0) 2>&1
echo "=== GARG-UPGRADE-TEST begin $(date -u +%FT%TZ)"
echo "--- credential in place:"
ls -l /etc/ostree/auth.json
echo "--- bootc status:"
bootc status 2>&1 | head -25
echo "--- bootc upgrade --check:"
timeout 300 bootc upgrade --check 2>&1 | tail -20
echo "check_exit=$?"
echo "--- bootc upgrade:"
timeout 900 bootc upgrade 2>&1 | tail -30
echo "upgrade_exit=$?"
echo "--- bootc status after:"
bootc status 2>&1 | head -25
echo "=== GARG-UPGRADE-TEST end"
systemctl poweroff 2>/dev/null || true
