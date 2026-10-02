---
id: 02-first-boot-activation
title: First boot and activation
applies_to: ["*"]
---
## What happens on first boot

The box boots to multi-user without waiting for anyone. Setup is offered in two places at once:

- on the local console (tty1), as a short text wizard
- in a browser at `https://<box>:9443`, the web console in setup mode

Both show the same steps: hostname, admin user and SSH keys, optional components, and whether to link the box to an account. Every step can be skipped. A seed volume answers them all in advance (awnix-seed.json(5)).

## Open the web console

The console banner on the local and serial console shows the address, a one-time setup code and the certificate fingerprint:

```
awnix-console: https://192.0.2.10:9443 fp=3f2a9c01b7de
```

The certificate is self-signed, so the browser warns. Check that the fingerprint in the browser's certificate details starts with the `fp=` value before you continue. Then enter the setup code.

After setup finishes the console switches to console mode. Its sign-in code is no longer shown on the banner; read it on the box:

```
sudo awnix console code
```

## Check what setup recorded

```
awnix setup --status
```

Setup records its answers in `/etc/awnix/setup.json`. It never stores a password.

## Activate the license {variants: garg-appliance, aitheros, aitheros-cloud}

A licensed appliance needs its license before it can pull private images or updates. Use any one of these; they all end in the same place:

- put `license.lic` on the seed volume before first boot
- paste the license in the License step of the web console
- on the box: `sudo aitheros login --license @/path/to/license.lic`

Check the result with `aitheros status`. `valid` with the registry `armed` means the box can update. See appliance.lic(5) for the states.

## On the garg appliance {variants: garg-appliance}

The product itself does not wait for setup: garg-firstboot(8) starts the model server (port 8089), qdrant (6333) and the backend (8900) on every boot, and creates the secret key on the first. Open `http://<box>:8900` for the product and `https://<box>:9443` for administration.
