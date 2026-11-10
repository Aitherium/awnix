#!/bin/sh
# awnix-node — the node runtime awsh runs on, installed from nodejs.org and ASSERTED.
#
#   awnix-node.sh install    build time: fetch the pinned official tarball, verify its
#                            sha256, unpack to /opt/node-v$VER, link node/npm/npx into
#                            /usr/local/bin, point npm's global prefix at /usr/local, then
#                            run the check below. A node that fails the check fails the build.
#   awnix-node.sh --check    build AND boot (aither-node-check.service): prove the node on
#                            PATH can construct and use Intl.Segmenter. Exit 0 ok, 1 broken,
#                            2 no node at all (could not judge).
#   awnix-node.sh --self-test  prove --check can fail (a fake node that segfaults).
#
# WHY NOT THE EL9 MODULE. Measured 2026-11-09 on a WSL2 guest (kernel 6.6.x-microsoft-
# standard-WSL2): EL9's nodejs-20.20.2 module build (system ICU) SEGFAULTS (exit 139) on
# `new Intl.Segmenter('en')`; the official nodejs.org 20.19.2 build (bundled full ICU) on
# the same kernel does not. awsh lays out every line through string-width, which builds
# an Intl.Segmenter at import, so awsh died on every prompt. Pinning an older module NEVRA
# is not durable: EL9 mirrors keep only the current module build, so the pin 404s on the
# next rebuild. The official tarball is version- and checksum-pinned, its ICU is bundled
# (no dependency on the distro ICU that differs from what upstream tests), and a bump is
# one line here plus the assertion that already guards it.
#
# The assertion is the point: a bad node must fail LOUDLY at build and at boot, not
# silently on the user's first prompt.
set -eu

NODE_VERSION="${AWNIX_NODE_VERSION:-20.19.2}"
# sha256 of node-v$NODE_VERSION-linux-<arch>.tar.xz, from nodejs.org/dist/v$VER/SHASUMS256.txt
SHA_X64="cbe59620b21732313774df4428586f7222a84af29e556f848abf624ba41caf90"
SHA_ARM64="0d0c4a1c3a5aa657b76873eaa962936c7dc7a45047bd3957322544967713dc72"
DIST_URL="${AWNIX_NODE_DIST_URL:-https://nodejs.org/dist}"
PREFIX="/opt/node-v${NODE_VERSION}"
BIN_DIR="${AWNIX_NODE_BIN_DIR:-/usr/local/bin}"

# Construct, segment, and count: the three things string-width does at import + per call.
PROBE='const s=new Intl.Segmenter("en",{granularity:"grapheme"});const n=[...s.segment("e\u0301\uD83C\uDDFA\uD83C\uDDF8a")].length;if(n!==3){console.error("Intl.Segmenter returned "+n+" graphemes, want 3");process.exit(1)}new Intl.Segmenter();'

check() {
    node_bin="${1:-}"
    if [ -z "$node_bin" ]; then
        node_bin="$(command -v node 2>/dev/null || true)"
    fi
    if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
        echo "awnix-node: no node on PATH -- cannot judge Intl.Segmenter" >&2
        return 2
    fi
    rc=0
    "$node_bin" -e "$PROBE" || rc=$?
    if [ "$rc" -ne 0 ]; then
        ver="$("$node_bin" --version 2>/dev/null || echo unknown)"
        echo "awnix-node: FAIL $node_bin ($ver): Intl.Segmenter probe exited $rc" \
             "(139 = segfault). awsh cannot lay out text on this node." >&2
        echo "awnix-node: install the pinned official build: awnix-node.sh install" >&2
        return 1
    fi
    echo "awnix-node: ok $node_bin $("$node_bin" --version) Intl.Segmenter works"
    return 0
}

self_test() {
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    printf '#!/bin/sh\nkill -SEGV $$\n' > "$tmp/node"
    chmod 0755 "$tmp/node"
    rc=0
    check "$tmp/node" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] || { echo "awnix-node self-test: segfaulting node judged rc=$rc, want 1" >&2; return 1; }
    rc=0
    check "$tmp/absent" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ] || { echo "awnix-node self-test: missing node judged rc=$rc, want 2" >&2; return 1; }
    echo "awnix-node self-test: ok (segfault -> 1, missing -> 2)"
}

install_node() {
    case "${TARGETARCH:-$(uname -m)}" in
        amd64|x86_64)  arch=x64;   sha="$SHA_X64" ;;
        arm64|aarch64) arch=arm64; sha="$SHA_ARM64" ;;
        *) echo "awnix-node: unsupported arch '${TARGETARCH:-$(uname -m)}'" >&2; exit 1 ;;
    esac
    if [ -n "${AWNIX_NODE_VERSION:-}" ] && [ -z "${AWNIX_NODE_SHA256:-}" ]; then
        echo "awnix-node: AWNIX_NODE_VERSION set without AWNIX_NODE_SHA256 -- refusing an unverified tarball" >&2
        exit 1
    fi
    sha="${AWNIX_NODE_SHA256:-$sha}"
    tarball="node-v${NODE_VERSION}-linux-${arch}.tar.xz"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL --retry 3 -o "$tmp/$tarball" "$DIST_URL/v${NODE_VERSION}/$tarball"
    echo "$sha  $tmp/$tarball" | sha256sum -c -
    rm -rf "$PREFIX"
    mkdir -p "$PREFIX"
    tar -xJf "$tmp/$tarball" -C "$PREFIX" --strip-components=1 --no-same-owner
    # npm's global prefix defaults to its own install dir, whose bin is not on PATH;
    # /usr/local puts `npm install -g` bins in /usr/local/bin next to this node.
    mkdir -p "$PREFIX/etc"
    printf 'prefix=/usr/local\n' > "$PREFIX/etc/npmrc"
    mkdir -p "$BIN_DIR"
    for b in node npm npx corepack; do
        if [ -e "$PREFIX/bin/$b" ]; then
            ln -sfn "$PREFIX/bin/$b" "$BIN_DIR/$b"
        fi
    done
    check "$BIN_DIR/node"
    # The node a bare `node` resolves to must be THIS one, or the check above proved
    # nothing about what awsh's `#!/usr/bin/env node` will run.
    resolved="$(command -v node)"
    if [ "$(readlink -f "$resolved")" != "$(readlink -f "$PREFIX/bin/node")" ]; then
        echo "awnix-node: PATH resolves node to $resolved, not $PREFIX/bin/node" >&2
        exit 1
    fi
}

case "${1:-}" in
    install)      install_node ;;
    --check)      check "${2:-}" ;;
    --self-test)  self_test ;;
    *) echo "usage: awnix-node.sh install | --check [NODE] | --self-test" >&2; exit 2 ;;
esac
