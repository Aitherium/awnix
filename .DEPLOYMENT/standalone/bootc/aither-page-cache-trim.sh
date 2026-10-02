#!/bin/sh
# aither-page-cache-trim.sh -- give the WSL guest's page cache back to Windows.
#
# Why (measured 2026-09-29 on the awnix fleet host): the guest's buff/cache grew to
# 46 GB while the guest itself USED 15 GB, and vmmemWSL held all of it against the
# Windows desktop. One `echo 1 > /proc/sys/vm/drop_caches` took vmmemWSL 39.5 -> 30.9 GB
# with nothing restarted. WSL's own autoMemoryReclaim cannot be relied on: `gradual`
# only fires when the VM is idle (it never is), and `dropcache` needs a `wsl --shutdown`
# to take effect. This script is the in-guest, no-restart version: run from
# aither-page-cache-trim.timer every 10 minutes, it drops the page cache only when the
# droppable part exceeds a threshold, and logs one line either way.
#
# droppable = Buffers + Cached - Shmem   (Shmem/tmpfs is counted in Cached but cannot
# be dropped; counting it would fire forever on a host with a big tmpfs).
# Mode 1 = page cache only. Dentries/inodes (mode 2) are not dropped by default: the
# fleet's overlay + inotify users re-walk them and the win was never measured.
#
# Usage:
#   aither-page-cache-trim.sh             # trim if over threshold
#   aither-page-cache-trim.sh --dry-run   # decide + log, never write drop_caches
#   aither-page-cache-trim.sh --self-test # prove the decision logic on fake meminfo
# Env (EnvironmentFile=-/etc/aither/page-cache-trim.env):
#   AITHER_PAGE_CACHE_TRIM_MIB   threshold in MiB (default 8192 = 8 GiB)
#   AITHER_PAGE_CACHE_DROP_MODE  1 (page cache, default) | 2 | 3
#   AITHER_PAGE_CACHE_MEMINFO / AITHER_PAGE_CACHE_DROP  test seams (default /proc paths)
# Exit: 0 trimmed or skipped · 1 drop write failed · 2 cannot judge (meminfo unreadable,
# bad config). Never 0 on silence.
set -u

THRESHOLD_MIB=${AITHER_PAGE_CACHE_TRIM_MIB:-8192}
MODE=${AITHER_PAGE_CACHE_DROP_MODE:-1}
MEMINFO=${AITHER_PAGE_CACHE_MEMINFO:-/proc/meminfo}
DROP=${AITHER_PAGE_CACHE_DROP:-/proc/sys/vm/drop_caches}
TAG=aither-page-cache-trim

# droppable_kb <meminfo>: prints Buffers+Cached-Shmem in kB, or nothing if unreadable.
droppable_kb() {
    awk '
        $1 == "Buffers:" { b = $2; seen++ }
        $1 == "Cached:"  { c = $2; seen++ }
        $1 == "Shmem:"   { s = $2 }
        END { if (seen == 2) { d = b + c - s; if (d < 0) d = 0; print d } }
    ' "$1" 2>/dev/null
}

# run <dry_run 0|1>: one decision, one log line. Returns the exit code above.
run() {
    dry=$1
    case $THRESHOLD_MIB in ''|*[!0-9]*) echo "$TAG: ERROR bad threshold '$THRESHOLD_MIB'"; return 2;; esac
    case $MODE in 1|2|3) ;; *) echo "$TAG: ERROR bad drop mode '$MODE'"; return 2;; esac
    before=$(droppable_kb "$MEMINFO")
    if [ -z "$before" ]; then
        echo "$TAG: ERROR cannot read Buffers/Cached from $MEMINFO"
        return 2
    fi
    before_mib=$((before / 1024))
    if [ "$before_mib" -le "$THRESHOLD_MIB" ]; then
        echo "$TAG: skip droppable=${before_mib}MiB threshold=${THRESHOLD_MIB}MiB"
        return 0
    fi
    if [ "$dry" = 1 ]; then
        echo "$TAG: dry-run would-drop mode=$MODE droppable=${before_mib}MiB threshold=${THRESHOLD_MIB}MiB"
        return 0
    fi
    sync
    if ! { echo "$MODE" > "$DROP"; } 2>/dev/null; then
        echo "$TAG: ERROR cannot write $DROP (mode=$MODE droppable=${before_mib}MiB)"
        return 1
    fi
    after=$(droppable_kb "$MEMINFO")
    after_mib=$(( ${after:-0} / 1024 ))
    echo "$TAG: dropped mode=$MODE droppable=${before_mib}MiB->${after_mib}MiB freed=$((before_mib - after_mib))MiB threshold=${THRESHOLD_MIB}MiB"
    return 0
}

self_test() {
    t=$(mktemp -d 2>/dev/null || echo "/tmp/$TAG.$$")
    mkdir -p "$t"
    fail=0
    check() { # <name> <expected rc> <expected drop content or -> <actual rc>
        got=$(cat "$t/drop" 2>/dev/null || echo -)
        if [ "$4" != "$2" ] || [ "$got" != "$3" ]; then
            echo "$TAG: SELF-TEST FAIL $1: rc=$4 (want $2) drop='$got' (want '$3')"
            fail=1
        fi
        rm -f "$t/drop"
    }
    mk() { printf 'MemTotal: 67108864 kB\nBuffers: %s kB\nCached: %s kB\nShmem: %s kB\n' "$1" "$2" "$3" > "$t/meminfo"; }
    # Each case: set inputs, run once, assert rc AND what reached the drop file.
    MEMINFO="$t/meminfo"; DROP="$t/drop"; THRESHOLD_MIB=8192; MODE=1
    mk 262144 12582912 0; run 0 >/dev/null; check over-threshold 0 1 $?
    mk 0 4194304 0;       run 0 >/dev/null; check under-threshold 0 - $?
    mk 0 10485760 6291456; run 0 >/dev/null; check shmem-excluded 0 - $?
    mk 0 12582912 0;      run 1 >/dev/null; check dry-run 0 - $?
    MEMINFO="$t/absent";  run 0 >/dev/null; check no-meminfo 2 - $?
    MEMINFO="$t/meminfo"; DROP="$t/nodir/drop"; run 0 >/dev/null; check unwritable 1 - $?
    DROP="$t/drop"; THRESHOLD_MIB=8G; run 0 >/dev/null; check bad-threshold 2 - $?
    rm -rf "$t"
    if [ "$fail" = 0 ]; then echo "$TAG: self-test ok (7 cases)"; return 0; fi
    return 1
}

case ${1:-} in
    --self-test) self_test; exit $? ;;
    --dry-run)   run 1; exit $? ;;
    '')          run 0; exit $? ;;
    *)           echo "usage: $0 [--dry-run|--self-test]" >&2; exit 2 ;;
esac
