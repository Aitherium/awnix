---
name: awnix-setup
section: 8
summary: first-boot setup on the console, from a seed, or re-run later
applies_to: ["*"]
cli: /usr/bin/awnix-setup
verbs_from: awnix-setup.py
verbs_mode: list-verbs
files_from: [awnix-setup.py, awnix_setup/core.py, awnix-setup.service]
status: live
---
## SYNOPSIS

`awnix-setup` [`--tty`|`--status`|`--apply-seed`|`--validate-steps`|`--reset`|`--self-test`] · `awnix setup ...`

## DESCRIPTION

On first boot, awnix asks a few questions: hostname, admin user and SSH keys, which optional components to add, and whether to link the box to an account. Every question can be skipped, and none of them holds up the boot: the box reaches multi-user without anyone at the keyboard.

The same steps are offered in two places: on the local console (tty1), and in the web console at `https://<box>:9443` in setup mode (awnix-console(8)). Answers can also come from a seed volume (awnix-seed.json(5)), which is how an unattended install finishes with no questions at all.

The steps come from `/usr/share/awnix-setup/steps.d`, one file per step, filtered by the variant of the image. When every step is done or skipped, setup writes `/etc/awnix/setup.json` and the web console switches to console mode.

## COMMANDS

`--tty`
: run the questions on the local console. This is the default with no option, and what `awnix-setup.service` starts on tty1.

`--status`
: print what setup recorded: hostname, admin user, key fingerprints, components, link state, and each step's status.

`--apply-seed`
: apply a seed volume labelled `AWNIX_SEED`; run at boot.

`--validate-steps`
: `--validate-steps DIR` checks every step file in a directory and exits 1 on any bad one.

`--reset`
: `--reset --yes` removes `/etc/awnix/setup.json`, so setup runs again and the web console returns to setup mode.

`--self-test`
: prove the step rules offline; installs nothing.

## Licensed appliances {variants: garg-appliance, aitheros, aitheros-cloud}

A `license.lic` on the seed volume is copied to `/var/lib/aither/license/incoming.lic`, which activates the license (appliance.lic(5)). The license step in setup does the same with a pasted license.

## FILES

`/etc/awnix/setup.json`
: the one setup-complete marker (schema 2). Removing it puts the web console back into setup mode.

`/etc/awnix/link.token`
: the account-link credential, mode 0600, when the box is linked.

`/usr/share/awnix-setup/steps.d`
: the setup steps for this image.

## SEE ALSO

awnix(8), awnix-console(8), awnix-seed.json(5)
