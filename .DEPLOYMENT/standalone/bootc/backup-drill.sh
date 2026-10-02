#!/bin/sh
# backup-drill.sh -- the on-image proof for `awnix backup|restore|reset`.
#
# Runs INSIDE an awnix or garg VM in hosted CI (never on a customer box, never
# on the dev host). It seeds a marker under the customer data tree, backs up,
# mutates, restores, compares, then runs a factory reset with --no-reboot and
# asserts the box is back in setup mode. The last line on the serial console is
#   AWNIX-BACKUP-DRILL: PASS   or   AWNIX-BACKUP-DRILL: FAIL <step>
# and the exit code is 0 / 1 (2 when the CLI is missing: could not judge).
set -u

CLI=/usr/libexec/awnix/awnix-backup
LABEL="drill-$(date -u +%Y%m%dT%H%M%SZ)"
SEED_DIR=/var/lib/aither/backup-drill
[ -d /var/lib/gargbot ] && SEED_DIR=/var/lib/gargbot/uploads/backup-drill

say() {
    echo "$*"
    [ -w /dev/console ] && echo "$*" > /dev/console 2>/dev/null
    return 0
}
fail() { say "AWNIX-BACKUP-DRILL: FAIL $1"; exit 1; }

if [ ! -x "$CLI" ]; then
    say "AWNIX-BACKUP-DRILL: FAIL cli-missing"
    exit 2
fi
"$CLI" --self-test >/dev/null 2>&1 || fail self-test

mkdir -p "$SEED_DIR"
head -c 65536 /dev/urandom > "$SEED_DIR/blob.bin"
echo "drill $LABEL" > "$SEED_DIR/note.txt"
before=$(sha256sum "$SEED_DIR/blob.bin" "$SEED_DIR/note.txt" | sha256sum | cut -c1-64)

out=$("$CLI" backup create --label "$LABEL" 2>&1) || { say "$out"; fail create; }
say "$(echo "$out" | grep '^awnix-backup:')"
"$CLI" backup verify --label "$LABEL" >/dev/null 2>&1 || fail verify

echo "MUTATED" > "$SEED_DIR/note.txt"
rm -f "$SEED_DIR/blob.bin"

"$CLI" restore --label "$LABEL" >/dev/null 2>&1 || fail restore
after=$(sha256sum "$SEED_DIR/blob.bin" "$SEED_DIR/note.txt" 2>/dev/null | sha256sum | cut -c1-64)
[ "$before" = "$after" ] || fail compare

HOST=$(head -n1 /etc/hostname 2>/dev/null | tr -d "[:space:]")
[ -n "$HOST" ] || HOST=$(hostname)
"$CLI" reset --scope factory --confirm "wrong phrase" --no-reboot >/dev/null 2>&1
[ $? -eq 1 ] || fail wrong-phrase-not-refused
out=$("$CLI" reset --scope factory --confirm "erase $HOST" --no-reboot 2>&1) || { say "$out"; fail reset; }
say "$(echo "$out" | grep '^awnix-reset:')"
[ -f /var/lib/awnix/reset-receipt.json ] || fail receipt
[ ! -e /etc/awnix/setup.json ] || fail setup-mode
[ ! -e "$SEED_DIR/note.txt" ] || fail customer-data-survived

say "AWNIX-BACKUP-DRILL: PASS"
exit 0
