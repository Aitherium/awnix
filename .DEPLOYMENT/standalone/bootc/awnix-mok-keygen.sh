#!/bin/sh
# awnix-mok-keygen.sh -- mint the awnix kernel-module signing key pair (MOK).
#
# BUILD HOST / CI ONLY. The private key is a release credential: it goes into the vault
# as AWNIX_MOK_SIGNING_KEY and is mounted into image builds as the build secret
# id=awnix-mok-key. It must never exist on a booted awnix box, so this script refuses to
# run on one (/run/ostree-booted).
#
# Usage: awnix-mok-keygen.sh --out DIR [--cn "awnix MOK kmod signing"] [--days 3650]
#        awnix-mok-keygen.sh --self-test
# Writes DIR/awnix-mok.key (0600, PEM private key), DIR/awnix-mok.der (public cert, DER)
# and DIR/awnix-mok.pem (public cert, PEM). Only the .der is committed to the image.
# Exit: 0 ok, 1 refused, 2 tool missing / bad usage (--self-test: 2 = could not judge).
set -eu

OSTREE_MARKER="${AWNIX_OSTREE_MARKER:-/run/ostree-booted}"
CN="awnix MOK kmod signing"
DAYS=3650
OUT=""

refuse_on_awnix() {
    if [ -e "$OSTREE_MARKER" ]; then
        echo "awnix-mok-keygen: refusing: this is a booted awnix/ostree system ($OSTREE_MARKER)." >&2
        echo "awnix-mok-keygen: the signing key must never exist on a box; mint it on a build host." >&2
        exit 1
    fi
}

gen() {
    out="$1"
    command -v openssl >/dev/null 2>&1 || { echo "awnix-mok-keygen: openssl not found" >&2; exit 2; }
    umask 077
    mkdir -p "$out"
    if [ -e "$out/awnix-mok.key" ]; then
        echo "awnix-mok-keygen: $out/awnix-mok.key exists; refusing to overwrite a signing key" >&2
        exit 1
    fi
    cfg="$out/.mok.cnf"
    cat > "$cfg" <<EOF
[ req ]
distinguished_name = dn
prompt = no
x509_extensions = ext
[ dn ]
CN = $CN
O = Aitherium
[ ext ]
basicConstraints = critical,CA:FALSE
keyUsage = digitalSignature
extendedKeyUsage = codeSigning, 1.3.6.1.4.1.2312.16.1.2
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
    openssl req -new -x509 -newkey rsa:2048 -nodes -sha256 -days "$DAYS" \
        -config "$cfg" -keyout "$out/awnix-mok.key" -out "$out/awnix-mok.pem" >/dev/null 2>&1
    openssl x509 -in "$out/awnix-mok.pem" -outform DER -out "$out/awnix-mok.der"
    rm -f "$cfg"
    chmod 600 "$out/awnix-mok.key"
    chmod 644 "$out/awnix-mok.der" "$out/awnix-mok.pem"
    fp=$(openssl x509 -in "$out/awnix-mok.pem" -noout -fingerprint -sha256 | sed 's/.*=//')
    echo "awnix-mok-keygen: wrote $out/awnix-mok.der (SHA256 $fp)"
    echo "awnix-mok-keygen: store $out/awnix-mok.key in the vault as AWNIX_MOK_SIGNING_KEY, then delete it here"
}

self_test() {
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    fails=0
    : > "$tmp/ostree-booted"
    if AWNIX_OSTREE_MARKER="$tmp/ostree-booted" sh "$0" --out "$tmp/k1" >/dev/null 2>&1; then
        echo "SELF-TEST FAIL: ran on a booted awnix marker"; fails=$((fails + 1))
    fi
    [ -e "$tmp/k1/awnix-mok.key" ] && { echo "SELF-TEST FAIL: key written on refusal"; fails=$((fails + 1)); }
    if command -v openssl >/dev/null 2>&1; then
        if ! AWNIX_OSTREE_MARKER="$tmp/none" sh "$0" --out "$tmp/k2" >/dev/null 2>&1; then
            echo "SELF-TEST FAIL: keygen failed on a build host"; fails=$((fails + 1))
        else
            [ -s "$tmp/k2/awnix-mok.der" ] || { echo "SELF-TEST FAIL: no der"; fails=$((fails + 1)); }
            openssl x509 -inform DER -in "$tmp/k2/awnix-mok.der" -noout -text 2>/dev/null \
                | grep -q "1.3.6.1.4.1.2312.16.1.2" \
                || { echo "SELF-TEST FAIL: module-signing EKU missing"; fails=$((fails + 1)); }
            if AWNIX_OSTREE_MARKER="$tmp/none" sh "$0" --out "$tmp/k2" >/dev/null 2>&1; then
                echo "SELF-TEST FAIL: overwrote an existing key"; fails=$((fails + 1))
            fi
        fi
    else
        # Never 0 on silence: without openssl the EKU claim is not judged.
        echo "awnix-mok-keygen self-test: openssl absent, cannot judge the generation leg"
        echo "awnix-mok-keygen self-test: $fails failure(s), generation leg UNJUDGED"
        [ "$fails" -eq 0 ] || exit 1
        exit 2
    fi
    echo "awnix-mok-keygen self-test: $fails failure(s)"
    [ "$fails" -eq 0 ]
}

while [ $# -gt 0 ]; do
    case "$1" in
        --self-test) self_test; exit $? ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --cn) CN="${2:-}"; shift 2 ;;
        --days) DAYS="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "awnix-mok-keygen: unknown argument $1" >&2; exit 2 ;;
    esac
done

refuse_on_awnix
[ -n "$OUT" ] || { echo "awnix-mok-keygen: --out DIR is required" >&2; exit 2; }
gen "$OUT"
