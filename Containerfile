# ═══════════════════════════════════════════════════════════════════════════
# awnix — a bootable, immutable Linux base for running containerised services
#
#   podman build -t awnix:latest -f Containerfile .
#
# This is the OS layer. It boots, it runs containers rootlessly, it can be
# managed over Cockpit, and it updates atomically via bootc. It ships no
# services of its own — what you run on top is yours.
# ═══════════════════════════════════════════════════════════════════════════
FROM quay.io/centos-bootc/centos-bootc:stream9

# ── EPEL ───────────────────────────────────────────────────────────────────
# podman-compose lives in EPEL, not in the stream9 bootc base repos. A build
# that assumes otherwise fails with "Unable to find a match: podman-compose",
# and it fails at BUILD time rather than at first use, which is the good
# direction — but only if EPEL is added up front. The fallback to the direct
# RPM matters on hosts where the metalink is unreachable.
RUN dnf install -y --setopt=install_weak_deps=False epel-release || \
    dnf install -y --setopt=install_weak_deps=False \
        https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm

# ── cloud-init ─────────────────────────────────────────────────────────────
# This is what makes a KEY-ONLY image reachable. On a cloud provider it injects
# the launch keypair into the unprivileged account below. Without it, an image
# that ships no password has no password AND no key path — unreachable rather
# than insecure, which is a worse outcome than the shared credential it
# replaced. If you fork this and drop cloud-init, give yourself another way in
# first.
RUN dnf install -y --setopt=install_weak_deps=False cloud-init \
    && systemctl enable cloud-init cloud-init-local cloud-config cloud-final \
    && dnf clean all

# ── Runtime ────────────────────────────────────────────────────────────────
RUN dnf install -y --setopt=install_weak_deps=False \
        podman podman-compose buildah skopeo crun \
        slirp4netns fuse-overlayfs \
        curl wget git jq openssl \
        firewalld \
        cockpit cockpit-podman cockpit-storaged \
        python3 python3-pip \
        systemd-container \
        policycoreutils-python-utils \
        bash-completion man-pages \
    && dnf clean all

# ── Rootless podman ────────────────────────────────────────────────────────
COPY storage.conf /etc/containers/storage.conf
RUN echo "awnix:100000:65536" >> /etc/subuid && \
    echo "awnix:100000:65536" >> /etc/subgid

# ── The service account ────────────────────────────────────────────────────
# Created LOCKED: no password, ever. Access is by SSH key, injected by
# cloud-init on a cloud image or placed by you on bare metal.
#
# NOPASSWD sudo is the necessary companion, not a convenience: an account with
# a locked password cannot sudo at all if sudo demands one, so key-only login
# without this gives you a machine you can reach and cannot administer. This is
# the same convention ec2-user and ubuntu use, and for the same reason.
RUN useradd -m -G wheel -s /bin/bash -c "awnix service account" awnix && \
    passwd -l awnix && \
    loginctl enable-linger awnix 2>/dev/null || true && \
    echo 'awnix ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/awnix && \
    chmod 0440 /etc/sudoers.d/awnix

# With no password anywhere, password authentication is dead weight and an
# attack surface. Image scanners flag it, and they are right to.
RUN sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
        /etc/ssh/sshd_config || true

# ── Filesystem layout ──────────────────────────────────────────────────────
# Deliberately generic. bootc gives you an immutable /usr, so anything with
# state belongs under /var. Put your own subdirectories here; awnix does not
# presume what you will run.
RUN mkdir -p /var/lib/awnix /var/log/awnix /opt/awnix && \
    chown -R awnix:awnix /var/lib/awnix /var/log/awnix /opt/awnix

# ── Base services ──────────────────────────────────────────────────────────
RUN systemctl enable cockpit.socket && \
    systemctl enable firewalld

# A zone, with NO ports opened. Opening ports is a decision about what you are
# running, and a base image that guesses will either block you or expose you.
# See units/service.container.j2 for the shape of a service, and open its port
# yourself:
#     firewall-cmd --zone=awnix --add-port=8080/tcp --permanent
RUN firewall-offline-cmd --new-zone=awnix 2>/dev/null || true

LABEL org.opencontainers.image.title="awnix" \
      org.opencontainers.image.description="Bootable immutable Linux base for containerised services" \
      org.opencontainers.image.licenses="Apache-2.0" \
      awnix.layer="base"
