---
id: 05-storage
title: Storage
applies_to: ["*"]
---
## What persists

`/usr`
: read-only; replaced by every update.

`/etc`
: persistent configuration; your changes are merged into each update when it is applied. Each deployment then keeps its own copy, so a rollback returns `/etc` to how it was when the update was applied (chapter 6).

`/var`
: persistent data, shared by every deployment; never touched by an update or a rollback.

Anything you want to keep belongs in `/etc` or `/var`.

## Add a disk

Mount extra storage under `/var`, for example for container images or data:

```
sudo mkfs.xfs /dev/sdb
echo '/dev/sdb /var/lib/containers xfs defaults 0 0' | sudo tee -a /etc/fstab
sudo mount -a
```

Rootless podman keeps its images under each user's home; set `graphroot` in `~/.config/containers/storage.conf` to move them.

## Models

On inference variants the model catalogue downloads weights under `/var` on first use; nothing large is baked into the image.

## On the garg appliance {variants: garg-appliance}

Baked models live in `/opt/bonsai/models` (read-only). Downloaded tiers live in `/var/lib/gargbot/models`. All product data is under `/var/lib/gargbot`; give that path its own disk on a busy appliance.
