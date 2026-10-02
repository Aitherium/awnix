#!/bin/sh
# sbom-seal-awnix-image.sh -- SPDX SBOM + cosign attestation + awseal seal for ONE
# awnix/garg image digest (AFRL proof plan G9, Step 0).
#
#   sbom-seal-awnix-image.sh <repo@sha256:D> [--out DIR] [--no-attest | --require-attest]
#   sbom-seal-awnix-image.sh normalize-digests FILE...
#   sbom-seal-awnix-image.sh --verify DIR --key HEX [--ref repo@sha256:D]
#   sbom-seal-awnix-image.sh install-syft [DIR]
#   sbom-seal-awnix-image.sh --self-test
#
# What it produces (DIR, default ./sbom-seal/<name>-<digest12>):
#   sbom.spdx.json  syft SPDX-JSON of the image AT THAT DIGEST (never a tag)
#   digest.txt      ref=, repo=, digest=, syft=, cosign_attest=, created= (key=value)
#   awseal.json     Ed25519 seal over the two files above (awseal, AitherOS/packages/awseal)
#
# The seal binds the SBOM to the digest: change either byte and `awseal verify` fails.
# The cosign attestation (`--type spdxjson`) attaches the same SBOM to the image in
# the registry, keyless via GitHub OIDC (needs `id-token: write`). cosign itself is
# installed by sign-awnix-image.sh (wave-1 "updates"); this script adds NO second
# installer and uses $COSIGN_BIN, then $COSIGN, then `cosign` on PATH. If no cosign is
# found the attestation is skipped and digest.txt says so -- the seal still happens --
# UNLESS --require-attest is given, which makes a missing cosign exit 2 (never a green
# run that silently lacks the attestation). NOTE: keyless attest writes an entry to the
# PUBLIC Sigstore Rekor log; callers opt in explicitly (awnix-sbom-seal.yml gates it
# behind the repo variable AWNIX_ATTEST_PUBLIC_REKOR).
#
# normalize-digests reads the files publish-awnix-images.sh --digests-out writes, in
# EITHER form -- 'repo@sha256:<64hex>' or '<repo[:tag]> sha256:<64hex>' (one line per
# tag pushed) -- and prints each distinct 'repo@sha256:..' once. Any other non-comment
# line exits 1 (a tag alone is never sealed); no digest at all exits 1 too.
#
# syft is pinned: SYFT_VERSION + the release tarball's sha256 below. A syft on PATH
# (or $SYFT) is used only if it reports exactly SYFT_VERSION; otherwise the pinned
# tarball is fetched and checked. Never anchore/syft:latest, never an anchore/* action
# (the org Actions allowlist admits only actions/github/docker).
#
# Key: $AWSEAL_KEY_PATH, a 0600 PEM file the CI job writes from the AWSEAL_SIGNING_KEY
# secret. It is never echoed. If .DEPLOYMENT/standalone/bootc/awseal-image.pub exists
# (or $AWSEAL_EXPECT_KEY is set) the key must match it, so a stray key cannot mint a
# seal that looks official. AWSEAL_REQUIRE_PUBKEY=1 (set by CI) makes an ABSENT pub file
# exit 2 instead of accepting whatever key is present.
#
# Exit: 0 ok · 1 verification failed / key mismatch · 2 tool or credential missing,
# bad arguments, could not judge.
set -u

SYFT_VERSION="1.52.0"
# sha256 of syft_<v>_linux_<arch>.tar.gz from
# https://github.com/anchore/syft/releases/download/v1.52.0/syft_1.52.0_checksums.txt
SYFT_SHA256_AMD64="caeedb81fb0491615f1ebd1761e4145d41ee86dd2cc7bf80669f9f5ad9d6133d"
SYFT_SHA256_ARM64="c46d5e4c28e12aa4c5becfaa343ef1c7f89045b6b895f2c21d471c62db09c706"

HERE=$(cd "$(dirname "$0")" && pwd)
SELF="$HERE/$(basename "$0")"
PUBKEY_FILE="${AWSEAL_PUBKEY_FILE:-$HERE/awseal-image.pub}"
SYFT_DIR="${SYFT_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/awnix-syft-$SYFT_VERSION}"

say() { echo "sbom-seal: $*" >&2; }
die() { say "$1"; exit "${2:-2}"; }

# ------------------------------------------------------------------ tools

py_with_awseal() {
    for p in ${AWSEAL_PYTHON:-} python3.11 python3 python; do
        [ -n "$p" ] || continue
        if "$p" -c 'import awseal' >/dev/null 2>&1; then command -v "$p"; return 0; fi
    done
    return 1
}

awseal_run() {  # awseal_run <args...>
    if [ -n "${AWSEAL:-}" ]; then "$AWSEAL" "$@"; return $?; fi
    _py=$(py_with_awseal) || { say "awseal is not importable (pip install AitherOS/packages/awseal)"; return 2; }
    "$_py" -m awseal.cli "$@"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

syft_arch_sha() {
    case "$(uname -m)" in
        x86_64|amd64) echo "amd64 $SYFT_SHA256_AMD64" ;;
        aarch64|arm64) echo "arm64 $SYFT_SHA256_ARM64" ;;
        *) return 1 ;;
    esac
}

syft_is_pinned() {  # syft_is_pinned <bin>
    "$1" version 2>/dev/null | grep -Eq "^Version:[[:space:]]+v?$SYFT_VERSION\$"
}

cmd_install_syft() {
    dir="${1:-$SYFT_DIR}"
    pair=$(syft_arch_sha) || die "unsupported arch $(uname -m)"
    arch=${pair%% *}; want=${pair#* }
    command -v curl >/dev/null 2>&1 || die "curl missing"
    mkdir -p "$dir" || die "cannot create $dir"
    tgz="$dir/syft.tar.gz"
    curl -fsSL --retry 3 -o "$tgz" \
        "https://github.com/anchore/syft/releases/download/v$SYFT_VERSION/syft_${SYFT_VERSION}_linux_${arch}.tar.gz" \
        || { rm -f "$tgz"; die "could not download syft $SYFT_VERSION"; }
    got=$(sha256_of "$tgz")
    [ "$got" = "$want" ] || { rm -f "$tgz"; die "syft sha256 mismatch: got $got want $want -- refusing" 1; }
    tar -xzf "$tgz" -C "$dir" syft || { rm -f "$tgz"; die "could not unpack syft"; }
    rm -f "$tgz"
    chmod 0755 "$dir/syft"
    say "syft $SYFT_VERSION installed at $dir/syft (tarball sha256 $got)"
    echo "$dir/syft"
}

resolve_syft() {
    for cand in "${SYFT:-}" "$SYFT_DIR/syft" "$(command -v syft 2>/dev/null || true)"; do
        [ -n "$cand" ] && [ -x "$cand" ] || continue
        if syft_is_pinned "$cand"; then echo "$cand"; return 0; fi
        say "ignoring $cand: not syft $SYFT_VERSION"
    done
    [ -n "${SYFT:-}" ] && return 1          # an explicit SYFT that is not pinned is refused
    cmd_install_syft "$SYFT_DIR" | tail -1
}

resolve_cosign() {
    for cand in "${COSIGN_BIN:-}" "${COSIGN:-}" cosign; do
        [ -n "$cand" ] || continue
        if command -v "$cand" >/dev/null 2>&1; then command -v "$cand"; return 0; fi
    done
    return 1
}

# ------------------------------------------------------------------ seal

valid_ref() {
    printf '%s' "$1" | grep -Eq '^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$'
}

cmd_seal() {
    ref=""; out=""; attest=1; need_attest=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --out) out="${2:-}"; shift 2 ;;
            --no-attest) attest=0; shift ;;
            --require-attest) need_attest=1; shift ;;
            -*) die "unknown flag $1" ;;
            *) [ -z "$ref" ] || die "one ref per call"; ref=$1; shift ;;
        esac
    done
    [ -n "$ref" ] || die "usage: $(basename "$0") <repo@sha256:D> [--out DIR] [--no-attest | --require-attest]"
    [ "$attest$need_attest" = 01 ] && die "--no-attest and --require-attest contradict"
    valid_ref "$ref" || die "not a digest ref (repo@sha256:<64hex>): $ref -- a tag is never sealed"
    repo=${ref%@*}; digest=${ref#*@}
    name=$(basename "$repo")
    [ -n "$out" ] || out="./sbom-seal/${name}-$(printf '%s' "${digest#sha256:}" | cut -c1-12)"

    [ -n "${AWSEAL_KEY_PATH:-}" ] && [ -r "$AWSEAL_KEY_PATH" ] \
        || die "AWSEAL_KEY_PATH is unset or unreadable -- refusing to produce an unsealed SBOM"
    # Two steps, not a pipe: `a | tail -1` returns tail's status and would hide a failure.
    pubout=$(awseal_run pubkey --path "$AWSEAL_KEY_PATH" 2>/dev/null) \
        || die "cannot read the awseal public key from AWSEAL_KEY_PATH"
    pub=$(printf '%s\n' "$pubout" | tail -1)
    [ -n "$pub" ] || die "cannot read the awseal public key from AWSEAL_KEY_PATH (empty)"
    expect="${AWSEAL_EXPECT_KEY:-}"
    [ -z "$expect" ] && [ -r "$PUBKEY_FILE" ] && expect=$(tr -d ' \r\n' < "$PUBKEY_FILE")
    if [ -z "$expect" ] && [ "${AWSEAL_REQUIRE_PUBKEY:-0}" = 1 ]; then
        die "AWSEAL_REQUIRE_PUBKEY=1 but $PUBKEY_FILE is absent -- no trust anchor, refusing"
    fi
    if [ -n "$expect" ] && [ "$pub" != "$expect" ]; then
        die "signing key does not match the published awseal-image.pub -- refusing" 1
    fi

    syft=$(resolve_syft) || die "no pinned syft $SYFT_VERSION available"
    [ -n "$syft" ] && [ -x "$syft" ] || die "no pinned syft $SYFT_VERSION available"

    mkdir -p "$out" || die "cannot create $out"
    rm -f "$out/sbom.spdx.json" "$out/digest.txt" "$out/awseal.json"
    say "syft $SYFT_VERSION scan registry:$ref"
    if ! "$syft" scan "registry:$ref" -q -o "spdx-json=$out/sbom.spdx.json"; then
        die "syft scan failed for $ref (registry auth? SYFT_REGISTRY_AUTH_*)"
    fi
    [ -s "$out/sbom.spdx.json" ] || die "syft wrote an empty SBOM -- not a pass"
    grep -q '"spdxVersion"' "$out/sbom.spdx.json" || die "SBOM is not SPDX JSON"
    npkg=$(grep -c '"SPDXID": *"SPDXRef-Package' "$out/sbom.spdx.json" || true)
    [ "${npkg:-0}" -gt 0 ] || die "SBOM lists zero packages -- the scan saw nothing"

    att="skipped-by-flag"
    if [ "$attest" = 1 ]; then
        if cos=$(resolve_cosign); then
            if "$cos" attest --yes --type spdxjson --predicate "$out/sbom.spdx.json" "$ref" >/dev/null 2>&1; then
                att="ok"
            else
                die "cosign attest failed for $ref (id-token: write? registry auth?)"
            fi
        else
            [ "$need_attest" = 1 ] && die "--require-attest but no cosign found (COSIGN_BIN/COSIGN/PATH)"
            att="skipped-no-cosign"
            say "no cosign found (COSIGN_BIN/COSIGN/PATH): attestation skipped, seal continues"
        fi
    fi

    {
        echo "ref=$ref"
        echo "repo=$repo"
        echo "digest=$digest"
        echo "syft=$SYFT_VERSION"
        echo "spdx_packages=$npkg"
        echo "cosign_attest=$att"
        echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$out/digest.txt"

    awseal_run sign "$out" --subject "awnix-image:$digest" --key-path "$AWSEAL_KEY_PATH" >/dev/null \
        || die "awseal sign failed"
    if ! awseal_run verify "$out" --key "$pub" >/dev/null; then
        die "a freshly written seal does not verify -- refusing" 1
    fi
    echo "SBOM-SEAL: ok ref=$ref packages=$npkg attest=$att pubkey=$pub dir=$out"
    return 0
}

cmd_verify() {
    dir=""; key=""; ref=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --key) key="${2:-}"; shift 2 ;;
            --ref) ref="${2:-}"; shift 2 ;;
            *) [ -z "$dir" ] || die "one DIR"; dir=$1; shift ;;
        esac
    done
    [ -n "$dir" ] && [ -n "$key" ] || die "usage: --verify DIR --key HEX [--ref repo@sha256:D]"
    [ -d "$dir" ] || die "no such dir: $dir"
    for f in digest.txt sbom.spdx.json awseal.json; do
        [ -s "$dir/$f" ] || die "$dir/$f missing or empty"
    done
    awseal_run verify "$dir" --key "$key"
    rc=$?
    [ "$rc" = 0 ] || { echo "SBOM-SEAL-VERIFY: FAIL rc=$rc dir=$dir"; return "$rc"; }
    if [ -n "$ref" ]; then
        grep -qx "ref=$ref" "$dir/digest.txt" \
            || { echo "SBOM-SEAL-VERIFY: FAIL seal is for $(grep '^ref=' "$dir/digest.txt"), not $ref"; return 1; }
    fi
    echo "SBOM-SEAL-VERIFY: ok dir=$dir $(grep '^ref=' "$dir/digest.txt")"
    return 0
}

cmd_normalize() {  # normalize-digests FILE... -> distinct repo@sha256:.. lines on stdout
    [ $# -gt 0 ] || die "usage: normalize-digests FILE..."
    for f in "$@"; do [ -r "$f" ] || die "cannot read $f"; done
    cat "$@" | tr -d '\r' | awk '
        function repo_of(r,   i, last) {        # strip a :tag from the LAST path element only
            i = match(r, /\/[^\/]*$/); last = i ? substr(r, i) : r
            if (index(last, ":")) r = substr(r, 1, length(r) - length(last) + index(last, ":") - 1)
            return r
        }
        function emit(k) { if (!(k in seen)) { seen[k] = 1; print k }; n++ }
        /^[ \t]*(#|$)/ { next }
        NF == 1 && $1 ~ /^[a-z0-9][a-z0-9._:\/-]*@sha256:[0-9a-f]+$/ && length(substr($1, index($1, "@sha256:") + 8)) == 64 {
            at = index($1, "@"); emit(repo_of(substr($1, 1, at - 1)) substr($1, at)); next
        }
        NF == 2 && $1 ~ /^[a-z0-9][a-z0-9._:\/-]*$/ && $2 ~ /^sha256:[0-9a-f]+$/ && length($2) == 71 {
            emit(repo_of($1) "@" $2); next
        }
        { print "sbom-seal: not a digest line (a tag is never sealed): " $0 > "/dev/stderr"; bad = 1 }
        END {
            if (bad) exit 1
            if (!n) { print "sbom-seal: no digest lines -- an empty list is not a pass" > "/dev/stderr"; exit 1 }
        }'
}

# ------------------------------------------------------------------ self-test

self_test() {
    rc=0
    t() { if [ "$2" = "$3" ]; then echo "  PASS $1"; else echo "  FAIL $1 (want '$3', got '$2')"; rc=1; fi; }
    py=$(py_with_awseal) || { echo "SELF-TEST COULD NOT JUDGE: awseal not importable"; return 2; }
    tmp=$(mktemp -d) || { echo "SELF-TEST COULD NOT JUDGE: no temp dir"; return 2; }

    # Pin integrity, and the script is LF-only (the garg CRLF lesson).
    printf '%s' "$SYFT_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; t "syft version is pinned" "$?" 0
    for s in "$SYFT_SHA256_AMD64" "$SYFT_SHA256_ARM64"; do
        printf '%s' "$s" | grep -Eq '^[0-9a-f]{64}$'; t "syft checksum $(printf %s "$s" | cut -c1-8).. is 64 hex" "$?" 0
    done
    n=$(tr -cd '\r' < "$SELF" | wc -c | tr -d ' '); t "no CR byte in this script" "$n" 0

    # Stubs: a syft that reports the pinned version and writes a 1-package SPDX, a
    # cosign that records its argv, and a throwaway awseal key.
    mkdir -p "$tmp/bin"
    cat > "$tmp/bin/syft" <<EOF
#!/bin/sh
if [ "\$1" = version ]; then echo "Version:           \${STUB_SYFT_VERSION:-$SYFT_VERSION}"; exit 0; fi
for a in "\$@"; do case "\$a" in spdx-json=*) o=\${a#spdx-json=} ;; esac; done
if [ "\${STUB_SYFT_EMPTY:-0}" = 1 ]; then : > "\$o"; exit 0; fi
printf '{"spdxVersion":"SPDX-2.3","packages":[{"SPDXID": "SPDXRef-Package-rpm-bash","name":"bash"}]}\n' > "\$o"
EOF
    cat > "$tmp/bin/cosign" <<EOF
#!/bin/sh
echo "\$*" >> "$tmp/cosign.args"
EOF
    chmod +x "$tmp/bin/syft" "$tmp/bin/cosign"
    "$py" -m awseal.cli keygen --path "$tmp/k.pem" >/dev/null 2>&1
    "$py" -m awseal.cli keygen --path "$tmp/other.pem" >/dev/null 2>&1
    pub=$("$py" -m awseal.cli pubkey --path "$tmp/k.pem" | tail -1)
    other=$("$py" -m awseal.cli pubkey --path "$tmp/other.pem" | tail -1)
    ref="ghcr.io/aitherium/awnix@sha256:$(printf 'a%.0s' $(seq 1 64))"

    run() {  # run <env assignments...> -- <args>
        ( export SYFT="$tmp/bin/syft" COSIGN_BIN="$tmp/bin/cosign" AWSEAL_PYTHON="$py" \
                 AWSEAL_PUBKEY_FILE="$tmp/none.pub" AWSEAL_KEY_PATH="$tmp/k.pem"
          while [ "$1" != "--" ]; do export "$1"; shift; done; shift
          sh "$SELF" "$@" ) >"$tmp/last.log" 2>&1
        echo $?
    }

    t "seal a digest ref" "$(run -- "$ref" --out "$tmp/s1")" 0
    for f in digest.txt sbom.spdx.json awseal.json; do [ -s "$tmp/s1/$f" ]; t "  wrote $f" "$?" 0; done
    grep -q "attest --yes --type spdxjson --predicate $tmp/s1/sbom.spdx.json $ref" "$tmp/cosign.args" 2>/dev/null
    t "  cosign attest --type spdxjson on the digest" "$?" 0
    grep -qx "cosign_attest=ok" "$tmp/s1/digest.txt"; t "  digest.txt records the attestation" "$?" 0
    t "verify with the publisher key" "$(run -- --verify "$tmp/s1" --key "$pub" --ref "$ref")" 0
    t "verify with a foreign key fails" "$(run -- --verify "$tmp/s1" --key "$other")" 1
    t "verify against a different ref fails" \
        "$(run -- --verify "$tmp/s1" --key "$pub" --ref "${ref%?}b")" 1
    printf ' ' >> "$tmp/s1/sbom.spdx.json"
    t "a tampered SBOM fails verify (negative control)" "$(run -- --verify "$tmp/s1" --key "$pub")" 1
    t "a tag is refused" "$(run -- ghcr.io/aitherium/awnix:stable --out "$tmp/s2")" 2
    t "no signing key is refused" "$(run AWSEAL_KEY_PATH="$tmp/missing.pem" -- "$ref" --out "$tmp/s3")" 2
    t "a key that is not the published one is refused" \
        "$(run AWSEAL_EXPECT_KEY="$other" -- "$ref" --out "$tmp/s4")" 1
    t "an empty SBOM is refused" "$(run STUB_SYFT_EMPTY=1 -- "$ref" --out "$tmp/s5")" 2
    t "an unpinned syft is refused" "$(run STUB_SYFT_VERSION=9.9.9 -- "$ref" --out "$tmp/s6")" 2
    : > "$tmp/cosign.args"
    t "--no-attest seals without cosign" "$(run -- "$ref" --out "$tmp/s7" --no-attest)" 0
    [ -s "$tmp/cosign.args" ]; t "  and cosign was not called" "$?" 1
    t "no cosign anywhere still seals" "$(run COSIGN_BIN="$tmp/nope" COSIGN="$tmp/nope" PATH="/usr/bin:/bin" -- "$ref" --out "$tmp/s8")" 0
    grep -qx "cosign_attest=skipped-no-cosign" "$tmp/s8/digest.txt" 2>/dev/null; t "  and says the attestation was skipped" "$?" 0
    t "verify of an unsealed dir is could-not-judge" "$(run -- --verify "$tmp/s2" --key "$pub")" 2
    t "--require-attest with no cosign is refused" \
        "$(run COSIGN_BIN="$tmp/nope" COSIGN="$tmp/nope" PATH="/usr/bin:/bin" -- "$ref" --out "$tmp/s9" --require-attest)" 2
    t "AWSEAL_REQUIRE_PUBKEY=1 with no pub file is refused" \
        "$(run AWSEAL_REQUIRE_PUBKEY=1 -- "$ref" --out "$tmp/s10")" 2
    printf '%s\n' "$pub" > "$tmp/good.pub"
    t "AWSEAL_REQUIRE_PUBKEY=1 with the matching pub file seals" \
        "$(run AWSEAL_REQUIRE_PUBKEY=1 AWSEAL_PUBKEY_FILE="$tmp/good.pub" -- "$ref" --out "$tmp/s11")" 0
    printf '#!/bin/sh\nexit 3\n' > "$tmp/bin/badseal"; chmod +x "$tmp/bin/badseal"
    t "a failing awseal pubkey is exit 2, not masked by the pipe" \
        "$(run AWSEAL="$tmp/bin/badseal" -- "$ref" --out "$tmp/s12")" 2
    grep -q "cannot read the awseal public key" "$tmp/last.log"; t "  and says why" "$?" 0

    # normalize-digests: both producer forms, dedupe, tags and junk refused.
    d64=$(printf 'c%.0s' $(seq 1 64)); e64=$(printf 'e%.0s' $(seq 1 64))
    printf '# pushed\nghcr.io/aitherium/awnix:beta sha256:%s\nghcr.io/aitherium/awnix:2026-09-28 sha256:%s\r\nghcr.io/aitherium/awnix@sha256:%s\nlocalhost:5000/garg:1 sha256:%s\n\n' \
        "$d64" "$d64" "$d64" "$e64" > "$tmp/dg1.txt"
    got=$(sh "$SELF" normalize-digests "$tmp/dg1.txt" 2>/dev/null); t "normalize: mixed forms exit 0" "$?" 0
    t "  deduped to 2 lines" "$(printf '%s\n' "$got" | wc -l | tr -d ' ')" 2
    printf '%s\n' "$got" | grep -qx "ghcr.io/aitherium/awnix@sha256:$d64"; t "  tag stripped to repo@digest" "$?" 0
    printf '%s\n' "$got" | grep -qx "localhost:5000/garg@sha256:$e64"; t "  registry port kept, tag stripped" "$?" 0
    printf 'ghcr.io/aitherium/awnix:beta\n' > "$tmp/dg2.txt"
    sh "$SELF" normalize-digests "$tmp/dg2.txt" >/dev/null 2>&1; t "normalize: a bare tag exits 1" "$?" 1
    printf '# nothing\n\n' > "$tmp/dg3.txt"
    sh "$SELF" normalize-digests "$tmp/dg3.txt" >/dev/null 2>&1; t "normalize: no digests exits 1" "$?" 1
    printf 'ghcr.io/aitherium/awnix sha256:abc\n' > "$tmp/dg4.txt"
    sh "$SELF" normalize-digests "$tmp/dg4.txt" >/dev/null 2>&1; t "normalize: a short digest exits 1" "$?" 1

    rm -rf "$tmp"
    echo
    [ "$rc" = 0 ] && echo "self-test: PASS" || echo "self-test: FAIL"
    return "$rc"
}

case "${1:-}" in
    --self-test) self_test; exit $? ;;
    --verify) shift; cmd_verify "$@"; exit $? ;;
    install-syft) shift; cmd_install_syft "$@" >/dev/null; exit $? ;;
    normalize-digests) shift; cmd_normalize "$@"; exit $? ;;
    ""|-h|--help) sed -n '2,44p' "$SELF"; exit 2 ;;
    *) cmd_seal "$@"; exit $? ;;
esac
