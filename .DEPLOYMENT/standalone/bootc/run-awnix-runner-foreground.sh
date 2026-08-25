#!/bin/bash
# Register + run the GitHub Actions runner in the FOREGROUND, for the case
# where this image runs as a plain `podman run` container on a generic host
# (a fresh EC2 Ubuntu box) rather than as the awnix bootc appliance itself.
#
# WHY THIS EXISTS SEPARATELY FROM register-awnix-runner.sh:
# that script's last step is `svc.sh install` -- a systemd USER unit, which
# needs a real systemd + a logind user session inside whatever it runs in.
# A bootc-built awnix box IS that (systemd as PID 1, `loginctl enable-linger
# runner` baked in at image build time) -- a plain `podman run` container on
# a generic host is NOT, unless launched with `--systemd=always` and cgroup
# delegation, which is real extra complexity this path does not need.
# Simpler and equally correct for a container: `config.sh` once, then EXEC
# `run.sh` in the FOREGROUND so the container's own main process IS the
# runner listener -- `podman run -d --restart=always` on the HOST is what
# gives this the same resilience `svc.sh` would have provided via systemd.
#
# Usage (as the container's entrypoint/command, run as root, drops to
# 'runner' internally -- same env-var contract as register-awnix-runner.sh):
#   GH_RUNNER_TOKEN_FILE=/run/secrets/gh-token GH_RUNNER_URL=https://github.com/Aitherium \
#     GH_RUNNER_NAME=awnix-aws-1 GH_RUNNER_LABELS=self-hosted,Linux,X64,awnix,aws \
#     /usr/local/sbin/run-awnix-runner-foreground.sh
set -euo pipefail

die() { echo "run-awnix-runner-foreground: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (drops to 'runner' internally) -- got uid $(id -u)"

# MAKE / SHARED WHILE WE ARE STILL ROOT. This is what lets the jobs this runner
# executes use podman at all, and awnix's whole purpose is building images.
#
# Measured 2026-08-21, twice, on a freshly provisioned box: every build died with
#   "/" is not a shared mount ... rootless containers
#   configure storage: overlay: failed to make mount private ... permission denied
# under overlay, fuse-overlayfs AND vfs.
#
# The outer container already carries --cap-add SYS_ADMIN, and that is genuinely
# enough -- proven directly: `mount --make-rprivate /` succeeds inside a container
# with exactly those scoped flags. It fails for the RUNNER because the inner podman
# is ROOTLESS: `runner` (uid 1000, unprivileged in this container) enters its own
# user namespace, and SYS_ADMIN granted to the container's root does not travel
# there. So the propagation has to be fixed HERE, in the one window where this
# process is still root, before `su runner`.
#
# Best-effort on purpose: a host that already presents a shared / makes this a
# no-op, and a host that forbids it should not stop the runner from registering
# and reporting the real failure at build time with a message that names the cause.
if mount --make-rshared / 2>/dev/null; then
  echo "run-awnix-runner-foreground: / is now rshared (nested rootless podman can mount)"
else
  echo "run-awnix-runner-foreground: WARNING could not make / rshared -- nested podman" \
       "builds will fail with 'failed to make mount private'; the container needs" \
       "--cap-add SYS_ADMIN (see deploy_aws_runner.py)" >&2
fi
[ -n "${GH_RUNNER_TOKEN_FILE:-}" ] || die "GH_RUNNER_TOKEN_FILE is required (a file, never an argv token)"
[ -r "$GH_RUNNER_TOKEN_FILE" ] || die "GH_RUNNER_TOKEN_FILE not readable: $GH_RUNNER_TOKEN_FILE"
[ -n "${GH_RUNNER_URL:-}" ] || die "GH_RUNNER_URL is required (e.g. https://github.com/Aitherium)"

NAME="${GH_RUNNER_NAME:-$(hostname)}"
LABELS="${GH_RUNNER_LABELS:-self-hosted,Linux,X64,awnix}"
TOKEN="$(tr -d '\r\n' < "$GH_RUNNER_TOKEN_FILE")"
[ -n "$TOKEN" ] || die "token file was empty"

DEST=/opt/actions-runner
[ -x "$DEST/config.sh" ] || die "runner binary missing at $DEST -- image build did not stage it"

# RUN AS ROOT, DELIBERATELY -- this image exists to build bootable media.
#
# bootc-image-builder needs root: build-awnix-iso.sh refuses immediately with
# "must run as root (bootc-image-builder needs --privileged)". Measured 2026-08-21,
# the image layer built fine as `runner` and the MEDIA step could not start -- so an
# awnix runner that drops privileges cannot do the one job awnix is for.
#
# The container already runs --privileged (nested podman needs it; the scoped
# capability set left / PRIVATE and every storage driver failed to make its mount
# private). Dropping to an unprivileged user INSIDE a privileged container buys very
# little: `runner` has no sudo, and the boundary that matters is the container.
#
# The runner refuses to run as root unless told to -- a good default on a shared
# machine and the wrong one here. Gated on AWNIX_RUNNER_AS_ROOT so this stays a
# per-deployment decision rather than a property of the script: a box provisioned to
# build media sets it, anything else keeps the unprivileged path.
AS_ROOT="${AWNIX_RUNNER_AS_ROOT:-0}"

if [ "$AS_ROOT" = "1" ]; then
  echo "running the listener as ROOT (AWNIX_RUNNER_AS_ROOT=1) -- bootc-image-builder"
  echo "  cannot run unprivileged; the container is already --privileged"
  export RUNNER_ALLOW_RUNASROOT=1
else
  chown -R runner:runner "$DEST"
fi

run_as() {  # run_as <command-string>, as root or as runner per AS_ROOT
  if [ "$AS_ROOT" = "1" ]; then
    ( cd "$DEST" && eval "$1" )
  else
    su - runner -c "cd '$DEST' && $1"
  fi
}

if [ ! -f "$DEST/.runner" ]; then
  run_as "./config.sh \
    --url '$GH_RUNNER_URL' \
    --token '$TOKEN' \
    --name '$NAME' \
    --labels '$LABELS' \
    --work _work \
    --unattended --replace"
else
  echo "already configured -- reusing existing registration (idempotent restart)"
fi

echo "starting listener in the foreground: $NAME"
# exec, not a backgrounded call -- this process IS the container's PID 1
# equivalent from `podman run`'s perspective. `--restart=always` on the
# HOST's `podman run` invocation is what recovers this if the listener
# ever exits.
if [ "$AS_ROOT" = "1" ]; then
  cd "$DEST" && exec ./run.sh
else
  exec su - runner -c "cd '$DEST' && exec ./run.sh"
fi
