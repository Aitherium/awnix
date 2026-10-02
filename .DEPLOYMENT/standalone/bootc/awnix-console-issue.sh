#!/bin/sh
# awnix-console-issue.sh -- tell the person at the tty/serial console where the web
# console is (ExecStartPost of awnix-console.service; also re-run after a code rotates).
#
# Writes /run/issue.d/50-awnix-console.issue with the URL and the certificate
# fingerprint. The login code is printed ONLY in setup mode (no /etc/awnix/setup.json):
# in console mode it stays in /run/awnix-console/token, read with
# `sudo awnix console code`. Also prints the serial boot-proof marker
#   awnix-console: https://<ip>:9443 fp=<sha12>
# to stdout and /dev/console (contract `garg-boot-proof-markers`).
#
#   awnix-console-issue.sh [--self-test]
# Exit: 0 written, 1 could not write, 2 console not ready (no fingerprint after wait).
set -u

CONSOLE="${AWNIX_ISSUE_CONSOLE:-/usr/libexec/awnix/awnix-console}"
TLS_FP="${AWNIX_ISSUE_FP:-/var/lib/awnix-console/tls/cert.fp}"
SETUP_MARKER="${AWNIX_ISSUE_SETUP_MARKER:-/etc/awnix/setup.json}"
SETUP_CODE="${AWNIX_ISSUE_SETUP_CODE:-/run/awnix/setup-code}"
ISSUE_DIR="${AWNIX_ISSUE_DIR:-/run/issue.d}"
TTY_OUT="${AWNIX_ISSUE_TTY:-/dev/console}"
WAIT="${AWNIX_ISSUE_WAIT:-15}"

write_issue() {
    i=0
    while [ ! -s "$TLS_FP" ] && [ "$i" -lt "$WAIT" ]; do
        sleep 1
        i=$((i + 1))
    done
    if [ ! -s "$TLS_FP" ]; then
        echo "awnix-console-issue: no certificate fingerprint at $TLS_FP" >&2
        return 2
    fi
    fp=$(head -c 12 "$TLS_FP")
    url=$("$CONSOLE" url 2>/dev/null) || url=""
    url=${url%/}
    [ -n "$url" ] || url="https://$(hostname 2>/dev/null || echo localhost):9443"
    mkdir -p "$ISSUE_DIR" || return 1
    tmp="$ISSUE_DIR/.50-awnix-console.issue.$$"
    {
        echo
        echo "  Web console: $url"
        echo "  Certificate fingerprint (sha256, first 12): $fp"
        if [ ! -f "$SETUP_MARKER" ]; then
            code=$(head -n 1 "$SETUP_CODE" 2>/dev/null | tr -cd 'A-Z0-9')
            if [ -n "$code" ]; then
                echo "  Setup code: $code"
            else
                echo "  Setup code: not issued yet (sudo awnix console code)"
            fi
        else
            echo "  Sign-in code: run 'sudo awnix console code'"
        fi
        echo
    } > "$tmp" || return 1
    chmod 0644 "$tmp" 2>/dev/null
    mv -f "$tmp" "$ISSUE_DIR/50-awnix-console.issue" || return 1
    marker="awnix-console: $url fp=$fp"
    echo "$marker"
    if [ -w "$TTY_OUT" ]; then
        echo "$marker" > "$TTY_OUT" 2>/dev/null || true
    fi
    # Refresh getty's copy of /etc/issue so an already-drawn login prompt updates.
    if command -v agetty >/dev/null 2>&1; then
        agetty --reload >/dev/null 2>&1 || true
    fi
    return 0
}

self_test() {
    tmp=$(mktemp -d 2>/dev/null || mktemp -d -t awnixissue)
    trap 'rm -rf "$tmp"' EXIT
    printf '#!/bin/sh\necho https://192.0.2.10:9443/\n' > "$tmp/console"
    chmod +x "$tmp/console"
    printf '0123456789abcdef%048d\n' 0 > "$tmp/cert.fp"
    printf 'ABCDE23456\n' > "$tmp/setup-code"
    fails=0
    t() {
        if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (want '$2' got '$3')"; fails=$((fails + 1)); fi
    }
    self="$0"
    run() {
        AWNIX_ISSUE_CONSOLE="$tmp/console" AWNIX_ISSUE_FP="$tmp/cert.fp" \
            AWNIX_ISSUE_SETUP_MARKER="$tmp/setup.json" AWNIX_ISSUE_SETUP_CODE="$tmp/setup-code" \
            AWNIX_ISSUE_DIR="$tmp/issue.d" AWNIX_ISSUE_TTY="$tmp/tty" AWNIX_ISSUE_WAIT=1 \
            sh "$self"
    }
    out=$(run)
    t "serial marker format" "awnix-console: https://192.0.2.10:9443 fp=0123456789ab" "$out"
    t "setup mode shows the setup code" 1 "$(grep -c 'Setup code: ABCDE23456' "$tmp/issue.d/50-awnix-console.issue")"
    echo '{"version": 2}' > "$tmp/setup.json"
    run >/dev/null
    t "console mode never prints a code" 0 "$(grep -c 'ABCDE23456' "$tmp/issue.d/50-awnix-console.issue")"
    t "console mode points at sudo awnix console code" 1 "$(grep -c 'sudo awnix console code' "$tmp/issue.d/50-awnix-console.issue")"
    rm -f "$tmp/cert.fp"
    run >/dev/null 2>&1
    t "no fingerprint -> exit 2" 2 "$?"
    if [ "$fails" -eq 0 ]; then echo "awnix-console-issue self-test: PASS"; return 0; fi
    echo "awnix-console-issue self-test: FAIL ($fails)"
    return 1
}

case "${1:-}" in
    --self-test) self_test; exit $? ;;
    --list-verbs) exit 0 ;;
    "") write_issue; exit $? ;;
    *) echo "usage: awnix-console-issue.sh [--self-test]" >&2; exit 2 ;;
esac
