#!/usr/bin/env bash
# register-spark-arm64-runner.sh -- make the DGX Spark (GB10, aarch64) an arm64 BUILD
# runner for .github/workflows/awnix-arm64.yml without touching what it already does.
#
# The Spark serves production inference. Everything below exists so a build can
# never take that away:
#   * a dedicated, unprivileged account (awnix-build): no sudo, no docker group,
#     rootless podman through subuid/subgid only;
#   * its OWN podman store (/etc/awnix-build/storage.conf -> /var/lib/awnix-build),
#     which the workflow names in CONTAINERS_STORAGE_CONF -- the inference store is
#     never opened, so no build can prune, migrate or lock it;
#   * a capped systemd slice (CPUQuota, MemoryMax, low CPU/IO weight), so a build
#     runs in the pool's leftovers, never its headroom;
#   * labels [self-hosted, Linux, ARM64, spark-build, offbox] and NEVER aitheros-local
#     (check_awnix_multiarch.py AMA007; the 2026-09-21 migrate incident).
# It never runs docker, nvidia-smi, or anything against the pool's units.
#
# Usage:
#   register-spark-arm64-runner.sh                         # --dry-run: print the plan
#   register-spark-arm64-runner.sh --apply --token-file F  # OWNER-CONSENTED, on the Spark
#   register-spark-arm64-runner.sh --self-test
#
# The registration token is read from a 0600 file and handed to config.sh through
# its ACTIONS_RUNNER_INPUT_TOKEN environment input -- never argv, never echoed.
# Exit: 0 ok, 1 refused/failed, 2 could not judge (not aarch64, not root for --apply).
set -euo pipefail

MODE=dry-run
TOKEN_FILE=""
REPO_URL="${AWNIX_BUILD_REPO_URL:-https://github.com/Aitherium/AitherOS}"
RUNNER_VERSION="${AWNIX_BUILD_RUNNER_VERSION:-2.336.0}"
# SHA-256 actions/runner publishes for actions-runner-linux-arm64-2.336.0.tar.gz (the
# release notes and the API asset digest agree, checked 2026-09-28). A different
# AWNIX_BUILD_RUNNER_VERSION must come with its own AWNIX_BUILD_RUNNER_SHA256.
RUNNER_SHA256="${AWNIX_BUILD_RUNNER_SHA256:-58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1}"
RUNNER_NAME="${AWNIX_BUILD_RUNNER_NAME:-spark-arm64-build}"
LABELS="spark-build,offbox"        # config.sh adds self-hosted,Linux,ARM64 itself; offbox = SHW015 pool
BUILD_USER=awnix-build
HOME_DIR=/var/lib/awnix-build
STORE_CONF=/etc/awnix-build/storage.conf
RUNNER_DIR=/opt/awnix-build-runner
SLICE=awnix-build.slice
UNIT=awnix-build-runner.service
CPU_QUOTA="${AWNIX_BUILD_CPU_QUOTA:-400%}"     # 4 of the GB10's 20 cores
MEM_MAX="${AWNIX_BUILD_MEM_MAX:-16G}"          # of 128G unified; the pool keeps the rest
SUBID_START=300000

die() { echo "register-spark-arm64-runner: $*" >&2; exit "${2:-1}"; }

storage_conf() {
  cat <<EOF
# awnix-build's OWN podman store. Written by register-spark-arm64-runner.sh.
# The inference containers live elsewhere; this file is what keeps builds out.
[storage]
driver = "overlay"
graphroot = "$HOME_DIR/storage"
runroot = "$HOME_DIR/runroot"
EOF
}

slice_unit() {
  cat <<EOF
[Unit]
Description=awnix arm64 build runner (capped; inference comes first)

[Slice]
CPUQuota=$CPU_QUOTA
CPUWeight=20
MemoryMax=$MEM_MAX
MemoryHigh=$MEM_MAX
IOWeight=20
TasksMax=4096
EOF
}

service_unit() {
  cat <<EOF
[Unit]
Description=GitHub Actions runner $RUNNER_NAME (awnix arm64 builds)
After=network-online.target
Wants=network-online.target

[Service]
User=$BUILD_USER
WorkingDirectory=$RUNNER_DIR
Slice=$SLICE
Nice=10
Environment=CONTAINERS_STORAGE_CONF=$STORE_CONF
Environment=HOME=$HOME_DIR
ExecStart=$RUNNER_DIR/run.sh
Restart=on-failure
RestartSec=30
KillMode=mixed
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

plan() {
  cat <<EOF
== plan (arch $(uname -m), mode $MODE)
1. useradd --system --create-home --home-dir $HOME_DIR --shell /usr/sbin/nologin $BUILD_USER
   (no sudoers entry, no container-engine group membership)
2. subuid/subgid: $BUILD_USER:$SUBID_START:65536 (only if the account has none)
3. mkdir $HOME_DIR/{storage,runroot} owned by $BUILD_USER, 0700
4. write $STORE_CONF:
$(storage_conf | sed 's/^/     /')
5. write /etc/systemd/system/$SLICE:
$(slice_unit | sed 's/^/     /')
6. download actions-runner-linux-arm64-$RUNNER_VERSION, check it against sha256 $RUNNER_SHA256, extract into $RUNNER_DIR (owned by $BUILD_USER)
7. config.sh --unattended --url $REPO_URL --name $RUNNER_NAME --labels $LABELS --replace
   (token from the 0600 --token-file via ACTIONS_RUNNER_INPUT_TOKEN)
8. write /etc/systemd/system/$UNIT and enable --now it:
$(service_unit | sed 's/^/     /')
Never: docker, nvidia-smi, the pool's units, podman against the default store.
EOF
}

self_test() {
  local bad=0 text
  text="$(plan)"
  echo "$text" | grep -q "graphroot = \"$HOME_DIR/storage\"" || { echo "  [FAIL] own graphroot missing"; bad=1; }
  echo "$text" | grep -q "CPUQuota=" || { echo "  [FAIL] no CPU cap"; bad=1; }
  echo "$text" | grep -q "MemoryMax=" || { echo "  [FAIL] no memory cap"; bad=1; }
  echo "$text" | grep -q "Environment=CONTAINERS_STORAGE_CONF=$STORE_CONF" || { echo "  [FAIL] unit does not pin the store"; bad=1; }
  case ",$LABELS," in *,aitheros-local,*) echo "  [FAIL] aitheros-local label"; bad=1 ;; esac
  echo "$RUNNER_SHA256" | grep -qE '^[0-9a-f]{64}$' || { echo "  [FAIL] runner tarball SHA-256 is not pinned"; bad=1; }
  grep -q 'sha256sum -c' "${BASH_SOURCE[0]}" || { echo "  [FAIL] the runner tarball is extracted without a hash check"; bad=1; }
  # the script body must not contain the verbs it promises never to run
  if grep -vE '^\s*#' "${BASH_SOURCE[0]}" | grep -vE 'grep|Never:|never' \
      | grep -qE '(docker|nvidia-smi|podman +(system|image|container|volume|rm|stop))'; then
    echo "  [FAIL] a forbidden verb appears in the script body"; bad=1
  fi
  [ "$bad" -eq 0 ] && echo "self-test: PASS" || echo "self-test: FAIL"
  return "$bad"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry-run; shift ;;
    --apply) MODE=apply; shift ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --self-test) self_test; exit $? ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

if [ "$MODE" = dry-run ]; then
  plan
  echo "(dry run: nothing was changed; --apply --token-file F on the Spark, owner-consented)"
  exit 0
fi

# ── apply ─────────────────────────────────────────────────────────────────────
[ "$(uname -m)" = aarch64 ] || die "this is for the Spark (aarch64); host is $(uname -m)" 2
[ "$(id -u)" -eq 0 ] || die "--apply needs root (it creates an account and units)" 2
[ -n "$TOKEN_FILE" ] && [ -r "$TOKEN_FILE" ] || die "--apply needs --token-file F (a 0600 file holding a registration token)"
perm="$(stat -c %a "$TOKEN_FILE")"
[ "$perm" = 600 ] || [ "$perm" = 400 ] || die "$TOKEN_FILE is mode $perm; refusing a registration token readable by others"
command -v podman >/dev/null || die "podman is not installed; install it first (this script installs nothing system-wide)"

plan
id "$BUILD_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$BUILD_USER"
grep -q "^$BUILD_USER:" /etc/subuid || echo "$BUILD_USER:$SUBID_START:65536" >> /etc/subuid
grep -q "^$BUILD_USER:" /etc/subgid || echo "$BUILD_USER:$SUBID_START:65536" >> /etc/subgid
install -d -m 0700 -o "$BUILD_USER" -g "$BUILD_USER" "$HOME_DIR/storage" "$HOME_DIR/runroot"
install -d -m 0755 "$(dirname "$STORE_CONF")"
storage_conf > "$STORE_CONF"; chmod 0644 "$STORE_CONF"
slice_unit > "/etc/systemd/system/$SLICE"
install -d -m 0755 -o "$BUILD_USER" -g "$BUILD_USER" "$RUNNER_DIR"
if [ ! -x "$RUNNER_DIR/config.sh" ]; then
  tmp="$(mktemp)"
  curl -fsSL -o "$tmp" "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz"
  echo "$RUNNER_SHA256  $tmp" | sha256sum -c - >/dev/null || { rm -f "$tmp"; die "actions-runner tarball does not match the pinned SHA-256; refusing to extract it as root"; }
  tar -xzf "$tmp" -C "$RUNNER_DIR"; rm -f "$tmp"
  chown -R "$BUILD_USER:$BUILD_USER" "$RUNNER_DIR"
fi
# The token goes through the runner's own environment input, never argv.
ACTIONS_RUNNER_INPUT_TOKEN="$(cat "$TOKEN_FILE")" \
  runuser -u "$BUILD_USER" --preserve-environment -- \
  env HOME="$HOME_DIR" "$RUNNER_DIR/config.sh" --unattended --url "$REPO_URL" \
    --name "$RUNNER_NAME" --labels "$LABELS" --replace
service_unit > "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable --now "$UNIT"
systemctl --no-pager --lines=0 status "$UNIT" || true
echo "registered $RUNNER_NAME [self-hosted, Linux, ARM64, $LABELS]; store $STORE_CONF; slice $SLICE ($CPU_QUOTA, $MEM_MAX)"
