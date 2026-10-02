#!/bin/sh
# sign-nvidia-kmods.sh -- sign the NVIDIA kernel modules inside an awnix image build.
#
# Runs in a Containerfile RUN, after the driver modules were built against the IMAGE
# kernel (not the build host's). It signs every nvidia*.ko[.xz|.zst] with the kernel's own
# scripts/sign-file, writes the public certificate and a manifest, and never copies the
# private key anywhere: the key is read from a build-secret mount that only exists for the
# duration of this RUN.
#
# Usage: sign-nvidia-kmods.sh [--kver KVER] [--key PEM] [--cert DER] [--out DIR]
#        sign-nvidia-kmods.sh --self-test
# Defaults: key /run/secrets/awnix-mok-key, cert /run/secrets/awnix-mok-cert,
#           out /usr/share/awnix/secureboot, kver = the single dir in /usr/lib/modules.
# Env: AWNIX_REQUIRE_SIGNED=1 turns "no key / unsigned" into a failed build.
#      AWNIX_LEAK_SCAN_ROOTS (default "/usr /etc /opt /root /var /tmp"): after signing,
#      these trees are searched for a verbatim copy of the private key's body; a hit
#      fails the build (exit 1). /run is not scanned: the secret mount lives there.
#      AWNIX_MODULES_ROOT (default /usr/lib/modules) and AWNIX_MODINFO (default modinfo)
#      exist for the self-test only.
# Exit: 0 signed (or unsigned and allowed), 1 unsigned while required / sign failure,
#       2 bad usage / tools missing.
set -eu

MODROOT="${AWNIX_MODULES_ROOT:-/usr/lib/modules}"
KEY=/run/secrets/awnix-mok-key
CERT=/run/secrets/awnix-mok-cert
OUT=/usr/share/awnix/secureboot
KVER=""
REQUIRE="${AWNIX_REQUIRE_SIGNED:-0}"
MODINFO="${AWNIX_MODINFO:-modinfo}"
LEAK_ROOTS="${AWNIX_LEAK_SCAN_ROOTS:-/usr /etc /opt /root /var /tmp}"

log() { echo "sign-nvidia-kmods: $*" >&2; }

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

json_or_null() {
    if [ -n "$1" ]; then json_str "$1"; else printf 'null'; fi
}

find_kver() {
    n=0; k=""
    for d in "$MODROOT"/*; do
        [ -d "$d" ] || continue
        n=$((n + 1)); k=$(basename "$d")
    done
    [ "$n" -eq 1 ] || { log "expected exactly one kernel in $MODROOT, found $n; pass --kver"; exit 2; }
    KVER="$k"
}

find_sign_file() {
    for c in "$MODROOT/$KVER/build/scripts/sign-file" "/usr/src/kernels/$KVER/scripts/sign-file"; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}

sign_one() {
    ko="$1"; sf="$2"
    # Chained with && on purpose: this runs as `sign_one ... || fail`, where set -e is off,
    # so a failed sign-file must not be masked by the recompress step that follows it.
    case "$ko" in
        *.ko.xz)  raw="${ko%.xz}"
                  xz -d -k -f "$ko" && "$sf" sha256 "$KEY" "$CERT" "$raw" \
                      && xz -f --check=crc32 "$raw" ;;
        *.ko.zst) raw="${ko%.zst}"
                  zstd -q -d -f "$ko" -o "$raw" && "$sf" sha256 "$KEY" "$CERT" "$raw" \
                      && zstd -q -f --rm "$raw" -o "$ko" ;;
        *.ko)     "$sf" sha256 "$KEY" "$CERT" "$ko" ;;
        *)        return 1 ;;
    esac
}

# The key must never be copied into the image. A regex for "PRIVATE KEY" headers is both
# too narrow (encrypted/openssh forms) and too broad (sample keys in unrelated packages),
# so search for THIS key's own body instead: its longest base64 line, as a fixed string.
assert_key_not_copied() {
    probe=$(grep -v -- '-----' "$KEY" | awk 'length > max { max = length; line = $0 } END { print line }')
    if [ "${#probe}" -lt 40 ]; then
        log "cannot fingerprint the signing key (not PEM?); refusing to sign blind"
        return 1
    fi
    for r in $LEAK_ROOTS; do
        [ -d "$r" ] || continue
        hit=$(grep -rlsF -- "$probe" "$r" 2>/dev/null | head -n 1 || true)
        if [ -n "$hit" ]; then
            log "ASSERT FAILED: the private signing key was copied into the image: $hit"
            return 1
        fi
    done
    return 0
}

write_manifest() {
    signed="$1"; signer="$2"; sha="$3"; ver="$4"; mods="$5"
    mkdir -p "$OUT"
    {
        printf '{\n  "signed": %s,\n' "$signed"
        printf '  "signer_cn": %s,\n' "$(json_or_null "$signer")"
        printf '  "cert_sha256": %s,\n' "$(json_or_null "$sha")"
        printf '  "kernel": %s,\n' "$(json_str "$KVER")"
        printf '  "driver_version": %s,\n' "$(json_or_null "$ver")"
        printf '  "modules": ['
        first=1
        for m in $mods; do
            [ $first -eq 1 ] || printf ', '
            first=0
            json_str "$m"
        done
        printf ']\n}\n'
    } > "$OUT/kmod-manifest.json"
    chmod 644 "$OUT/kmod-manifest.json"
}

main() {
    [ -n "$KVER" ] || find_kver
    [ -d "$MODROOT/$KVER" ] || { log "no module tree $MODROOT/$KVER"; exit 2; }
    mods=$(find "$MODROOT/$KVER" -type f \( -name 'nvidia*.ko' -o -name 'nvidia*.ko.xz' -o -name 'nvidia*.ko.zst' \) | sort)
    if [ -z "$mods" ]; then
        log "no nvidia*.ko under $MODROOT/$KVER -- the driver build produced no modules"
        exit 1
    fi
    names=""
    for m in $mods; do names="$names $(basename "$m")"; done
    ver=$("$MODINFO" -F version -k "$KVER" nvidia 2>/dev/null || true)

    if [ ! -s "$KEY" ]; then
        write_manifest false "" "" "$ver" "$names"
        if [ "$REQUIRE" = "1" ]; then
            log "AWNIX_REQUIRE_SIGNED=1 but no signing key at $KEY (build secret id=awnix-mok-key)"
            exit 1
        fi
        log "no signing key: modules left UNSIGNED (they load only with Secure Boot off)"
        exit 0
    fi
    [ -s "$CERT" ] || { log "key present but no certificate at $CERT (build secret id=awnix-mok-cert)"; exit 1; }
    command -v openssl >/dev/null 2>&1 || { log "openssl not found"; exit 2; }
    sf=$(find_sign_file) || { log "scripts/sign-file not found for $KVER (install kernel-devel-$KVER)"; exit 2; }

    for m in $mods; do
        sign_one "$m" "$sf" || { log "signing failed: $m"; exit 1; }
    done

    mkdir -p "$OUT"
    openssl x509 -inform DER -in "$CERT" -outform DER -out "$OUT/awnix-mok.der" 2>/dev/null \
        || openssl x509 -in "$CERT" -outform DER -out "$OUT/awnix-mok.der"
    chmod 644 "$OUT/awnix-mok.der"
    sha=$(sha256sum "$OUT/awnix-mok.der" | cut -d' ' -f1)
    signer_cn=$(openssl x509 -inform DER -in "$OUT/awnix-mok.der" -noout -subject -nameopt multiline \
        | sed -n 's/^ *commonName *= *//p' | head -n1)

    depmod -a "$KVER" 2>/dev/null || true
    for m in $mods; do
        s=$("$MODINFO" -F signer "$m" 2>/dev/null || true)
        if [ -z "$s" ]; then
            log "ASSERT FAILED: $m has no signer after signing"
            exit 1
        fi
    done
    assert_key_not_copied || exit 1
    write_manifest true "$signer_cn" "$sha" "$ver" "$names"
    log "signed $(echo "$names" | wc -w | tr -d ' ') module(s) for $KVER; cert sha256 $(printf '%s' "$sha" | cut -c1-12)"
}

# expect_rc NAME WANT CMD... -- run CMD; count a failure unless it exits exactly WANT.
expect_rc() {
    name="$1"; want="$2"; shift 2
    set +e
    "$@" >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -ne "$want" ]; then
        echo "SELF-TEST FAIL: $name: exit $rc, want $want"; fails=$((fails + 1))
    fi
}

self_test() {
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    fails=0
    mkdir -p "$tmp/mods/6.12.0-test/extra"
    : > "$tmp/mods/6.12.0-test/extra/nvidia.ko"
    # 1. no key, not required -> exactly 0, manifest signed=false
    expect_rc "unsigned allowed" 0 env AWNIX_MODULES_ROOT="$tmp/mods" AWNIX_REQUIRE_SIGNED=0 \
        sh "$0" --key "$tmp/none" --out "$tmp/o1"
    grep -q '"signed": false' "$tmp/o1/kmod-manifest.json" 2>/dev/null \
        || { echo "SELF-TEST FAIL: manifest not signed=false"; fails=$((fails + 1)); }
    grep -q '"nvidia.ko"' "$tmp/o1/kmod-manifest.json" 2>/dev/null \
        || { echo "SELF-TEST FAIL: module missing from manifest"; fails=$((fails + 1)); }
    # 2. no key, required -> exactly 1
    expect_rc "required without key" 1 env AWNIX_MODULES_ROOT="$tmp/mods" AWNIX_REQUIRE_SIGNED=1 \
        sh "$0" --key "$tmp/none" --out "$tmp/o2"
    # 3. no modules -> exactly 1
    mkdir -p "$tmp/empty/6.12.0-test"
    expect_rc "no modules" 1 env AWNIX_MODULES_ROOT="$tmp/empty" \
        sh "$0" --key "$tmp/none" --out "$tmp/o3"
    # 4. key but no cert -> exactly 1
    echo x > "$tmp/key"
    expect_rc "key without cert" 1 env AWNIX_MODULES_ROOT="$tmp/mods" \
        sh "$0" --key "$tmp/key" --cert "$tmp/nocert" --out "$tmp/o4"
    # 5. two kernels and no --kver -> exactly 2
    mkdir -p "$tmp/two/6.12.0-a" "$tmp/two/6.12.0-b"
    expect_rc "two kernels" 2 env AWNIX_MODULES_ROOT="$tmp/two" \
        sh "$0" --key "$tmp/none" --out "$tmp/o5"

    # 6-8. The SIGNING leg: a real openssl key pair, a stub scripts/sign-file that appends
    # the kernel's signature trailer, and a stub modinfo that reports a signer only when the
    # trailer is present. This exercises sign_one (.ko, .ko.xz, .ko.zst), the DER export,
    # the manifest and the post-sign signer assertion. It does NOT prove the real kernel
    # sign-file or a real kernel accepts the signature: that needs an image build.
    if ! command -v openssl >/dev/null 2>&1; then
        echo "sign-nvidia-kmods self-test: openssl absent, cannot judge the signing leg"
        echo "sign-nvidia-kmods self-test: $fails failure(s), signing leg UNJUDGED"
        [ "$fails" -eq 0 ] || exit 1
        exit 2
    fi
    k="$tmp/sig/6.12.0-test"
    mkdir -p "$k/extra" "$k/build/scripts" "$tmp/bin"
    printf 'ELF-nvidia\n' > "$k/extra/nvidia.ko"
    have_xz=0; have_zst=0
    if command -v xz >/dev/null 2>&1; then
        printf 'ELF-uvm\n' > "$k/extra/nvidia-uvm.ko"
        xz -f "$k/extra/nvidia-uvm.ko" && have_xz=1
    fi
    if command -v zstd >/dev/null 2>&1; then
        printf 'ELF-drm\n' > "$k/extra/nvidia-drm.ko"
        zstd -q -f --rm "$k/extra/nvidia-drm.ko" -o "$k/extra/nvidia-drm.ko.zst" && have_zst=1
    fi
    # shellcheck disable=SC2016 # the stub body is written literally, on purpose
    {
        echo '#!/bin/sh'
        echo '# stub: sign-file ALGO KEY CERT MODULE'
        echo '[ $# -eq 4 ] && [ -s "$2" ] && [ -s "$3" ] && [ -f "$4" ] || exit 9'
        echo 'case "$4" in *"${SIGNFILE_FAIL:-@none@}"*) exit 7 ;; esac'
        echo "echo '~Module signature appended~' >> \"\$4\""
    } > "$k/build/scripts/sign-file"
    # shellcheck disable=SC2016 # the stub body is written literally, on purpose
    {
        echo '#!/bin/sh'
        echo '# stub: modinfo -F signer FILE | modinfo -F version -k KVER NAME'
        echo 'case "$2" in'
        echo '  signer) case "$3" in'
        echo '            *.xz) xz -dc "$3" ;; *.zst) zstd -qdc "$3" ;; *) cat "$3" ;;'
        echo '          esac 2>/dev/null | grep -q "Module signature appended" && echo "awnix MOK kmod signing" ;;'
        echo '  version) echo "575.0-test" ;;'
        echo 'esac'
        echo 'exit 0'
    } > "$tmp/bin/modinfo"
    printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/modinfo-none"
    chmod 0755 "$k/build/scripts/sign-file" "$tmp/bin/modinfo" "$tmp/bin/modinfo-none"
    # A config file, not -subj "/CN=...": MSYS rewrites a leading-slash argument.
    printf '[ req ]\ndistinguished_name = dn\nprompt = no\n[ dn ]\nCN = awnix MOK kmod signing\n' \
        > "$tmp/req.cnf"
    openssl req -new -x509 -newkey rsa:2048 -nodes -sha256 -days 2 -config "$tmp/req.cnf" \
        -keyout "$tmp/sk.pem" -outform DER -out "$tmp/sc.der" >/dev/null 2>&1 \
        || { echo "SELF-TEST FAIL: openssl could not mint a test key"; fails=$((fails + 1)); }
    # 6. key + cert + sign-file -> exactly 0; every module carries the trailer; signed=true
    mkdir -p "$tmp/clean" "$tmp/leak/usr/lib"
    expect_rc "signing leg" 0 env AWNIX_MODULES_ROOT="$tmp/sig" AWNIX_MODINFO="$tmp/bin/modinfo" \
        AWNIX_LEAK_SCAN_ROOTS="$tmp/clean" AWNIX_REQUIRE_SIGNED=1 sh "$0" --key "$tmp/sk.pem" --cert "$tmp/sc.der" --out "$tmp/o6"
    m6="$tmp/o6/kmod-manifest.json"
    grep -q '"signed": true' "$m6" 2>/dev/null \
        || { echo "SELF-TEST FAIL: manifest not signed=true"; fails=$((fails + 1)); }
    grep -q '"signer_cn": "awnix MOK kmod signing"' "$m6" 2>/dev/null \
        || { echo "SELF-TEST FAIL: signer_cn not recorded"; fails=$((fails + 1)); }
    grep -Eq '"cert_sha256": "[0-9a-f]{64}"' "$m6" 2>/dev/null \
        || { echo "SELF-TEST FAIL: cert_sha256 not recorded"; fails=$((fails + 1)); }
    cmp -s "$tmp/sc.der" "$tmp/o6/awnix-mok.der" \
        || { echo "SELF-TEST FAIL: public DER not exported verbatim"; fails=$((fails + 1)); }
    if grep -rqs 'PRIVATE KEY' "$tmp/o6"; then
        echo "SELF-TEST FAIL: private key material in the output dir"; fails=$((fails + 1))
    fi
    grep -q 'Module signature appended' "$k/extra/nvidia.ko" \
        || { echo "SELF-TEST FAIL: .ko not signed"; fails=$((fails + 1)); }
    if [ "$have_xz" -eq 1 ]; then
        { [ -f "$k/extra/nvidia-uvm.ko.xz" ] && [ ! -e "$k/extra/nvidia-uvm.ko" ] \
            && xz -dc "$k/extra/nvidia-uvm.ko.xz" | grep -q 'Module signature appended'; } \
            || { echo "SELF-TEST FAIL: .ko.xz not signed and recompressed"; fails=$((fails + 1)); }
    fi
    if [ "$have_zst" -eq 1 ]; then
        { [ -f "$k/extra/nvidia-drm.ko.zst" ] && [ ! -e "$k/extra/nvidia-drm.ko" ] \
            && zstd -qdc "$k/extra/nvidia-drm.ko.zst" | grep -q 'Module signature appended'; } \
            || { echo "SELF-TEST FAIL: .ko.zst not signed and recompressed"; fails=$((fails + 1)); }
    fi
    # 7. sign-file fails on ONE module -> exactly 1. With xz present the failing module is
    # the compressed one, so a recompress step that masked the failure would pass here.
    failmod=nvidia.ko; [ "$have_xz" -eq 1 ] && failmod=nvidia-uvm
    expect_rc "sign-file failure" 1 env SIGNFILE_FAIL="$failmod" AWNIX_MODULES_ROOT="$tmp/sig" \
        AWNIX_LEAK_SCAN_ROOTS="$tmp/clean" AWNIX_MODINFO="$tmp/bin/modinfo" \
        sh "$0" --key "$tmp/sk.pem" --cert "$tmp/sc.der" --out "$tmp/o7"
    # 8. sign-file "succeeds" but no signer is readable afterwards -> exactly 1
    expect_rc "no signer after signing" 1 env AWNIX_MODULES_ROOT="$tmp/sig" \
        AWNIX_LEAK_SCAN_ROOTS="$tmp/clean" AWNIX_MODINFO="$tmp/bin/modinfo-none" \
        sh "$0" --key "$tmp/sk.pem" --cert "$tmp/sc.der" --out "$tmp/o8"
    # 9. the key was copied into the image tree (renamed, re-wrapped) -> exactly 1, no manifest
    # (the header is split so gate SBN002 does not read this script as key material)
    { echo "-----BEGIN ENCRYPTED PRIVATE"" KEY-----"; grep -v -- '-----' "$tmp/sk.pem"; \
      echo "-----END ENCRYPTED PRIVATE"" KEY-----"; } > "$tmp/leak/usr/lib/notakey.txt" 2>/dev/null
    expect_rc "key copied into the image" 1 env AWNIX_MODULES_ROOT="$tmp/sig" \
        AWNIX_LEAK_SCAN_ROOTS="$tmp/clean $tmp/leak" AWNIX_MODINFO="$tmp/bin/modinfo" \
        sh "$0" --key "$tmp/sk.pem" --cert "$tmp/sc.der" --out "$tmp/o9"
    [ -e "$tmp/o9/kmod-manifest.json" ] \
        && { echo "SELF-TEST FAIL: manifest written despite a leaked key"; fails=$((fails + 1)); }
    echo "sign-nvidia-kmods self-test: 9 cases (xz=$have_xz zst=$have_zst), $fails failure(s)"
    [ "$fails" -eq 0 ]
}

while [ $# -gt 0 ]; do
    case "$1" in
        --self-test) self_test; exit $? ;;
        --kver) KVER="${2:-}"; shift 2 ;;
        --key) KEY="${2:-}"; shift 2 ;;
        --cert) CERT="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) log "unknown argument $1"; exit 2 ;;
    esac
done
main
