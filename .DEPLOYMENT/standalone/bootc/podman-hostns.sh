#!/bin/sh
# /usr/local/bin/podman -- run podman in systemd's mount namespace on the WSL fleet host.
#
# ROOT CAUSE (measured on awnix 2026-09-27, podman 5.8.5 / crun 1.29.1, SELinux disabled):
# under WSL with systemd=true, a `wsl -d awnix` session does NOT share PID 1's mount
# namespace (session mnt:[4026536341] vs pid1 mnt:[4026532219]; `/` is `private` in
# both, so nothing propagates). Every quadlet container is started by systemd, so its
# overlay rootfs is mounted in PID 1's namespace only: the session sees 6 overlay
# mounts, PID 1 sees 296. `podman exec` with a named User (aither, nextjs, nobody,
# even `root`) resolves the name by reading <MergedDir>/etc/passwd -- which from the
# session is an EMPTY directory -- and fails:
#     Error: unable to find user aither: no matching entries in passwd file
# A container with no User= (redis) never does the lookup, which is why `--user 0`
# "fixed" it. Same command under `nsenter -t 1 -m` prints uid=1000(aither).
# Not the image's passwd, not idmapped mounts, not crun/conmon, not SELinux.
#
# So this wrapper enters PID 1's mount namespace when (and only when) it is not
# already there. Inside systemd units the namespaces match and it is a plain exec.
# --wd keeps relative paths (podman build .) working: /mnt/c and the fleet dirs are
# mounted in both namespaces. Non-root callers cannot setns and get the plain binary.
# AITHER_PODMAN_NO_HOSTNS=1 opts out.
REAL=/usr/bin/podman
if [ -z "${AITHER_PODMAN_NO_HOSTNS:-}" ] && [ "$(id -u)" = 0 ]; then
    self=$(readlink /proc/self/ns/mnt 2>/dev/null)
    init=$(readlink /proc/1/ns/mnt 2>/dev/null)
    if [ -n "$init" ] && [ "$self" != "$init" ]; then
        if nsenter -t 1 -m --wd="$PWD" true 2>/dev/null; then
            exec nsenter -t 1 -m --wd="$PWD" -- "$REAL" "$@"
        fi
        exec nsenter -t 1 -m --wd=/ -- "$REAL" "$@"
    fi
fi
exec "$REAL" "$@"
