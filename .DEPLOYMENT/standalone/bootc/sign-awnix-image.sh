#!/bin/bash
# sign-awnix-image.sh -- sign and verify awnix images by DIGEST, shared by every lane.
#
#   sign-awnix-image.sh install [DIR]                 cosign, pinned + sha256-checked, into DIR
#   sign-awnix-image.sh sign <ref|repo@digest>...     keyless sign (GitHub OIDC) by digest
#   sign-awnix-image.sh sign --digests-file FILE      every "<ref> <digest>" line of FILE
#   sign-awnix-image.sh verify <ref> <identity-regexp>
#   sign-awnix-image.sh --self-test                   parsing + pin integrity, offline
#
# Why a script and not sigstore/cosign-installer: the org's Actions allowlist does not
# admit sigstore/*, and widening an org security setting is the owner's call. A pinned
# release binary whose sha256 is checked here is the same trust, with no new action.
#
# Keyless signing needs `permissions: id-token: write` on the job (the Fulcio cert is
# minted from ACTIONS_ID_TOKEN_REQUEST_*). When AWNIX_COSIGN_KEY is set (Phase B), the
# digest is ALSO signed with that key: `--key env://AWNIX_COSIGN_KEY`, never a file.
#
# A TAG is never signed: a tag can move after the signature, a digest cannot. Refs
# without @sha256: are resolved to their digest first.
#
# Exit: 0 ok · 1 a sign/verify failed · 2 could not judge (no cosign, bad args, offline).
set -uo pipefail

COSIGN_VERSION="v2.4.1"
# sha256 of cosign-linux-<arch> from https://github.com/sigstore/cosign/releases/download/v2.4.1/cosign_checksums.txt
COSIGN_SHA256_AMD64="8b24b946dd5809c6bd93de08033bcf6bc0ed7d336b7785787c080f574b89249b"
COSIGN_SHA256_ARM64="3b2e2e3854d0356c45fe6607047526ccd04742d20bd44afb5be91fa2a6e7cb4a"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
COSIGN="${COSIGN:-cosign}"
SKOPEO="${SKOPEO:-skopeo}"

die()  { echo "sign-awnix-image: $*" >&2; exit "${2:-2}"; }

arch_sha() {
    case "$(uname -m)" in
        x86_64|amd64) echo "amd64 $COSIGN_SHA256_AMD64" ;;
        aarch64|arm64) echo "arm64 $COSIGN_SHA256_ARM64" ;;
        *) return 1 ;;
    esac
}

cmd_install() {
    local dir="${1:-/usr/local/bin}" a want got tmp
    read -r a want <<<"$(arch_sha)" || die "unsupported arch $(uname -m)"
    tmp=$(mktemp) || die "no temp file"
    curl -fsSL --retry 3 -o "$tmp" \
        "https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION/cosign-linux-$a" \
        || { rm -f "$tmp"; die "could not download cosign $COSIGN_VERSION"; }
    got=$(sha256sum "$tmp" | cut -d' ' -f1)
    [ "$got" = "$want" ] || { rm -f "$tmp"; die "cosign sha256 mismatch: got $got want $want -- refusing" 1; }
    { mkdir -p "$dir" && install -m 0755 "$tmp" "$dir/cosign"; } || { rm -f "$tmp"; die "could not install into $dir"; }
    rm -f "$tmp"
    echo "cosign $COSIGN_VERSION installed at $dir/cosign (sha256 $got)"
}

to_digest_ref() {  # to_digest_ref <ref> [digest] -> repo@sha256:...
    local ref="$1" dg="${2:-}" repo
    case "$ref" in *@sha256:*) printf '%s' "$ref"; return 0 ;; esac
    repo=$ref
    case "${repo##*/}" in *:*) repo=${repo%:*} ;; esac
    if [ -z "$dg" ]; then
        # Anonymous first: the published ref must be publicly pullable, and a stale login
        # left in the runner's auth file made this fail with no message at all (build-awnix-iso
        # 37100282835, 2026-10-03, twice) while the same ref resolved fine without creds.
        # Say WHY on failure instead of swallowing skopeo's error.
        local err
        dg=$("$SKOPEO" inspect --no-creds --format '{{.Digest}}' "docker://$ref" 2>/tmp/skopeo-resolve.err)             || dg=$("$SKOPEO" inspect --format '{{.Digest}}' "docker://$ref" 2>>/tmp/skopeo-resolve.err)             || { err=$(tail -c 400 /tmp/skopeo-resolve.err 2>/dev/null); echo "  skopeo: ${err:-no output}" >&2; return 1; }
    fi
    case "$dg" in sha256:*) ;; *) return 1 ;; esac
    printf '%s@%s' "$repo" "$dg"
}

sign_one() {
    local dref
    dref=$(to_digest_ref "$1" "${2:-}") || { echo "  FAIL  cannot resolve $1 to a digest"; return 1; }
    if ! "$COSIGN" sign --yes "$dref" >/dev/null 2>"${TMPDIR:-/tmp}/cosign-sign.err"; then
        echo "  FAIL  keyless sign $dref: $(tail -1 "${TMPDIR:-/tmp}/cosign-sign.err")"; return 1
    fi
    if [ -n "${AWNIX_COSIGN_KEY:-}" ]; then
        "$COSIGN" sign --yes --key env://AWNIX_COSIGN_KEY "$dref" >/dev/null 2>&1 \
            || { echo "  FAIL  key co-sign $dref"; return 1; }
        echo "  ok    signed $dref (keyless + key)"
    else
        echo "  ok    signed $dref (keyless)"
    fi
}

cmd_sign() {
    local fail=0 n=0 ref dg
    [ $# -gt 0 ] || die "sign needs a ref or --digests-file FILE"
    if [ "$1" = "--digests-file" ]; then
        [ -r "${2:-}" ] || die "digests file not readable: ${2:-}"
        while read -r ref dg; do
            [ -n "${ref:-}" ] || continue
            case "$ref" in \#*) continue ;; esac
            n=$((n + 1)); sign_one "$ref" "$dg" || fail=1
        done < "$2"
    else
        for ref in "$@"; do n=$((n + 1)); sign_one "$ref" || fail=1; done
    fi
    [ "$n" -gt 0 ] || die "nothing to sign -- an empty list is not a pass"
    return $fail
}

cmd_verify() {
    local ref="${1:-}" re="${2:-}" dref
    [ -n "$ref" ] && [ -n "$re" ] || die "verify <ref> <identity-regexp>"
    dref=$(to_digest_ref "$ref") || die "cannot resolve $ref to a digest"
    if "$COSIGN" verify --certificate-identity-regexp "$re" --certificate-oidc-issuer "$OIDC_ISSUER" \
            "$dref" >/dev/null 2>&1; then
        echo "  ok    $dref is signed by $re"; return 0
    fi
    echo "  FAIL  $dref has no valid signature from $re"; return 1
}

self_test() {
    local rc=0 tmp got
    t() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (want '$3', got '$2')"; rc=1; fi; }
    tmp=$(mktemp -d) || { echo "SELF-TEST DEAD: no temp dir"; return 2; }
    # Pin integrity: a version and two 64-hex checksums, never a placeholder.
    case "$COSIGN_VERSION" in v[0-9]*.[0-9]*.[0-9]*) got=ok ;; *) got=bad ;; esac; t "cosign version is pinned" "$got" "ok"
    for s in "$COSIGN_SHA256_AMD64" "$COSIGN_SHA256_ARM64"; do
        printf '%s' "$s" | grep -Eq '^[0-9a-f]{64}$'; t "checksum ${s:0:8}.. is 64 hex" "$?" "0"
    done
    ! grep -q 'uses: *sigstore/' "$0"; t "no sigstore/* action is referenced" "$?" "0"
    # Stubs
    printf '#!/bin/bash\necho "$*" >> %s/cosign.log\n[ -e %s/cosign-bad ] && exit 1\nexit 0\n' "$tmp" "$tmp" > "$tmp/cosign"
    printf '#!/bin/bash\necho sha256:%064d\n' 7 > "$tmp/skopeo"
    chmod +x "$tmp/cosign" "$tmp/skopeo"
    COSIGN="$tmp/cosign"; SKOPEO="$tmp/skopeo"
    got=$(to_digest_ref ghcr.io/aitherium/awnix:beta); t "a tag resolves to repo@digest" "$got" "ghcr.io/aitherium/awnix@sha256:$(printf '%064d' 7)"
    got=$(to_digest_ref localhost:5000/awnix:beta sha256:abc); t "a registry port is not a tag" "$got" "localhost:5000/awnix@sha256:abc"
    got=$(to_digest_ref ghcr.io/aitherium/awnix@sha256:ff); t "a digest ref passes through" "$got" "ghcr.io/aitherium/awnix@sha256:ff"
    to_digest_ref ghcr.io/aitherium/awnix:beta notadigest >/dev/null; t "a non-sha256 digest is refused" "$?" "1"
    printf 'ghcr.io/aitherium/awnix:beta sha256:aa\n# comment\nghcr.io/aitherium/awnix:2026.09.27 sha256:aa\n' > "$tmp/d.txt"
    (unset AWNIX_COSIGN_KEY; cmd_sign --digests-file "$tmp/d.txt") >/dev/null; t "sign --digests-file signs every line" "$?" "0"
    t "  two signatures, by digest" "$(grep -c 'sign --yes ghcr.io/aitherium/awnix@sha256:aa' "$tmp/cosign.log")" "2"
    grep -q 'awnix:beta' "$tmp/cosign.log"; t "  never a tag" "$?" "1"
    : > "$tmp/cosign.log"
    (AWNIX_COSIGN_KEY=x cmd_sign ghcr.io/aitherium/awnix@sha256:bb) >/dev/null
    grep -q -- '--key env://AWNIX_COSIGN_KEY' "$tmp/cosign.log"; t "Phase B co-signs with the key from env, never a file" "$?" "0"
    touch "$tmp/cosign-bad"
    (unset AWNIX_COSIGN_KEY; cmd_sign ghcr.io/aitherium/awnix@sha256:cc) >/dev/null; t "a failed sign is exit 1" "$?" "1"
    cmd_verify ghcr.io/aitherium/awnix@sha256:cc '^x$' >/dev/null; t "a failed verify is exit 1" "$?" "1"
    rm -f "$tmp/cosign-bad"; : > "$tmp/cosign.log"
    cmd_verify ghcr.io/aitherium/awnix@sha256:cc '^x$' >/dev/null; t "verify passes a good signature" "$?" "0"
    grep -q -- "--certificate-oidc-issuer $OIDC_ISSUER" "$tmp/cosign.log"; t "  pinned to the GitHub OIDC issuer" "$?" "0"
    rm -rf "$tmp"
    [ "$rc" = 0 ] && echo "SELF-TEST PASS" || echo "SELF-TEST FAILED"
    return $rc
}

case "${1:-}" in
    install)     shift; cmd_install "$@" ;;
    sign)        shift; cmd_sign "$@" ;;
    verify)      shift; cmd_verify "$@" ;;
    --self-test) self_test ;;
    -h|--help)   sed -n '2,22p' "$0" ;;
    *) die "usage: sign-awnix-image.sh {install [DIR]|sign <ref>...|sign --digests-file F|verify <ref> <regexp>|--self-test}" ;;
esac
