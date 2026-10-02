#!/bin/bash
# awnix-greenboot-fallback.sh -- finish greenboot's fallback when greenboot does not.
#
# Runs as awnix-greenboot-fallback.service, pulled in by OnFailure= of
# greenboot-healthcheck.service (drop-in awnix-greenboot-fallback.conf).
#
# Why it exists (measured, hosted G6 runs 36415942227 and 36417253922, greenboot 0.16.4
# on centos-bootc:stream9): on the LAST failed boot of a bad update, greenboot's health
# runner fails but does not reboot. grub's boot_counter is spent and the fallback is
# armed, yet the node sits on the faulty image in multi-user.target until something
# else reboots it. In those runs that "something" was the G6 test agent. A fielded
# appliance has no test agent, so this unit is the reboot.
#
# It reboots ONLY when a fallback is actually armed for the next boot:
#   * grubenv boot_success=0 and boot_counter is set (grub decrements it, and at 0
#     or -1 boots the rollback entry -- the 08_fallback_counting hunk), or
#   * `bootc status` reports rollbackQueued=true.
# and NEVER when the image it would fall back to has already been fallen back to and
# failed too (state in /var/lib/awnix/greenboot-fallback.state): two bad images must
# not ping-pong forever. Then it prints "manual intervention" and exits 1.
#
# Exit: 0 reboot issued or nothing armed (healthy/no update) · 1 refused (loop guard)
#       · 2 could not judge (no grub2-editenv and no bootc).
set -u

STATE_DIR=${AWNIX_FALLBACK_STATE_DIR:-/var/lib/awnix}
STATE="$STATE_DIR/greenboot-fallback.state"
DELAY=${AWNIX_FALLBACK_REBOOT_DELAY:-5}

say() {
    printf 'awnix-greenboot-fallback: %s\n' "$*"
    { printf 'awnix-greenboot-fallback: %s\n' "$*" > /dev/kmsg; } 2>/dev/null || true
}

grubenv_get() {
    grub2-editenv - list 2>/dev/null | sed -n "s/^$1=//p" | tail -1
}

# booted, rollback digests and rollbackQueued from bootc's own status.
bootc_facts() {
    local js
    js=$(bootc status --format=json 2>/dev/null || bootc status --json 2>/dev/null || true)
    [ -n "$js" ] || { echo "none none unknown"; return; }
    printf '%s' "$js" | python3 -c '
import json, sys
try:
    st = (json.load(sys.stdin) or {}).get("status") or {}
except Exception:
    st = {}
def d(k):
    e = st.get(k) or {}
    return ((e.get("image") or {}).get("imageDigest")) or "none"
q = st.get("rollbackQueued")
print(d("booted"), d("rollback"), "unknown" if q is None else str(bool(q)).lower())
' 2>/dev/null || echo "none none unknown"
}

main() {
    local have_grub=0 have_bootc=0 bs bc booted rollback queued armed=""
    command -v grub2-editenv >/dev/null 2>&1 && have_grub=1
    command -v bootc >/dev/null 2>&1 && have_bootc=1
    if [ "$have_grub" = 0 ] && [ "$have_bootc" = 0 ]; then
        say "cannot judge: neither grub2-editenv nor bootc is present"
        return 2
    fi

    bs=$(grubenv_get boot_success)
    bc=$(grubenv_get boot_counter)
    read -r booted rollback queued <<< "$(bootc_facts)"
    say "greenboot failed: boot_success=${bs:-unset} boot_counter=${bc:-unset} booted=$booted rollback=$rollback rollbackQueued=$queued"

    if [ "${bs:-0}" = 1 ]; then
        say "boot_success=1: this boot was already marked good; nothing to do"
        return 0
    fi
    # -1 is also "take the fallback" to grub, so it counts as armed; a node that is
    # ALREADY on its fallback and failing is caught by the loop guard below.
    if [ -n "$bc" ]; then armed="grub boot_counter=$bc"; fi
    if [ "$queued" = true ]; then armed="${armed:+$armed, }bootc rollbackQueued"; fi
    if [ -z "$armed" ]; then
        say "no fallback armed (no pending update): manual intervention needed"
        return 0
    fi

    # Loop guard: if we already rebooted AWAY from the image we would now fall back
    # to, and are now failing on the image we fell back TO, both are bad -- stop.
    if [ -s "$STATE" ] && [ "$booted" != none ]; then
        local from to
        from=$(sed -n 's/^from=//p' "$STATE" | tail -1)
        to=$(sed -n 's/^to=//p' "$STATE" | tail -1)
        if [ "$to" = "$booted" ] && [ "$from" = "$rollback" ]; then
            say "refusing: already fell back from $from to $booted and it failed too -- manual intervention needed"
            return 1
        fi
    fi

    mkdir -p "$STATE_DIR"
    { printf 'from=%s\n' "$booted"; printf 'to=%s\n' "$rollback"; date -u +at=%FT%TZ; } > "$STATE.tmp" \
        && mv -f "$STATE.tmp" "$STATE"
    sync
    say "fallback armed ($armed): rebooting in ${DELAY}s so the previous deployment boots"
    sleep "$DELAY"
    systemctl --no-block reboot
    return 0
}

main "$@"
