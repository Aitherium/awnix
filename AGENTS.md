# awnix for agents

Read this if you are an agent (or a human) editing this repo. Short on purpose:
the traps that cost a session, and where the rest lives. Nothing here is read
at runtime — it is for you.

## What this repo is

- The OS: `Containerfile` (the base) + `bootc/Containerfile.awnix*` (the
  variants). `podman build -t awnix:latest -f Containerfile .` builds it;
  bootc-image-builder turns an image into bootable media (the ISO lane).
- Parts of this repo are a **synced mirror**: root docs and everything under
  `.DEPLOYMENT/standalone/bootc/**` are staged from the monorepo that owns
  them. Hand edits to mirrored files are overwritten on the next sync — change
  the source and let the lane publish, or say so in an issue.
- Files withheld from this mirror are withheld by a derived rule (a variant
  manifest), not a list someone maintains by hand. If a file you expect is
  missing, that rule removed it on purpose — do not copy it here.

## Build and check

| command | asserts |
|---|---|
| `podman build -t awnix:latest -f Containerfile .` | the base image builds |
| `podman build -t awnix-ai-full -f bootc/Containerfile.awnix-ai-full .` | the flagship variant builds |
| `pwsh -File .DEPLOYMENT/standalone/bootc/install-awnix-wsl.ps1 -SelfTest` | the WSL2 installer's pure self-test (Windows; touches neither WSL nor the disk) |
| `sh .DEPLOYMENT/standalone/bootc/awnix-to-wsl.sh --self-test` | the export half's self-test (inside a distro with podman) |

## Rules that keep this lane installable for strangers

- **The public ladder is `awnix` -> `awnix-full` -> `awnix-ai-full`.** Appliance
  and fleet tooling are maintained elsewhere and are not published here; do not
  add files that name internal deployments, customers, hosts, ports or env keys.
- **One-liners must fail loudly.** Every install path here verifies what it
  produced — a tarball exists and is rootfs-sized, the distro is registered,
  PID 1 is systemd — rather than trusting an exit code. Keep that shape; a
  "successful" run that installed nothing is the failure class this repo has
  paid for repeatedly.
- **Scripts run on stock Windows PowerShell 5.1 and plain `sh`.** No pwsh-7-only
  syntax in `.DEPLOYMENT/standalone/bootc/*.ps1`, and quote-proof multi-layer
  command passing (see how `install-awnix-wsl.ps1` base64s its `sh` payload
  through PowerShell -> wsl.exe -> sh).

## Read next

- `llms.txt` — the agent-executable install path for a NEW machine (point an
  agent at this file and say "set this machine up").
- `README.md` — the human front door
- `docs/` — the docs site source
