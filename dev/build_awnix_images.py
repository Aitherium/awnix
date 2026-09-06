#!/usr/bin/env python3
"""Build the awnix image family (base -> runner -> runner-ai) and verify
each layer actually works, not just that `podman build` exited 0.

This replaces a hand-typed sequence run BY HAND in two distinct sessions
while building the awrun/awnix plan (`.DEPLOYMENT/standalone/bootc/`):

    cd .DEPLOYMENT/standalone/bootc
    wsl -d Debian -u root podman build --no-cache -t awnix-base:latest \\
        -f Containerfile.awnix .
    wsl -d Debian -u root podman build --no-cache -t awnix-runner:latest \\
        -f Containerfile.awnix-runner .
    wsl -d Debian -u root podman build --no-cache -t awnix-runner-ai:latest \\
        -f Containerfile.awnix-runner-ai .

Recorded in AitherOS/config/automation_backlog.yaml as `status: automated`,
target: this file (.claude/skills/automate-the-manual/SKILL.md, AT003).

    python AitherOS/dev/tools/build_awnix_images.py                 # all 3, idempotent
    python AitherOS/dev/tools/build_awnix_images.py --layer runner-ai
    python AitherOS/dev/tools/build_awnix_images.py --force         # rebuild even if the tag exists
    python AitherOS/dev/tools/build_awnix_images.py --self-test

Idempotent: a layer whose target tag already exists is SKIPPED (not
rebuilt) unless --force, but verification always re-runs regardless -- an
image that exists and was built wrong must not read as a pass just because
the build step itself was skipped this time. `--no-cache` is used on every
real build (not a re-run of layers already correct) because this family has
hit stale-podman-storage corruption ("layer not known") more than once this
session; `--no-cache` costs a slower rebuild, never a wrong one.
"""

from __future__ import annotations

import argparse
import inspect
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# stdout defaults to the LOCALE codec (cp1252 here). This tool prints build
# output containing non-ASCII, and a UnicodeEncodeError on a status glyph
# killed the run WHILE IT WAS REPORTING A FAILURE -- so the real error was
# replaced by a traceback about an encoding, which names neither the cause
# nor the step. errors="replace" so an unrenderable glyph degrades to a
# placeholder instead of taking the build down.
for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if _rc is not None:
        try:
            _rc(encoding="utf-8", errors="replace")
        except (ValueError, OSError) as _e:
            _enc_note = f"could not set UTF-8 output: {_e}"

DISTRO = "Debian"
BOOTC_DIR = "AitherOS-Fresh/.DEPLOYMENT/standalone/bootc"  # WSL-side, relative to the C: mount

#: GB of free space the preflight must see before ANY layer build starts
#: (the ENOSPC guard, see main()). The chain's biggest single layer
#: (awnix-full: node + 28 packages) lands ~5-6GB of layers; 15G is that plus
#: real headroom for the store's own churn.
PREFLIGHT_NEED_GB = 15.0

try:
    from check_disk_preflight import preflight as _disk_preflight
except ImportError:
    _disk_preflight = None


def _shell_bootc_dir() -> str:
    """The bootc directory AS THE SHELL WILL SEE IT.

    Named apart from `_bootc_dir()` below, which answers a DIFFERENT question (where
    the Containerfiles are on the local filesystem, as a Path). They collided once:
    Python keeps the last definition, so this one was shadowed and silently never ran.

    The second half of the same host assumption `_host_prefix` fixes. On the Windows
    workstation podman lives in the distro and the repo is reached through the C: mount,
    so the path is `/mnt/c/AitherOS-Fresh/...`. On a Linux host -- the AWS awnix runner,
    where these builds belong -- the checkout is wherever Actions put it
    (`/opt/actions-runner/_work/AitherOS/AitherOS`), which is nowhere near `/mnt/c`.

    Derived from THIS FILE's own location rather than an env var: the tool already knows
    where it is, and a variable someone must remember to set is wrong by default on the
    machine that needs it most.
    """
    if shutil.which("wsl"):
        return f"/mnt/c/{BOOTC_DIR}"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".DEPLOYMENT/standalone/bootc"
        if candidate.is_dir():
            return candidate.as_posix()
    # Never guess a path that does not exist -- a `cd` to nowhere fails with a message
    # about the directory, which reads as a missing checkout rather than a wrong root.
    raise DeadError(
        "cannot locate .DEPLOYMENT/standalone/bootc from "
        f"{here} -- this tool must run inside the repo")


class DeadError(Exception):
    """Could not build or judge. Exits 2 — never 0."""


@dataclass(frozen=True)
class Layer:
    name: str
    tag: str
    containerfile: str
    #: A shell one-liner run INSIDE a fresh container of this image, whose
    #: exit code is the verification. Kept as one line so it composes with
    #: the same `_wsl()` bytes-not-text plumbing as the build step.
    verify_cmd: str
    verify_label: str
    #: Named build contexts, `(name, path-relative-to-the-bootc-dir)`. Podman
    #: resolves `COPY --from=<name>` against these. Empty for every awnix layer;
    #: only the appliance reaches outside its own directory.
    contexts: tuple[tuple[str, str], ...] = ()
    #: Context NAMES that may be absent without refusing the build — the
    #: appliance's `svcimg` (the fleet oci-archives staged by
    #: stage-fleet-bake.sh) is one: a build with no staged archives is a VALID
    #: unbaked image whose loader unit fails LOUDLY at first boot (the
    #: Containerfile's own design), so the builder must not refuse it.
    optional_contexts: tuple[str, ...] = ()


LAYERS: tuple[Layer, ...] = (
    Layer(
        name="base",
        tag="localhost/awnix-base:latest",
        containerfile="Containerfile.awnix",
        # python3.11, NEVER the system python3 (3.9) -- Containerfile.awnix
        # installs the aw* tools under python3.11 specifically because their
        # `requires-python >= 3.10` silently filters every candidate wheel
        # under 3.9 (pip then reports "no version satisfies" naming no real
        # cause). Caught live on this tool's own first real run: a fresh,
        # correctly-built image FAILED this check under plain `python3`
        # with ModuleNotFoundError -- the image was right, the check was
        # calling the wrong interpreter for exactly the reason the
        # Containerfile's own comment warns about.
        # All SEVEN, not the five on PyPI. awrecover and awseal install from source
        # (git+https), which is the fragile half -- a moved branch, a repo turned
        # private or no network at build time all fail there and nowhere else. A
        # verification that checked only the five would report OK on exactly that.
        # The setup must also be ENABLED, not merely present: an installed unit that
        # systemd never runs boots straight to a login prompt having asked nothing,
        # which looks identical to a first boot that went fine.
        verify_cmd=("python3.11 -c \"import awgit, awgraph, awrelay, awm, awshare, "
                     "awrecover, awseal\" "
                     # The image must SAY it is awnix. It shipped an ISO announcing
                     # itself as CentOS Stream 9 -- honest about lineage, useless as
                     # identity, and invisible to every other check here.
                     # NAME, not ID: ID must stay `centos` or bootc-image-builder
                     # cannot resolve a distro def ("awnix-9") and the ISO build
                     # fails at manifest generation. NAME/PRETTY_NAME are what a
                     # person actually sees.
                     "&& grep -q '^NAME=.awnix.$' /usr/lib/os-release "
                     "&& test -x /usr/bin/awnix-setup "
                     "&& test -L /etc/systemd/system/multi-user.target.wants/awnix-setup.service "
                     "&& echo AWNIX_BASE_IMPORTS_OK"),
        verify_label="all seven aw* tools import and the first-boot setup is enabled",
    ),
    Layer(
        name="runner",
        tag="localhost/awnix-runner:latest",
        containerfile="Containerfile.awnix-runner",
        verify_cmd=("test -x /opt/actions-runner/config.sh "
                     "&& id runner >/dev/null 2>&1 "
                     "&& grep -q '^runner:' /etc/subuid "
                     "&& echo AWNIX_RUNNER_STAGED_OK"),
        verify_label="the runner binary is staged and the unprivileged rootless user exists",
    ),
    Layer(
        name="runner-ai",
        tag="localhost/awnix-runner-ai:latest",
        containerfile="Containerfile.awnix-runner-ai",
        verify_cmd=(
            "BIN=$(find /opt/bonsai/bin -name llama-server -type f | head -1) "
            "&& /opt/bonsai/lib/ld-linux-x86-64.so.2 --library-path /opt/bonsai/lib "
            "\"$BIN\" --version && echo AWNIX_RUNNER_AI_LLAMA_SERVER_OK"
        ),
        verify_label="the bundled GLIBCXX runtime actually resolves and llama-server runs "
                      "(the fix for the GLIBCXX_3.4.30 gap -- see Containerfile.awnix-runner-ai)",
    ),
    # ── awnix-full ─ the batteries-included variant ──────────────────────
    # This layer was declared in awnix-variants.yaml with `iso: true` and existed in
    # NEITHER this tool nor the ISO workflow, so `awnix-full` could be pushed as an
    # image (publish-awnix-images.sh reads the variants file) and could never be built
    # as MEDIA. That is why it is the one public variant with no ISO release: not a
    # failed build, an absent lane. Same class as the appliance comment below.
    Layer(
        name="full",
        tag="localhost/awnix-full:latest",
        containerfile="Containerfile.awnix-full",
        # The MODULE list, mirroring Containerfile.awnix-full's own build-time check.
        # Repeating it here is deliberate: an image PULLED from a registry never ran
        # that RUN step, and this tool verifies the image rather than trusting how it
        # was produced. `awdk` imports as `adk` -- asserting the distribution name
        # would fail on a perfectly good image. No count is hardcoded (the
        # Containerfile LABEL says 26 and the list holds 27); the check prints what
        # it actually found so the two cannot drift into a lie.
        verify_cmd=(
            "python3.11 -c \"import awgit, awgraph, awrelay, awm, awshare, awrecover, "
            "awseal, adk, awprism, awreason, awrecurse, awrepl, awsync, awbac, "
            "awbrowse, awdit, awfind, awiam, awkno, awmail, awnboard, awnest, awnet, "
            "awpredict, awresearch, awrun, awtunnel; "
            "print('modules ok')\" "
            "&& echo AWNIX_FULL_IMPORTS_OK"
        ),
        verify_label="every aw* package in the batteries-included variant imports",
    ),
    Layer(
        name="dev",
        tag="localhost/awnix-dev:latest",
        containerfile="Containerfile.awnix-dev",
        # The workspace sandbox lane spawns this image (AitherTunnel
        # DEV_WORKSPACE_IMAGE); a session inside it dies on `command not
        # found` if any of these is missing, while the container itself looks
        # fine. claude must EXECUTE, not merely exist — npm creates the shim
        # regardless (the ONB002 class).
        verify_cmd=(
            "command -v gh && command -v tmux && command -v ssh "
            "&& command -v claude && claude --version >/dev/null 2>&1 "
            "&& command -v code-server "
            "&& python3.11 -c 'import adk; print(\"adk ok\")' "
            "&& echo AWNIX_DEV_STAGED_OK"
        ),
        verify_label="dev toolchain + Claude Code + code-server staged on the awnix chain",
    ),
    Layer(
        name="gobbonet",
        tag="localhost/gobbonet-appliance:latest",
        containerfile="Containerfile.gobbonet-appliance",
        # Verifies the image, never the build. A pulled image never ran the
        # Containerfile's own RUN checks, and this tool exists to judge the
        # artifact -- so the two claims that make this variant what it says it is
        # are re-asserted here: the weights are really present (a first boot that
        # answers with no network is the whole promise), and the gobbonet pack
        # imports (the launcher and the scoped-memory tools are the same package,
        # so a broken awdk ships a chat window calling itself an agent platform).
        # Sizes are checked, not just existence: a truncated GGUF is an image that
        # ships and then fails to load, which reads as a bad model.
        verify_cmd=(
            "test -s /opt/bonsai/models/Ternary-Bonsai-1.7B-Q2_0.gguf "
            "&& test -s /opt/bonsai/models/Ternary-Bonsai-4B-Q2_0.gguf "
            "&& [ \"$(stat -c%s /opt/bonsai/models/Ternary-Bonsai-4B-Q2_0.gguf)\" -gt 900000000 ] "
            "&& test -f /opt/gobbonet/chat.html "
            "&& python3.11 -c \"from adk.packs.gobbonet import campaign_memory, cards, "
            "retrieval; print('pack ok')\" "
            "&& echo GOBBONET_APPLIANCE_OK"
        ),
        verify_label="Bonsai baked in, GobboNet present, and the agent pack imports",
    ),
    # ── the AitherOS appliance, which since 2026-08-21 STACKS on awnix ────────────
    # These four Containerfiles sat in this directory and this tool did not know them,
    # so they were built by hand or not at all -- and "not at all" is what happened:
    # Containerfile.base's own comment records that no bootc base image had ever been
    # produced here, "and so why no ISO or AMI ever had either". One chain now, one tool.
    Layer(
        name="appliance-base",
        tag="localhost/aitheros-bootc-base:latest",
        containerfile="Containerfile.base",
        # Asserts this layer's OWN additions AND that the awnix inheritance survived.
        # The second half is the whole point of the rebase: if this image stops carrying
        # the aw* tools, the chain has quietly gone back to being parallel rather than
        # stacked, and nothing else in the tree would notice.
        verify_cmd=(
            "command -v podman-compose >/dev/null "
            "&& command -v cockpit-bridge >/dev/null "
            "&& id aither >/dev/null 2>&1 "
            "&& test -d /var/lib/aitheros/library "
            "&& python3.11 -c 'import awgit, awgraph, awrelay, awm, awshare' "
            "&& echo AITHEROS_BASE_ON_AWNIX_OK"),
        verify_label="podman-compose, cockpit and the aither account are present AND the "
                     "awnix aw* tools survived the rebase",
    ),
    Layer(
        name="appliance-desktop",
        tag="localhost/aitheros-bootc-desktop:latest",
        containerfile="Containerfile.desktop",
        # awnix-setup MUST be masked here: it claims tty1 (TTYPath=/dev/tty1,
        # Before=getty@tty1) and so does GDM once the default target is graphical with
        # auto-login. Two units claiming one console is not a race worth having.
        verify_cmd=(
            "test $(systemctl is-enabled awnix-setup.service 2>&1) = masked "
            "&& test $(systemctl get-default) = graphical.target "
            "&& echo AITHEROS_DESKTOP_OK"),
        verify_label="GNOME is the default target and awnix-setup is masked, so nothing "
                     "fights GDM for tty1",
    ),
    Layer(
        name="appliance-gpu",
        tag="localhost/aitheros-bootc-gpu:latest",
        containerfile="Containerfile.gpu-nvidia",
        verify_cmd=("test -f /etc/yum.repos.d/nvidia-container-toolkit.repo "
                    "-o -d /usr/share/containers/oci/hooks.d "
                    "&& echo AITHEROS_GPU_OK"),
        verify_label="the NVIDIA container toolkit repo or its OCI hooks landed",
    ),
    Layer(
        name="appliance",
        tag="localhost/aitheros-bootc:latest",
        containerfile="Containerfile.aitheros",
        # `COPY --from=context ../rocky-linux/...` -- the `..` is relative to the
        # CONTEXT, not the Containerfile, so the context is the bootc dir's parent.
        # Without this the build dies at the first COPY with "no such build
        # context", ~20 minutes in, reading as a broken Containerfile.
        # TWO contexts, because one cannot reach both trees.
        #
        # `context` is the REPO ROOT, for AitherOS/config/services.yaml and
        # AitherOS/apps/awnode/... -- unreachable from .DEPLOYMENT/standalone,
        # and buildah REFUSES a `..` that escapes a context rather than
        # resolving it ("possible escaping context directory error").
        #
        # `deploy` is .DEPLOYMENT/standalone, and must stay SEPARATE because the
        # repo-root .dockerignore excludes `.DEPLOYMENT/` wholesale. Under a
        # single repo-root context every deploy file is silently FILTERED OUT --
        # buildah says "no items matching glob ... (1 filtered out)", which
        # reads as a missing file while the file sits there at 29 KB. Each
        # context resolves its own ignores, so this one escapes that line.
        contexts=(("context", "../../.."), ("deploy", ".."), ("svcimg", "/var/tmp/aws-svcimg")),
        # The fleet-bake archives may not be staged (valid unbaked build —
        # the loader fails loudly at first boot, never the build).
        optional_contexts=("svcimg",),
        verify_cmd=("command -v aitheros-ctl >/dev/null "
                    "&& systemctl is-enabled aitheros-autostart.service >/dev/null "
                    "&& echo AITHEROS_APPLIANCE_OK"),
        verify_label="the control plane and its autostart unit are installed",
    ),
    # ── the garg single-tenant appliance ─────────────────────────────────────────
    # The self-service fallback for garg.aitherium.com. NOT built by the generic
    # podman build alone: the Containerfile COPYs .gargbot-backend (the tenant's
    # backend, staged by build-garg-appliance.sh locally or by the workflow's
    # "Stage the tenant backend" step — the tenant repo is PRIVATE, an in-image
    # clone has no credentials). The generic build path therefore requires the
    # stage to have happened; the builder's optional-context rule does not apply
    # because the COPY source is context-relative, not a named context.
    Layer(
        name="garg",
        tag="localhost/garg-appliance:latest",
        containerfile="Containerfile.garg-appliance",
        verify_cmd=(
            "test -s /opt/bonsai/models/Ternary-Bonsai-1.7B-Q2_0.gguf "
            "&& test -s /opt/bonsai/models/Ternary-Bonsai-4B-Q2_0.gguf "
            "&& [ \"$(stat -c%s /opt/bonsai/models/Ternary-Bonsai-4B-Q2_0.gguf)\" -gt 900000000 ] "
            "&& test -x /opt/qdrant/qdrant "
            "&& test -d /opt/gargbot/backend/portal_kit_backend "
            "&& python3.11 -c \"import sys; sys.path.insert(0, '/opt/gargbot/backend'); "
            "from app.config import Settings; print('backend config ok')\" "
            "&& test -L /etc/systemd/system/multi-user.target.wants/garg-firstboot.service "
            "&& echo GARG_APPLIANCE_OK"),
        verify_label="Bonsai baked in, qdrant + the tenant backend staged, and "
                     "garg-firstboot enabled (the image a first boot exercises)",
    ),
)


def _host_prefix() -> list[str]:
    """How to reach a shell that can see podman, on THIS host.

    🚨 THIS TOOL ASSUMED WINDOWS+WSL AND SO COULD RUN NOWHERE ELSE. Every build went
    through `wsl -d Debian -u root`, which on any Linux box is
    `FileNotFoundError: [Errno 2] No such file or directory: 'wsl'` -- measured
    2026-08-21 on the AWS awnix runner, which is the box actually provisioned to do
    these builds (podman + buildah, nothing else contending for it) while the
    workstation sat at load average 299 and could not answer `podman images` in 600s.

    Same class as SHW003 (a bare `docker` verb on a podman fleet): a HOST assumption
    baked into a tool, invisible until the tool runs somewhere else. The preflight in
    build-awnix-iso.yml correctly reported podman and python3 present -- the box was
    fine; the tool could not use it.

    The ladder is: use `wsl` when it exists (the Windows workstation, where podman
    lives inside the distro), otherwise run bash DIRECTLY (any Linux host, where
    podman is right there). Deliberately not an env var -- a flag someone must know to
    set is a flag that is wrong by default on the machine that needs it most.
    """
    if shutil.which("wsl"):
        return ["wsl", "-d", DISTRO, "-u", "root", "bash", "-s"]
    return ["bash", "-s"]


def _wsl(script: str, timeout: int = 900) -> tuple[int, str]:
    """Run bash where podman is reachable -- inside the fleet distro on Windows, or
    directly on a Linux host (see `_host_prefix`).

    BYTES not text=True (Windows' text pipe turns \\n into \\r\\n, which lands inside
    the command and breaks bash), stdin script rather than nested `-c "..."` quoting
    (which has silently mangled `$var` expansion in this codebase before). Returns
    (returncode, combined stdout+stderr) rather than raising on a nonzero exit -- a
    failed BUILD is a real, reportable outcome here, not a tool malfunction."""
    r = subprocess.run(
        _host_prefix(),
        input=script.replace("\r\n", "\n").encode("utf-8"),
        capture_output=True, timeout=timeout,
    )
    out = (r.stdout.decode("utf-8", errors="replace")
           + r.stderr.decode("utf-8", errors="replace"))
    return r.returncode, out


def _image_exists(tag: str) -> bool:
    """Is the tag in local storage? Retries once on a probe timeout.

    Measured 2026-08-27: on the loaded workstation (load 23-30), a 30s
    `podman image exists` probe timed out and TimeoutExpired propagated as a
    raw traceback mid-run — the tool CRASHED instead of judging. A build tool
    that cannot ask the store one question must say DEAD, not traceback.
    """
    for attempt, timeout in ((1, 30), (2, 180)):
        try:
            code, _out = _wsl(f"podman image exists {tag}", timeout=timeout)
            return code == 0
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise DeadError(
                    f"podman store did not answer 'image exists {tag}' "
                    f"in 30s+180s — cannot judge, refusing to guess"
                ) from None
    return False  # unreachable; keeps the type checker honest


_FROM_LOCAL = re.compile(r"^\s*FROM\s+(localhost/[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?)",
                         re.I | re.M)


def parent_of(layer: Layer) -> "Layer | None":
    """The layer this one builds FROM, derived from its Containerfile.

    Read rather than declared: a `parent=` field would be a second copy of what the
    Containerfile already states, and the two drift. Only `FROM localhost/...` counts --
    an upstream `FROM quay.io/...` stage is pulled normally and is not ours to build.
    """
    path = _bootc_dir() / layer.containerfile
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # LOUD. Returning None here would silently mean "no ancestors", so a missing
        # Containerfile would build one layer, fail on a FROM it never satisfied, and
        # blame the registry -- the exact misdirection this resolver exists to remove.
        print(f"[chain] cannot read {path}: {exc} -- ancestors UNKNOWN for "
              f"{layer.name}", file=sys.stderr)
        return None
    by_tag = {other.tag: other for other in LAYERS}
    for ref in _FROM_LOCAL.findall(text):
        # Tags in LAYERS carry an explicit :latest; a bare FROM may not.
        for cand in (ref, f"{ref}:latest"):
            if cand in by_tag and by_tag[cand].name != layer.name:
                return by_tag[cand]
    return None


def chain_for(layer: Layer) -> list[Layer]:
    """`layer` preceded by every ancestor, outermost-last. Cycle-safe."""
    out: list[Layer] = []
    seen: set[str] = set()
    cur: "Layer | None" = layer
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        out.append(cur)
        cur = parent_of(cur)
    out.reverse()
    return out


def build_layer(layer: Layer, *, force: bool, verbose: bool = True) -> bool:
    """Build (if needed) and verify one layer. Returns True on a verified
    pass. Never raises DeadError itself -- callers decide how to aggregate
    failures across layers, since one broken layer should not stop the
    caller from at least reporting the others."""
    if verbose:
        print(f"[{layer.name}] tag={layer.tag}")

    if force or not _image_exists(layer.tag):
        if verbose:
            print(f"[{layer.name}] building (--no-cache) ...")
        # A declared build context whose directory is absent fails at COPY time,
        # deep into the build, with an error naming the CONTEXT rather than the
        # missing path. Checked up front instead: it costs nothing and can say why.
        # An OPTIONAL context (the appliance's svcimg) may be absent — that is a
        # valid unbaked build — and is simply not emitted.
        missing = [f'{name}={rel}' for name, rel in layer.contexts
                   if name not in layer.optional_contexts
                   and not (_bootc_dir() / rel).is_dir()]
        if missing:
            print(f"[{layer.name}] BUILD REFUSED - declared build context(s) do "
                  f"not exist: {', '.join(missing)}")
            return False
        ctx_parts = []
        for name, rel in layer.contexts:
            if name in layer.optional_contexts and not (_bootc_dir() / rel).is_dir():
                print(f"[{layer.name}] {name} context absent - building WITHOUT "
                      f"the fleet bake (the loader unit will fail loudly at "
                      f"first boot)")
                continue
            ctx_parts.append(f' --build-context {name}={rel}')
        ctx_args = ''.join(ctx_parts)
        # --network=host so the BUILD can resolve DNS.
        #
        # Measured 2026-08-21 on the AWS awnix runner: STEP 2/11's `dnf install` died
        # with "Could not resolve host: mirrors.centos.org". The same step had
        # succeeded on that box days earlier -- what changed is WHO runs it. The runner
        # was switched to root (bootc-image-builder cannot run unprivileged), which
        # moves podman from ROOTLESS to ROOTFUL: rootless uses slirp4netns/pasta and
        # inherits the surrounding container's networking, while rootful builds a
        # bridge whose resolver does not work from inside another container. So making
        # the media step possible silently broke the layer step's network.
        #
        # Host networking is the ordinary answer for building inside a container, and
        # it is safe here in the way that matters: this is a single-purpose,
        # provision-and-terminate box already running --privileged, so the build shares
        # a namespace it could reach anyway.
        # CGROUP MANAGER, decided at run time. This is what has been failing every
        # ISO build on the AWS box, at STEP 2 of the base layer:
        #
        #   warning: cgroupv2 manager is set to systemd but there is no systemd
        #            user session available
        #   warning: Falling back to --cgroup-manager=cgroupfs
        #   error:   unable to start unit "runc-buildah-....scope" ...
        #            Interactive authentication required
        #
        # Read those together: podman announces the fallback and RUNC STILL ASKS
        # SYSTEMD (note `Slice: system.slice` in the failure). The fallback applies
        # to podman's own bookkeeping and does not reach the runtime invocation, so
        # the build dies on a box where podman had already decided systemd was
        # unusable. Passing it explicitly is what actually propagates.
        #
        # The usual advice -- `loginctl enable-linger`, mkdir /run/user/<uid> -- is
        # written for a machine with logind. This runner has no logind session and
        # no passwordless sudo (this workflow's own preflight measures and prints
        # both), so that advice cannot be followed here; RPS001 documents the same
        # class for the remote runner.
        #
        # DETECTED, not hardcoded: forcing cgroupfs unconditionally would also
        # change the workstation path, where systemd IS available and is the
        # default for a reason. A build needs no systemd cgroup delegation, so
        # falling back is safe -- but only where it is actually needed.
        # The probe runs BEFORE the cd, so the original `cd ... && podman build`
        # chain stays intact. Putting it after (`cd X && CGM=''; ... ; podman
        # build`) reads fine and is wrong: `&&` would bind only to the
        # assignment, and a FAILED cd would still run the build -- in whatever
        # directory the shell happened to be in.
        cgroup_probe = (
            "CGM=''; "
            "systemctl --user is-system-running >/dev/null 2>&1 "
            "|| CGM='--cgroup-manager=cgroupfs'; "
        )
        build_script = (
            f"{cgroup_probe}"
            f"cd {_shell_bootc_dir()} && "
            f"podman build $CGM --no-cache --network=host{ctx_args} -t {layer.tag} "
            f"-f {layer.containerfile} ."
        )
        code, out = _wsl(build_script, timeout=1800)
        if code != 0:
            print(f"[{layer.name}] BUILD FAILED (exit {code}):")
            print(out[-4000:])
            return False
        if verbose:
            print(f"[{layer.name}] build OK")
    elif verbose:
        print(f"[{layer.name}] tag already exists, skipping build (--force to rebuild)")

    # Re-assert regardless of whether a build just ran -- an existing image
    # that was skipped this round must not be assumed correct.
    #
    # A heredoc, not `bash -c "{verify_cmd}"`: several verify_cmds contain
    # their OWN double quotes (e.g. base's `python3 -c "import ..."`), and
    # nesting those inside an outer `-c "..."` collides -- the outer quote
    # closes early, bash hands python a bare `-c import` with the rest as
    # stray tokens, and `python3 -c import` is a SyntaxError that reads as
    # "the package doesn't import" when the real defect is quoting. Caught
    # live on the base layer's first real run of this tool: runner and
    # runner-ai happened to have no embedded double quotes and passed by
    # accident, which would have hidden this for every layer that DID.
    verify_script = (f"podman run --rm -i {layer.tag} bash -s <<'AWNIX_VERIFY_EOF'\n"
                      f"{layer.verify_cmd}\nAWNIX_VERIFY_EOF")
    code, out = _wsl(verify_script, timeout=120)
    passed = code == 0 and "_OK" in out
    marker = "ok" if passed else "FAIL"
    print(f"[{layer.name}] verify: {marker} — {layer.verify_label}")
    if not passed:
        print(out[-2000:])
    return passed


ISO_SCRIPT = "build-awnix-iso.sh"

#: The media step's disk floor, in GB. KEPT IN STEP WITH MIN_FREE_GB in
#: build-awnix-iso.sh -- the shell script refuses below this number, so the
#: tool's own preflight must use the SAME number or a build can pass here and
#: die there. The self-test asserts the two copies agree, so a change to one
#: without the other is a loud failure instead of a silent mismatch.
ISO_FLOOR_GB = 40.0

def _iso_cmd_for_test(image: str = "IMG", iso_type: str = "iso",
                      out: str = "OUT", min_free_gb: float | None = None) -> str:
    """The exact command build_iso would run. Exposed so --self-test can assert its
    shape without a distro: a wrapper whose only failure mode is a malformed command
    line should not need a 30-minute build to catch one."""
    extra = (f" --min-free-gb {int(min_free_gb)}"
             if min_free_gb is not None else "")
    return (f"bash {_shell_bootc_dir()}/{ISO_SCRIPT} "
            f"--image {image} --type {iso_type} --out {out}{extra}")



def iso_floor_error(gb: float | None) -> str | None:
    """Why a --iso-min-free-gb value must be refused, or None if acceptable.

    The shell guard needs an INTEGER: `[ 24 -lt 40.6 ]` errors and the
    comparison evaluates FALSE, i.e. the refusal never fires. int(40.6)=40
    would silently WEAKEN the floor, which is the same class of silent
    safety change; refuse the value instead of rounding it in either
    direction. Pure so the self-test asserts the real judgment path.
    """
    if gb is not None and not float(gb).is_integer():
        return (f"--iso-min-free-gb must be an INTEGER (got {gb!r}) -- the "
                f"shell's `[ ... -lt ... ]` rejects a float operand, and a "
                f"rejected guard is a skipped guard. Pass 41, not 40.6.")
    return None


def resolve_iso_image(iso_layer: str | None, iso_image: str) -> str:
    """`--iso-layer <name>` resolves a LAYERS entry to its image tag.

    Raises ValueError when the name is unknown, or when BOTH --iso-layer and an
    explicit --iso-image were given -- two sources for one answer is exactly
    the ambiguity that shipped a base ISO for a gobbonet dispatch (measured
    2026-08-29). The default --iso-image value is not "explicit": it equals
    LAYERS[0].tag, so --iso-layer with the untouched default resolves.
    """
    if iso_layer is None:
        return iso_image
    for layer in LAYERS:
        if layer.name == iso_layer:
            if iso_image != LAYERS[0].tag:
                raise ValueError(
                    f"give ONE of --iso-layer and --iso-image, not both "
                    f"(layer {iso_layer!r} resolves to {layer.tag!r}, but "
                    f"--iso-image names {iso_image!r})")
            return layer.tag
    raise ValueError(f"no such layer: {iso_layer}")


def resolve_iso_ghcr(iso_layer: str) -> str:
    """`--iso-published` resolves a layer to its PUBLISHED ghcr ref.

    The released ISO must be built from the registry image, never the local
    one: publish-awnix-iso.sh refuses an ISO recording `localhost/` refs (the
    bootc-upgrade path breaks), measured by its own localhost scan. The ref is
    taken from the variants manifest's publish:true entry for this layer —
    registry/{repo}:latest — and never guessed. Raises ValueError when the
    variant is absent, unpublishable, or names no repo (the pre-flip state:
    this flag must fail loudly, not fall back to the local image).
    """
    try:
        from check_awnix_variants import load_variants
    except ImportError:
        raise ValueError("--iso-published needs check_awnix_variants.load_variants")
    variants = load_variants(Path(__file__).resolve().parents[3]
                             / ".DEPLOYMENT" / "standalone" / "bootc"
                             / "awnix-variants.yaml")
    for name, v in variants.items():
        if v.get("layer") == iso_layer:
            # load_variants parses the manifest with yaml.safe_load, so
            # `publish: true` arrives as the BOOL True, while the shell
            # publish-awnix-images.sh reads the raw text "true". Accept both
            # spellings; anything else (False, "private", absent) is the
            # pre-flip state and must refuse loudly.
            pub = v.get("publish")
            if pub is not True and pub != "true":
                raise ValueError(
                    f"--iso-published: variant {name!r} is publish={pub!r} "
                    f"— there is no published image to build media from (the flip "
                    f"has not happened)")
            repo = v.get("repo", "")
            if not repo:
                raise ValueError(f"--iso-published: variant {name!r} declares no repo")
            reg = v.get("registry", "ghcr.io/aitherium")
            return f"{reg}/{repo}:latest"
    raise ValueError(f"--iso-published: no variant has layer={iso_layer!r}")


def _iso_out_is_distro_path(out: str) -> bool:
    """True when --iso-out is a plausible path INSIDE the distro.

    Pure, so the self-test can exercise BOTH directions. The guard itself sits
    at the top of build_iso(), which starts a multi-minute build on anything it
    accepts -- so from there only the refusal half is reachable, and a guard
    tested in one direction can refuse everything and still pass.
    """
    return (out.startswith("/")
            and "Program Files" not in out
            and ":/" not in out[:4])


def build_iso(image: str, iso_type: str, out: str, *,
              min_free_gb: float | None = None, verbose: bool = True) -> bool:
    # REFUSE a path Git-Bash already mangled. --iso-out names a directory INSIDE
    # the distro, and MSYS rewrites a leading-slash argument before python sees
    # it: `/var/tmp/x` arrives as `C:/Program Files/Git/var/tmp/x`. The space
    # then splits the argument and the builder dies with "unknown argument:
    # Files/Git/var/tmp/x" -- a fragment of a path nobody typed, one line into a
    # job that takes tens of minutes.
    #
    # A mangled path WITHOUT a space is worse: it would not error at all. The
    # image would be written inside the Git installation and the run would
    # report success.
    #
    # The message carries the fix because the fix is not derivable from the
    # symptom.
    if not _iso_out_is_distro_path(out):
        print(f"[iso] REFUSING --iso-out {out!r}: this must be an absolute path "
              f"INSIDE the distro, and Git-Bash has rewritten it into a Windows "
              f"path. Re-run with MSYS_NO_PATHCONV=1, e.g.\n"
              f"      MSYS_NO_PATHCONV=1 python {__file__.split(chr(92))[-1]} "
              f"--iso ... --iso-out /var/tmp/...")
        return False

    """Turn a built layer into bootable media, inside the distro.

    Everything here exists because doing it by hand is a trap rather than a chore. The
    script lives on the Windows filesystem, which does not carry the execute bit, so it
    is invoked as `bash <script>` rather than executed -- a chmod would appear to work
    and then be undone by the next checkout. Paths are `/mnt/c/...` because that is what
    the distro sees, and the arguments are passed through `_wsl`'s stdin-script form,
    which is the one that does not mangle `$vars` or line endings.

    The heavy lifting, the disk guard and the "exit 0 but no artifact" check all stay in
    the shell script: it must also work when run directly inside the distro, which is how
    it will be run on a Linux host that has no Windows layer at all.
    """
    if verbose:
        floor = f" (min-free-gb={min_free_gb})" if min_free_gb else ""
        print(f"[iso] image={image} type={iso_type} out={out}{floor}")
    script = (
        f"bash {_shell_bootc_dir()}/{ISO_SCRIPT} "
        f"--image {image} --type {iso_type} --out {out}"
        + (f" --min-free-gb {min_free_gb}" if min_free_gb is not None else "")
    )
    # An ISO build downloads the Anaconda payload and runs osbuild; 30 minutes is a
    # working build, not a hang. The image builds above use 1800s for the same reason.
    code, out_text = _wsl(script, timeout=5400)
    print(out_text[-4000:])
    if code != 0:
        print(f"[iso] FAILED (exit {code}) -- nothing usable was produced")
        return False
    if verbose:
        print("[iso] OK")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layer", choices=[layer.name for layer in LAYERS],
                     help="build only this layer (and its verification) -- still requires its "
                          "parent tag to already exist")
    ap.add_argument("--force", action="store_true", help="rebuild even if the tag already exists")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--iso", action="store_true",
                    help="build bootable media from an already-built layer "
                         "(instead of building the container layers)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the disk-space preflight (check_disk_preflight) — "
                         "the ENOSPC guard; skip only with a reason")
    ap.add_argument("--preflight-need", type=float, default=None,
                    help=f"GB the preflight must see free before a build starts "
                         f"(default {PREFLIGHT_NEED_GB} for a layer build, "
                         f"{ISO_FLOOR_GB} for --iso -- the ISO floor, because the "
                         f"media step's own guard would refuse anything less)")
    # `localhost/awnix:latest` is a tag NO layer in this file builds -- the base layer
    # is `localhost/awnix-base:latest`. So the default sent every media build at an
    # image that does not exist, and the failure ("image not in local storage") reads
    # as a build that did not run rather than a default naming the wrong thing.
    # Derived from LAYERS so a retag cannot reintroduce the mismatch.
    ap.add_argument("--iso-layer", choices=[layer.name for layer in LAYERS],
                    help="turn THIS layer's image into media -- the layer that was "
                         "actually built. Without it every media build used the base "
                         "image even when the dispatch asked for gobbonet (measured "
                         "2026-08-29: the GobOS appliance ISO was built from awnix-base "
                         "and would have shipped without GobboNet). Mutually exclusive "
                         "with --iso-image.")
    ap.add_argument("--iso-image", default=LAYERS[0].tag,
                    help=f"image to turn into media (default: {LAYERS[0].tag})")
    ap.add_argument("--iso-published", action="store_true",
                    help="build media from the PUBLISHED ghcr ref of --iso-layer's "
                         "variant (registry/repo:latest from awnix-variants.yaml), "
                         "pulling it if absent — the released ISO must record the "
                         "registry ref, never localhost (publish-awnix-iso.sh refuses "
                         "a localhost ref; pre-flip this fails loudly)")
    ap.add_argument("--iso-min-free-gb", type=float, default=None,
                    help=f"disk floor passed to the ISO script as --min-free-gb; "
                         f"defaults to the script's own MIN_FREE_GB ({ISO_FLOOR_GB}G). "
                         f"Forwarded as an INTEGER: the shell's `[ ... -lt ... ]` "
                         f"refuses a float operand, and a refused guard is a skipped "
                         f"guard.")
    ap.add_argument("--iso-type", default="iso", choices=["iso", "qcow2", "raw", "vmdk"])
    ap.add_argument("--iso-out", default="/var/tmp/awnix-iso",
                    help="output directory INSIDE the distro")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    # `--iso-layer` names a LAYERS entry, not an image tag -- resolving it here
    # keeps the workflow from naming a tag the tool does not know about (and
    # from silently building media from the BASE image when a deeper layer was
    # the point of the dispatch; measured 2026-08-29).
    try:
        args.iso_image = resolve_iso_image(args.iso_layer, args.iso_image)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.iso_published:
        if not args.iso_layer:
            print("--iso-published requires --iso-layer (a layer names its variant)",
                  file=sys.stderr)
            return 2
        try:
            args.iso_image = resolve_iso_ghcr(args.iso_layer)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not _image_exists(args.iso_image):
            print(f"[iso] pulling {args.iso_image} (published ref not local)")
            _wsl(f"podman pull {args.iso_image}", timeout=1800)

    floor_err = iso_floor_error(args.iso_min_free_gb)
    if floor_err is not None:
        print(floor_err, file=sys.stderr)
        return 2

    # ── disk preflight (the ENOSPC guard) ──────────────────────────────────
    # Measured 2026-08-27: this tool started a build with the podman store at
    # 100%, the full layer died at `dnf install` with "At least 187MB more
    # space needed" twenty minutes in, and nothing beforehand had said a word.
    # A build that cannot SEE its disk is refused here; when short, --clear
    # reclaims dangling images / stopped containers / build cache first.
    # Runs BEFORE both branches: the ISO lane (bootc-image-builder) needs
    # even more space than the layer chain. When --iso, the preflight uses the
    # ISO floor (ISO_FLOOR_GB, kept equal to the shell script's MIN_FREE_GB by
    # the self-test) -- otherwise a build could pass THIS check and die at the
    # media step's own guard after 20 minutes of layer building.
    if not args.skip_preflight:
        if _disk_preflight is None:
            print("PREFLIGHT DEAD: check_disk_preflight.py is not importable — "
                  "cannot bless a build on an unmeasured disk.", file=sys.stderr)
            return 2
        need = args.preflight_need
        if need is None:
            need = ISO_FLOOR_GB if args.iso else PREFLIGHT_NEED_GB
        code = _disk_preflight(need, clear=True, prune_volumes=False)
        if code != 0:
            print(f"PREFLIGHT FAILED (exit {code}) — refusing to start the build. "
                  f"Free disk or run with --skip-preflight and a reason.",
                  file=sys.stderr)
            return 2 if code == 2 else 1

    if args.iso:
        return 0 if build_iso(args.iso_image, args.iso_type, args.iso_out,
                              min_free_gb=args.iso_min_free_gb) else 1

    if args.layer is None:
        wanted = list(LAYERS)
    else:
        one = next((x for x in LAYERS if x.name == args.layer), None)
        # A named layer brings its ancestors. Without this, asking for runner-ai on a box
        # that has not built runner fails with buildah trying to PULL `localhost/...` as
        # if it were a registry -- an error that names the network, not the missing step.
        # Already-built ancestors are skipped by build_layer's own tag check, so this
        # costs nothing on a warm box.
        wanted = chain_for(one) if one else []
        if one is not None and len(wanted) > 1:
            print(f"[chain] {args.layer} needs: "
                  f"{' -> '.join(x.name for x in wanted)}")
    if not wanted:
        print(f"no such layer: {args.layer}", file=sys.stderr)
        return 2

    all_ok = True
    for layer in wanted:
        if not build_layer(layer, force=args.force):
            all_ok = False
    return 0 if all_ok else 1


def _parent_of(containerfile: str) -> str:
    """The image a Containerfile builds FROM (its LAST FROM, ignoring build stages).

    Multi-stage files here open with a throwaway stage (awnix-runner-ai pulls a newer
    libstdc++ from stream10), so the FIRST FROM is not the parent -- taking it would
    have reported the wrong answer for that layer.
    """
    path = _bootc_dir() / containerfile
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    parents = [ln.split()[1] for ln in text.splitlines()
               if ln.startswith("FROM ") and len(ln.split()) > 1]
    if not parents:
        return ""
    # Resolve `FROM ${VAR}` against `ARG VAR=default` in the same file.
    # Containerfile.aitheros parameterises its base so the appliance can be
    # rebased (headless / desktop / gpu) without editing the file, and reading
    # that FROM literally yielded the string "${AITHEROS_BASE}" -- which matches
    # no tag, so the chain arm reported BROKEN while the default was exactly the
    # previous layer's tag and the real build proved the chain fine. A gate that
    # goes red on a correct, deliberate idiom gets switched off.
    #
    # A variable with NO default is left unresolved and still fails: the chain
    # genuinely is not decidable from the file in that case.
    return _resolve_arg(parents[-1], text)


def _resolve_arg(ref: str, text: str) -> str:
    """`${VAR}` -> its `ARG VAR=default` in the same file, else unchanged."""
    if not (ref.startswith("${") and ref.endswith("}")):
        return ref
    var = ref[2:-1]
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("ARG ") and "=" in ln:
            name, _, default = ln[4:].partition("=")
            if name.strip() == var:
                return default.strip().strip('"').strip("'")
    return ref


def _bootc_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent / ".DEPLOYMENT" / "standalone" / "bootc"
    return here.parents[3] / ".DEPLOYMENT" / "standalone" / "bootc"


def _chain_is_unbroken() -> bool:
    """Each layer's parent is the upstream base or the tag of the layer before it."""
    seen: set[str] = set()
    for layer in LAYERS:
        parent = _parent_of(layer.containerfile)
        if not parent:
            return False
        if not (parent.startswith("quay.io/centos-bootc/") or parent in seen):
            return False
        seen.add(layer.tag)
    return True


def _self_test() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'} - {label}")
        if not cond:
            ok = False

    def _raises_value_error(fn) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    bootc_dir_local = Path(__file__).resolve().parents[3] / ".DEPLOYMENT" / "standalone" / "bootc"
    check("the ISO script --iso shells actually exists on disk",
          (bootc_dir_local / ISO_SCRIPT).is_file())

    # --iso-out, both directions. Git-Bash rewrites a leading-slash argument
    # before python sees it, so /var/tmp/x arrived as
    # "C:/Program Files/Git/var/tmp/x"; the space split the argument and the
    # builder died naming a path fragment nobody typed, one line into a job that
    # takes tens of minutes.
    #
    # The ACCEPT arms matter as much as the refusals: a guard that refuses
    # everything passes a one-directional test while having deleted the feature.
    check("a real distro path is accepted",
          _iso_out_is_distro_path("/var/tmp/aitheros-appliance-raw"))
    check("...and so is the tool's own default",
          _iso_out_is_distro_path("/var/tmp/awnix-iso"))
    check("a Git-Bash-mangled path is REFUSED (the one that was hit live)",
          not _iso_out_is_distro_path("C:/Program Files/Git/var/tmp/x"))
    check("a mangled path with NO space is refused too -- that one would not "
          "error at all, it would write the image inside the Git install and "
          "report success",
          not _iso_out_is_distro_path("C:/msys64/var/tmp/x"))
    check("a bare relative path is refused",
          not _iso_out_is_distro_path("var/tmp/x"))
    # Assert the SUBSTITUTED command, not the template. The first version of this arm
    # looked for the literal "{image}" in the rendered string and could never pass --
    # a test that fails on correct code is worse than no test, because the next person
    # deletes it rather than reading it.
    _cmd = _iso_cmd_for_test(image="IMG", iso_type="qcow2", out="/tmp/OUT")
    check("build_iso passes the image through rather than hardcoding one",
          "--image IMG" in _cmd)
    check("build_iso passes the requested type through",
          "--type qcow2" in _cmd)
    check("build_iso passes the output directory through",
          "--out /tmp/OUT" in _cmd)
    # The ISO floor must agree with the shell script's own MIN_FREE_GB: the tool
    # blesses a build via check_disk_preflight with ISO_FLOOR_GB, and the script
    # then refuses below MIN_FREE_GB. If they disagree, one of them lies -- a
    # build can pass the tool's preflight and die at the script's guard (or be
    # refused here for a floor the script would accept). Parsed from the script
    # rather than pinned, so the two cannot drift silently.
    _iso_script = (bootc_dir_local / ISO_SCRIPT).read_text(encoding="utf-8")
    _mfloor = None
    for ln in _iso_script.splitlines():
        if ln.startswith("MIN_FREE_GB="):
            _mfloor = ln.split("=", 1)[1].strip()
    check("the ISO script declares a MIN_FREE_GB", _mfloor is not None)
    check("ISO_FLOOR_GB matches the script's MIN_FREE_GB "
          f"(tool {ISO_FLOOR_GB} vs script {_mfloor})",
          _mfloor is not None and float(_mfloor) == ISO_FLOOR_GB)
    # The WORKFLOW's preflight is a THIRD copy of the floor (`-ge 40` in the
    # preflight step) and it refused before the build from 2026-08-29 on.
    # Tool-vs-script parity alone would pass while the workflow drifted to a
    # different number -- the preflight then refuses at a floor the build tool
    # would bless, or blesses one the media step refuses. Parse the workflow
    # the same way the script is parsed, so all three copies must move
    # together or this arm names the straggler.
    _wf = (Path(__file__).resolve().parents[3] / ".github" / "workflows"
           / "build-awnix-iso.yml")
    _wfloor = None
    if _wf.is_file():
        # Match the COMPARISON, not any `-ge N`: the workflow also carries
        # `[ "$PARTS" -ge 1 ]` and `[ "${N:-0}" -ge 1 ]` (the publish-split
        # guards) -- a bare `-ge` scan takes the LAST one and reports the
        # publish step's 1 as the preflight's floor. The first draft of this
        # arm did exactly that and FAILED on itself (workflow 1 vs tool 40),
        # which is the arm working: a floor comparison that names FREE_GB is
        # the preflight, and it is unambiguous.
        import re
        for ln in _wf.read_text(encoding="utf-8").splitlines():
            _m = re.search(r"FREE_GB\"\s+-ge\s+(\d+)", ln)
            if _m:
                _wfloor = _m.group(1)
                break
    check("the workflow preflight's floor matches the tool's "
          f"(workflow {_wfloor} vs tool {ISO_FLOOR_GB})",
          _wfloor is not None and float(_wfloor) == ISO_FLOOR_GB)

    # --iso-layer resolves a LAYERS name to the image the dispatch actually
    # asked to build. This arm exists because the workflow never passed the
    # layer, so every media build used the BASE image -- and the GobOS
    # appliance ISO would have shipped without GobboNet (measured 2026-08-29).
    check("--iso-layer gobbonet resolves to the gobbonet image tag",
          resolve_iso_image("gobbonet", LAYERS[0].tag)
          == next(x.tag for x in LAYERS if x.name == "gobbonet"))
    check("no --iso-layer leaves the default image untouched",
          resolve_iso_image(None, LAYERS[0].tag) == LAYERS[0].tag)
    check("an unknown layer name is refused",
          _raises_value_error(lambda: resolve_iso_image("nope", LAYERS[0].tag)))
    check("--iso-layer AND an explicit --iso-image are refused as ambiguous",
          _raises_value_error(lambda: resolve_iso_image(
              "base", "localhost/something-else:latest")))

    # --iso-published resolves the layer's variant to its PUBLISHED registry
    # ref. The boolean-vs-string arm exists because load_variants parses
    # `publish: true` as the BOOL True while the shell script reads raw text
    # "true" -- the first version compared against the string and refused
    # EVERY publishable variant with "the flip has not happened" (measured
    # 2026-09-01, fixed the same day). The manifest's own values decide both
    # directions, so a flip that changes the manifest changes this verdict.
    try:
        from check_awnix_variants import load_variants
    except ImportError:
        load_variants = None
    variants_file = (Path(__file__).resolve().parents[3]
                     / ".DEPLOYMENT" / "standalone" / "bootc"
                     / "awnix-variants.yaml")
    _want_pub = None
    if load_variants is not None and variants_file.is_file():
        for _name, _v in load_variants(variants_file).items():
            if _v.get("layer") == "base":
                _pub = _v.get("publish")
                _want_pub = (f"{_v.get('registry', 'ghcr.io/aitherium')}/"
                             f"{_v['repo']}:latest") if _pub is True else None
                break
    if _want_pub is not None:
        check("--iso-published resolves a publish:true variant to its "
              "registry ref (bool True spelling)",
              resolve_iso_ghcr("base") == _want_pub)
    check("--iso-published refuses a publish:false variant (the pre-flip "
          "state must fail loudly, never fall back to the local image)",
          _raises_value_error(lambda: resolve_iso_ghcr("garg")))
    check("--iso-published refuses an unknown layer",
          _raises_value_error(lambda: resolve_iso_ghcr("nope")))

    # The min-free-gb floor is forwarded to the shell script as an INTEGER.
    # Bash's `[ "$a" -lt "$b" ]` rejects a float operand with "integer
    # expression expected" -- and the guard then evaluates FALSE, i.e. the
    # refusal never fires. A floor that fails open is not a floor.
    check("the floor is forwarded as an integer, never a float",
          _iso_cmd_for_test(min_free_gb=40.0).endswith("--min-free-gb 40"))
    check("no floor -> no --min-free-gb flag at all",
          "--min-free-gb" not in _iso_cmd_for_test())
    check("a fractional floor is REFUSED, never silently rounded down -- "
          "int(40.6)=40 would weaken a safety floor",
          iso_floor_error(40.6) is not None)
    check("a whole-number floor is accepted",
          iso_floor_error(41.0) is None)
    check("no floor at all is accepted",
          iso_floor_error(None) is None)

    # The PROPERTY, not one host's spelling of it. This arm read
    # `_cmd.startswith("bash /mnt/c/")`, which baked in the same Windows assumption the
    # tool itself carried -- so the moment the path became host-derived (so builds could
    # run on the AWS awnix runner, where there is no /mnt/c and no `wsl` at all) the
    # self-test failed on a correct change. An over-specified assertion does not protect
    # behaviour; it pins an accident.
    # ── the host ladder ──────────────────────────────────────────────────────────
    # This tool assumed Windows+WSL and so could run NOWHERE else: every build shelled
    # `wsl -d Debian -u root`, which on the AWS awnix runner -- the box actually
    # provisioned for these builds -- is `FileNotFoundError: 'wsl'`. Both directions are
    # asserted, because a ladder that always takes one rung is not a ladder.
    _real_which = shutil.which
    try:
        shutil.which = lambda _n, *_a, **_k: "/usr/bin/wsl"
        check("with wsl present, commands go through the fleet distro",
              _host_prefix()[:1] == ["wsl"] and _shell_bootc_dir().startswith("/mnt/c/"))
        shutil.which = lambda _n, *_a, **_k: None
        check("with no wsl, commands run bash DIRECTLY (the Linux/AWS host)",
              _host_prefix() == ["bash", "-s"])
        # The path must be DERIVED, not the Windows one with the prefix stripped -- on a
        # runner the checkout is at /opt/actions-runner/_work/..., nowhere near /mnt/c.
        _p = _shell_bootc_dir()
        check("with no wsl, the bootc dir is derived from this file, not /mnt/c",
              not _p.startswith("/mnt/c/") and _p.endswith(".DEPLOYMENT/standalone/bootc"))
    finally:
        shutil.which = _real_which

    # The build must use host networking: rootful podman inside a container builds a
    # bridge whose resolver does not work, and the symptom is a DNS failure deep in a
    # dnf step rather than anything naming the network.
    _bs = inspect.getsource(build_layer)
    check("podman build must use --network=host (rootful-in-container has no working "
          "resolver otherwise)", "--network=host" in _bs)

    # ...and so must the MEDIA step, for exactly the same reason. It is a SEPARATE
    # podman run in a SEPARATE file, so the layer-build arm above cannot speak for it,
    # and it was missing the flag while the layer build had it -- measured on run
    # 32547979438, where the image layer built clean and bootc-image-builder then died
    # depsolving with "Could not resolve host: mirrors.centos.org". Read from the shell
    # script on disk rather than from a Python string: the script is what actually runs,
    # and asserting a copy of it would pass while the artifact regressed.
    _iso_src = _bootc_dir() / ISO_SCRIPT
    if not _iso_src.is_file():
        check("the ISO script is readable (cannot judge its flags otherwise)", False)
    else:
        _iso_text = _iso_src.read_text(encoding="utf-8", errors="replace")
        _bib_run = _iso_text.split("podman run --rm --privileged", 1)
        check("the bootc-image-builder run is present to judge", len(_bib_run) == 2)
        if len(_bib_run) == 2:
            # Only the flags of THAT invocation, not the whole file -- a --network=host
            # mentioned anywhere in a comment must not discharge this.
            _flags = _bib_run[1].split('"$BIB"', 1)[0]
            check("bootc-image-builder must use --network=host (it depsolves against "
                  "the CentOS mirrors and rootful-in-container has no resolver)",
                  "--network=host" in _flags)

    check("build_iso invokes via `bash <script>` -- the execute bit does not survive "
          "the Windows filesystem, so a chmod would appear to work and be undone",
          _cmd.startswith("bash /"))
    # ONE unbroken chain. Asserted as a PROPERTY, not a count: until 2026-08-21 the
    # AitherOS appliance started FROM the upstream bootc base in parallel with awnix
    # rather than on top of it, so the appliance carried none of the aw* tools and
    # every awnix fix had to be made twice. A `len(LAYERS) == N` arm cannot express
    # that -- both chains had a perfectly good length.
    check("the awnix chain is intact (base -> runner -> runner-ai)",
          [x.name for x in LAYERS[:3]] == ["base", "runner", "runner-ai"])
    check("the appliance STACKS on awnix rather than running parallel to it",
          _parent_of("Containerfile.base") == "localhost/awnix-base:latest")
    check("every layer FROMs either the upstream bootc base or the previous tag",
          _chain_is_unbroken())
    # A parameterised base resolves through its ARG default, and one WITHOUT a
    # default does not -- the second half is what keeps this from turning the
    # chain arm into a rubber stamp for any variable someone introduces.
    _sample = ("ARG AITHEROS_BASE=localhost/aitheros-bootc-gpu:latest\n"
               "FROM ${AITHEROS_BASE}\n")
    check("FROM ${VAR} resolves through its ARG default",
          _resolve_arg("${AITHEROS_BASE}", _sample)
          == "localhost/aitheros-bootc-gpu:latest")
    check("...and a variable with NO default stays unresolved, so the chain "
          "still fails rather than passing on something undecidable",
          _resolve_arg("${NOPE}", _sample) == "${NOPE}")
    for layer in LAYERS:
        check(f"{layer.name}: tag is fully-qualified localhost/ (never a bare name podman "
              f"could resolve against a remote registry by mistake)",
              layer.tag.startswith("localhost/"))
        check(f"{layer.name}: verify_cmd ends in an explicit success marker, "
              f"never just 'exit 0 of the last command'",
              layer.verify_label != "" and "_OK" in layer.verify_cmd)

    containerfiles = {layer.name: layer.containerfile for layer in LAYERS}
    # Walk up to .git rather than a fixed parents[N] -- a hardcoded index
    # has been wrong before in this exact family of tools (SHW family,
    # aitheros-dispatch.md), silently scanning the wrong tree instead of
    # failing loudly.
    repo_root = Path(__file__).resolve()
    while repo_root != repo_root.parent and not (repo_root / ".git").exists():
        repo_root = repo_root.parent
    bootc_dir = repo_root / ".DEPLOYMENT" / "standalone" / "bootc"
    check("resolved a real repo root (found a .git directory walking up)",
          (repo_root / ".git").exists())
    for name, cf in containerfiles.items():
        check(f"{name}: {cf} actually exists on disk", (bootc_dir / cf).is_file())

    # Every COPY --from= must name a declared context or a build stage. An
    # UNDECLARED name dies ~20 minutes into the build at the first COPY with
    # "failed to resolve named context", after the layer has already pulled
    # and installed everything before it -- the exact shape a one-context
    # regression of Containerfile.aitheros produced on 2026-08-27 (an
    # in-flight edit dropped the `deploy` context; the two-context layout is
    # load-bearing: the repo-root .dockerignore filters .DEPLOYMENT/
    # wholesale, so a single context silently drops every deploy file).
    def _contexts_ok(layer: Layer, bd: Path) -> bool:
        cf = bd / layer.containerfile
        if not cf.is_file():
            return True  # existence is asserted above
        text = cf.read_text(encoding="utf-8", errors="replace")
        stages = set(re.findall(r"FROM\s+\S+\s+AS\s+([A-Za-z0-9_]+)", text))
        declared = {name for name, _ in layer.contexts}
        for m in re.finditer(r"COPY\s+--from=([A-Za-z0-9_]+)", text):
            if m.group(1) not in declared and m.group(1) not in stages:
                return False
        return True

    for layer in LAYERS:
        check(f"{layer.name}: every COPY --from= names a declared context or stage",
              _contexts_ok(layer, bootc_dir))
    # The failing direction: an undeclared context name must fail the check.
    bad = Layer(name="badctx", tag="localhost/badctx:latest",
                containerfile="Containerfile.badctx",
                verify_cmd="true && echo BADCTX_OK", verify_label="unreachable")
    (bootc_dir / "Containerfile.badctx").write_text(
        "FROM scratch\nCOPY --from=ghost /x /y\n", encoding="utf-8")
    check("an undeclared COPY --from= name fails the context check",
          not _contexts_ok(bad, bootc_dir))
    (bootc_dir / "Containerfile.badctx").unlink()
    # The optional-context direction: a layer MAY declare a context whose dir
    # is absent (the appliance's svcimg — a valid unbaked build), so the
    # missing-dir refusal must not fire for it, while a REQUIRED absent
    # context must still refuse.
    opt = Layer(name="optctx", tag="localhost/optctx:latest",
                containerfile="Containerfile.optctx",
                verify_cmd="true && echo OPTCTX_OK", verify_label="unreachable",
                contexts=(("svcimg", "/var/tmp/definitely-not-staged"),),
                optional_contexts=("svcimg",))
    (bootc_dir / "Containerfile.optctx").write_text(
        "FROM scratch\nCOPY --from=svcimg /x /y\n", encoding="utf-8")
    check("an OPTIONAL absent context is accepted (unbaked build is valid)",
          _contexts_ok(opt, bootc_dir)
          and not [f'{n}={r}' for n, r in opt.contexts
                   if n not in opt.optional_contexts
                   and not (bootc_dir / r).is_dir()])
    (bootc_dir / "Containerfile.optctx").unlink()

    # build_layer() must not raise even when the WSL call itself fails --
    # that is a reportable outcome (a print + False), never a crash.
    fake_layer = Layer(name="fake", tag="localhost/does-not-exist:latest",
                        containerfile="Containerfile.does-not-exist",
                        verify_cmd="true && echo FAKE_OK", verify_label="unreachable")
    orig_wsl = globals()["_wsl"]

    def _boom(script: str, timeout: int = 900) -> tuple[int, str]:
        return 1, "podman: no such file or directory"

    globals()["_wsl"] = _boom
    try:
        result = build_layer(fake_layer, force=True, verbose=False)
        check("a build failure is reported as False, never an exception",
              result is False)
    except Exception as exc:
        check(f"a build failure is reported as False, never an exception (raised {exc!r})",
              False)
    finally:
        globals()["_wsl"] = orig_wsl

    # ── _image_exists: a probe timeout is a clean DEAD, never a traceback ──
    # Measured 2026-08-27: on the loaded box a 30s probe timed out and
    # TimeoutExpired crashed the whole tool mid-run. The retry arm must pass
    # on a second-attempt success, and a double timeout must raise DeadError
    # (the tool's own 'cannot judge' contract), not TimeoutExpired.
    from unittest import mock

    with mock.patch.object(sys.modules[__name__], "_wsl",
                           side_effect=[subprocess.TimeoutExpired([], 30),
                                        (0, "")]):
        check("an image-exists probe timeout retries and succeeds on attempt 2",
              _image_exists("localhost/fake:latest") is True)
    with mock.patch.object(sys.modules[__name__], "_wsl",
                           side_effect=[subprocess.TimeoutExpired([], 30),
                                        subprocess.TimeoutExpired([], 180)]):
        try:
            _image_exists("localhost/fake:latest")
            check("a double probe timeout raises DEAD, never a traceback", False)
        except DeadError:
            check("a double probe timeout raises DEAD, never a traceback", True)
        except Exception as exc:
            check(f"a double probe timeout raises DEAD, never a traceback "
                  f"(raised {type(exc).__name__})", False)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
