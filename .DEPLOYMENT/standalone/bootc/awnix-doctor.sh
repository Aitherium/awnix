#!/bin/sh
# awnix doctor -- one table of every plane's own health verdict (installed as
# /usr/libexec/awnix/awnix-doctor, run as `awnix doctor`).
#
# It judges NOTHING itself: each row is the owning CLI's doctor/status verb and its
# exit code (0 ok, 1 failed, 2 could not judge, 3 not entitled). A plane whose CLI is
# not installed is `absent`, which counts as could-not-judge, never as fine.
#
#   awnix doctor [--json]
#   awnix doctor --bundle     also write a REDACTED support tarball to /var/tmp
#   awnix doctor --self-test | --list-verbs
#
# Exit: 1 if any plane failed; else 2 if any could not be judged; else 0.
set -u

AWNIX="${AWNIX_DOCTOR_AWNIX:-/usr/bin/awnix}"
AITHEROS="${AWNIX_DOCTOR_AITHEROS:-/usr/bin/aitheros}"
BUNDLE_DIR="${AWNIX_DOCTOR_BUNDLE_DIR:-/var/tmp}"
TIMEOUT="${AWNIX_DOCTOR_TIMEOUT:-60}"

# name|argv (space-separated, no user input ever reaches these)
CHECKS='license|AITHEROS license doctor
updates|AWNIX update doctor
components|AWNIX component list --json
endpoints|AWNIX endpoints probe --json
console|AWNIX console status --json
setup|AWNIX setup --status --json
renewal|AWNIX renewal status --json
offline|AWNIX offline-update status --json
ops|AWNIX ops-doctor --json'

# Strip anything shaped like a credential before it leaves the box.
redact() {
    sed -e 's/AITHER1\.[A-Za-z0-9_=-]*\.[A-Za-z0-9_=-]*/AITHER1.<redacted>/g' \
        -e 's/gh[pousr]_[A-Za-z0-9]\{20,\}/<redacted-token>/g' \
        -e 's/x-access-token:[^"[:space:]]*/x-access-token:<redacted>/g' \
        -e 's/"\(auth\|token\|password\|secret\)"[[:space:]]*:[[:space:]]*"[^"]*"/"\1": "<redacted>"/g'
}

have_timeout() { command -v timeout >/dev/null 2>&1; }

# run_check NAME ARGV... -> sets RC and OUT
run_check() {
    name=$1
    shift
    bin=$1
    case "$bin" in
        AWNIX) bin=$AWNIX ;;
        AITHEROS) bin=$AITHEROS ;;
    esac
    shift
    if [ ! -x "$bin" ]; then
        RC=absent
        OUT="$bin not installed"
        return
    fi
    # `awnix <verb>` with the verb missing exits 2 from the dispatcher: absent too.
    if [ "$bin" = "$AWNIX" ] && ! "$AWNIX" --list-verbs 2>/dev/null | grep -qx "$1"; then
        RC=absent
        OUT="awnix $1 not installed"
        return
    fi
    if have_timeout; then
        OUT=$(timeout "$TIMEOUT" "$bin" "$@" 2>&1)
    else
        OUT=$("$bin" "$@" 2>&1)
    fi
    RC=$?
    [ "$RC" = 124 ] && OUT="timed out after ${TIMEOUT}s"
}

verdict_of() {
    case "$1" in
        0) echo ok ;;
        1) echo FAILED ;;
        2) echo could-not-judge ;;
        3) echo not-entitled ;;
        absent) echo absent ;;
        124) echo timeout ;;
        *) echo "error($1)" ;;
    esac
}

json_str() {
    # minimal JSON string escaper for one line of text
    printf '"%s"' "$(printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

doctor() {
    as_json=$1
    bundle=$2
    worst=0
    work=""
    if [ "$bundle" = 1 ]; then
        work=$(mktemp -d "${BUNDLE_DIR%/}/awnix-support.XXXXXX") || { echo "awnix doctor: cannot create a bundle dir in $BUNDLE_DIR" >&2; return 2; }
    fi
    [ "$as_json" = 1 ] || printf '%-12s %-16s %s\n' PLANE VERDICT DETAIL
    first=1
    [ "$as_json" = 1 ] && printf '{"checks": ['
    old_ifs=$IFS
    IFS='
'
    for line in $CHECKS; do
        IFS=$old_ifs
        name=${line%%|*}
        # shellcheck disable=SC2086 # the argv is a fixed literal from CHECKS
        run_check "$name" ${line#*|}
        v=$(verdict_of "$RC")
        detail=$(printf '%s' "$OUT" | redact | tail -n 1 | cut -c1-100)
        case "$RC" in
            0|3) ;;
            1|124) worst=1 ;;
            *) [ "$worst" = 1 ] || worst=2 ;;
        esac
        if [ "$as_json" = 1 ]; then
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '{"plane": "%s", "exit": %s, "verdict": "%s", "detail": %s}' \
                "$name" "$( [ "$RC" = absent ] && echo null || echo "$RC")" "$v" "$(json_str "$detail")"
        else
            printf '%-12s %-16s %s\n' "$name" "$v" "$detail"
        fi
        if [ -n "$work" ]; then
            printf '%s\n' "$OUT" | redact > "$work/$name.txt"
        fi
        IFS='
'
    done
    IFS=$old_ifs
    if [ "$as_json" = 1 ]; then
        printf '], "exit": %s}\n' "$worst"
    fi
    if [ -n "$work" ]; then
        {
            cat /etc/os-release 2>/dev/null
            cat /usr/lib/awnix/release.env 2>/dev/null
        } | redact > "$work/release.txt"
        if command -v bootc >/dev/null 2>&1; then
            bootc status 2>&1 | redact > "$work/bootc-status.txt"
        fi
        if command -v journalctl >/dev/null 2>&1; then
            journalctl -b --no-pager -n 2000 -u 'awnix-*' -u 'aither-license*' \
                -u garg-firstboot 2>&1 | redact > "$work/journal.txt"
        fi
        host=$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9.-')
        out="${BUNDLE_DIR%/}/awnix-support-${host:-box}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
        if tar -czf "$out" -C "$work" . 2>/dev/null; then
            chmod 0600 "$out" 2>/dev/null
            echo "support bundle (redacted): $out" >&2
        else
            echo "awnix doctor: could not write $out" >&2
            worst=2
        fi
        rm -rf "$work"
    fi
    return "$worst"
}

self_test() {
    tmp=$(mktemp -d 2>/dev/null || mktemp -d -t awnixdoc)
    trap 'rm -rf "$tmp"' EXIT
    cat > "$tmp/awnix" <<'STUB'
#!/bin/sh
case "$1" in
  --list-verbs) printf 'update\ncomponent\nconsole\nsetup\n' ;;
  update) echo "update fine"; exit 0 ;;
  component) echo '{"ok": true}'; exit 0 ;;
  console) echo 'console AITHER1.abc.def gh'"p_"'abcdefghijklmnopqrstuvwxyz0123'; exit 1 ;;
  setup) echo "setup cannot judge"; exit 2 ;;
  *) exit 2 ;;
esac
STUB
    chmod +x "$tmp/awnix"
    fails=0
    t() {
        if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (want '$2' got '$3')"; fails=$((fails + 1)); fi
    }
    self="$0"
    env_run() {
        AWNIX_DOCTOR_AWNIX="$tmp/awnix" AWNIX_DOCTOR_AITHEROS="$tmp/no-aitheros" \
            AWNIX_DOCTOR_BUNDLE_DIR="$tmp" sh "$self" "$@"
    }
    out=$(env_run); rc=$?
    t "a FAILED plane makes the exit 1" 1 "$rc"
    t "absent license CLI is 'absent'" 1 "$(printf '%s\n' "$out" | grep -c '^license *absent')"
    t "endpoints verb missing from the dispatcher is 'absent'" 1 \
        "$(printf '%s\n' "$out" | grep -c '^endpoints *absent')"
    t "table redacts envelopes and tokens" 0 "$(printf '%s\n' "$out" | grep -c 'AITHER1\.abc\|ghp_abc')"
    json=$(env_run --json)
    t "--json is one document with exit 1" 1 "$(printf '%s' "$json" | grep -c '"exit": 1}$')"
    if command -v python3 >/dev/null 2>&1 && python3 -c pass >/dev/null 2>&1; then
        printf '%s' "$json" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null
        t "--json parses" 0 "$?"
    fi
    env_run --bundle >/dev/null 2>&1
    n=$(find "$tmp" -maxdepth 1 -name 'awnix-support-*.tar.gz' | wc -l | tr -d ' ')
    t "--bundle writes one tarball" 1 "$n"
    b=$(find "$tmp" -maxdepth 1 -name 'awnix-support-*.tar.gz' | head -n 1)
    if [ -n "$b" ]; then
        leaked=$(tar -xzOf "$b" 2>/dev/null | grep -c 'AITHER1\.abc\|ghp_abc')
        t "bundle is redacted" 0 "$leaked"
    fi
    if [ "$fails" -eq 0 ]; then echo "awnix doctor self-test: PASS"; return 0; fi
    echo "awnix doctor self-test: FAIL ($fails)"
    return 1
}

as_json=0
bundle=0
for a in "$@"; do
    case "$a" in
        --json) as_json=1 ;;
        --bundle) bundle=1 ;;
        --self-test) self_test; exit $? ;;
        --list-verbs) echo doctor; exit 0 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "awnix doctor: unknown argument '$a'" >&2; exit 2 ;;
    esac
done
doctor "$as_json" "$bundle"
exit $?
