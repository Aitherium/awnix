#!/bin/bash
# greenboot WANTED check -- installed as /etc/greenboot/check/wanted.d/50-awnix-ops.sh
#
# Runs awnix-ops-doctor and REPORTS. It goes in wanted.d, never required.d: an
# air-gapped box whose clock has never synced, or a full-ish log disk, is something
# to tell an operator about, not a reason for greenboot to roll the OS image back.
# A wanted check that fails is logged and the boot is still marked green.
#
# The exit code is passed through so greenboot logs failures ("wanted check ...
# failed"). Exit 2 (could not judge) is reported as a failure too: silence must
# never read as health.
set -u
DOCTOR=/usr/libexec/awnix/awnix-ops-doctor
if [ ! -x "$DOCTOR" ]; then
    echo "awnix-ops: $DOCTOR missing -- cannot judge clock/log/boot health" >&2
    exit 2
fi
out=$("$DOCTOR" --json 2>&1)
rc=$?
printf 'awnix-ops-doctor rc=%s %s\n' "$rc" "$(printf '%s' "$out" | tr -d '\n' | head -c 2000)"
exit "$rc"
