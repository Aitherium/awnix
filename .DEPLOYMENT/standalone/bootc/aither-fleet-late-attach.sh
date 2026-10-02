#!/bin/sh
# aither-fleet-late-attach.sh -- bring the fleet up when its data disk arrives AFTER boot.
#
# The fleet's data lives on the migrated source disk, which the Windows host attaches
# with `wsl --mount --vhd <ext4.vhdx> --bare` (task AitherOS-AttachFleetData). If
# ANYTHING starts this distro first -- a `wsl` typed in a terminal, the pin task, an
# agent's probe -- the distro boots before the disk exists:
# aither-attach-fleet-data.service fails, the quadlet generator sees an empty dir,
# zero containers start, and first-boot setup offers to configure a "new" machine.
# Measured 2026-10-01 02:39: that state persisted until a human terminated the distro.
#
# udev runs this (via aither-fleet-late-attach@<uuid>.service) every time a block
# device with a filesystem UUID appears. It acts only when that UUID is the fleet's
# source disk AND the data is not bound yet; then it re-runs the attach, reloads
# systemd so the quadlet generator sees the fleet's units, and starts the core fleet
# exactly as a normal boot would (aither-fleet-start.service). Idempotent: a disk that
# is already bound, or any other disk, is a no-op.
#
#   aither-fleet-late-attach.sh <uuid>
#   aither-fleet-late-attach.sh --self-test
set -u

ENV_FILE="${AITHER_FLEET_ENV:-/etc/aither/awnix-migration.env}"
say() { printf 'aither-fleet-late-attach: %s\n' "$*"; }

is_uuid() { printf '%s' "$1" | grep -Eq '^[A-Fa-f0-9-]{8,64}$'; }

# Decide from (seen uuid, fleet uuid, bound?) -- pure, so the self-test can prove it.
decide() {
    seen="$1"; want="$2"; bound="$3"
    is_uuid "$seen" || { echo skip-bad-uuid; return; }
    is_uuid "$want" || { echo skip-no-fleet; return; }
    [ "$seen" = "$want" ] || { echo skip-other-disk; return; }
    [ "$bound" = yes ] && { echo skip-already-bound; return; }
    echo attach
}

self_test() {
    fail=0
    u=3477ce20-8e90-4272-9984-fd2b3a867e26
    [ "$(decide "$u" "$u" no)" = attach ] || { echo "SELFTEST: fleet disk not attached"; fail=1; }
    [ "$(decide "$u" "$u" yes)" = skip-already-bound ] || { echo "SELFTEST: re-attached"; fail=1; }
    [ "$(decide 2dd79cab-3d85 "$u" no)" = skip-other-disk ] || { echo "SELFTEST: other disk"; fail=1; }
    [ "$(decide '$(reboot)' "$u" no)" = skip-bad-uuid ] || { echo "SELFTEST: injected uuid"; fail=1; }
    [ "$(decide "$u" "" no)" = skip-no-fleet ] || { echo "SELFTEST: no env"; fail=1; }
    [ "$fail" -eq 0 ] && echo "self-test ok"
    return "$fail"
}

[ "${1:-}" = "--self-test" ] && { self_test; exit $?; }

SEEN="${1:-}"
[ -r "$ENV_FILE" ] || { say "no $ENV_FILE: not a migrated host"; exit 0; }
WANT="$(sed -n 's/^AITHER_FLEET_SRC_UUID=//p' "$ENV_FILE" | tr -d '"'"'" | head -1)"
MP="$(sed -n 's/^AITHER_FLEET_MOUNT_POINT=//p' "$ENV_FILE" | tr -d '"'"'" | head -1)"
BOUND=no
# Bound = the container store is a mount from another device (the source disk).
if command -v findmnt >/dev/null 2>&1 && findmnt -n /var/lib/containers >/dev/null 2>&1; then
    BOUND=yes
fi

ACTION="$(decide "$SEEN" "$WANT" "$BOUND")"
say "uuid=$SEEN fleet=$WANT bound=$BOUND -> $ACTION"
[ "$ACTION" = attach ] || exit 0

# While the boot attach unit is still running it owns this (it waits for the disk);
# act once it has FAILED (booted without the disk) or never ran.
case "$(systemctl is-active aither-attach-fleet-data.service 2>/dev/null || true)" in
    activating|reloading) say "boot attach still running; it will find the disk"; exit 0 ;;
esac

systemctl reset-failed aither-attach-fleet-data.service 2>/dev/null || true
if ! systemctl restart aither-attach-fleet-data.service; then
    say "FAIL attach unit did not start; see journalctl -u aither-attach-fleet-data"
    exit 1
fi
findmnt -n /var/lib/containers >/dev/null 2>&1 || { say "FAIL attach ran but store is not bound"; exit 1; }
say "fleet data bound${MP:+ (source at $MP)}; reloading units"
systemctl daemon-reload
systemctl reset-failed aither-fleet-start.service 2>/dev/null || true
systemctl restart --no-block aither-fleet-start.service
say "core fleet start queued"
exit 0
