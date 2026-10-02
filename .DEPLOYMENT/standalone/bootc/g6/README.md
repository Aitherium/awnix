# G6: bootc upgrade, rollback and greenboot fallback proof

This is the AFRL proof plan gap G6, demo Steps 4 and 5. The harness runs a real
`bootc upgrade` to N+1 and a real `bootc rollback` to N's exact digest. It then asks
whether the IMAGE, with no help from the harness, gets a node on a faulty N+2 back to N,
and it checks that `/var` agent state survives every step.

**Status (2026-09-28).** Runs 36415942227 and 36417253922 recorded PASS, but their
fallback was not automatic. On the last N+2 boot greenboot 0.16.4 failed its health
check and did **not** reboot. The node reached `multi-user.target` on N+2, and the G6
test agent's own reboot is what brought N back. A fielded node with no agent would have
stayed on N+2. The verdict now fails any run in which the agent rebooted N+2, and the
agent no longer does it. The image now carries `../awnix-greenboot-fallback.{sh,service,conf}`,
an `OnFailure=` of `greenboot-healthcheck.service` that reboots only when a fallback is
armed.

Hosted run **36420682028** (commit `dd6ad1010d`) is the first run under these rules: the
proof ended PASS with `agent_reboots_on_n2=0` and `--evidence` exit 0, and the negative
control ended NO-AUTO-FALLBACK with exit 1. See the Record below. The measured claim is
"greenboot plus `awnix-greenboot-fallback` return a node from a faulty N+2 to N with no
outside reboot, on centos-bootc:stream9 in a KVM VM". `fallback_reboot=added` in that
run: the unit came from this harness, because neither product Containerfile ships it
yet. For awnix and garg the unattended fallback stays BUILT-UNMEASURED until they
install it and a `--base` run passes. The images come from a local registry, so no credential is
needed. The last proof, `../upgrade-proof.sh`, ended AUTH-WALL because it needed a
ghcr token.

## What runs where

| file | role |
|---|---|
| `g6-proof.sh` | Host harness (root). It builds N, N+1 and N+2, serves them from `registry:2` on **127.0.0.1:5000**, and installs N with `bootc install to-disk --via-loopback`. It boots the disk under qemu+KVM once per boot (`-no-reboot`), retags `:stable` when the guest asks, and runs the verdict. |
| `Containerfile.g6` | The three images. `BASE` defaults to public `quay.io/centos-bootc/centos-bootc:stream9`, and `--base` swaps in awnix or garg. It adds greenboot and the real `../awnix-health-check.sh` (as `required.d/10-awnix-core.sh`) only when the base lacks them. It sets `GREENBOOT_MAX_BOOT_ATTEMPTS=3` if unset, a serial karg, and the two agent units. N and N+1 differ only in a role layer; N+2 also carries `n2-fail.sh`. |
| `g6-agent.sh` + `g6-agent.service` | In-guest state machine in `/var/lib/awnix-g6`. It takes one step per boot and prints markers to ttyS0 and to `/var/lib/awnix-g6/agent.log`. Getty can eat the serial tail, so the host also reads the log off the disk. |
| `g6-mark.service` | Counts every boot before greenboot runs. A faulty N+2 is rebooted before `multi-user.target`, so this marker is the only trace it leaves and the source of `greenboot_attempts`. |
| `n2-fail.sh` | A greenboot `required.d` check that always exits 1. It stands in for "the model service fails health". |
| `50-g6-registry.conf` | Marks `10.0.2.2:5000` insecure, meaning plain HTTP with no auth. qemu user networking maps that address to the host's 127.0.0.1. |
| `08_g6_fallback_counting.cfg` | The grub2 boot-counting hunk. It is added **only** when the image's bootupd static grub config has no `boot_counter` logic. `/usr/lib/g6/grub_counting` records `present`, `added`, `absent` or `n/a`, and the verdict carries that value. |
| `g6_verdict.py` | Turns the serial log and `digests.json` into `verdict.json` plus a last line `G6-VERDICT: <V>`. Its `--self-test` runs the four fixtures in `fixtures/`. |

## The cycle

| launch | image booted | agent does |
|---|---|---|
| 1 | N | writes the `/var/lib/aither/g6-sentinel` and its sha, then `G6-WANT: stable=n1` and power-off |
| 2 | N | `bootc upgrade`; it fails if nothing was staged (bootc exits 0 on "no update") |
| 3 | N+1 | `bootc rollback`, or `awnix update rollback` when the image has it |
| 4 | N | `G6-WANT: stable=n2` |
| 5 | N | `bootc upgrade` stages N+2 |
| 6–8 | N+2 | greenboot fails the required check and reboots; grub decrements `boot_counter` |
| 9 | N+2 | greenboot fails again and does NOT reboot. The agent prints `G6-HOLD` and never reboots. `awnix-greenboot-fallback.service` has to issue the reboot |
| 10 | N (grub fallback) | `G6-STEP: fallback rc=0 n2_boots=4`, then `G6-END` |

If nothing in the image reboots boot 9, the agent's hold runs out (300 s,
`/usr/lib/g6/hold_secs`) and it records `fallback rc=1`, which is NO-AUTO-FALLBACK.

Every `G6:` line re-hashes the sentinel. A mismatch at any boot gives STATE-LOST.

Rows 6 to 8 and the boot-9 greenboot failure are what hosted runs 36411166098,
36415942227 and 36417253922 did on centos-bootc:stream9 with greenboot 0.16.4. In all
three, boot 9 then printed `G6: still on n2 after 4 boot(s); rebooting to give greenboot
its count`: the old agent rebooted into N, not the image. The real markers of two of them
are in `fixtures/run-36411166098.serial.log` and `fixtures/run-36417253922.serial.log`,
and both must now judge NO-AUTO-FALLBACK. The armed counter comes from N, the image
running when N+2 is staged: greenboot-rs enables `greenboot-set-rollback-trigger.service`
as a `Requires=` of `ostree-finalize-staged.service`.

The agent writes every marker to both ttyS0 and `/dev/kmsg`, so a real serial log has
each line twice. The parser counts each boot once.
An agent reboot of N+2 (the old `still on n2 ... rebooting` line) is counted in
`agent_reboots_on_n2`. Any value above 0 makes the fallback step NO-AUTO-FALLBACK.

## Verdict

`verdict.json` uses schema 1: `{verdict, exit_code, reason, base_image, digests{n,n1,n2,n_registry},
plan, steps[{step, expect_digest, booted_digest, rc, ok}], boots, greenboot_max,
greenboot_attempts, grub_counting, fallback_reboot, agent_reboots_on_n2,
state_sentinel_intact, kvm, mode, inject, runner, gh_run_id, started_at, ended_at}`.

| verdict | exit | meaning |
|---|---|---|
| PASS | 0 | Every planned step booted the expected digest and the sentinel stayed intact |
| UPGRADE-FAILED | 1 | After the upgrade the node did not boot N+1's digest |
| ROLLBACK-FAILED | 1 | After the rollback the node did not boot N's digest (as reported at boot 1) |
| NO-AUTO-FALLBACK | 1 | N+2 kept booting past `GREENBOOT_MAX_BOOT_ATTEMPTS + 1`, the agent's hold on N+2 ran out, or the agent rebooted N+2 |
| STATE-LOST | 1 | A boot reported `sentinel=missing` |
| UNJUDGED | 2 | Markers are missing: no G6-END, a truncated log, or equal digests |
| CONTAINER-ONLY | 2 | No `/dev/kvm`. The images were built and linted but nothing booted. **Never MEASURED.** |

N is the digest the guest reports at boot 1. An install from containers-storage can
carry a different manifest digest from the one pushed, so the pushed digest is kept as
`n_registry`.

## Running it

In hosted CI (the normal route):

```bash
gh workflow run awnix-g6-proof.yml --ref <branch>                          # proof + nothing else
gh workflow run awnix-g6-proof.yml --ref <branch> -f inject=no-greenboot   # negative control
```

A push to `feat/awnix-w2-g6-**` that touches this directory, and the weekly schedule,
run **both** the proof and the negative control.

The negative control is `inject=no-greenboot`, and it is built into **every** role. N+2
still fails health, but greenboot's health gate is taken out. On greenboot-rs that means
two units:

- `greenboot-healthcheck.service`, which fails the boot, reboots and rolls back. It is
  **masked**, because run 36414661590 showed it still starts after `disable`.
- The rollback trigger that arms grub's `boot_counter` when N stages N+2:
  `greenboot-set-rollback-trigger.service` on greenboot-rs, or
  `greenboot-grub2-set-counter.service` on the older bash greenboot. It is only
  **disabled**, never masked: `ostree-finalize-staged` requires it, so masking it
  would break every staging.

The build fails unless the runner and one trigger were found. The same faulty N+2 must
then stay booted, and the job passes only when the harness ends NO-AUTO-FALLBACK with
exit 1. That shows two things: the check can fail, and the real run's fallback came
from greenboot.

Three earlier versions were no-ops and N+2 fell back anyway:

- Run 36411166098 masked only the 0.15 unit names.
- Run 36413303281 disabled only the trigger.
- Run 36414661590 also disabled the runner, but did not mask it.

G6P007 pins the current version.

To check the evidence:

```bash
gh run download <id> -n awnix-g6-proof-<id>-none
python AitherOS/dev/tools/check_bootc_g6_proof.py --evidence verdict.json   # 0 = MEASURED
```

On a disposable metal box (root, podman, skopeo, qemu-system-x86_64, python3):

```bash
sudo .DEPLOYMENT/standalone/bootc/g6/g6-proof.sh --work /var/tmp/awnix-g6
sudo .DEPLOYMENT/standalone/bootc/g6/g6-proof.sh --base ghcr.io/aitherium/awnix-garg-appliance@sha256:... # product base
```

Never run it on the owner's workstation. It builds three OS images and boots a VM up
to 16 times.

## Record: run 36420682028 (current)

Triggered by the push of `dd6ad1010d` on a hosted `ubuntu-24.04` runner with KVM, base
`quay.io/centos-bootc/centos-bootc:stream9`, greenboot 0.16.4, 2026-09-28 12:16-12:24Z.

| job | harness exit | verdict | `--evidence` exit |
|---|---|---|---|
| `g6-proof (none)` | 0 | PASS: 10 boots, `greenboot_attempts=4`, `agent_reboots_on_n2=0`, `grub_counting=present`, `fallback_reboot=added` | 0 (MEASURED-in-VM) |
| `g6-proof (no-greenboot)` | 1 | NO-AUTO-FALLBACK: N+2 booted once, the agent's 300 s hold ran out, the fallback unit never fired | 1 |

| role | digest |
|---|---|
| N (booted at boot 1) | `sha256:7c1383cc01a3a1cd56a4f11bd47b6197335e33dad670e280376df0aeff2fd58a` |
| N+1 | `sha256:c9f40d514a9bff3b6fe83f930f83cded7bbd9b8f72e079df12735505d8397606` |
| N+2 | `sha256:b0a8bea737517fda0ef0ec4ba886fdf201a0063abf3a4e5c8fe4ba1a5c740321` |

Boot 9 on N+2: greenboot failed, the serial shows `awnix-greenboot-fallback: fallback
armed (grub boot_counter=0): rebooting`, and the agent printed only `G6-HOLD`. Boot 10
came up on N and printed `G6-STEP: fallback rc=0 n2_boots=4`. On boots 6 to 8 the
fallback unit also fired (counter 3, 2, 1), next to greenboot's own reboot, so those
reboots cannot be attributed to one or the other. Both are part of the image.

The verdicts and the proof job's `digests.json` are committed in `evidence/` as
`36420682028-*`, and a test re-judges them. Artifacts: `awnix-g6-proof-36420682028-{none,no-greenboot}`.

## Record: run 36415942227 (superseded 2026-09-28: the fallback half is refuted)

This record is kept as it was measured. Its fallback line ("came back up on N by
itself") is wrong: boot 9 was rebooted by the G6 agent (see Status at the top).
`check_bootc_g6_proof.py --evidence` now refuses the committed proof verdict with G6E004
(exit 1), and a test pins that. The upgrade and rollback rows below still hold.

Run 36415942227 was triggered by a push of commit `52be8a9e3c`. It ran on a hosted
`ubuntu-24.04` runner with KVM, base image `quay.io/centos-bootc/centos-bootc:stream9`,
and greenboot 0.16.4.

| job | harness exit | verdict | job result |
|---|---|---|---|
| `g6-proof (none)` | 0 | PASS | success |
| `g6-proof (no-greenboot)` | 1 | NO-AUTO-FALLBACK (N+2 stayed booted for 5 boots) | success (the control failed as required) |

The proof job's digests:

| role | digest |
|---|---|
| N (booted at boot 1) | `sha256:8bffd38456ff9e0132bad46b2b5e83359b3928f93aedc8f8c4da4d5b0b78feec` |
| N+1 | `sha256:26f60fdaf579f50f57356090e5246dbf6800deff440f4e4c6759aaa29e762c0e` |
| N+2 | `sha256:7fd17160548862406979b8c090057a027981155f6c731423c53ed514dca8eee9` |

What the proof job recorded:

- 10 boots in total.
- `bootc upgrade` returned rc 0 and the next boot was N+1.
- `bootc rollback` returned rc 0 and the next boot was N's exact digest.
- N+2 was rejected by greenboot on 4 boots, with `GREENBOOT_MAX_BOOT_ATTEMPTS=3`.
- ~~The node then came back up on N by itself (`boot_success=1`).~~ Refuted: boot 9
  stayed on N+2 in `multi-user.target`, and the agent's reboot brought N back.
- `grub_counting=present`, so the image's own bootupd grub config did the counting and
  no G6 shim was used.
- The `/var` sentinel was intact on every boot.

When it was recorded, `check_bootc_g6_proof.py --evidence` returned MEASURED (exit 0) on
the proof job's `verdict.json` and FAIL (exit 1) on the control's. It now returns exit 1
on both. The evidence artifacts are
`awnix-g6-proof-36415942227-{none,no-greenboot}`, kept for 90 days. Copies of both
verdicts, plus the proof job's `digests.json`, are committed in `evidence/`, and a test
re-judges them.

## What MEASURED means for G6

G6 can move to **MEASURED-in-VM** only when all of these hold:

1. A hosted run of `awnix-g6-proof.yml` with `inject=none` ends `G6-VERDICT: PASS` and
   the job exits 0.
2. `check_bootc_g6_proof.py --evidence` on its `verdict.json` exits 0. That requires
   `kvm=true`, `mode=vm`, three distinct sha256 digests, every step ok, the sentinel
   intact, and `agent_reboots_on_n2: 0` (G6E004).
3. The sibling `inject=no-greenboot` job in the same run ends NO-AUTO-FALLBACK with
   exit 1.

The record has to carry the run id, the three digests, `boots`, `greenboot_attempts`,
`grub_counting` and `fallback_reboot`. If `fallback_reboot=added`, the reboot came from
this harness's copy of `awnix-greenboot-fallback` and not from the image as shipped; the
product Containerfiles must install it (handoff) before the claim covers awnix or garg. If `grub_counting=added`, the automatic fallback relied on this
harness's grub hunk and not on the image as shipped. In that case the claim covers the
mechanism only, and shipping the hunk in the awnix images is a follow-up.

These parts stay open even after a PASS:

- **Metal:** the same harness on a physical box.
- **Product base:** `--base` set to the awnix or garg image, run on a runner that can
  pull it.
- **Offline transport:** the upgrade here goes to a local registry. The removable-media
  `bootc switch --transport oci-archive` path is G5's `awnix offline-update`.

## Zero open ports

The registry publishes on `127.0.0.1:5000` only. qemu forwards no host port into the
guest (no `hostfwd`). The images disable sshd, and the agent is a oneshot that never
listens. `check_bootc_g6_proof.py` G6P005 enforces this.
