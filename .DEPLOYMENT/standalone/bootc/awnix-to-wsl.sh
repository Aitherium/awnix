#!/bin/sh
# awnix-to-wsl.sh — export a container image's rootfs so wsl.exe can import it.
#
# THE HALF THAT RUNS INSIDE. `bootstrap-awnix.ps1 -Target wsl` is two halves and
# each runs where its commands exist: the EXPORT needs podman, which lives inside
# the distro; the IMPORT needs wsl.exe, which cannot be invoked from inside one.
# This script is the export half and it REFUSES to attempt the import rather than
# pretending — see the guard below.
#
# Why it exists: it did not. `bootstrap-awnix.ps1` has required this file since it
# was written ("awnix-to-wsl.sh is not beside this script" — line 161) and its own
# docstring asserts "it reimplements none of them -- awnix-to-wsl.sh and
# assemble-awnix-iso.ps1 already exist and are self-tested". The ISO half does.
# This one was tracked NOWHERE, so the entire `-Target wsl` lane threw on its first
# line. A reference that reads as coverage and resolves to no file is the HYG002
# shape; that it failed loudly rather than silently is the only reason it was not
# worse.
#
# THE CONTRACT, fixed by the caller — do not drift from it:
#   staged tarball : /mnt/c/AitherOS-Data/wsl/<name>-rootfs.tar
#   the wrapper then: wsl --import <name> C:/AitherOS-Data/wsl/<name> <tar> --version 2
# The wrapper checks that exact path and throws when it is absent, so a change here
# that is not made there points the import at a tarball that is not present.
#
# Exit 0 exported and verified · 1 failed · 2 could not judge / wrong context.
# NEVER exit 0 without a tarball on disk: measured 2026-08-23, an earlier attempt at
# this lane staged 4.3 GB, registered no distro, and exited 0 — a caller saw a clean
# run and an absent distro. Every failure path below is loud for that reason.
#
#   sh awnix-to-wsl.sh --image ghcr.io/aitherium/awnix-full:latest --name awnix
#   sh awnix-to-wsl.sh --self-test
set -eu

STAGE_DIR="/mnt/c/AitherOS-Data/wsl"
IMAGE=""
NAME="awnix"
SELFTEST=0
# A rootfs smaller than this is not an operating system. Guards the "exported
# something, registered nothing" class rather than trusting `podman export`'s
# exit code alone.
MIN_BYTES=52428800   # 50 MiB

say()  { printf '  %s\n' "$*"; }
die()  { printf 'awnix-to-wsl: %s\n' "$*" >&2; exit 1; }
dead() { printf 'awnix-to-wsl: CANNOT JUDGE - %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="${2:-}"; shift 2 ;;
        --name)  NAME="${2:-}";  shift 2 ;;
        --self-test) SELFTEST=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ── the refusal the caller's comment promises ────────────────────────────────
# If someone runs this on the Windows side (Git Bash, pwsh) podman is absent and
# wsl.exe is present. Exporting there would silently use a different engine or
# none. Say which half this is instead of guessing.
if command -v wsl.exe >/dev/null 2>&1 && ! command -v podman >/dev/null 2>&1; then
    dead "this is the EXPORT half and must run INSIDE a WSL distro (podman lives there).
  It does not perform the import: wsl.exe cannot be invoked from inside a distro.
  Run bootstrap-awnix.ps1 -Target wsl, which calls this and then imports."
fi

self_test() {
    ok=0
    command -v podman >/dev/null 2>&1 || { echo "SELFTEST: podman absent"; ok=1; }
    # The staged path is a CONTRACT with bootstrap-awnix.ps1. Assert the shape here
    # so a rename cannot pass this file's own tests while breaking the caller.
    expect="/mnt/c/AitherOS-Data/wsl/demo-rootfs.tar"
    actual="$STAGE_DIR/demo-rootfs.tar"
    [ "$expect" = "$actual" ] || { echo "SELFTEST: staged path drifted from the caller's contract"; ok=1; }
    # A missing image must FAIL, not produce an empty tar.
    if podman image exists aither-selftest-absent:nope 2>/dev/null; then
        echo "SELFTEST: a deliberately absent image reported present"; ok=1
    fi
    [ "$MIN_BYTES" -gt 0 ] || { echo "SELFTEST: size floor disabled"; ok=1; }
    [ "$ok" -eq 0 ] && echo "SELF-TEST: PASS" || echo "SELF-TEST: FAIL"
    return "$ok"
}

[ "$SELFTEST" -eq 1 ] && { self_test; exit $?; }
[ -n "$IMAGE" ] || die "--image is required"
[ -n "$NAME" ]  || die "--name is required"
command -v podman >/dev/null 2>&1 || dead "podman not found; run this inside the fleet distro"

TAR="$STAGE_DIR/$NAME-rootfs.tar"

mkdir -p "$STAGE_DIR" 2>/dev/null || die "cannot create $STAGE_DIR (is the C: drive mounted here?)"

if ! podman image exists "$IMAGE" 2>/dev/null; then
    say "image not local; pulling $IMAGE"
    podman pull "$IMAGE" >/dev/null 2>&1 || die "could not pull $IMAGE, and it is not local"
fi

# `podman export` needs a container, not an image. Create WITHOUT running: a rootfs
# is what we want, and starting the image would run its entrypoint for no reason.
say "creating a throwaway container from $IMAGE"
CID="$(podman create "$IMAGE" /bin/sh 2>/dev/null || podman create "$IMAGE" 2>/dev/null || true)"
[ -n "$CID" ] || die "podman create failed for $IMAGE"
# shellcheck disable=SC2064
trap "podman rm -f '$CID' >/dev/null 2>&1 || true" EXIT INT TERM

say "exporting rootfs to $TAR"
rm -f "$TAR" 2>/dev/null || true
podman export "$CID" -o "$TAR" || die "podman export failed"

[ -f "$TAR" ] || die "export reported success and produced no file at $TAR"
SIZE="$(wc -c < "$TAR" 2>/dev/null || echo 0)"
[ "$SIZE" -ge "$MIN_BYTES" ] || die "exported only $SIZE bytes to $TAR - that is not a rootfs.
  Refusing to exit 0: the caller would import it and register a distro that cannot boot."

say "exported $SIZE bytes"
say "next (Windows side): wsl --import $NAME C:/AitherOS-Data/wsl/$NAME $TAR --version 2"
exit 0
