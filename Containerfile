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
        python3.11 python3.11-pip \
        systemd-container \
        policycoreutils-python-utils \
        bash-completion man-pages \
    && dnf clean all && \
    ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/pip3.11 /usr/bin/pip3

# ── The aw family ──────────────────────────────────────────────────────────
# The reason this image exists. The README's table will promise eighteen tools.
# Until now the image shipped NONE of them: it installed python3-pip and stopped.
# A base image whose whole pitch is "a Linux you can hand to an agent" that
# answers `awgit` with command-not-found is advertising something it does not
# ship -- the same defect as a documented API route that was never written.
#
#   awdk       the agent runtime                -- one loop you can read, configured offline
#   awgit      no one else is editing this file  -- a lease, refused at commit time
#   awgraph    grep found everything             -- an AST + call graph to traverse
#   awrelay    a SaaS between your agents        -- findings/alerts on your transport
#   awshare    the download is intact            -- content-addressed, verified on fetch
#   awseal     the artifact is from who you think -- Ed25519; verify key != sign key
#   awm        memory stayed in its lane         -- tenant:user:project scopes
#   awrecover  the restore worked                -- lands fully or not at all
#   awbrowse   a page said what you were told   -- the render, DOM and requests
#   awfind     one vendor's idea of the web     -- providers you configured
#   awnest     there is a person on the other end -- a verdict, with evidence
#   awnboard   a share link anyone can use      -- an addressed, revocable gate
#   awmail     an agent that cannot write back -- send and receive, no domain
#   awsh       a terminal for your agents       -- interactive REPL, runs offline
#   awreason   explanations for decisions       -- trace through the reasoning
#   awrecurse  recursion without blowup         -- step through recursive calls
#   awprism    see through the abstraction      -- map logical to physical
#   awrepl     a loop that speaks your language -- Python/Go/Rust support
#   awresearch navigate the knowledge base      -- papers, repos, documentation
#   awkno      man pages for the aw family      -- offline, self-describing
#
# --no-cache-dir because this is an immutable image: a pip cache baked into /usr
# is dead weight in every layer and every rollback. Most tools install from git
# because of PyPI throttling; awdk was the first to publish but also installs
# from git for consistency with the others during the PyPI 429 window.
RUN pip3 install --no-cache-dir --disable-pip-version-check setuptools wheel

# Fifteen aw* tools install FROM GIT, not PyPI. This is not a limitation —
# it is a consequence of PyPI throttling on NEW project names, which hit 2026-08-19
# as an account-level ratelimit ("429 Too many new projects created") that blocks
# every publish attempt for new projects. The build environment may also have
# network restrictions that prevent PyPI access in the bootc layer.
#
# The repos are public and mirrored; the install is real. The image ships what
# the README promises today. MOVE A TOOL UP TO A SEPARATE PyPI LINE once PyPI
# throttling clears: a git install is less reproducible than a version-pinned
# wheel, which is the only reason each lives here and not there. Publish
# awgit awgraph awrelay awshare first (they are the foundation), then the rest.
# ONE ENTRY BELOW IS A BARE NAME, NOT A GIT URL, and it must stay that way.
# `git+https://github.com/Aitherium/awm` installs the distribution
# `aither-world-model`, whose module is `world_model` — a DIFFERENT product
# living under the awm name. The awm BRICK is a portable scoped agent memory
# (what the registry describes, and what the import proof below asserts), and it
# is on PyPI. Measured 2026-08-20: installing from that repo produced a green
# pip step, 18 successful wheels, and then `ModuleNotFoundError: No module
# named 'awm'` — the pip log names `aither-world-model`, never awm, so nothing
# in the output points at the cause.
RUN pip3 install --no-cache-dir \
        git+https://github.com/Aitherium/awdk \
        git+https://github.com/Aitherium/awgit \
        git+https://github.com/Aitherium/awgraph \
        git+https://github.com/Aitherium/awrelay \
        git+https://github.com/Aitherium/awshare \
        git+https://github.com/Aitherium/awseal \
        awm \
        git+https://github.com/Aitherium/awrecover \
        git+https://github.com/Aitherium/awbrowse \
        git+https://github.com/Aitherium/awfind \
        git+https://github.com/Aitherium/awnest \
        git+https://github.com/Aitherium/awnboard \
        git+https://github.com/Aitherium/awmail \
        git+https://github.com/Aitherium/awreason \
        git+https://github.com/Aitherium/awrecurse \
        git+https://github.com/Aitherium/awprism \
        git+https://github.com/Aitherium/awrepl \
        git+https://github.com/Aitherium/awresearch \
        git+https://github.com/Aitherium/awkno

# ── Node 20, NOT the default ───────────────────────────────────────────────
# Stream 9's default `nodejs` package is v16.20.2. `@aitherium/shell-cli`
# declares `node >=18.0.0`, and one of its dependencies wants `>=20.17.0`.
#
# npm only WARNS on an engine mismatch -- it installs anyway, exits 0, and puts
# the binary on PATH. So every cheap check passes (the package is present, the
# shim resolves, `command -v awsh` succeeds) and the command fails the first
# time a human runs it. Measured 2026-08-20: EBADENGINE on shell-cli,
# @inquirer/prompts and marked, against node v16.20.2.
#
# The module stream has to be enabled BEFORE nodejs is installed, or dnf
# resolves the default stream and enabling afterwards is a no-op.
RUN dnf module reset -y nodejs && \
    dnf module enable -y nodejs:20 && \
    dnf install -y --setopt=install_weak_deps=False nodejs npm && \
    dnf clean all && \
    node --version

# ── awsh (from npm, not pip) ────────────────────────────────────────────────
# awsh is @aitherium/shell-cli on npm. PyPI has an unrelated awsh by a third
# party (Jean-Martin Archer SSH tool) -- if we pip install awsh we get the
# wrong thing. The user who types awsh must get the Aither World shell. Install
# from npm and provide a shim on PATH with the expected name.
RUN npm install -g @aitherium/shell-cli && \
    echo '#!/bin/bash' > /usr/local/bin/awsh && \
    echo 'aither-shell "$@"' >> /usr/local/bin/awsh && \
    chmod +x /usr/local/bin/awsh

# ── awdk daemon systemd unit ────────────────────────────────────────────────
# awdk is installed but UNCONFIGURED. It is present and its daemon systemd unit
# is installed and enabled, but it will not connect to a backend or run any code
# until you point it at one. This keeps the base image free from secrets and
# credentials while shipping the capability pre-built.
#
# The architectural choice: tooling goes in the base (that is what these eighteen
# do, and awdk/awsh are tools by the same test), but no agent identity, no
# backend URL, and no account are baked. The daemon is enabled and will start
# on boot, and it is inert until configured -- an enabled unit that silently
# does nothing is transparent only if this is stated plainly in the README.
#
# Operators who layer on top can configure it by writing /etc/awdk/config.yaml
# before starting the container or host, or by running awdk configure from
# the command line.
RUN mkdir -p /etc/awdk && \
    printf '[Unit]\nDescription=Aither World Development Kit daemon\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart=/usr/local/bin/awdk-daemon\nRestart=on-failure\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/awdk-daemon.service && \
    # NO `systemctl daemon-reload` here. There is no running systemd during a
    # container build -- no PID 1, no D-Bus -- so it exits 1 and takes the whole
    # RUN with it. Measured 2026-08-20: the image could not build at all.
    # `systemctl enable` is fine offline (it just writes the wants/ symlink),
    # which is why the cockpit and firewalld lines below have always worked
    # without a reload. The reload happens on first boot, for free.
    systemctl enable awdk-daemon

# ── Proof step ─────────────────────────────────────────────────────────────
# Prove the tools are actually THERE. A pip step that resolved is not the same
# claim as a binary on PATH -- and this image is immutable, so a missing tool is
# found by a user, not by a rebuild. Import checks catch missing modules; command
# checks catch missing or misnamed console scripts; unit checks catch broken
# systemd integration.
# NOTE THE LAST ENTRY: `adk`, not `awdk`. A distribution name is not a module
# name, and this list is of MODULES. The awdk distribution imports as `adk`
# (measured: `pip install awdk` then `import awdk` -> ModuleNotFoundError,
# `import adk` -> ok). The same trap cost a build one layer up, where the awm
# git URL installed `aither-world-model` and provided no `awm` module at all.
# Both failures look identical from the pip log, which reports success.
RUN for m in awgit awgraph awrelay awshare awseal awm awrecover awbrowse awfind awnest awnboard awmail awreason awrecurse awprism awrepl awresearch awkno adk; do python3 -c "import $m" || { echo "FATAL: $m did not install"; exit 1; }; done && echo "aw family (Python): all 19 import"

# Verify console scripts exist on PATH for the tools that provide them.
# EXECUTE it, do not just resolve it. `command -v` proves a file exists on
# PATH and nothing else -- and the way this breaks is an engine mismatch that
# npm reports as a WARNING, so the shim is present and correct and the program
# dies on first run. A resolve-only check passes on exactly that failure.
RUN command -v awsh >/dev/null 2>&1 || { echo "FATAL: awsh not on PATH"; exit 1; } && \
    awsh --version >/dev/null 2>&1 || awsh --help >/dev/null 2>&1 || \
    { echo "FATAL: awsh is on PATH but cannot execute (check the node engine)"; exit 1; } && \
    echo "awsh: on PATH and executes"

# Verify awdk daemon unit file exists and is enabled.
RUN systemctl is-enabled awdk-daemon >/dev/null 2>&1 || { echo "FATAL: awdk-daemon unit not enabled"; exit 1; } && echo "awdk-daemon: enabled"

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
