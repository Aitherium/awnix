#!/bin/bash
# g6-agent.sh -- in-guest state machine for the G6 upgrade / rollback / greenboot proof.
#
#   g6-agent.sh mark   early every boot (g6-mark.service, before greenboot): count the
#                      boot, print `G6-MARK: boot=<k> role=<r>`. The ONLY trace a faulty
#                      N+2 leaves, because greenboot reboots it before multi-user.
#   g6-agent.sh run    after greenboot (g6-agent.service): print the boot line, do the
#                      next step of the plan, reboot or power off. It never reboots a
#                      boot of N+2: on N+2 it prints G6-HOLD and waits for the IMAGE to
#                      fall back; if nothing does, fallback rc=1 and power-off.
#
# State lives in /var/lib/awnix-g6 (/var survives every deployment switch -- that is
# half of what is being proven). The agent never judges; the host's g6_verdict.py does,
# from the markers. Plan and role are BAKED into the image (/usr/lib/g6/{plan,role}),
# so a boot always knows which image it is without trusting its own digest arithmetic.
#
# Host protocol: a `G6-WANT: stable=<role>` line followed by a power-off asks the host
# to retag the local registry's :stable to that role before the next launch. qemu runs
# with -no-reboot, so every reboot or power-off ends one launch.
#
# Output goes to /var/lib/awnix-g6/agent.log AND /dev/ttyS0 line by line, never through
# a pipe whose exit status would replace the command's (upgrade-proof-unit.sh recorded
# tail's $? as bootc's; measured 2026-09-28).
set -u

S=/var/lib/awnix-g6
SENT_DIR=/var/lib/aither
SENT="$SENT_DIR/g6-sentinel"
LOG="$S/agent.log"
mkdir -p "$S" "$SENT_DIR"

say() {
    local line="$*"
    printf '%s\n' "$line" >> "$LOG"
    { printf '%s\r\n' "$line" > /dev/ttyS0; } 2>/dev/null || true
    printf '%s\n' "$line" > /dev/kmsg 2>/dev/null || true
}

# Run a command, log its output line by line, return ITS exit code.
logged() {
    local out rc
    out=$(mktemp)
    "$@" > "$out" 2>&1
    rc=$?
    while IFS= read -r l; do say "  | $l"; done < <(tail -n 40 "$out")
    rm -f "$out"
    return "$rc"
}

role() { cat /usr/lib/g6/role 2>/dev/null || echo unknown; }
plan() { cat /usr/lib/g6/plan 2>/dev/null || echo upgrade,rollback,fallback; }
in_plan() { case ",$(plan)," in *",$1,"*) return 0 ;; esac; return 1; }
gb_max() {
    local v
    v=$(sed -n 's/^GREENBOOT_MAX_BOOT_ATTEMPTS=\([0-9]*\).*/\1/p' /etc/greenboot/greenboot.conf 2>/dev/null | tail -1)
    echo "${v:-3}"
}
getn() { cat "$S/$1" 2>/dev/null || echo "${2:-0}"; }
setv() { printf '%s\n' "$2" > "$S/$1.tmp" && mv -f "$S/$1.tmp" "$S/$1"; sync; }

# booted staged rollback digests from bootc's own status, "none" when absent.
digests() {
    local js
    js=$(bootc status --format=json 2>/dev/null || bootc status --json 2>/dev/null || true)
    printf '%s' "$js" | python3 -c '
import json, sys
try:
    st = (json.load(sys.stdin) or {}).get("status") or {}
except Exception:
    st = {}
def d(k):
    e = st.get(k) or {}
    return ((e.get("image") or {}).get("imageDigest")) or "none"
print(d("booted"), d("staged"), d("rollback"))
' 2>/dev/null || echo "none none none"
}

sentinel_state() {
    [ -s "$SENT" ] && [ -s "$S/sentinel.sha256" ] || { echo missing; return; }
    if [ "$(sha256sum "$SENT" | cut -d' ' -f1)" = "$(cat "$S/sentinel.sha256")" ]; then echo ok; else echo missing; fi
}

finish() { setv phase "done"; say "G6-END"; sync; sleep 2; systemctl --no-block poweroff; exit 0; }
reboot_now() { sync; sleep 2; systemctl --no-block reboot; exit 0; }
want() { say "G6-WANT: stable=$1"; sync; sleep 2; systemctl --no-block poweroff; exit 0; }

# Stage an upgrade from the tracked ref (…/g6/awnix:stable) and report whether
# something was actually staged: bootc exits 0 on "no update available" too.
do_upgrade() {
    local rc staged
    logged timeout 1200 bootc upgrade
    rc=$?
    read -r _ staged _ <<< "$(digests)"
    if [ "$rc" -eq 0 ] && [ "$staged" = "none" ]; then
        say "  (bootc upgrade exited 0 but staged nothing -- the registry :stable was not moved)"
        rc=97
    fi
    return "$rc"
}

do_rollback() {
    local rc
    # The product path when the image carries it (awnix update rollback stages
    # `bootc rollback` and schedules its own detached reboot); plain bootc otherwise.
    if [ -x /usr/libexec/awnix/awnix-update ] && command -v awnix >/dev/null 2>&1; then
        VIA=awnix-update
        AWNIX_NO_REBOOT=1 logged timeout 600 awnix update rollback
        rc=$?
    else
        VIA=bootc
        logged timeout 600 bootc rollback
        rc=$?
    fi
    return "$rc"
}

cmd_mark() {
    local k
    k=$(( $(getn boots 0) + 1 ))
    setv boots "$k"
    if [ "$(role)" = n2 ]; then setv n2_boots $(( $(getn n2_boots 0) + 1 )); fi
    say "G6-MARK: boot=$k role=$(role)"
}

cmd_run() {
    local boot phase r b st rb sent rc
    boot=$(getn boots 0)
    phase=$(getn phase init)
    r=$(role)
    if [ "$phase" = init ] && [ ! -s "$SENT" ]; then
        # The /var state an agent would keep: random content, hashed now, re-hashed
        # on every later boot of every deployment.
        head -c 48 /dev/urandom | base64 > "$SENT"
        date -u +%FT%TZ >> "$SENT"
        sha256sum "$SENT" | cut -d' ' -f1 > "$S/sentinel.sha256"
        sync
    fi
    read -r b st rb <<< "$(digests)"
    sent=$(sentinel_state)
    say "G6: boot=$boot booted=$b staged=$st rollback=$rb phase=$phase sentinel=$sent role=$r"

    case "$phase" in
    init)
        say "G6-PLAN: steps=$(plan) greenboot_max=$(gb_max) grub_counting=$(cat /usr/lib/g6/grub_counting 2>/dev/null || echo unknown) inject=$(cat /usr/lib/g6/inject 2>/dev/null || echo none) fallback_reboot=$(cat /usr/lib/g6/fallback_reboot 2>/dev/null || echo unknown)"
        if in_plan upgrade; then setv phase upgrade; want n1
        elif in_plan fallback; then setv phase stage-n2; want n2
        else say "G6: empty plan"; finish; fi ;;
    upgrade)
        do_upgrade; rc=$?
        say "G6-STEP: upgrade rc=$rc"
        [ "$rc" -eq 0 ] || finish
        setv phase verify-upgrade; reboot_now ;;
    verify-upgrade)
        if in_plan rollback; then
            setv phase verify-rollback
            do_rollback; rc=$?
            say "G6-STEP: rollback rc=$rc via=$VIA"
            [ "$rc" -eq 0 ] || finish
            reboot_now
        elif in_plan fallback; then setv phase stage-n2; want n2
        else finish; fi ;;
    verify-rollback)
        if in_plan fallback; then setv phase stage-n2; want n2; else finish; fi ;;
    stage-n2)
        do_upgrade; rc=$?
        say "G6-STAGE: n2 rc=$rc"
        if [ "$rc" -ne 0 ]; then say "G6-STEP: fallback rc=3 n2_boots=0"; finish; fi
        setv phase awaiting-fallback; reboot_now ;;
    awaiting-fallback)
        local n2b
        n2b=$(getn n2_boots 0)
        if [ "$r" = n2 ]; then
            # The agent NEVER reboots N+2. Returning to N must come from the image alone
            # (greenboot + awnix-greenboot-fallback.service), exactly as on a fielded
            # node with no test harness. Until 2026-09-28 this branch rebooted N+2
            # itself "to give greenboot its count", and in hosted runs 36415942227 and
            # 36417253922 that agent reboot -- not greenboot -- ended boot 9 on N+2.
            # Hold, and if nothing in the image reboots us, record the failure.
            local hold
            hold=$(cat /usr/lib/g6/hold_secs 2>/dev/null || echo 300)
            say "G6-HOLD: boot=$boot role=n2 n2_boots=$n2b hold=${hold}s -- the agent will not reboot N+2"
            say "  greenboot: $(grub2-editenv - list 2>/dev/null | tr '\n' ' ')"
            sleep "$hold"
            say "G6-STEP: fallback rc=1 n2_boots=$n2b reason=still-on-n2-after-hold"
            finish
        fi
        if [ "$n2b" -ge 1 ]; then say "G6-STEP: fallback rc=0 n2_boots=$n2b"
        else say "G6-STEP: fallback rc=2 n2_boots=0"; fi
        say "  greenboot: $(grub2-editenv - list 2>/dev/null | tr '\n' ' ')"
        finish ;;
    done)
        say "G6-END"; sync; systemctl --no-block poweroff ;;
    *)
        say "G6: unknown phase $phase"; finish ;;
    esac
}

case "${1:-run}" in
    mark) cmd_mark ;;
    run)  cmd_run ;;
    *)    echo "usage: $0 mark|run" >&2; exit 2 ;;
esac
