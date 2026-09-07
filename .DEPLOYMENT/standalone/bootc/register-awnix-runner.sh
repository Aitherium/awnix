#!/bin/bash
# Register this awnix-runner box as a GitHub Actions runner, AT FIRST BOOT.
#
# WHY THIS IS A SEPARATE FIRST-BOOT STEP AND NOT BAKED INTO THE IMAGE:
# a registration token is per-instance and short-lived; baking one into the
# image would either go stale before the image is ever booted, or -- worse
# -- get reused across every instance booted from the same image, which
# `config.sh --replace` would silently let happen (each new boot evicting
# the previous instance's registration). cloud-init user-data invokes this
# script with a fresh token minted for THIS instance only.
#
# WHY THE ENTIRE LIFECYCLE RUNS AS THE UNPRIVILEGED `runner` USER, NOT ROOT:
# GitHub's own runner supports a non-root systemd USER unit install (not the
# system-wide `svc.sh install root` path `provision_github_runner.sh` uses
# on the trusted local host) -- svc.sh decides which one based on who invokes
# it, so config.sh AND svc.sh both run via `su - runner`, never as root
# directly. `loginctl enable-linger runner` (set at image build time) is
# what keeps the resulting user unit alive across boots/logout.
#
# Usage (from cloud-init user-data, as root):
#   GH_RUNNER_TOKEN_FILE=/tmp/token GH_RUNNER_URL=https://github.com/Aitherium \
#     GH_RUNNER_NAME=awnix-aws-1 GH_RUNNER_LABELS=self-hosted,Linux,X64,awnix \
#     /usr/local/sbin/register-awnix-runner.sh
set -euo pipefail

die() { echo "register-awnix-runner: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (drops to 'runner' internally) -- got uid $(id -u)"
[ -n "${GH_RUNNER_TOKEN_FILE:-}" ] || die "GH_RUNNER_TOKEN_FILE is required (a file, never an argv token)"
[ -r "$GH_RUNNER_TOKEN_FILE" ] || die "GH_RUNNER_TOKEN_FILE not readable: $GH_RUNNER_TOKEN_FILE"
[ -n "${GH_RUNNER_URL:-}" ] || die "GH_RUNNER_URL is required (e.g. https://github.com/Aitherium)"

NAME="${GH_RUNNER_NAME:-$(hostname)}"
LABELS="${GH_RUNNER_LABELS:-self-hosted,Linux,X64,awnix}"
TOKEN="$(tr -d '\r\n' < "$GH_RUNNER_TOKEN_FILE")"
[ -n "$TOKEN" ] || die "token file was empty"

DEST=/opt/actions-runner
[ -x "$DEST/config.sh" ] || die "runner binary missing at $DEST -- image build did not stage it"

if [ -f "$DEST/.runner" ]; then
  echo "already configured -- nothing to do (idempotent, matches provision_github_runner.sh)"
  exit 0
fi

chown -R runner:runner "$DEST"

su - runner -c "cd '$DEST' && ./config.sh \
  --url '$GH_RUNNER_URL' \
  --token '$TOKEN' \
  --name '$NAME' \
  --labels '$LABELS' \
  --work _work \
  --unattended --replace"

su - runner -c "cd '$DEST' && ./svc.sh install"

# RESTART=ALWAYS, before the service is ever started.
#
# GitHub's svc.sh generates a unit with NO `Restart=` line, so systemd's default
# `Restart=no` applies -- and a GitHub runner exits CLEANLY (status 0) when it
# self-updates. So it needs no fault at all to die permanently: the agent
# updates itself, exits 0, systemd does what it was told, and the registration
# sits in the runner list looking like capacity forever. It happens in WAVES,
# because the update reaches every box at once.
#
# This is D-2131, arriving on the awnix fleet before the awnix fleet exists.
# Measured on the EC2 pool 2026-09-05/06: the offline set went
# {8,9,12,15,16,18,19,20,21,23} -> {15,16,18,21,23}, every survivor carrying an
# IDENTICAL fresh first-offline stamp, i.e. all of them had been online in
# between. That was diagnosed for a year as dead instances worth ~$9/day, and
# the proposed repair -- terminate and relaunch -- would have paid to replace
# machines that were coming back on their own AND rebuilt the defect on each new
# box. deploy_aws_runner.py was fixed the same day; this is the same defect in
# the lane we are MOVING TO, fixed before it can ship.
#
# `Restart=on-failure` would NOT help: the exit this must survive is a clean one.
#
# The unit NAME is read from $DEST/.service, which svc.sh writes, rather than
# reconstructed from org+name. A reconstructed `actions.runner.<org>.<name>.service`
# that is subtly wrong yields a drop-in for a unit that does not exist, and that
# is SILENT -- systemd loads nothing, the runner works fine today, and the
# resilience is simply absent.
SVC_NAME="$(tr -d '[:space:]' < "$DEST/.service" 2>/dev/null || true)"
[ -n "$SVC_NAME" ] || die "svc.sh install left no unit name in $DEST/.service -- refusing to
start a runner that cannot survive its own self-update (D-2131)"

# svc.sh installs a SYSTEM unit when invoked by root and a USER unit otherwise,
# and this script deliberately delegates DOWN to `runner`. Rather than assume
# which happened, look -- and refuse if neither exists, because a drop-in
# written to the wrong scope is the silent failure described above.
RUNNER_HOME="$(getent passwd runner | cut -d: -f6)"
USER_UNIT="$RUNNER_HOME/.config/systemd/user/$SVC_NAME"
SYS_UNIT="/etc/systemd/system/$SVC_NAME"
if [ -f "$USER_UNIT" ]; then
  install -d -o runner -g runner "$RUNNER_HOME/.config/systemd/user/$SVC_NAME.d"
  cat > "$RUNNER_HOME/.config/systemd/user/$SVC_NAME.d/restart.conf" <<'RESTART'
[Service]
Restart=always
RestartSec=15
RESTART
  chown runner:runner "$RUNNER_HOME/.config/systemd/user/$SVC_NAME.d/restart.conf"
  su - runner -c "systemctl --user daemon-reload"
  echo "pinned Restart=always on USER unit $SVC_NAME (D-2131)"
elif [ -f "$SYS_UNIT" ]; then
  mkdir -p "/etc/systemd/system/$SVC_NAME.d"
  cat > "/etc/systemd/system/$SVC_NAME.d/restart.conf" <<'RESTART'
[Service]
Restart=always
RestartSec=15
RESTART
  systemctl daemon-reload
  echo "pinned Restart=always on SYSTEM unit $SVC_NAME (D-2131)"
else
  die "unit $SVC_NAME is in neither $USER_UNIT nor $SYS_UNIT -- refusing to start a runner
that cannot survive its own self-update (D-2131)"
fi

su - runner -c "cd '$DEST' && ./svc.sh start"

echo "registered and started (non-root, user systemd unit): $NAME"
