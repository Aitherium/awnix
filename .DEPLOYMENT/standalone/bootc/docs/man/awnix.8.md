---
name: awnix
section: 8
summary: the awnix administration command
applies_to: ["*"]
cli: /usr/bin/awnix
verbs_from: awnix-dispatch.sh
verbs_mode: dispatch
verb_sources:
  update: awnix-update.sh
  component: awnix-component.py
  license: awnix-license.sh
  setup: awnix-setup.py
  console: awnix-console.py
  endpoints: awnix-endpoints.py
  doctor: awnix-doctor.sh
  role: awnix
  renewal: awnix-renewal.py
  offline-update: awnix-offline-update.py
  mesh: awnix-mesh.py
  backup: awnix-backup.py
  restore: awnix-restore.sh
  reset: awnix-reset.sh
  proof: proof/awnix-proof.py
  secureboot: awnix-secureboot.py
  awsh: awsh/awnix-awsh.py
  ops-doctor: ops/awnix-ops-doctor.py
  awdk: awdk/awnix-awdk.py
  wheelhouse: awnix-wheelhouse.py
  zero-ports: awnix-zero-ports.sh
files_from: [awnix-dispatch.sh]
status: live
---
## SYNOPSIS

`awnix` *verb* [*arguments*]

`awnix help` · `awnix --list-verbs`

## DESCRIPTION

`awnix` is a thin dispatcher. `awnix <verb> ...` runs `/usr/libexec/awnix/awnix-<verb>`, falling back to `/usr/bin/awnix-<verb>`, with the remaining arguments. `awnix help` and `awnix --list-verbs` list only the verbs that are present on this image, so a verb that a variant does not ship is never offered.

Every verb follows the same conventions: `--json` on status-like commands, `--self-test` that never touches the network, and the exit codes below.

## COMMANDS

`update`
: signed image updates, channels and rollback. See awnix-update(8).

`component`
: install, remove and roll back optional components. See awnix-component(8).

`setup`
: re-run first-boot setup, or show what it recorded. See awnix-setup(8).

`console`
: the on-box web console on port 9443; `awnix console code` prints its sign-in code. See awnix-console(8).

`endpoints`
: show and probe every vendor endpoint this box dials. See awnix-endpoints(8).

`doctor`
: one report across every verb that is present; `--bundle` writes a support bundle.

`role`
: list, add, remove and configure the optional OS roles this image offers.

`offline-update`
: verify a signed update bundle from removable media, then stage it; no network needed. See awnix-offline-update(8).

`mesh`
: join this box to your workspace mesh as a client-only node, show it, and leave it. See awnix-mesh(8).

`backup`
: create, list, verify and drop verified backups. See awnix-backup(8).

`restore`
: verify, then restore a backup; `--undo` reverts one. See awnix-backup(8).

`reset`
: erase customer data, or return the box to setup. Never offered by the web console. See awnix-backup(8).

`secureboot`
: Secure Boot and NVIDIA driver state, and enrollment of the module-signing key. See awnix-secureboot(8).

`awsh`
: check and set up the awsh shell for offline use. See awnix-awsh(8).

`ops-doctor`
: the operations floor: the clock, log retention, time sync with no egress, boot health.

`proof`
: the air-gap proof steps, each an exit-code check, and a sealed export an offline verifier checks.

`zero-ports`
: present on the air-gap profile image: apply and prove zero non-loopback listeners.

`awdk`
: present on images that ship the local agent daemon: its status, health and egress probe.

`wheelhouse`
: present on images that ship vendored Python wheels: verify, list and resolve them offline.

`help`
: list the verbs present on this image.

### License {variants: garg-appliance, aitheros, aitheros-cloud}

`license`
: import, refresh and inspect the appliance license; a shim over `aitheros license`. See aitheros(1).

`renewal`
: the license lifecycle: phase, days to expiry, grace, and the gates updates ask. See awnix-renewal(8).

## EXIT STATUS

`0`
: ok.

`1`
: the operation failed, or a check found a violation.

`2`
: could not judge (offline, missing input).

`3`
: `awnix component` only: the license does not entitle this component.

## FILES

`/usr/bin/awnix`
: the dispatcher.

`/usr/libexec/awnix`
: one executable per verb, `awnix-<verb>`.

## SEE ALSO

awnix(7), awnix-update(8), awnix-component(8), awnix-console(8), awnix-endpoints(8), awnix-setup(8), awnix-offline-update(8), awnix-mesh(8), awnix-backup(8), awnix-secureboot(8), awnix-awsh(8)
