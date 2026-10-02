#!/bin/sh
# stage-awnix-wheelhouse.sh -- stage one layer's vendored wheels into a build context.
#
#   stage-awnix-wheelhouse.sh --layer L --out DIR [--from-bundle TAR] [--wheels-dir D]
#                             [--python PY] [--no-closure-proof]
#
# Reads wheels/<L>.lock.txt (refresh_awnix_wheel_lock.py writes it) and fills DIR
# with exactly the wheel files it names:
#   * pypi rows: `pip download --no-deps --require-hashes --only-binary=:all:`
#     for the lock's python/platform -- pip refuses any file whose sha256 differs;
#   * git rows (`# git <url>@<sha40>`): cloned at that sha and built with
#     `pip wheel` (SOURCE_DATE_EPOCH = commit time), then hash-compared;
#   * --from-bundle TAR: no network at all; the wheels come from a private
#     awrtifact/awshare tarball and are hash-checked the same way.
# Then it writes DIR/SHA256SUMS and PROVES the closure: a dry-run
# `pip install --no-index --find-links DIR --constraint <L>.constraints.txt <targets>`
# for the lock's platform must resolve with nothing but DIR.
#
# Exit: 0 staged and closure proven, 1 hash or closure mismatch,
#       2 network, tool or lock missing (could not judge).
# POSIX sh; LF only.
set -u

LAYER=""
OUT=""
BUNDLE=""
WHEELS_DIR=""
PY="${PYTHON:-}"
PROVE=1

die2() { echo "stage-awnix-wheelhouse: $*" >&2; exit 2; }
die1() { echo "stage-awnix-wheelhouse: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --layer) LAYER="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --from-bundle) BUNDLE="${2:-}"; shift 2 ;;
        --wheels-dir) WHEELS_DIR="${2:-}"; shift 2 ;;
        --python) PY="${2:-}"; shift 2 ;;
        --no-closure-proof) PROVE=0; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) die2 "unknown argument: $1" ;;
    esac
done

[ -n "$LAYER" ] || die2 "--layer is required"
[ -n "$OUT" ] || die2 "--out is required"
case "$LAYER" in *[!A-Za-z0-9._-]*) die2 "bad layer name: $LAYER" ;; esac

HERE=$(cd "$(dirname "$0")" && pwd)
[ -n "$WHEELS_DIR" ] || WHEELS_DIR="$HERE/wheels"
LOCK="$WHEELS_DIR/$LAYER.lock.txt"
CONS="$WHEELS_DIR/$LAYER.constraints.txt"
[ -f "$LOCK" ] || die2 "lock not found: $LOCK"
[ -f "$CONS" ] || die2 "constraints not found: $CONS"

if [ -z "$PY" ]; then
    for c in python3.11 python3 python; do
        if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
    done
fi
[ -n "$PY" ] || die2 "no python found (set PYTHON or --python)"
"$PY" -m pip --version >/dev/null 2>&1 || die2 "$PY has no pip"

HEADER=$(sed -n '1p' "$LOCK")
case "$HEADER" in "# awnix-wheels schema=1 "*) ;; *) die2 "not an awnix-wheels schema=1 lock: $LOCK" ;; esac
PYV=$(printf '%s\n' "$HEADER" | sed -n 's/.* python=\([0-9.]*\) .*/\1/p')
PLAT=$(printf '%s\n' "$HEADER" | sed -n 's/.* platform=\([^ ]*\) .*/\1/p')
[ -n "$PYV" ] && [ -n "$PLAT" ] || die2 "lock header lacks python/platform: $HEADER"
ABI="cp$(printf '%s' "$PYV" | tr -d .)"

# pip does NOT expand `--platform manylinux_2_34_x86_64` to the older manylinux
# tags (it only does that for the legacy manylinux2014/2010 spellings), so a wheel
# tagged only manylinux_2_17 (cffi, pydantic-core, ...) reads as "no version".
# Pass every compatible tag, newest first -- pip ranks them in this order, which is
# the order refresh_awnix_wheel_lock.py used to pick the locked file.
PLAT_ARGS=""
case "$PLAT" in
    manylinux_2_*_*)
        rest=${PLAT#manylinux_2_}
        GLIBC_MINOR=${rest%%_*}
        ARCH=${rest#*_}
        m=$GLIBC_MINOR
        while [ "$m" -ge 5 ]; do
            PLAT_ARGS="$PLAT_ARGS --platform manylinux_2_${m}_${ARCH}"
            m=$((m - 1))
        done
        [ "$GLIBC_MINOR" -ge 17 ] && PLAT_ARGS="$PLAT_ARGS --platform manylinux2014_${ARCH}"
        [ "$GLIBC_MINOR" -ge 12 ] && PLAT_ARGS="$PLAT_ARGS --platform manylinux2010_${ARCH}"
        PLAT_ARGS="$PLAT_ARGS --platform manylinux1_${ARCH}"
        ;;
    *) PLAT_ARGS="--platform $PLAT" ;;
esac

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        "$PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
    fi
}

mkdir -p "$OUT" || die2 "cannot create $OUT"
TMP=$(mktemp -d 2>/dev/null || mktemp -d -t awnixwh) || die2 "mktemp failed"
trap 'rm -rf "$TMP"' EXIT INT TERM

# rows: name version sha file giturl gitsha  (one per line, tab separated)
grep -v '^#' "$LOCK" | sed '/^[[:space:]]*$/d' | while IFS= read -r line; do
    nv=${line%% *}
    name=${nv%%==*}
    ver=${nv#*==}
    sha=$(printf '%s\n' "$line" | sed -n 's/.*--hash=sha256:\([0-9a-f]\{64\}\).*/\1/p')
    file=$(printf '%s\n' "$line" | sed -n 's/.*file=\([^ ]*\.whl\).*/\1/p')
    git=$(printf '%s\n' "$line" | sed -n 's/.*# git \([^ ]*\)@\([0-9a-f]\{40\}\) .*/\1/p')
    gsha=$(printf '%s\n' "$line" | sed -n 's/.*# git [^ ]*@\([0-9a-f]\{40\}\) .*/\1/p')
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$ver" "$sha" "$file" "${git:--}" "${gsha:--}"
done > "$TMP/rows.tsv"
NROWS=$(wc -l < "$TMP/rows.tsv" | tr -d ' ')
[ "$NROWS" -gt 0 ] || die2 "no rows in $LOCK"
if awk -F'\t' '$3=="" || $4==""' "$TMP/rows.tsv" | grep -q .; then
    die1 "lock rows without a sha256 or file= name (AVW002): regenerate $LOCK"
fi

if [ -n "$BUNDLE" ]; then
    [ -f "$BUNDLE" ] || die2 "bundle not found: $BUNDLE"
    mkdir -p "$TMP/bundle"
    tar -xf - -C "$TMP/bundle" < "$BUNDLE" || die2 "cannot extract $BUNDLE"
    while IFS="$(printf '\t')" read -r name ver sha file git gsha; do
        src=$(find "$TMP/bundle" -type f -name "$file" | head -n 1)
        [ -n "$src" ] || die1 "bundle lacks $file ($name==$ver)"
        cp "$src" "$OUT/$file" || die2 "copy failed: $file"
    done < "$TMP/rows.tsv"
else
    # pypi rows through pip's own hash enforcement
    awk -F'\t' '$5=="-" {printf "%s==%s --hash=sha256:%s\n", $1, $2, $3}' "$TMP/rows.tsv" > "$TMP/pypi.txt"
    if [ -s "$TMP/pypi.txt" ]; then
        "$PY" -m pip download --no-deps --require-hashes --only-binary=:all: \
            $PLAT_ARGS --python-version "$PYV" --implementation cp --abi "$ABI" \
            --disable-pip-version-check --no-cache-dir -q \
            -r "$TMP/pypi.txt" -d "$OUT" > "$TMP/pip.log" 2>&1
        rc=$?
        if [ $rc -ne 0 ]; then
            tail -n 20 "$TMP/pip.log" >&2
            if grep -q "DO NOT MATCH THE HASHES" "$TMP/pip.log"; then
                die1 "a downloaded wheel does not match the lock hash"
            fi
            die2 "pip download failed (rc=$rc) -- network or index unavailable"
        fi
    fi
    # git rows: build at the pinned sha
    awk -F'\t' '$5!="-"' "$TMP/rows.tsv" | while IFS="$(printf '\t')" read -r name ver sha file git gsha; do
        src="$TMP/git-$name"
        git init -q "$src" && git -C "$src" fetch -q --depth 1 "$git" "$gsha" \
            && git -C "$src" checkout -q FETCH_HEAD || { echo "git fetch $git@$gsha failed" >&2; exit 2; }
        SOURCE_DATE_EPOCH=$(git -C "$src" log -1 --format=%ct)
        export SOURCE_DATE_EPOCH
        "$PY" -m pip wheel --no-deps --no-cache-dir -q -w "$TMP/gitwheels" "$src" >&2 || exit 2
        built=$(find "$TMP/gitwheels" -name "*.whl" -newer "$src/.git/HEAD" | head -n 1)
        [ -n "$built" ] || exit 2
        cp "$built" "$OUT/$file" || exit 2
    done
    rc=$?
    [ $rc -eq 0 ] || die2 "a git row could not be fetched or built"
fi

# every lock row present with its exact hash; nothing else in OUT
BAD=0
: > "$TMP/SHA256SUMS"
while IFS="$(printf '\t')" read -r name ver sha file git gsha; do
    if [ ! -f "$OUT/$file" ]; then
        echo "MISSING $file ($name==$ver)" >&2; BAD=1; continue
    fi
    got=$(sha256_of "$OUT/$file")
    if [ "$got" != "$sha" ]; then
        echo "MISMATCH $file: lock $sha got $got" >&2; BAD=1; continue
    fi
    printf '%s  %s\n' "$sha" "$file" >> "$TMP/SHA256SUMS"
done < "$TMP/rows.tsv"
for f in "$OUT"/*.whl; do
    [ -e "$f" ] || continue
    b=$(basename "$f")
    grep -q "  $b\$" "$TMP/SHA256SUMS" || { echo "UNLISTED $b (not in $LAYER.lock.txt)" >&2; BAD=1; }
done
[ $BAD -eq 0 ] || die1 "staged wheels disagree with $LOCK"
sort -k2 "$TMP/SHA256SUMS" > "$OUT/SHA256SUMS"

if [ $PROVE -eq 1 ]; then
    grep '^# target ' "$LOCK" | sed 's/^# target //' > "$TMP/targets.txt"
    [ -s "$TMP/targets.txt" ] || die2 "lock names no '# target' lines"
    # The proof must evaluate environment markers for the TARGET, not this host:
    # pip's dry-run applies `platform_system == "Windows"` etc. to the machine it
    # runs on even with --platform, so it is only trusted on Linux. uv resolves
    # for --python-platform, so it is preferred wherever it exists.
    UVP=""
    case "$PLAT" in manylinux_*_*_*) UVP="${ARCH}-manylinux_2_${GLIBC_MINOR}" ;; esac
    if command -v uv >/dev/null 2>&1 && [ -n "$UVP" ]; then
        PROVER="uv"
        uv pip compile --offline --no-index --find-links "$OUT" --no-header --quiet \
            --python-version "$PYV" --python-platform "$UVP" \
            --constraint "$CONS" -o "$TMP/proof.txt" "$TMP/targets.txt" > "$TMP/proof.log" 2>&1
        rc=$?
    elif [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
        PROVER="pip"
        "$PY" -m pip install --dry-run --ignore-installed --no-index --find-links "$OUT" \
            --constraint "$CONS" --only-binary=:all: \
            $PLAT_ARGS --python-version "$PYV" --implementation cp --abi "$ABI" \
            --target "$TMP/target" --disable-pip-version-check -q \
            -r "$TMP/targets.txt" > "$TMP/proof.log" 2>&1
        rc=$?
        if [ $rc -ne 0 ] && grep -qi "no such option: --dry-run" "$TMP/proof.log"; then
            die2 "pip too old for --dry-run (need >= 22.2)"
        fi
    else
        die2 "closure proof needs uv or a Linux host (pip evaluates markers for the host); staged+hashed OK"
    fi
    if [ $rc -ne 0 ]; then
        tail -n 20 "$TMP/proof.log" >&2
        die1 "closure NOT proven ($PROVER): the targets do not resolve from $OUT alone"
    fi
fi

echo "stage-awnix-wheelhouse: layer=$LAYER wheels=$NROWS out=$OUT closure=$([ $PROVE -eq 1 ] && echo proven || echo skipped)"
exit 0
