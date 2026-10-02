---
name: awnix-seed.json
section: 5
summary: the seed volume that answers install and first-boot questions unattended
applies_to: ["*"]
verbs_mode: none
files_from: [make-awnix-seed.py, awnix-setup.py]
status: live
---
## SYNOPSIS

A filesystem labelled `AWNIX_SEED` with `awnix-seed.json` at its root.

## DESCRIPTION

A seed is a second USB stick, or an extra virtual disk, that answers the installer and first-boot setup so that no one has to be at the keyboard. Create it with `make-awnix-seed`, which is the only supported writer: it refuses plaintext passwords and private keys.

The installer reads the seed for the target disk and the admin user. At first boot `awnix-setup --apply-seed` applies the rest and then deletes the copy it made under `/etc/awnix`.

## FORMAT

```
{
  "schema": 1,
  "admin": {"name": "admin", "ssh_keys": ["ssh-ed25519 AAAA...example admin@laptop"]},
  "hostname": "box-01",
  "disk": "auto",
  "wipe": false,
  "setup": {"components": ["awbrowse"]}
}
```

`admin`
: `name`, `ssh_keys[]` (public keys only), optional `github_user` to fetch keys from, optional `password_hash` (a crypt hash, never a password).

`hostname`
: optional.

`disk`
: `auto` or a device such as `/dev/nvme0n1`.

`wipe`
: must be `true` to install over a disk that already has partitions.

`setup`
: answers keyed by setup step id.

## Licensed appliances {variants: garg-appliance, aitheros, aitheros-cloud}

A licensed appliance seed may also carry `license.lic` (the license file, appliance.lic(5)) and `setup-answers.json` (platform answers, no inline secrets) at its root.

## FILES

`/etc/awnix/seed.json`
: the transient copy first boot applies and removes.

## SEE ALSO

awnix-setup(8)
