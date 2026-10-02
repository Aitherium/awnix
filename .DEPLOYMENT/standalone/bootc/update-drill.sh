#!/usr/bin/env bash
# update-drill.sh -- prove the signed update chain end to end, in a real VM.
#
#   N           = the image under test + awnix-update FROM THIS TREE + a drill unit
#   N+1-signed  = N plus a generation marker, signed with an ephemeral cosign key
#   N+1-unsigned= N plus a different marker, never signed
#
# A local TLS registry (registry:2, self-signed CA baked into N) serves both on
# 10.0.2.2:5000 -- the address QEMU user networking gives the host. `beta` points at the
# unsigned digest and `stable` at the signed one, so ONE boot can prove both answers.
# Verdicts, each printed to the serial console by the guest:
#
#   UNSIGNED-REFUSED  `awnix update check` on beta exits 1, state unsigned-refused, nothing staged
#   SIGNED-STAGED     `awnix update check` on stable exits 0 and stages exactly that digest
#   APPLIED           `awnix update apply` reboots into N+1-signed
#   ROLLBACK-OK       `awnix update rollback` returns to N
#
# Run as root on a host with podman, qemu and /dev/kvm (the hosted awnix-update-proof
# lane runs it with sudo on ubuntu-latest; never the fleet host):
#   sudo bash update-drill.sh [base-image]    # default ghcr.io/aitherium/awnix:beta
# Env: AWNIX_DRILL_WORK, AWNIX_DRILL_STORAGE, AWNIX_DRILL_BIB (as rollback-drill.sh).
#   AWNIX_DRILL_SIGNER=key      (default) an ephemeral key pair; the guest verifies with
#                               `key:` + no tlog. Runs anywhere.
#   AWNIX_DRILL_SIGNER=keyless  the PRODUCTION path: sign with the GitHub Actions OIDC
#                               identity (Fulcio cert, Rekor entry), and the guest verifies
#                               with --certificate-identity-regexp + the OIDC issuer, so it
#                               must reach Fulcio/Rekor/TUF from inside the VM. Needs
#                               `permissions: id-token: write` and the ACTIONS_ID_TOKEN_*
#                               env passed through sudo. AWNIX_DRILL_IDENTITY overrides the
#                               identity regexp (default: this repo's awnix-update-proof.yml).
# Exit: 0 all four verdicts · 1 a verdict was violated · 2 could not judge.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${1:-ghcr.io/aitherium/awnix:beta}"
WORK="${AWNIX_DRILL_WORK:-/var/tmp/awnix-update-drill}"
STORAGE="${AWNIX_DRILL_STORAGE:-/var/lib/containers/storage}"
BIB="${AWNIX_DRILL_BIB:-quay.io/centos-bootc/bootc-image-builder:latest}"
REGHOST=10.0.2.2:5000
REPO="$REGHOST/awnix-drill"
SERIAL="$WORK/serial.log"
dead() { echo "DRILL-DEAD: $*"; exit 2; }

rm -rf "$WORK"; mkdir -p "$WORK/n" "$WORK/n1s" "$WORK/n1u" "$WORK/out" "$WORK/reg" || dead "no work dir"
for t in podman qemu-system-x86_64 openssl curl; do command -v "$t" >/dev/null || dead "$t missing"; done
[ -e /dev/kvm ] || dead "/dev/kvm missing"

# ── cosign (pinned, checksum-verified) + an EPHEMERAL key that dies with this job ───
bash "$HERE/sign-awnix-image.sh" install "$WORK/bin" >/dev/null || dead "cosign install failed"
COSIGN="$WORK/bin/cosign"
SIGNER_MODE="${AWNIX_DRILL_SIGNER:-key}"
case "$SIGNER_MODE" in
  key)
    ( cd "$WORK" && COSIGN_PASSWORD="" "$COSIGN" generate-key-pair >/dev/null 2>&1 ) || dead "could not generate a key" ;;
  keyless)
    [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] && [ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ] \
      || dead "keyless needs the GitHub Actions OIDC env (id-token: write, sudo --preserve-env=ACTIONS_ID_TOKEN_REQUEST_URL,ACTIONS_ID_TOKEN_REQUEST_TOKEN)"
    GH_REPO_RE=$(printf '%s' "${GITHUB_REPOSITORY:-Aitherium/AitherOS}" | sed 's/[.]/\\./g')
    IDENTITY="${AWNIX_DRILL_IDENTITY:-^https://github\.com/$GH_REPO_RE/\.github/workflows/awnix-update-proof\.yml@.*\$}" ;;
  *) dead "AWNIX_DRILL_SIGNER must be key or keyless (got '$SIGNER_MODE')" ;;
esac

# ── the registry: TLS with a throwaway CA, reachable as 10.0.2.2 from host AND guest ──
ip addr show dev lo | grep -q "10.0.2.2/32" || ip addr add 10.0.2.2/32 dev lo || dead "cannot alias 10.0.2.2"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=awnix-drill-registry" \
  -addext "subjectAltName=IP:10.0.2.2,IP:127.0.0.1" \
  -keyout "$WORK/reg/tls.key" -out "$WORK/reg/tls.crt" >/dev/null 2>&1 || dead "no registry cert"
mkdir -p "/etc/containers/certs.d/$REGHOST" && cp "$WORK/reg/tls.crt" "/etc/containers/certs.d/$REGHOST/ca.crt"
podman rm -f awnix-drill-registry >/dev/null 2>&1
podman run -d --name awnix-drill-registry -p 5000:5000 -v "$WORK/reg":/certs:Z \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/tls.crt -e REGISTRY_HTTP_TLS_KEY=/certs/tls.key \
  docker.io/library/registry:2 >/dev/null || dead "registry did not start"
trap 'podman rm -f awnix-drill-registry >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do curl -s --cacert "$WORK/reg/tls.crt" "https://$REGHOST/v2/" >/dev/null && break; sleep 1; done

if ! podman image exists "$BASE"; then podman pull "$BASE" >/dev/null || dead "could not pull $BASE"; fi

# ── N: the chain under test, pointed at the drill registry and the drill key ─────────
cp "$HERE/awnix-update.sh" "$WORK/n/awnix-update"
cp "$COSIGN" "$WORK/n/cosign"; cp "$WORK/reg/tls.crt" "$WORK/n/drill-ca.crt"
if [ "$SIGNER_MODE" = key ]; then
  cp "$WORK/cosign.pub" "$WORK/n/drill-cosign.pub"
  printf '%s key:/usr/share/awnix/drill-cosign.pub\n' "$REPO" > "$WORK/n/signers.conf"
  printf 'CHANNEL=stable\nAUTO_APPLY=0\nIMAGE_REPO=%s\nCOSIGN_IGNORE_TLOG=1\n' "$REPO" > "$WORK/n/update.conf"
else
  : > "$WORK/n/drill-cosign.pub"   # the COPY below stays unconditional; unused in keyless
  printf '%s %s\n' "$REPO" "$IDENTITY" > "$WORK/n/signers.conf"
  printf 'CHANNEL=stable\nAUTO_APPLY=0\nIMAGE_REPO=%s\n' "$REPO" > "$WORK/n/update.conf"
fi
echo "drill signer: $SIGNER_MODE $(cat "$WORK/n/signers.conf")"
cat > "$WORK/n/drill.sh" <<'EOF'
#!/bin/bash
M=/var/lib/awnix-drill; mkdir -p "$M"
exec > >(tee -a "$M/log" > /dev/ttyS0) 2>&1
# DRILL lines are written SYNCHRONOUSLY, never through the tee above: when the unit's main
# process exits right after `awnix update apply`, systemd kills the cgroup -- tee included --
# before it drains, and run 36433443705 lost exactly the SIGNED-STAGED line that way.
say() { printf '%s\n' "$*" >> "$M/log"; printf '%s\n' "$*" > /dev/ttyS0; }
U=/usr/libexec/awnix/awnix-update
gen=$(cat /usr/share/awnix-drill/generation 2>/dev/null)
stage=$(cat "$M/stage" 2>/dev/null || echo 0)
st() { python3 -c 'import json;print(json.load(open("/var/lib/awnix/update-status.json"))["state"])' 2>/dev/null; }
staged() { { bootc status --json 2>/dev/null || bootc status --format=json 2>/dev/null; } | python3 -c 'import json,sys;s=(json.load(sys.stdin).get("status") or {}).get("staged");print(((s or {}).get("image") or {}).get("imageDigest") or "")'; }
say "DRILL: stage=$stage generation=$gen"
case "$stage" in
  0)
    [ "$gen" = n ] || { say "DRILL-FAIL: first boot is not N ($gen)"; systemctl poweroff; exit 0; }
    $U channel beta; $U check; rc=$?
    if [ "$rc" = 1 ] && [ "$(st)" = unsigned-refused ] && [ -z "$(staged)" ]; then say "DRILL-VERDICT: UNSIGNED-REFUSED"
    else say "DRILL-FAIL: unsigned beta was not refused (rc=$rc state=$(st) staged=$(staged))"; systemctl poweroff; exit 0; fi
    $U channel stable; $U check; rc=$?
    if [ "$rc" = 0 ] && [ "$(st)" = staged ] && [ -n "$(staged)" ]; then say "DRILL-VERDICT: SIGNED-STAGED $(staged)"
    else say "DRILL-FAIL: signed stable was not staged (rc=$rc state=$(st))"; systemctl poweroff; exit 0; fi
    echo 1 > "$M/stage"; sync; $U apply ;;
  1)
    if [ "$gen" = n1signed ]; then say "DRILL-VERDICT: APPLIED"; else say "DRILL-FAIL: apply did not boot N+1 ($gen)"; systemctl poweroff; exit 0; fi
    echo 2 > "$M/stage"; sync; $U rollback || { say "DRILL-FAIL: rollback refused"; systemctl poweroff; } ;;
  2)
    if [ "$gen" = n ]; then say "DRILL-VERDICT: ROLLBACK-OK"; else say "DRILL-FAIL: rollback did not return to N ($gen)"; fi
    say "DRILL-RESULT: DONE"; systemctl poweroff ;;
esac
EOF
cat > "$WORK/n/awnix-drill.service" <<'EOF'
[Unit]
Description=awnix update drill
After=network-online.target multi-user.target
Wants=network-online.target
[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=/usr/libexec/awnix-update-drill.sh
[Install]
WantedBy=multi-user.target
EOF
# awnix-setup.service is the INTERACTIVE first-boot prompt on tty1; headless it waits
# forever, multi-user.target is never reached and the drill unit never starts (run
# 36417109665: eight 15-min boots, no DRILL line). The drill image masks it.
cat > "$WORK/n/Containerfile" <<EOF
FROM $BASE
COPY awnix-update /usr/libexec/awnix/awnix-update
COPY cosign /usr/bin/cosign
COPY signers.conf /usr/share/awnix/signers.conf
COPY drill-cosign.pub /usr/share/awnix/drill-cosign.pub
COPY update.conf /etc/awnix/update.conf
COPY drill-ca.crt /etc/containers/certs.d/$REGHOST/ca.crt
COPY drill-ca.crt /etc/pki/ca-trust/source/anchors/awnix-drill-ca.crt
COPY drill.sh /usr/libexec/awnix-update-drill.sh
COPY awnix-drill.service /usr/lib/systemd/system/awnix-update-drill.service
RUN sed -i 's/\r\$//' /usr/libexec/awnix/awnix-update \
    && chmod 0755 /usr/libexec/awnix/awnix-update /usr/bin/cosign /usr/libexec/awnix-update-drill.sh \
    && update-ca-trust \
    && /usr/libexec/awnix/awnix-update --self-test \
    && mkdir -p /usr/share/awnix-drill && echo n > /usr/share/awnix-drill/generation \
    && systemctl enable awnix-update-drill.service \
    && (systemctl mask awnix-setup.service || true) \
    && (systemctl mask bootc-fetch-apply-updates.timer || true) \
    && mkdir -p /usr/lib/bootc/kargs.d \
    && printf 'kargs = ["console=ttyS0,115200n8"]\n' > /usr/lib/bootc/kargs.d/10-serial.toml \
    && mkdir -p /usr/lib/bootc/install \
    && printf '[install.filesystem.root]\ntype = "xfs"\n' > /usr/lib/bootc/install/00-drill.toml
LABEL awnix.drill=update-n
EOF
podman build -q -t localhost/awnix-update-drill-n:latest "$WORK/n" >/dev/null || dead "N build failed"
for g in n1signed n1unsigned; do
  d="$WORK/n1s"; [ "$g" = n1unsigned ] && d="$WORK/n1u"
  printf 'FROM localhost/awnix-update-drill-n:latest\nRUN echo %s > /usr/share/awnix-drill/generation\n' "$g" > "$d/Containerfile"
  podman build -q -t "localhost/awnix-update-drill-$g:latest" "$d" >/dev/null || dead "$g build failed"
done

# ── publish: stable -> signed, beta -> unsigned. Sign by DIGEST. ─────────────────────
podman push -q --digestfile "$WORK/signed.digest" localhost/awnix-update-drill-n1signed:latest "docker://$REPO:stable" || dead "push stable"
podman push -q --digestfile "$WORK/unsigned.digest" localhost/awnix-update-drill-n1unsigned:latest "docker://$REPO:beta" || dead "push beta"
SSL_CERT_FILE="$WORK/ca-bundle.pem"
cat /etc/ssl/certs/ca-certificates.crt "$WORK/reg/tls.crt" > "$SSL_CERT_FILE" 2>/dev/null || cp "$WORK/reg/tls.crt" "$SSL_CERT_FILE"
if [ "$SIGNER_MODE" = key ]; then
  ( cd "$WORK" && SSL_CERT_FILE="$SSL_CERT_FILE" COSIGN_PASSWORD="" "$COSIGN" sign --yes --tlog-upload=false \
      --key cosign.key "$REPO@$(cat signed.digest)" >/dev/null 2>&1 ) || dead "could not sign N+1"
  SSL_CERT_FILE="$SSL_CERT_FILE" "$COSIGN" verify --key "$WORK/cosign.pub" --insecure-ignore-tlog=true \
      "$REPO@$(cat "$WORK/unsigned.digest")" >/dev/null 2>&1 && dead "the unsigned image verifies -- the drill cannot tell"
else
  ( cd "$WORK" && SSL_CERT_FILE="$SSL_CERT_FILE" "$COSIGN" sign --yes "$REPO@$(cat signed.digest)" > sign.log 2>&1 ) \
      || { tail -5 "$WORK/sign.log"; dead "could not sign N+1 keylessly"; }
  SSL_CERT_FILE="$SSL_CERT_FILE" "$COSIGN" verify --certificate-identity-regexp "$IDENTITY" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
      "$REPO@$(cat "$WORK/signed.digest")" >/dev/null 2>&1 || dead "the keyless signature does not verify on the host"
  SSL_CERT_FILE="$SSL_CERT_FILE" "$COSIGN" verify --certificate-identity-regexp "$IDENTITY" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
      "$REPO@$(cat "$WORK/unsigned.digest")" >/dev/null 2>&1 && dead "the unsigned image verifies -- the drill cannot tell"
fi

# ── disk + boot loop (as rollback-drill.sh) ───────────────────────────────────────────
podman run --rm --privileged --network=host --security-opt label=type:unconfined_t \
  -v "$STORAGE":/var/lib/containers/storage -v "$WORK/out":/output \
  "$BIB" --type qcow2 --local localhost/awnix-update-drill-n:latest > "$WORK/bib.log" 2>&1
DISK=$(find "$WORK/out" -name '*.qcow2' | head -1)
[ -n "$DISK" ] || { tail -5 "$WORK/bib.log"; dead "no qcow2 (see $WORK/bib.log)"; }

: > "$SERIAL"
for launch in $(seq 1 8); do
  timeout 900 qemu-system-x86_64 -enable-kvm -m 4096 -smp 4 -cpu host \
    -drive file="$DISK",if=virtio,format=qcow2 -display none \
    -chardev file,id=s0,path="$SERIAL",append=on -serial chardev:s0 \
    -monitor none -no-reboot -nic user,model=virtio-net-pci >> "$WORK/qemu.log" 2>&1
  echo "launch $launch exited rc=$?" >> "$WORK/qemu.log"
  grep -qE "DRILL-RESULT:|DRILL-FAIL:" "$SERIAL" && break
done

grep -E "DRILL" "$SERIAL" | tail -30
if grep -q "DRILL-FAIL:" "$SERIAL"; then echo "VERDICT: VIOLATED"; exit 1; fi
ok=1
for v in UNSIGNED-REFUSED SIGNED-STAGED APPLIED ROLLBACK-OK; do
  grep -q "DRILL-VERDICT: $v" "$SERIAL" || { echo "missing verdict: $v"; ok=0; }
done
[ "$ok" = 1 ] && { echo "VERDICT: PASS (unsigned refused, signed staged, applied, rolled back)"; exit 0; }
echo "VERDICT: UNJUDGED"; exit 2
