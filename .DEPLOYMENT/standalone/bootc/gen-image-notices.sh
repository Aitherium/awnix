#!/bin/sh
# gen-image-notices.sh -- write the third-party licence inventory INTO an awnix/garg image.
#
#   gen-image-notices.sh [--notices FILE] [--out DIR]      (run in a Containerfile RUN)
#   gen-image-notices.sh --self-test
#
# Writes, under DIR (default /usr/share/licenses/awnix):
#   THIRD-PARTY-NOTICES.md      the CUSTOMER-FACING cut of the repo's notices
#                               (licenses/THIRD-PARTY-NOTICES.md): only the "## Redistributed"
#                               section, per component only its heading and the Upstream /
#                               Licence / Version pinned / Modified / Our fork / Licence text
#                               ships at fields. Internal build paths ("Pinned at"), intake
#                               verdicts, editorial prose and the Integrated / Evaluated /
#                               Declined sections never enter an image.
#   THIRD-PARTY-INVENTORY.tsv   source<TAB>name<TAB>version<TAB>license, one row per
#                               RPM (rpm -qa) and per Python distribution (*.dist-info)
#
# RPM per-package licence TEXTS stay where the RPMs put them (/usr/share/licenses/<pkg>);
# this file is the index a buyer's compliance reviewer asks for first. It is regenerated
# in every leaf image because each layer adds packages -- a base-layer inventory is
# stale the moment awnix-runner-ai or garg-appliance installs one more RPM.
#
# Exit: 0 written · 1 the inventory would be empty or the notices are missing ·
# 2 bad arguments. An empty inventory is a failure, never a pass.
set -u

OUT="/usr/share/licenses/awnix"
NOTICES=""
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
RPM="${RPM:-rpm}"
# Where Python distributions live in a bootc image (system + /opt venvs).
SITE_GLOBS="${SITE_GLOBS:-/usr/lib/python3*/site-packages /usr/lib64/python3*/site-packages /usr/local/lib/python3*/site-packages /usr/local/lib64/python3*/site-packages /opt/*/lib/python3*/site-packages /opt/*/*/lib/python3*/site-packages}"

die() { echo "gen-image-notices: $1" >&2; exit "${2:-2}"; }

# Customer-facing notices: stdin = the repo notices, stdout = the image cut (see header).
image_notices() {
    tr -d '\r' | awk '
        BEGIN {
            print "# Third-party notices"
            print ""
            print "This image redistributes the open-source components below. Each keeps its own"
            print "licence. The complete per-package list for THIS image (every RPM and Python"
            print "distribution, with its licence) is THIRD-PARTY-INVENTORY.tsv in this directory;"
            print "RPM licence texts are under /usr/share/licenses/<package>."
        }
        /^<!--/ { incomment = 1 }
        incomment { if (/-->/) incomment = 0; next }
        /^## / { insec = ($0 == "## Redistributed"); next }
        !insec { next }
        /^### / { print ""; print $0; print ""; n++; next }
        /^- \*\*(Upstream|Licence|Version pinned|Modified|Our fork|Licence text ships at)\*\*:/ { print; next }
        END { if (!n) exit 1 }'
}

# One TSV row per *.dist-info/METADATA: name, version, licence. Licence preference:
# License-Expression (PEP 639) > License (first line, capped) > the last
# "Classifier: License :: ..." segment > UNKNOWN. Only the header block is read.
pydist_rows() {
    for g in $SITE_GLOBS; do
        for meta in $g/*.dist-info/METADATA; do
            [ -f "$meta" ] || continue
            awk -F': ' '
                { sub(/\r$/, "") }
                /^$/ { exit }
                $1 == "Name" && name == "" { name = substr($0, 7) }
                $1 == "Version" && ver == "" { ver = substr($0, 10) }
                $1 == "License-Expression" && expr == "" { expr = substr($0, 21) }
                $1 == "License" && lic == "" { lic = substr($0, 10) }
                $1 == "Classifier" && index($0, "License ::") { n = split($0, p, " :: "); cls = p[n] }
                END {
                    l = expr != "" ? expr : (lic != "" && lic != "UNKNOWN" ? lic : (cls != "" ? cls : "UNKNOWN"))
                    gsub(/\t/, " ", l); if (length(l) > 120) l = substr(l, 1, 117) "..."
                    if (name != "") printf "python\t%s\t%s\t%s\n", name, ver, l
                }' "$meta"
        done
    done
}

rpm_rows() {
    command -v "$RPM" >/dev/null 2>&1 || return 0
    "$RPM" -qa --qf 'rpm\t%{NAME}\t%{VERSION}-%{RELEASE}\t%{LICENSE}\n' 2>/dev/null
}

generate() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --notices) NOTICES="${2:-}"; shift 2 ;;
            --out) OUT="${2:-}"; shift 2 ;;
            *) die "unknown argument $1" ;;
        esac
    done
    [ -n "$NOTICES" ] || NOTICES="$(dirname "$SELF")/licenses/THIRD-PARTY-NOTICES.md"
    [ -s "$NOTICES" ] || die "notices file missing or empty: $NOTICES" 1
    mkdir -p "$OUT" || die "cannot create $OUT"
    tmp="$OUT/.inventory.$$"
    { rpm_rows; pydist_rows; } | tr -d '\r' | LC_ALL=C sort -u > "$tmp.body"
    rows=$(wc -l < "$tmp.body" | tr -d ' ')
    if [ "$rows" -eq 0 ]; then
        rm -f "$tmp.body"
        die "inventory would be EMPTY (no rpm database, no python dist-info) -- refusing" 1
    fi
    { printf 'source\tname\tversion\tlicense\n'; cat "$tmp.body"; } > "$OUT/THIRD-PARTY-INVENTORY.tsv"
    rm -f "$tmp.body"
    image_notices < "$NOTICES" > "$OUT/THIRD-PARTY-NOTICES.md" \
        || { rm -f "$OUT/THIRD-PARTY-NOTICES.md"; die "no '## Redistributed' components in $NOTICES -- refusing" 1; }
    chmod 0644 "$OUT/THIRD-PARTY-INVENTORY.tsv" "$OUT/THIRD-PARTY-NOTICES.md"
    nrpm=$(grep -c '^rpm	' "$OUT/THIRD-PARTY-INVENTORY.tsv" || true)
    npy=$(grep -c '^python	' "$OUT/THIRD-PARTY-INVENTORY.tsv" || true)
    echo "gen-image-notices: $rows row(s) (rpm=$nrpm python=$npy) -> $OUT/THIRD-PARTY-INVENTORY.tsv"
}

self_test() {
    rc=0
    t() { if [ "$2" = "$3" ]; then echo "  PASS $1"; else echo "  FAIL $1 (want '$3', got '$2')"; rc=1; fi; }
    tmp=$(mktemp -d) || { echo "SELF-TEST COULD NOT JUDGE: no temp dir"; return 2; }
    n=$(tr -cd '\r' < "$SELF" | wc -c | tr -d ' '); t "no CR byte in this script" "$n" 0

    mkdir -p "$tmp/bin" "$tmp/site/foo-1.0.dist-info" "$tmp/site/bar-2.0.dist-info" "$tmp/site/baz-3.dist-info"
    cat > "$tmp/bin/rpm" <<'EOF'
#!/bin/sh
printf 'rpm\tbash\t5.1.8-9.el9\tGPL-3.0-or-later\n'
printf 'rpm\tglibc\t2.34-100.el9\tLGPL-2.1-or-later AND GPL-2.0-or-later\n'
EOF
    chmod +x "$tmp/bin/rpm"
    printf 'Metadata-Version: 2.4\r\nName: foo\r\nVersion: 1.0\r\nLicense-Expression: MIT\r\nLicense: ignored\r\n\r\nbody: x\n' \
        > "$tmp/site/foo-1.0.dist-info/METADATA"
    printf 'Metadata-Version: 2.1\nName: bar\nVersion: 2.0\nClassifier: License :: OSI Approved :: Apache Software License\n\nLicense: body-not-header\n' \
        > "$tmp/site/bar-2.0.dist-info/METADATA"
    printf 'Metadata-Version: 2.1\nName: baz\nVersion: 3\n' > "$tmp/site/baz-3.dist-info/METADATA"
    cat > "$tmp/N.md" <<'EOF'
<!-- GENERATED FILE
     Source: AitherOS/config/upstreams.yaml -->

# Third-party notices

## Redistributed

### CPython

- **Upstream**: https://github.com/python/cpython
- **Licence**: PSF-2.0
- **Pinned at**: `.DEPLOYMENT/images/internal/Dockerfile`
- **Modified**: no
- **Intake verdict**: ADOPT

The interpreter every service in this platform runs on.

> an editorial quote

## Integrated

### InternalOnlyThing

- **Upstream**: https://example.invalid/x
EOF

    ( RPM="$tmp/bin/rpm" SITE_GLOBS="$tmp/site" sh "$SELF" --notices "$tmp/N.md" --out "$tmp/o1" ) >/dev/null 2>&1
    t "writes an inventory" "$?" 0
    inv="$tmp/o1/THIRD-PARTY-INVENTORY.tsv"
    t "  header + 2 rpm + 3 python rows" "$(wc -l < "$inv" | tr -d ' ')" 6
    grep -q "^rpm	glibc	2.34-100.el9	LGPL-2.1-or-later AND GPL-2.0-or-later\$" "$inv"; t "  rpm row verbatim" "$?" 0
    grep -q "^python	foo	1.0	MIT\$" "$inv"; t "  License-Expression wins, CRLF METADATA handled" "$?" 0
    grep -q "^python	bar	2.0	Apache Software License\$" "$inv"; t "  classifier fallback, body ignored" "$?" 0
    grep -q "^python	baz	3	UNKNOWN\$" "$inv"; t "  no licence -> UNKNOWN, never dropped" "$?" 0
    nt="$tmp/o1/THIRD-PARTY-NOTICES.md"
    grep -qx '### CPython' "$nt"; t "  redistributed component kept" "$?" 0
    grep -qx -- '- \*\*Licence\*\*: PSF-2.0' "$nt"; t "  its licence kept" "$?" 0
    grep -Eq 'Pinned at|Intake verdict|Source:|every service|editorial|InternalOnlyThing|\.DEPLOYMENT' "$nt"
    t "  no internal path, verdict, prose or non-redistributed section" "$?" 1
    printf '# notices\n## Integrated\n### X\n' > "$tmp/N2.md"
    ( RPM="$tmp/bin/rpm" SITE_GLOBS="$tmp/site" sh "$SELF" --notices "$tmp/N2.md" --out "$tmp/o4" ) >/dev/null 2>&1
    t "notices with no Redistributed component is a failure" "$?" 1

    ( RPM="$tmp/nope" SITE_GLOBS="$tmp/empty" sh "$SELF" --notices "$tmp/N.md" --out "$tmp/o2" ) >/dev/null 2>&1
    t "empty inventory is a failure" "$?" 1
    [ -e "$tmp/o2/THIRD-PARTY-INVENTORY.tsv" ]; t "  and no file is left behind" "$?" 1
    ( RPM="$tmp/bin/rpm" SITE_GLOBS="$tmp/site" sh "$SELF" --notices "$tmp/missing.md" --out "$tmp/o3" ) >/dev/null 2>&1
    t "missing notices is a failure" "$?" 1
    ( sh "$SELF" --bogus ) >/dev/null 2>&1; t "unknown argument -> 2" "$?" 2

    rm -rf "$tmp"
    echo
    [ "$rc" = 0 ] && echo "self-test: PASS" || echo "self-test: FAIL"
    return "$rc"
}

case "${1:-}" in
    --self-test) self_test; exit $? ;;
    *) generate "$@" ;;
esac
