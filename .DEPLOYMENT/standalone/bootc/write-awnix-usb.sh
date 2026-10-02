#!/usr/bin/env bash
# Write an awnix ISO to a USB stick -- and refuse every way that goes wrong.
#
#   ./write-awnix-usb.sh --iso awnix-x86_64.iso --device /dev/sdX
#   ./write-awnix-usb.sh --iso awnix-x86_64.iso --device /dev/disk4     # macOS
#   ./write-awnix-usb.sh --self-test
#
# Linux and macOS. On Windows use Fedora Media Writer, or Rufus in DD mode.
#
# Before a single byte is written it:
#   * verifies the ISO against awnix-iso.json or SHA256SUMS next to it (a bad download
#     written to a stick is a stick that does not boot, blamed on the product);
#   * refuses a device that is not removable/USB, is mounted, holds / or /boot, or is
#     larger than 256 GB (almost certainly not a stick) unless --force-large;
#   * shows the model, serial and size, and makes you TYPE the device name.
# Then it writes with dd (conv=fsync) and reads the first ISO-size bytes back to compare
# the sha256, because "dd exited 0" is not "the stick holds the image".
#
# Exit: 0 written and verified, 1 refused or mismatch, 2 could not judge.
set -uo pipefail

ISO=""; DEV=""; YES=0; FORCE_LARGE=0; SELFTEST=0
MAX_BYTES=$((256 * 1000 * 1000 * 1000))

die()  { echo "write-awnix-usb: $*" >&2; exit 1; }
dead() { echo "write-awnix-usb: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --iso) ISO="${2:-}"; shift 2 ;;
    --device) DEV="${2:-}"; shift 2 ;;
    --yes) YES=1; shift ;;
    --force-large) FORCE_LARGE=1; shift ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

sha_of() {  # sha_of FILE [BYTES]
  if [ -n "${2:-}" ]; then
    head -c "$2" "$1" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1
  else
    { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; } | cut -d' ' -f1
  fi
}

# The digest the ISO must have: awnix-iso.json first (our manifest), else SHA256SUMS.
expected_sha() {  # expected_sha ISO -> hex | ""
  local d; d="$(dirname "$1")"
  if [ -f "$d/awnix-iso.json" ]; then
    # The whole-image sha256 is the first one in the manifest (parts follow).
    grep -o '"sha256": "[0-9a-f]\{64\}"' "$d/awnix-iso.json" | head -1 | grep -o '[0-9a-f]\{64\}'
    return
  fi
  [ -f "$d/SHA256SUMS" ] && awk '/\.iso$/ {print $1; exit}' "$d/SHA256SUMS"
}

# THE DECISION, pure so the self-test drives it:
#   judge_device REMOVABLE(0|1) TRANSPORT SIZE_BYTES MOUNTED(0|1) HOLDS_ROOT(0|1) FORCE_LARGE
judge_device() {
  local rm="$1" tran="$2" size="$3" mounted="$4" root="$5" force="$6"
  [ "$root" = "1" ] && { echo "refuse:it holds / or /boot -- this is the running system's disk"; return; }
  [ "$mounted" = "1" ] && { echo "refuse:it is mounted -- unmount it first"; return; }
  if [ "$rm" != "1" ] && [ "$tran" != "usb" ]; then
    echo "refuse:it is neither removable nor USB (RM=$rm TRAN=${tran:-none})"; return
  fi
  case "$size" in ''|*[!0-9]*) echo "cannot:size unknown"; return ;; esac
  [ "$size" -gt 0 ] || { echo "cannot:size unknown"; return; }
  if [ "$size" -gt "$MAX_BYTES" ] && [ "$force" != "1" ]; then
    echo "refuse:it is $((size / 1000000000)) GB -- larger than any stick; --force-large if you are sure"; return
  fi
  echo ok
}

# Facts about a real device: "RM TRAN SIZE MOUNTED HOLDS_ROOT|MODEL|SERIAL"
device_facts() {
  local dev="$1"
  if [ "$(uname -s)" = "Darwin" ]; then
    local info rm tran size mounted root
    info="$(diskutil info "$dev" 2>/dev/null)" || { echo ""; return; }
    rm=0; printf '%s' "$info" | grep -Eq 'Removable Media: +(Removable|Yes)' && rm=1
    tran="$(printf '%s' "$info" | awk -F': +' '/Protocol:/ {print tolower($2); exit}')"
    size="$(printf '%s' "$info" | sed -n 's/.*Disk Size:.*(\([0-9]*\) Bytes).*/\1/p' | head -1)"
    mounted=0; mount | grep -q "^${dev}s\?[0-9]* " && mounted=1
    root=0; [ "$(df / | awk 'NR==2 {print $1}' | sed 's/s[0-9]*$//')" = "$dev" ] && root=1
    echo "$rm ${tran:-none} ${size:-0} $mounted $root|$(printf '%s' "$info" | awk -F': +' '/Media Name:/ {print $2; exit}')|-"
    return
  fi
  local row rm tran size model serial mounted=0 root=0 src pk
  row="$(lsblk -dnbo RM,TRAN,SIZE "$dev" 2>/dev/null | head -1)" || { echo ""; return; }
  [ -n "$row" ] || { echo ""; return; }
  read -r rm tran size <<<"$row"
  if [ -z "$size" ]; then size="$tran"; tran="none"; fi
  model="$(lsblk -dno MODEL "$dev" 2>/dev/null | head -1)"
  serial="$(lsblk -dno SERIAL "$dev" 2>/dev/null | head -1)"
  [ -n "$(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$')" ] && mounted=1
  for m in / /boot /boot/efi; do
    src="$(findmnt -no SOURCE "$m" 2>/dev/null)"; [ -n "$src" ] || continue
    pk="$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)"
    [ "/dev/${pk:-x}" = "$dev" ] || [ "$src" = "$dev" ] && root=1
  done
  echo "$rm ${tran:-none} $size $mounted $root|${model:-?}|${serial:-?}"
}

# dd, then read back exactly the ISO's length and compare.
# STDOUT CARRIES ONLY THE VERDICT: the caller does `case "$(write_and_verify ...)"`, so
# every byte dd prints (records in/out, progress) goes to STDERR -- where the human sees
# it -- and never into the captured verdict. One dd line serves both branches, so the
# self-test's chatty-dd case covers the real-device redirect too.
write_and_verify() {  # write_and_verify ISO DEV -> ok | mismatch
  local iso="$1" dev="$2" size want got bs="4M" conv="notrunc,fsync" prog=""
  [ "$(uname -s)" = "Darwin" ] && bs="4m"
  size=$(wc -c < "$iso" | tr -d ' ')
  if [ -b "$dev" ] || [ -c "$dev" ]; then
    conv="fsync"; prog="status=progress"
  fi
  dd if="$iso" of="$dev" bs="$bs" conv="$conv" ${prog:+"$prog"} 1>&2 || { echo mismatch; return; }
  sync
  want=$(sha_of "$iso")
  got=$(sha_of "$dev" "$size")
  [ "$want" = "$got" ] && echo ok || echo mismatch
}

if [ "$SELFTEST" = "1" ]; then
  fail=0
  chk() { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (got '$1' want '$2')"; fail=1; fi; }
  chk "$(judge_device 1 usb 32000000000 0 0 0)" "ok" "a 32 GB removable USB stick is accepted"
  chk "$(judge_device 0 usb 64000000000 0 0 0)" "ok" "a USB disk that does not report RM is accepted by transport"
  chk "$(judge_device 0 sata 500000000000 0 0 0 | cut -d: -f1)" "refuse" "an internal SATA disk is refused"
  chk "$(judge_device 0 nvme 1000000000000 0 0 0 | cut -d: -f1)" "refuse" "an NVMe disk is refused"
  chk "$(judge_device 1 usb 32000000000 1 0 0 | cut -d: -f1)" "refuse" "a mounted stick is refused"
  chk "$(judge_device 1 usb 32000000000 0 1 0 | cut -d: -f1)" "refuse" "the disk holding / is refused even if removable"
  chk "$(judge_device 1 usb 2000000000000 0 0 0 | cut -d: -f1)" "refuse" "a 2 TB 'stick' is refused"
  chk "$(judge_device 1 usb 2000000000000 0 0 1)" "ok" "...unless --force-large"
  chk "$(judge_device 1 usb '' 0 0 0 | cut -d: -f1)" "cannot" "an unknown size cannot be judged, never assumed"

  T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  head -c 300000 /dev/urandom > "$T/awnix.iso"
  : > "$T/device.img"
  chk "$(write_and_verify "$T/awnix.iso" "$T/device.img" 2>/dev/null)" "ok" "a write is read back and verified (temp-file device)"
  # A real dd is chatty (records in/out, progress on a block device). The verdict the
  # caller captures must still be exactly "ok" -- this was the every-stick-fails bug.
  : > "$T/device2.img"
  chk "$(dd() { echo "0+1 records in"; echo "12 bytes copied" >&2; command dd "$@"; }
         write_and_verify "$T/awnix.iso" "$T/device2.img" 2>/dev/null)" "ok" \
      "dd's own output never leaks into the captured verdict"
  printf 'X' | dd of="$T/device.img" bs=1 seek=1000 conv=notrunc 2>/dev/null
  chk "$([ "$(sha_of "$T/awnix.iso")" = "$(sha_of "$T/device.img" 300000)" ] && echo same || echo differs)" \
      "differs" "a corrupted device is detected by the read-back"
  printf '%s  install.iso\n' "$(sha_of "$T/awnix.iso")" > "$T/SHA256SUMS"
  chk "$(expected_sha "$T/awnix.iso")" "$(sha_of "$T/awnix.iso")" "SHA256SUMS supplies the expected digest"
  printf '{"schema": 1, "sha256": "%s", "parts": [{"name": "a", "size": 1, "sha256": "%s"}]}\n' \
    "$(sha_of "$T/awnix.iso")" "0000000000000000000000000000000000000000000000000000000000000000" > "$T/awnix-iso.json"
  chk "$(expected_sha "$T/awnix.iso")" "$(sha_of "$T/awnix.iso")" "awnix-iso.json wins and its FIRST sha is the image's"
  [ "$fail" = "0" ] && { echo "SELF-TEST PASS"; exit 0; } || { echo "SELF-TEST FAILED"; exit 1; }
fi

[ -n "$ISO" ] && [ -s "$ISO" ] || die "--iso FILE is required and must exist"
[ -n "$DEV" ] || die "--device is required (e.g. /dev/sdb, or /dev/disk4 on macOS)"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo): writing a raw device needs it"

WANT="$(expected_sha "$ISO")"
[ -n "$WANT" ] || dead "no awnix-iso.json or SHA256SUMS next to $ISO -- refusing to write an UNVERIFIED image"
echo "write-awnix-usb: verifying $ISO ..."
GOT="$(sha_of "$ISO")"
[ "$GOT" = "$WANT" ] || die "the ISO does NOT match its published sha256 (got $GOT, want $WANT) -- re-download it"
echo "  iso    : verified ($GOT)"

FACTS="$(device_facts "$DEV")"
[ -n "$FACTS" ] || dead "could not read facts about $DEV (is it a whole disk, e.g. /dev/sdb not /dev/sdb1?)"
read -r F_RM F_TRAN F_SIZE F_MOUNTED F_ROOT <<<"${FACTS%%|*}"
MODEL="$(printf '%s' "$FACTS" | cut -d'|' -f2)"; SERIAL="$(printf '%s' "$FACTS" | cut -d'|' -f3)"
VERDICT="$(judge_device "$F_RM" "$F_TRAN" "$F_SIZE" "$F_MOUNTED" "$F_ROOT" "$FORCE_LARGE")"
case "$VERDICT" in
  ok) : ;;
  cannot:*) dead "$DEV: ${VERDICT#cannot:}" ;;
  *) die "$DEV REFUSED: ${VERDICT#refuse:}" ;;
esac

echo "  device : $DEV"
echo "  model  : $MODEL"
echo "  serial : $SERIAL"
echo "  size   : $((F_SIZE / 1000000000)) GB  (transport ${F_TRAN}, removable ${F_RM})"
echo
echo "  EVERYTHING on $DEV will be erased."
if [ "$YES" != "1" ]; then
  printf '  Type the device name (%s) to continue: ' "$DEV"
  read -r TYPED
  [ "$TYPED" = "$DEV" ] || die "typed '$TYPED', not '$DEV' -- nothing was written"
fi

case "$(write_and_verify "$ISO" "$DEV")" in
  ok) echo; echo "  written and verified: the stick holds exactly $ISO"; exit 0 ;;
  *)  die "the read-back does NOT match the ISO -- the stick is bad or was removed; do not boot it" ;;
esac
