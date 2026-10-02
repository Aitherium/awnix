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
#   stage dir      : --stage-dir DIR, else $AITHER_WSL_STAGE_DIR (the wrappers pass it
#                    in /mnt form), else $AWNIX_WSL_STAGE, else the Windows user's
#                    %LOCALAPPDATA%\awnix\wsl mapped through wslpath. No drive or
#                    checkout of any one machine is assumed (PRT001).
#   staged tarball : <stage dir>/<name>-rootfs.tar
#   the wrapper then: wsl --import <name> <stage dir>/<name> <tar> --version 2
# The wrappers (rehearse-awnix.ps1, bootstrap-awnix.ps1) resolve the same dir with
# Resolve-AwnixStageRoot and pass it down, so the two halves cannot disagree.
# WHY E: (2026-09-27): C: had 3.8 GB free and a fleet rootfs is ~3 GB; an export
# that fills C: takes the host's page file and every WSL vhdx on it down with it.
# So the free space is checked BEFORE `podman export`, against the image's size.
#
# Exit 0 exported and verified · 1 failed · 2 could not judge / wrong context.
# NEVER exit 0 without a tarball on disk: measured 2026-08-23, an earlier attempt at
# this lane staged 4.3 GB, registered no distro, and exited 0 — a caller saw a clean
# run and an absent distro. Every failure path below is loud for that reason.
#
#   sh awnix-to-wsl.sh --image ghcr.io/aitherium/awnix-full:latest --name awnix
#   sh awnix-to-wsl.sh --self-test
set -eu

# %LOCALAPPDATA% of the Windows user, via interop. Empty when interop is off.
win_localappdata() {
    cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r'
}

# default_stage_dir -> prints the stage dir, or returns 1 when none can be derived.
default_stage_dir() {
    if [ -n "${AITHER_WSL_STAGE_DIR:-}" ]; then printf '%s' "${AITHER_WSL_STAGE_DIR%/}"; return 0; fi
    if [ -n "${AWNIX_WSL_STAGE:-}" ]; then printf '%s' "${AWNIX_WSL_STAGE%/}"; return 0; fi
    lad="$(win_localappdata || true)"
    case "$lad" in ''|'%LOCALAPPDATA%') return 1 ;; esac
    unix="$(wslpath -u "$lad" 2>/dev/null || true)"
    [ -n "$unix" ] || return 1
    printf '%s/awnix/wsl' "${unix%/}"
}
STAGE_DIR=""
IMAGE=""
NAME="awnix"
SELFTEST=0
# A rootfs smaller than this is not an operating system. Guards the "exported
# something, registered nothing" class rather than trusting `podman export`'s
# exit code alone.
MIN_BYTES=52428800   # 50 MiB
# Headroom kept free on the stage drive beyond the image's own size.
MARGIN_BYTES=2147483648   # 2 GiB

say()  { printf '  %s\n' "$*"; }
die()  { printf 'awnix-to-wsl: %s\n' "$*" >&2; exit 1; }
dead() { printf 'awnix-to-wsl: CANNOT JUDGE - %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="${2:-}"; shift 2 ;;
        --name)  NAME="${2:-}";  shift 2 ;;
        --stage-dir) STAGE_DIR="${2%/}"; shift 2 ;;
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

# space_ok FREE_BYTES NEED_BYTES -> exit 0 when FREE covers NEED + margin. Pure.
space_ok() {
    [ "$1" -ge 0 ] 2>/dev/null || return 1
    [ "$1" -ge $(( $2 + MARGIN_BYTES )) ]
}

# free_bytes DIR -> bytes available on DIR's filesystem, or -1.
free_bytes() {
    kb="$(df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}')"
    case "$kb" in ''|*[!0-9]*) echo -1 ;; *) echo $(( kb * 1024 )) ;; esac
}

self_test() {
    ok=0
    command -v podman >/dev/null 2>&1 || { echo "SELFTEST: podman absent"; ok=1; }
    # The stage dir is a CONTRACT with the wrappers: AITHER_WSL_STAGE_DIR, then
    # AWNIX_WSL_STAGE, then %LOCALAPPDATA%\awnix\wsl -- never a fixed drive.
    got="$(AITHER_WSL_STAGE_DIR=/stub/a/ default_stage_dir)"
    [ "$got" = "/stub/a" ] || { echo "SELFTEST: AITHER_WSL_STAGE_DIR not honoured ($got)"; ok=1; }
    got="$(AITHER_WSL_STAGE_DIR='' AWNIX_WSL_STAGE=/stub/b/ default_stage_dir)"
    [ "$got" = "/stub/b" ] || { echo "SELFTEST: AWNIX_WSL_STAGE not honoured ($got)"; ok=1; }
    got="$(win_localappdata() { printf 'Q:/stub'; }; wslpath() { printf '/stub/lad'; }
           AITHER_WSL_STAGE_DIR='' AWNIX_WSL_STAGE='' default_stage_dir)"
    [ "$got" = "/stub/lad/awnix/wsl" ] || { echo "SELFTEST: LOCALAPPDATA fallback drifted ($got)"; ok=1; }
    if (win_localappdata() { :; }; AITHER_WSL_STAGE_DIR='' AWNIX_WSL_STAGE='' default_stage_dir >/dev/null); then
        echo "SELFTEST: no interop and no override still produced a stage dir"; ok=1
    fi
    # The space guard must refuse the 2026-09-27 C: (3.8 GB free, 3 GB rootfs).
    if space_ok 4080218931 3221225472; then echo "SELFTEST: 3.8 GB free admitted a 3 GB export"; ok=1; fi
    space_ok 236223201280 3221225472 || { echo "SELFTEST: 220 GB free refused a 3 GB export"; ok=1; }
    if space_ok -1 1; then echo "SELFTEST: unknown free space admitted"; ok=1; fi
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

if [ -z "$STAGE_DIR" ]; then
    STAGE_DIR="$(default_stage_dir)" || dead "no stage dir: pass --stage-dir DIR or set AWNIX_WSL_STAGE (Windows interop is off, so %LOCALAPPDATA% cannot be read)"
fi
TAR="$STAGE_DIR/$NAME-rootfs.tar"

mkdir -p "$STAGE_DIR" 2>/dev/null || die "cannot create $STAGE_DIR (is that drive mounted here? pass --stage-dir)"

if ! podman image exists "$IMAGE" 2>/dev/null; then
    say "image not local; pulling $IMAGE"
    podman pull "$IMAGE" >/dev/null 2>&1 || die "could not pull $IMAGE, and it is not local"
fi

# Free space BEFORE the export, against the image's own size: a tarball that fills
# the drive half-way is worse than none.
IMG_BYTES="$(podman image inspect --format '{{.Size}}' "$IMAGE" 2>/dev/null || echo 0)"
case "$IMG_BYTES" in ''|*[!0-9]*) IMG_BYTES=0 ;; esac
FREE="$(free_bytes "$STAGE_DIR")"
[ "$FREE" -ge 0 ] || dead "could not read free space on $STAGE_DIR"
space_ok "$FREE" "$IMG_BYTES" || die "$STAGE_DIR has $FREE bytes free; the export needs $IMG_BYTES + 2 GiB.
  Pass --stage-dir (or AWNIX_WSL_STAGE) on a roomier drive."

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
say "next (Windows side): the wrapper imports $TAR as '$NAME' after its refusal checks"
exit 0
