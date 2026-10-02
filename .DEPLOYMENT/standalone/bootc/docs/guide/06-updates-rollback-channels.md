---
id: 06-updates-rollback-channels
title: Updates, rollback and channels
applies_to: ["*"]
---
## How updates work

An update is a whole new signed image. `awnix update check` resolves your channel to an image digest, verifies its signature, and stages it with bootc. The running system is not touched; the new image is used on the next boot, and the previous one is kept.

```
awnix update status
sudo awnix update check
sudo awnix update apply
```

`apply` schedules the reboot and returns first. Nothing reboots on its own unless you turn auto-apply on (`sudo awnix update auto-apply on`). The web console's Updates tab runs the same commands.

## Signatures

Every image is signed in CI with a keyless signature. `check` verifies it against the signing identities in `/usr/share/awnix/signers.conf` and refuses an image that is unsigned or signed by anyone else; the verdict is `unsigned-refused` and nothing is staged.

## Channels

stable
: the default. A build reaches stable only after it has passed the upgrade and rollback proof, and stable moves by digest, never by rebuilding a tag.

beta
: every build, as soon as it is published.

Switch with `sudo awnix update channel beta`. The setting lives in `/etc/awnix/update.conf` (awnix-update.conf(5)).

## Rollback

If a new image fails its health checks on three boots in a row, greenboot returns to the previous image by itself. To go back by hand:

```
sudo awnix update rollback
```

or pick the previous entry in the boot menu.

## What a rollback keeps

`/var` and `/home` are shared by every deployment: a rollback never touches them.

`/etc` is different. When an update is applied, your `/etc` is merged into the new deployment, and from then on each deployment keeps its own copy. Rolling back, by command, by greenboot or from the boot menu, boots the previous deployment with its `/etc` as it was when the update was applied. Every change to `/etc` made since then is not there, including:

- a license imported since the update (`/etc/aither/appliance.lic`) and the registry credential (`/etc/ostree/auth.json`);
- the setup answers and link token in `/etc/awnix`, so a box set up after the update comes back in setup mode;
- users, passwords, SSH keys and `sshd_config.d` drop-ins.

Before you roll back, copy anything you changed in `/etc` since the update (a `umask 077` archive as in chapter 8), then restore it after the reboot. `awnix doctor` and `awnix update status` show what the rolled-back system sees.

## License and registry access {variants: garg-appliance, aitheros, aitheros-cloud}

Licensed appliances pull private images. `check` first refreshes the registry credential from the license (`aitheros license refresh`). The credential is renewed every 45 minutes and lives 60 minutes, so a box that is offline for more than an hour loses registry access until it is back online. That is by design: an update is a network operation, and the product keeps running either way. An expired or revoked license stops updates, not the service.

## On the garg appliance {variants: garg-appliance}

`garg-update check|apply|status|doctor` is a shim over `awnix update`; existing scripts keep working (garg-update(8)). `garg-update.timer` runs `check` periodically. `garg-update doctor` also checks that the model mirror and feedback intake are reachable.

An appliance installed without a license still reads the legacy update token from `/var/lib/gargbot/update-token` or `token.txt` on a `GARGBOT_SEED` volume. Installing a license replaces it.
