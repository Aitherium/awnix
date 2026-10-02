---
id: 07-components
title: Components
applies_to: ["*"]
---
## Why a component command

The image is immutable, so optional software is installed beside it rather than into it. `awnix component` installs each component at the version pinned in this image's lock file, into its own environment under `/var/lib/awnix/components`, and keeps the previous version so you can roll back. Nothing is installed into the system Python, and a plain `pip install` outside a virtual environment is not supported.

## Everyday use

```
awnix component list
awnix component info awbrowse
sudo awnix component install awbrowse
sudo awnix component rollback awbrowse
sudo awnix component remove awbrowse
```

The first-boot setup's Components step and the web console's Components tab call the same command.

## After an update

The lock ships in the image, so an update can change which versions are available. On every boot `awnix-component-sync.service` runs `awnix component sync` to match the lock; it never blocks the boot. A rollback of the image rolls the lock back too.

## Container components

Some components are containers. They run as podman quadlets pinned by digest and listen on 127.0.0.1 only; publish them yourself if you need them on the network.

## Licensed components {variants: garg-appliance, aitheros, aitheros-cloud}

Some components need a license entitlement. `list` shows them as `needs-license`, and `install` exits 3 until the license grants them. Entitlements come from the license status, `aitheros status --json`.

## On the garg appliance {variants: garg-appliance}

The model the product serves is chosen with garg-model(8), not `awnix component`:

```
garg-model list
sudo garg-model set 8B
garg-model current
```

The baked 4B is always available and is the fallback when a larger tier's file is missing.
