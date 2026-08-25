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

su - runner -c "cd '$DEST' && ./svc.sh install && ./svc.sh start"

echo "registered and started (non-root, user systemd unit): $NAME"
