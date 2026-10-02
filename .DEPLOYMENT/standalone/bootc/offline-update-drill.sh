#!/usr/bin/env bash
# offline-update-drill.sh -- prove verify-then-stage of a signed update carried on USB (G5).
#
#   N    = BASE + awnix-offline-update + a drill trust key + a drill unit
#   N+1  = N + a banner (/etc/awnix-drill-banner = OFFLINE-N+1)
#   USB  = a FAT32 disk (label AWNIXUPD) holding three bundles built from N+1:
#            good/      signed by the drill key            -> must stage
#            flipped/   good/ with ONE byte flipped         -> refused before staging
#            wrongkey/  N+1 signed by a key N does not trust -> refused before staging
#   EVD  = a small FAT32 disk (label AWNIXEVD) the guest copies its evidence onto
#
# Boot 1 of N runs `awnix-offline-update stage` on flipped, wrongkey, then good, checking
# after each refusal that `bootc status` shows NOTHING staged, and reboots. Boot 2 must be
# N+1 (banner present, booted digest == the good bundle's manifest_digest) with exactly two
# offline-update.refused records in an awdit chain that verifies.
#
# CI ONLY (awnix-offline-update-proof.yml): builds two images, a qcow2 and ~3x the image
# size of FAT32 media. Never on the owner's desktop.
#
#   sudo bash offline-update-drill.sh [--base IMG] [--work DIR]
# Output: OFFLINE-DRILL-RESULT: PASS|FAIL <why> ; evidence in $WORK/evidence/
# Exit: 0 PASS · 1 FAIL · 2 could not judge (build/boot/tool failure).
set -uo pipefail

BASE="ghcr.io/aitherium/awnix:latest"
WORK=/var/tmp/awnix-offline-drill
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
BIB=quay.io/centos-bootc/bootc-image-builder:latest
SERIAL="$WORK/serial.log"
PY=python3

dead() { echo "OFFLINE-DRILL-RESULT: UNJUDGED $*"; exit 2; }
for t in podman qemu-system-x86_64 mkfs.vfat mcopy "$PY"; do
  command -v "$t" >/dev/null || dead "tool $t missing"
done
[ -r /dev/kvm ] && [ -w /dev/kvm ] || dead "/dev/kvm not usable"
"$PY" -c 'import awseal, awshare, awdit, cryptography' 2>/dev/null \
  || dead "host python lacks awseal/awshare/awdit/cryptography"

rm -rf "${WORK:?}"; mkdir -p "$WORK"/{n,n1,keys,media,out,evidence}
echo "drill: base=$BASE work=$WORK"
podman image exists "$BASE" || podman pull "$BASE" >/dev/null || dead "cannot pull $BASE"

# ── keys: one the node trusts, one it does not (ephemeral, never leave $WORK) ────────
"$PY" - "$WORK/keys" <<'PY' || dead "keygen failed"
import sys, awseal
from pathlib import Path
d = Path(sys.argv[1])
for n in ("trusted", "untrusted"):
    k = awseal.keygen(d / f"{n}.key")
    (d / f"{n}.pub").write_text(awseal.public_key_hex(path=k) + "\n")
PY

# ── N: the CLI + the trust key + the drill unit ──────────────────────────────────────
cp "$HERE/awnix-offline-update.py" "$HERE/awnix-offline-update.tmpfiles.conf" "$WORK/n/"
cp "$WORK/keys/trusted.pub" "$WORK/n/drill.pub"
mkdir -p "$WORK/n/pkgs"
for p in awseal awshare awdit; do cp -r "$REPO/AitherOS/packages/$p" "$WORK/n/pkgs/$p"; done
cat > "$WORK/n/drill.sh" <<'EOF'
#!/bin/bash
# In-guest half of offline-update-drill.sh. Serial is the only channel home.
M=/var/lib/awnix-drill; mkdir -p "$M"
exec > >(tee -a "$M/log" > /dev/ttyS0) 2>&1
OFU=/usr/libexec/awnix/awnix-offline-update
n=$(( $(cat "$M/boots" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$M/boots"
digest() { bootc status --format=json 2>/dev/null | python3.11 -c 'import json,sys
d=json.load(sys.stdin); s=(d.get("status") or {}).get(sys.argv[1]) or {}
print((s.get("image") or {}).get("imageDigest") or "none")' "$1"; }
evidence() {
  mkdir -p /run/evd && mount /dev/disk/by-label/AWNIXEVD /run/evd || { echo "OFFLINE-DRILL: evidence disk missing"; return; }
  cp -f /var/log/awnix/update-audit.jsonl* /var/lib/awnix/offline-update-status.json "$M/log" /run/evd/ 2>/dev/null
  bootc status --format=json > /run/evd/bootc-status.json 2>/dev/null
  echo "$1" > /run/evd/verdict.txt; sync; umount /run/evd
}
finish() { echo "OFFLINE-DRILL-RESULT: $*"; evidence "$*"; systemctl poweroff; exit 0; }
echo "OFFLINE-DRILL: boot $n booted=$(digest booted) staged=$(digest staged)"
MEDIA=/run/media/drill
mkdir -p "$MEDIA"; mount -o ro /dev/disk/by-label/AWNIXUPD "$MEDIA" || finish "FAIL media AWNIXUPD not mountable"
WANT=$(python3.11 -c 'import json;print(json.load(open("/run/media/drill/good/update.json"))["manifest_digest"])')
if [ ! -f "$M/staged" ]; then
  for b in flipped wrongkey; do
    $OFU stage "$MEDIA/$b" --json > "$M/$b.json"; rc=$?
    reason=$(python3.11 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("reason",""))' "$M/$b.json" 2>/dev/null)
    st=$(digest staged)
    echo "OFFLINE-DRILL-STEP: $b rc=$rc reason=$reason staged=$st"
    [ "$rc" = 1 ] || finish "FAIL $b was not refused (rc=$rc)"
    [ "$st" = none ] || finish "FAIL $b left something staged ($st)"
  done
  $OFU stage "$MEDIA/good" --json > "$M/good.json"; rc=$?
  st=$(digest staged)
  echo "OFFLINE-DRILL-STEP: good rc=$rc staged=$st want=$WANT"
  [ "$rc" = 0 ] || { cat "$M/good.json"; finish "FAIL good bundle not staged (rc=$rc)"; }
  touch "$M/staged"; sync
  systemctl reboot; exit 0
fi
booted=$(digest booted)
banner=$(cat /etc/awnix-drill-banner 2>/dev/null || echo absent)
refused=$(grep -c '"event":"offline-update.refused"' /var/log/awnix/update-audit.jsonl 2>/dev/null); refused=${refused:-0}
chain=$(python3.11 -c 'import awdit,sys;r=awdit.verify("/var/log/awnix/update-audit.jsonl");print("ok" if r.ok else "broken:%s" % r.problems[:2])')
echo "OFFLINE-DRILL: after reboot booted=$booted want=$WANT banner=$banner refused=$refused chain=$chain"
[ "$booted" = "$WANT" ] || finish "FAIL booted $booted, expected $WANT"
[ "$banner" = "OFFLINE-N+1" ] || finish "FAIL banner is $banner"
[ "$refused" = 2 ] || finish "FAIL $refused refused records, expected 2"
[ "$chain" = ok ] || finish "FAIL audit chain $chain"
finish "PASS staged+booted N+1 $booted; flipped+wrongkey refused pre-stage; chain ok"
EOF
cat > "$WORK/n/awnix-offline-drill.service" <<'EOF'
[Unit]
Description=awnix offline-update drill
After=multi-user.target
[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=/usr/libexec/awnix-offline-drill.sh
[Install]
WantedBy=multi-user.target
EOF
cat > "$WORK/n/Containerfile" <<EOF
FROM $BASE
COPY pkgs /tmp/pkgs
RUN python3.11 -m pip install --no-cache-dir /tmp/pkgs/awseal /tmp/pkgs/awshare /tmp/pkgs/awdit && rm -rf /tmp/pkgs
COPY awnix-offline-update.py /usr/libexec/awnix/awnix-offline-update
COPY awnix-offline-update.tmpfiles.conf /usr/lib/tmpfiles.d/awnix-offline-update.conf
COPY drill.pub /usr/share/awnix/offline-trust.d/drill.pub
COPY drill.sh /usr/libexec/awnix-offline-drill.sh
COPY awnix-offline-drill.service /usr/lib/systemd/system/awnix-offline-drill.service
RUN sed -i 's/\r\$//' /usr/libexec/awnix/awnix-offline-update /usr/libexec/awnix-offline-drill.sh \\
    && chmod 0755 /usr/libexec/awnix/awnix-offline-update /usr/libexec/awnix-offline-drill.sh \\
    && mkdir -p /usr/lib/awnix && printf 'AWNIX_VARIANT=drill\nAWNIX_IMAGE_REPO=localhost/awnix-offline-drill\n' > /usr/lib/awnix/release.env \\
    && /usr/libexec/awnix/awnix-offline-update --self-test \\
    && systemctl enable awnix-offline-drill.service \\
    && mkdir -p /usr/lib/bootc/kargs.d /usr/lib/bootc/install \\
    && printf 'kargs = ["console=ttyS0,115200n8"]\n' > /usr/lib/bootc/kargs.d/10-serial.toml \\
    && printf '[install.filesystem.root]\ntype = "xfs"\n' > /usr/lib/bootc/install/00-drill.toml
LABEL awnix.drill=offline-n
EOF
podman build -q -t localhost/awnix-offline-drill-n:latest "$WORK/n" > "$WORK/build-n.log" 2>&1 \
  || { tail -20 "$WORK/build-n.log"; dead "N build failed (incl. the CLI --self-test)"; }

# ── N+1: a visible change ────────────────────────────────────────────────────────────
cat > "$WORK/n1/Containerfile" <<'EOF'
FROM localhost/awnix-offline-drill-n:latest
RUN echo OFFLINE-N+1 > /etc/awnix-drill-banner
LABEL awnix.drill=offline-n1
EOF
podman build -q -t localhost/awnix-offline-drill-n1:latest "$WORK/n1" >/dev/null || dead "N+1 build failed"
podman save --format oci-archive -o "$WORK/n1.oci.tar" localhost/awnix-offline-drill-n1:latest \
  || dead "could not save N+1"

# ── the three bundles ────────────────────────────────────────────────────────────────
B="$REPO/AitherOS/dev/tools/build_offline_update_bundle.py"
"$PY" "$B" --archive-from "$WORK/n1.oci.tar" --variant drill --version n1 \
  --key "$WORK/keys/trusted.key" --out "$WORK/media/good" || dead "good bundle build failed"
"$PY" "$B" --archive-from "$WORK/n1.oci.tar" --variant drill --version n1 \
  --key "$WORK/keys/untrusted.key" --out "$WORK/media/wrongkey" || dead "wrongkey bundle build failed"
rm -f "$WORK/n1.oci.tar"
cp -r "$WORK/media/good" "$WORK/media/flipped"
"$PY" - "$WORK/media/flipped" <<'PY' || dead "byte flip failed"
import sys
from pathlib import Path
d = Path(sys.argv[1])
target = d / "image.oci.tar"
if not target.exists():
    target = sorted(d.glob("image.oci.tar.part*"))[0]
with open(target, "r+b") as fh:
    fh.seek(target.stat().st_size // 2)
    b = fh.read(1)
    fh.seek(-1, 1)
    fh.write(bytes([b[0] ^ 0x01]))
print(f"flipped one byte at {target.stat().st_size // 2} of {target.name}")
PY

# ── FAT32 media (no loop mount needed: mtools) ─────────────────────────────────────────
need_mb=$(( $(du -sm "$WORK/media" | cut -f1) * 115 / 100 + 128 ))
truncate -s "${need_mb}M" "$WORK/usb.img"
mkfs.vfat -F 32 -n AWNIXUPD "$WORK/usb.img" >/dev/null || dead "mkfs.vfat usb failed"
MTOOLS_SKIP_CHECK=1 mcopy -s -i "$WORK/usb.img" "$WORK/media/good" "$WORK/media/flipped" "$WORK/media/wrongkey" ::/ \
  || dead "mcopy to usb image failed"
rm -rf "${WORK:?}/media"
truncate -s 64M "$WORK/evd.img"; mkfs.vfat -F 32 -n AWNIXEVD "$WORK/evd.img" >/dev/null || dead "mkfs.vfat evd failed"

# ── disk of N ─────────────────────────────────────────────────────────────────────────
cat > "$WORK/config.toml" <<'EOF'
[[customizations.filesystem]]
mountpoint = "/"
minsize = "60 GiB"
EOF
podman run --rm --privileged --network=host --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage -v "$WORK/out":/output \
  -v "$WORK/config.toml":/config.toml:ro \
  "$BIB" --type qcow2 --local localhost/awnix-offline-drill-n:latest > "$WORK/bib.log" 2>&1
DISK=$(find "$WORK/out" -name '*.qcow2' | head -1)
[ -n "$DISK" ] || { tail -5 "$WORK/bib.log"; dead "no qcow2 (see $WORK/bib.log)"; }

# ── boot until the guest decides (-no-reboot: one launch per boot) ───────────────────
: > "$SERIAL"
for launch in $(seq 1 4); do
  timeout 1800 qemu-system-x86_64 -enable-kvm -m 6144 -smp 4 -cpu host \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$WORK/usb.img",if=virtio,format=raw,readonly=on \
    -drive file="$WORK/evd.img",if=virtio,format=raw \
    -display none -chardev file,id=s0,path="$SERIAL",append=on -serial chardev:s0 \
    -monitor none -no-reboot -net none >> "$WORK/qemu.log" 2>&1
  echo "launch $launch exited rc=$?" >> "$WORK/qemu.log"
  grep -q "OFFLINE-DRILL-RESULT:" "$SERIAL" && break
done

# ── evidence off the FAT disk, re-verified on the host ─────────────────────────────────
MTOOLS_SKIP_CHECK=1 mcopy -n -i "$WORK/evd.img" '::/*' "$WORK/evidence/" 2>/dev/null
cp "$SERIAL" "$WORK/evidence/serial.log"
if [ -s "$WORK/evidence/update-audit.jsonl" ]; then
  "$PY" - "$WORK/evidence/update-audit.jsonl" <<'PY' | tee "$WORK/evidence/host-verify.txt"
import sys, awdit
r = awdit.verify(sys.argv[1])
ev = [x.get("event") for x in awdit.read(sys.argv[1])]
print(f"HOST-AUDIT: chain_ok={bool(r.ok)} count={r.count} refused={ev.count('offline-update.refused')} "
      f"staged={ev.count('offline-update.staged')}")
PY
fi

grep -E "OFFLINE-DRILL" "$SERIAL" | tail -20
line=$(grep -a "OFFLINE-DRILL-RESULT:" "$SERIAL" | tail -1)
case "$line" in
  *"RESULT: PASS"*)
    if grep -q "chain_ok=True count=[0-9]* refused=2 staged=1" "$WORK/evidence/host-verify.txt" 2>/dev/null; then
      echo "OFFLINE-DRILL-VERDICT: PASS"; exit 0
    fi
    echo "OFFLINE-DRILL-VERDICT: FAIL guest said PASS but the host could not re-verify the audit chain"; exit 1 ;;
  *"RESULT: FAIL"*) echo "OFFLINE-DRILL-VERDICT: FAIL"; exit 1 ;;
esac
echo "OFFLINE-DRILL-RESULT: UNJUDGED no verdict in the serial log"; exit 2
