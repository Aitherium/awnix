---
name: awnix-component
section: 8
summary: install, remove and roll back optional components
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-component
verbs_from: awnix-component.py
verbs_mode: list-verbs
files_from: [awnix-component.py, awnix-component-sync.service]
status: live
---
## SYNOPSIS

`awnix component` {`list`|`info` *id*|`install` *id*|`remove` *id*|`rollback` *id*|`sync`|`gc`} [`--json`]

## DESCRIPTION

Optional components are pinned per image in a lock file, so the set of versions you can install rolls back with the image. A component is one of: `baked` (already in the image), `pypi` or `git` (installed into its own virtual environment under `/var/lib/awnix/components`), `container` (a podman quadlet pinned by digest, listening on 127.0.0.1 only) or `pack`.

Each install keeps its previous generation, so `rollback` returns to the version you had before. Nothing is installed into the system Python, and nothing under `/usr` changes.

`awnix-component-sync.service` runs `sync` on every boot. It never blocks the boot, and it prints `awnix-components: <n> baked, <m> installed, lock <sha12>` to the console.

## COMMANDS

`list [--json]`
: every component in the lock with its state: baked, installed, available, unavailable, needs-license or failed.

`info <id> [--json]`
: one component: kind, pin, version, what it provides and why it is or is not available.

`install <id>`
: install the pinned version.

`remove <id>`
: remove it; its data under `/var` is kept.

`rollback <id>`
: return to the previous generation.

`sync`
: make the installed set match the lock after an update.

`gc`
: delete generations no longer reachable by rollback.

`--self-test`
: prove the rules offline.

## EXIT STATUS

`0` ok, `1` failed, `2` could not judge, `3` the license does not entitle this component.

## FILES

`/usr/share/awnix/components.lock.json`
: what this image may install, with pins.

`/var/lib/awnix/components`
: `state.json` (what is installed, with generations) and one virtual environment per component and pin, with a `current` link.

`/etc/containers/systemd`
: `awnix-<id>.container` quadlets for container components.

## SEE ALSO

awnix(8), awnix-update(8)
