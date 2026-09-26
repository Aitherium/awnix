#!/bin/bash
# awnix required health check -- greenboot runs this on every boot.
#
# Exit non-zero and greenboot marks the boot FAILED. After GREENBOOT_MAX_BOOT_ATTEMPTS
# failed boots of a freshly updated deployment, the machine rolls back to the previous
# image by itself. That is the whole point: an update that breaks the box undoes itself
# instead of leaving the owner in emergency mode.
#
# WHAT THIS DELIBERATELY DOES NOT CHECK: the network. greenboot's stock
# 01_repository_dns_check fails on any box that boots without a network, which would
# roll back a laptop for being on a plane. awnix boots and works offline, so the check
# only asserts what an update can break and a location cannot.
#
# Usage:  awnix-health-check.sh            (what greenboot runs)
#         awnix-health-check.sh --self-test
set -u

fail=0
say() { echo "awnix-health: $*"; }
bad() { say "FAIL $*"; fail=1; }

check_var_writable() {
    local probe="${AWNIX_HEALTH_VAR:-/var}/.awnix-health-probe.$$"
    if ( : > "$probe" ) 2>/dev/null; then rm -f "$probe"; say "ok   /var is writable"
    else bad "/var is not writable"; fi
}

check_units() {
    # Only units this image ENABLES are held to the bar; a unit a user masked or a
    # layer never enabled is not a regression.
    local u
    for u in ${AWNIX_HEALTH_UNITS:-systemd-journald.service dbus-broker.service sshd.service NetworkManager.service}; do
        case "$(systemctl is-enabled "$u" 2>/dev/null)" in
            enabled|static|alias|indirect) ;;
            *) say "skip $u (not enabled in this image)"; continue ;;
        esac
        if systemctl is-failed --quiet "$u"; then bad "$u failed"; else say "ok   $u"; fi
    done
}

check_bootc() {
    command -v bootc >/dev/null 2>&1 || { say "skip bootc (not installed)"; return; }
    if bootc status --format=json >/dev/null 2>&1 || bootc status >/dev/null 2>&1; then
        say "ok   bootc status answers"
    else
        bad "bootc status does not answer -- the update plane itself is broken"
    fi
}

self_test() {
    local d rc
    d=$(mktemp -d)
    AWNIX_HEALTH_VAR="$d" AWNIX_HEALTH_UNITS=" " bash "$0" >/dev/null; rc=$?
    [ "$rc" -eq 0 ] || { echo "SELF-TEST FAILED: a healthy fixture reported $rc"; exit 1; }
    chmod 0500 "$d"
    if [ "$(id -u)" -ne 0 ]; then
        AWNIX_HEALTH_VAR="$d" AWNIX_HEALTH_UNITS=" " bash "$0" >/dev/null; rc=$?
        [ "$rc" -ne 0 ] || { echo "SELF-TEST FAILED: an unwritable /var passed"; exit 1; }
    fi
    chmod 0700 "$d"; rm -rf "$d"
    echo "SELF-TEST PASS"
    exit 0
}

[ "${1:-}" = "--self-test" ] && self_test

check_var_writable
check_units
check_bootc
exit "$fail"
