---
name: awnix-update.conf
section: 5
summary: update channel and auto-apply settings
applies_to: ["*"]
verbs_mode: none
files_from: [awnix-update.sh, awnix-update.conf]
status: live
---
## SYNOPSIS

`/etc/awnix/update.conf`

## DESCRIPTION

A shell-style `KEY=VALUE` file read by awnix-update(8). There is no expansion and no quoting beyond plain values. `awnix update channel` and `awnix update auto-apply` edit it for you.

## KEYS

`CHANNEL`
: `stable` (the default) or `beta`. There is no other channel.

`AUTO_APPLY`
: `0` (the default) stages updates and waits for `awnix update apply`; `1` reboots into a verified staged update on its own.

`IMAGE_REPO`
: an admin mirror of this box's image (for example a registry inside your network). Unset, the box follows the repository it was installed from. The signature is still verified against the signer in `/usr/share/awnix/signers.conf`.

`COSIGN_IGNORE_TLOG`
: `1` skips the transparency-log lookup. It is honoured only for a `key:` signer (an offline public key), never for a keyless signer; leave it unset unless your mirror has no route to the log.

## EXAMPLE

```
CHANNEL=stable
AUTO_APPLY=0
```

## FILES

`/etc/awnix/update.conf`
: the admin copy, kept across updates.

## SEE ALSO

awnix-update(8)
