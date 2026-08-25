#!/usr/bin/env sh
# One command to get awnix (or the GobboNet appliance) running, whichever way you
# want it: a container, a WSL2 distro, or a bootable ISO.
#
#   sh bootstrap-awnix.sh                        # container, the appliance
#   sh bootstrap-awnix.sh --target wsl
#   sh bootstrap-awnix.sh --target iso
#   sh bootstrap-awnix.sh --image localhost/gobbonet-appliance:latest
#   sh bootstrap-awnix.sh --self-test
#
# It does not reimplement any of the three lanes. awnix-to-wsl.sh and
# assemble-awnix-iso.sh already exist, are self-tested, and know things this
# script should not have to (that `wsl --import` is a Windows-side command and
# cannot read a path inside the distro; that a GitHub release asset caps at 2 GiB
# so the ISO ships in .part slices). This picks a lane and hands over.
#
# Exit: 0 done, 1 a step failed, 2 could not judge.
set -u

IMAGE="${AWNIX_IMAGE:-ghcr.io/aitherium/gobbonet-appliance:latest}"
FALLBACK="localhost/gobbonet-appliance:latest"
TARGET="container"
NAME="awnix"
PORT="8080"
SELFTEST=0

die() { echo "bootstrap-awnix: $*" >&2; exit 1; }
# Progress goes to STDERR. It used to go to stdout, and pull_or_local returns the
# resolved image name ON stdout -- so the progress line was captured as part of
# the reference and podman refused "  using local …\nlocalhost/…" as an invalid
# reference format. A status message and a return value must not share a channel.
say() { printf '  %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:-}"; shift 2 ;;
        --image)  IMAGE="${2:-}"; shift 2 ;;
        --name)   NAME="${2:-}"; shift 2 ;;
        --port)   PORT="${2:-}"; shift 2 ;;
        --self-test) SELFTEST=1; shift ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ── the guard that exists because this class already shipped ───────────────────
# Three of four advertised install.sh one-liners once served a 56 KB Next.js
# PAGE with HTTP 200, and `curl -fsSL` does not fail on a 200 -- so the published
# command piped HTML straight into bash. Anything fetched here is checked for
# what it actually is before it is used.
looks_like_html() {
    # Judged on how the content STARTS, not on whether a tag appears anywhere.
    # The first version matched `<html` in the first 512 bytes and refused a
    # perfectly good shell script whose comment mentioned chat.html -- a guard
    # that rejects the thing it protects gets deleted rather than fixed. Its own
    # self-test caught that before this shipped.
    first=$(sed '/^[[:space:]]*$/d' "$1" 2>/dev/null | head -1 | tr 'A-Z' 'a-z')
    case "$first" in
        '#!'*)                               return 1 ;;   # a shebang: a script
        '<!doctype html'*|'<html'*|'<head'*) return 0 ;;
        *)                                   return 1 ;;
    esac
}

fetch_verified() {
    # $1 url, $2 dest, $3 human description
    have curl || die "curl is required"
    curl -fsSL --retry 3 -o "$2" "$1" || die "could not download $3 from $1"
    [ -s "$2" ] || die "$3 downloaded as an EMPTY file from $1"
    if looks_like_html "$2"; then
        rm -f "$2"
        die "$1 served an HTML PAGE, not $3.
    A 200 with a web page in it is the failure mode this check exists for --
    curl does not treat it as an error and the next step would have run it."
    fi
}

self_test() {
    fail=0
    ck() { if [ "$2" = "1" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fail=$((fail+1)); fi; }
    tmp=$(mktemp -d)

    printf '<!DOCTYPE html>\n<html><head><title>hi</title></head></html>\n' > "$tmp/page.html"
    looks_like_html "$tmp/page.html" && ck "an HTML page is recognised" 1 || ck "an HTML page is recognised" 0

    printf '#!/bin/sh\necho hello\n' > "$tmp/real.sh"
    looks_like_html "$tmp/real.sh" && ck "a real script is not called HTML" 0 || ck "a real script is not called HTML" 1

    # A shell script that merely MENTIONS html must not be refused, or the guard
    # floods on exactly the scripts it is meant to pass.
    printf '#!/bin/sh\n# serves chat.html on <html> pages\necho ok\n' > "$tmp/mentions.sh"
    looks_like_html "$tmp/mentions.sh" && ck "a script mentioning html still passes" 0 || ck "a script mentioning html still passes" 1

    case "$TARGET" in container|wsl|iso) ck "the default target is known" 1 ;; *) ck "the default target is known" 0 ;; esac

    rm -rf "$tmp"
    echo
    [ "$fail" -eq 0 ] && { echo "SELF-TEST PASS"; return 0; } || { echo "SELF-TEST FAILED ($fail)"; return 1; }
}

[ "$SELFTEST" = "1" ] && { self_test; exit $?; }

# ── engine ─────────────────────────────────────────────────────────────────────
engine() {
    if have podman; then echo podman
    elif have docker; then echo docker
    else echo ""; fi
}

pull_or_local() {
    E="$1"
    if "$E" image exists "$IMAGE" 2>/dev/null || "$E" image inspect "$IMAGE" >/dev/null 2>&1; then
        say "using local $IMAGE"; echo "$IMAGE"; return 0
    fi
    say "pulling $IMAGE ..."
    if "$E" pull "$IMAGE" >/dev/null 2>&1; then echo "$IMAGE"; return 0; fi
    # A published image is not a precondition for a local build to be usable.
    if "$E" image exists "$FALLBACK" 2>/dev/null || "$E" image inspect "$FALLBACK" >/dev/null 2>&1; then
        say "$IMAGE is not published yet — using the locally built $FALLBACK"
        echo "$FALLBACK"; return 0
    fi
    return 1
}

HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd)

case "$TARGET" in
  container)
      E=$(engine)
      [ -n "$E" ] || die "no podman or docker on PATH — install one, or use --target wsl"
      IMG=$(pull_or_local "$E") || die "could not obtain $IMAGE (and no local build to fall back on)"
      say "starting $NAME on :$PORT"
      "$E" rm -f "$NAME" >/dev/null 2>&1 || true
      "$E" run -d --name "$NAME" -p "$PORT:8080" "$IMG" \
          adk gobbonet --ui /opt/gobbonet --port 8080 --host 0.0.0.0 --no-open >/dev/null \
          || die "the container did not start"
      i=0
      while [ "$i" -lt 40 ]; do
          if curl -sf -o /dev/null "http://127.0.0.1:$PORT/chat.html" 2>/dev/null; then
              say "GobboNet is up: http://127.0.0.1:$PORT/chat.html"; exit 0
          fi
          i=$((i+1)); sleep 2
      done
      echo "bootstrap-awnix: it started but never served chat.html; logs:" >&2
      "$E" logs "$NAME" 2>&1 | tail -20 >&2
      exit 1
      ;;
  wsl)
      [ -x "$HERE/awnix-to-wsl.sh" ] || die "awnix-to-wsl.sh is not beside this script (run from the bootc directory)"
      say "handing over to awnix-to-wsl.sh"
      exec sh "$HERE/awnix-to-wsl.sh" --image "$IMAGE" --name "$NAME"
      ;;
  iso)
      [ -x "$HERE/assemble-awnix-iso.sh" ] || die "assemble-awnix-iso.sh is not beside this script"
      say "handing over to assemble-awnix-iso.sh"
      exec sh "$HERE/assemble-awnix-iso.sh" "$@"
      ;;
  *)
      die "unknown --target '$TARGET' (container | wsl | iso)"
      ;;
esac
