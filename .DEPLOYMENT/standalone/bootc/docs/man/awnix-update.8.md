---
name: awnix-update
section: 8
summary: signed image updates, channels and rollback
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-update
verbs_from: awnix-update.sh
verbs_mode: list-verbs
files_from: [awnix-update.sh, awnix-update.timer]
status: live
---
## SYNOPSIS

`awnix update` {`status`|`check`|`apply`|`rollback`|`channel` *stable|beta*|`auto-apply` *on|off*|`doctor`} [`--json`]

## DESCRIPTION

An awnix box updates by switching to a newer signed image. `check` resolves the configured channel to an image digest, verifies the signature with `cosign verify` against the signers in `/usr/share/awnix/signers.conf` and the GitHub Actions OIDC issuer, and then stages it with `bootc switch <image>@<digest>`. Staging never changes the running system; the new image is used on the next boot.

`apply` and `rollback` schedule the reboot in the background and return first, so a web console or SSH session sees the answer before the box goes down. An unsigned or wrongly signed image is refused and recorded as `unsigned-refused`.

Channels are `stable` and `beta`. Every build is published to `beta`; `stable` moves only when a build has passed the upgrade and rollback proof, and it moves by digest.

## COMMANDS

`status [--json]`
: print the last verdict from the status file.

`check [--json]`
: look for a newer image on the channel, verify it, and stage it. Never reboots unless auto-apply is on.

`apply`
: reboot into a staged update.

`rollback`
: reboot into the previous deployment. `/var` is kept; `/etc` comes back as it was when the current update was applied, so changes made to `/etc` since then (a license import, setup answers, users and SSH settings) are not in the rolled-back system. Copy them first.

`channel <stable|beta>`
: switch channel; the next `check` follows it.

`auto-apply <on|off>`
: reboot into a verified staged update without asking.

`doctor [--json]`
: can this box reach the registry, verify signatures and read its credential?

`--self-test`
: prove the rules with a stubbed bootc and cosign; never touches the network.

## STATES

`current`, `staged`, `unsigned-refused`, `auth-refused`, `license-refused`, `offline`, `error`, `no-credential`, `never-checked`.

## Licensed appliances {variants: garg-appliance, aitheros, aitheros-cloud}

On a licensed appliance `check` first runs `aitheros license refresh --quiet`, which exchanges the license for a short-lived registry credential. A refused or revoked license stops private pulls and is reported as `license-refused`; the running system keeps working.

## FILES

`/etc/awnix/update.conf`
: `CHANNEL`, `AUTO_APPLY`, and the optional `IMAGE_REPO` mirror override. See awnix-update.conf(5).

`/var/lib/awnix/update-status.json`
: the last verdict: state, channel, booted, available and staged digests, signer identity.

`/usr/share/awnix/signers.conf`
: the signing identities an image must carry, one line per image repository: a keyless identity regular expression, or `key:` and the path of an offline public key.

`/etc/ostree/auth.json`
: the registry credential bootc reads. Written only by the license refresh on licensed appliances.

## SEE ALSO

awnix(8), awnix-update.conf(5), bootc(8), cosign(1)
