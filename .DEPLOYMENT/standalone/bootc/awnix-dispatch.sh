#!/bin/sh
# awnix -- the one admin command of an awnix appliance (installed as /usr/bin/awnix).
#
# Contract `awnix-dispatcher-and-cli-conventions`: `awnix <verb> ...` execs
# /usr/libexec/awnix/awnix-<verb>, falling back to /usr/bin/awnix-<verb>. A gap adds a
# verb ONLY by dropping a file there; nobody edits this dispatcher. Verbs and owners:
#   update (updates)  component (components)  license (activation shim)
#   setup (installer-setup, /usr/bin/awnix-setup)  console (console-surfaces)
#   endpoints (portability)  doctor (console-surfaces aggregator)
#
#   awnix help | awnix --list-verbs | awnix --self-test | awnix <verb> [args...]
#
# Exit: whatever the verb exits (0 ok, 1 failed, 2 could not judge, 3 not entitled);
# 2 for an unknown verb. POSIX sh on purpose -- it must work before python does.
set -u

LIBEXEC="${AWNIX_LIBEXEC:-/usr/libexec/awnix}"
BINDIR="${AWNIX_BINDIR:-/usr/bin}"

# A verb is a lowercase word; helpers carry an extension (awnix-console-issue.sh) and
# are never offered as verbs.
is_verb_name() {
    case "$1" in
        ''|*[!a-z0-9-]*|-*) return 1 ;;
    esac
    return 0
}

list_verbs() {
    for f in "$LIBEXEC"/awnix-* "$BINDIR"/awnix-*; do
        [ -f "$f" ] && [ -x "$f" ] || continue
        v=${f##*/awnix-}
        is_verb_name "$v" || continue
        printf '%s\n' "$v"
    done | sort -u
}

resolve() {
    for d in "$LIBEXEC" "$BINDIR"; do
        if [ -f "$d/awnix-$1" ] && [ -x "$d/awnix-$1" ]; then
            printf '%s\n' "$d/awnix-$1"
            return 0
        fi
    done
    return 1
}

usage() {
    echo "usage: awnix <verb> [args...]"
    echo "       awnix help | --list-verbs | --self-test"
    echo
    echo "verbs on this machine:"
    verbs=$(list_verbs)
    if [ -n "$verbs" ]; then
        printf '%s\n' "$verbs" | sed 's/^/  /'
    else
        echo "  (none installed)"
    fi
    echo
    echo "Each verb has its own help: awnix <verb> --help. Man page: awnix(8)."
}

self_test() {
    tmp=$(mktemp -d 2>/dev/null || mktemp -d -t awnixdisp)
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/libexec" "$tmp/bin"
    printf '#!/bin/sh\necho "libexec-update $*"\nexit 0\n' > "$tmp/libexec/awnix-update"
    printf '#!/bin/sh\necho "bin-update"\nexit 0\n' > "$tmp/bin/awnix-update"
    printf '#!/bin/sh\necho "bin-setup $*"\nexit 3\n' > "$tmp/bin/awnix-setup"
    printf '#!/bin/sh\nexit 0\n' > "$tmp/libexec/awnix-console-issue.sh"
    printf 'not executable\n' > "$tmp/libexec/awnix-ghost"
    chmod +x "$tmp/libexec/awnix-update" "$tmp/bin/awnix-update" "$tmp/bin/awnix-setup" \
        "$tmp/libexec/awnix-console-issue.sh"
    fails=0
    t() {  # name, expected, actual
        if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (want '$2' got '$3')"; fails=$((fails + 1)); fi
    }
    self="$0"
    run() { AWNIX_LIBEXEC="$tmp/libexec" AWNIX_BINDIR="$tmp/bin" sh "$self" "$@"; }
    t "libexec wins over /usr/bin" "libexec-update check --json" "$(run update check --json)"
    out=$(run setup --status); rc=$?
    t "falls back to /usr/bin/awnix-<verb>" "bin-setup --status" "$out"
    t "verb exit code is passed through (3)" "3" "$rc"
    t "--list-verbs lists executables only, no helpers" "setup update" \
        "$(run --list-verbs | tr '\n' ' ' | sed 's/ $//')"
    run bogus >/dev/null 2>&1; t "unknown verb exits 2" "2" "$?"
    run '../etc' >/dev/null 2>&1; t "path-like verb refused (2)" "2" "$?"
    run help >/dev/null 2>&1; t "help exits 0" "0" "$?"
    if [ "$fails" -eq 0 ]; then echo "awnix dispatcher self-test: PASS"; return 0; fi
    echo "awnix dispatcher self-test: FAIL ($fails)"
    return 1
}

verb="${1:-help}"
case "$verb" in
    help|-h|--help)
        usage
        exit 0 ;;
    --list-verbs)
        list_verbs
        exit 0 ;;
    --self-test)
        self_test
        exit $? ;;
esac

if ! is_verb_name "$verb"; then
    echo "awnix: '$verb' is not a verb (awnix help)" >&2
    exit 2
fi
if ! target=$(resolve "$verb"); then
    echo "awnix: unknown verb '$verb' -- not installed on this machine (awnix help)" >&2
    exit 2
fi
shift
exec "$target" "$@"
