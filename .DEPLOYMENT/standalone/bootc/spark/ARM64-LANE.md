---
id: arm64-spark-jetson
title: ARM64, DGX Spark and Jetson
applies_to: [awnix, awnix-ai, garg-appliance]
---

# ARM64, DGX Spark and Jetson

awnix is being made to build for x86_64 and aarch64 from one set of Containerfiles. The
rule is that each download that depends on the architecture is chosen by a `case` over
`TARGETARCH`, that the case falls back to `uname -m` when the build does not set it, and
that an unknown architecture stops the build with an error. `check_awnix_multiarch.py`
reports where the Containerfiles do not follow the rule yet; until it exits 0, some layers
still assume x86 (for example the base image's `${TARGETARCH:-amd64}` fallback).

No arm64 build has run yet. Every "Target" below is a design target, not a measured
result; see "How to describe it".

## What arm64 covers today

awnix
: The base image. PowerShell comes from the published linux-arm64 tarball, because
  PowerShell publishes no aarch64 RPM. Target: supported (not yet built on arm64).

awnix-runner
: The GitHub Actions runner (linux-arm64) and the Python tool cache (linux-24.04-arm64
  builds of 3.10, 3.11 and 3.12). The runner tarball is checked against the SHA-256
  actions/runner publishes. Target: supported (not yet built on arm64).

awnix-ai
: llama.cpp from the PrismML vulkan-arm64 release, plus the matching bundled loader.
  The serve script finds the loader by glob, so the same script runs on both
  architectures. Target: supported once its Containerfile picks the arm64 asset.

garg appliance
: Experimental on arm64. qdrant publishes an aarch64-musl build, and the appliance takes
  it once the Containerfile chooses it by architecture.

Desktop and platform appliances
: x86_64 only.

`check_awnix_multiarch.py` enforces this. If an x86 literal appears outside an
architecture case, the gate fails. It also fails if a case has no arm64 arm, or if it
lacks a `*)` arm that exits non-zero.

## Building on aarch64

The lane builds natively on an aarch64 machine. It does not cross-build under emulation,
because an image built that way can pass while carrying binaries for the wrong
architecture. Start a build:

```
gh workflow run awnix-arm64.yml -f layer=awnix -f runner=hosted -f publish=false
```

The build input choices:

layer
: `awnix`, `runner`, `runner-ai` or `garg`. The lane also builds every layer beneath the
  one you pick.

runner
: `hosted` runs on GitHub's `ubuntu-24.04-arm`. `spark` runs on a registered DGX Spark
  (see below).

iso, boot_smoke
: Hosted runs only. `iso` makes an aarch64 ISO with bootc-image-builder. `boot_smoke`
  boots a qcow2 of the same image under `qemu-system-aarch64` with no network and waits
  for `login:` on the serial console. It never boots the ISO, and the ISO is not kept
  after it is hashed.

publish
: Off by default, and only the owner may turn it on. The lane pushes nothing while it
  is off.

After every layer builds, the lane checks that the image architecture is `arm64`, that
`uname -m` inside the image reports `aarch64`, and that the layer's own contents are
present. It then uploads `arm64-evidence.json` with the layer, image digest, build and
probe exit codes, and the ISO and boot results.

## On a DGX Spark

A DGX Spark (GB10 Grace-Blackwell, aarch64) can build and run the awnix arm64 image as
a container while it goes on serving inference. The lane never reimages the box.

To register the Spark as a build runner, run `register-spark-arm64-runner.sh` on it. By
default the script only prints its plan. With `--apply --token-file F` it:

- creates an unprivileged `awnix-build` account with no sudo and no container-engine
  group, which runs rootless podman through subuid and subgid;
- gives that account its own podman store under `/var/lib/awnix-build`, named in
  `/etc/awnix-build/storage.conf`. The workflow sets `CONTAINERS_STORAGE_CONF` to that
  file, so a build never opens the store the inference containers use;
- runs the runner in a capped `awnix-build.slice` (CPU quota, memory ceiling, low CPU and
  I/O weight);
- labels the runner `self-hosted, Linux, ARM64, spark-build, offbox`, never
  `aitheros-local`.

A Spark build refuses to start when MemAvailable is below 24 GB. It also refuses ISO and
boot-smoke builds, because bootc-image-builder needs rootful `--privileged`. It never runs
a store-wide podman command such as `system prune`, `migrate` or `reset`.

To use the GB10 GPU from an EL9 image, point the NVIDIA CUDA repository at `rhel9/sbsa`
(the Grace server build) instead of `rhel9/x86_64`.

## Jetson

Jetson is a design only. JetPack's GPU driver comes from NVIDIA's Ubuntu-based L4T, not
EL9, so an awnix image booted on a Jetson Orin would have no GPU. The practical path is
to run the awnix arm64 container under podman on L4T. Nobody has built or tested that
yet.

## How to describe it

MEASURED
: Only for a run whose URL and exit codes are recorded: a green `publish=false` dispatch
  whose evidence shows `arch: arm64`, `build_exit: 0` and `probe_exit: 0`. A boot-smoke
  verdict of PASS measures only that a qcow2 of the same image boots to `login:`.

aarch64 ISO
: BUILT-UNMEASURED, even on a green run. The lane checks only its ISO9660 header,
  never boots it, and does not keep it, so its recorded sha256 cannot be re-checked
  (`boot_verified: false`, `retained: false` in the evidence).

BUILT-UNMEASURED
: The Containerfiles, the lane and a clean `check_awnix_multiarch.py`, with no green
  hosted run yet.

Spark
: "Hardware on hand; the awnix arm64 container runs on its userland" at most, and only
  after a Spark run is recorded.

Jetson
: DESIGN.

Safe wording: "x86_64 and aarch64 images built from one Containerfile set."
