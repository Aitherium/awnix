---
id: 11-secure-boot-nvidia
title: Secure Boot and NVIDIA
applies_to: ["*"]
---
## Where you stand

Most PCs ship with UEFI **Secure Boot** on. With it on, the Linux kernel loads a driver only if a key the machine trusts has signed it. The NVIDIA driver is built outside the kernel, so the distribution's key does not sign it; awnix GPU images sign it with the **awnix module key**, which your machine trusts after you enroll it once.

```
awnix secureboot status --brief
sudo awnix secureboot doctor
```

## What the states mean

`ok`
: Secure Boot is on, the key is enrolled and the NVIDIA driver is loaded. Nothing to do.

`not-applicable`
: there is no NVIDIA GPU, or this image has no NVIDIA kernel driver. Nothing to do.

`sb-off`
: Secure Boot is off, so the driver loads without a check. See *Turning Secure Boot on later*.

`needs-enroll`
: Secure Boot is on and the awnix key is not trusted yet. Enroll it (below).

`enroll-pending`
: the key is queued; MokManager asks for it at the next boot. Reboot and finish there.

`unsigned-kmod`
: Secure Boot is on, but this image's driver is unsigned. Update to a signed image, or turn Secure Boot off.

`kmod-rejected`
: the kernel refused the driver's signature. Check the key (`awnix secureboot cert`), then enroll again.

`driver-not-loaded`
: the driver is present and trusted but did not load. Run `sudo modprobe nvidia`, then read `journalctl -k -b`.

`unknown`
: Secure Boot or the key state could not be read. Run the doctor as root and check the firmware settings.

## Enrolling the awnix key

1. Run `sudo awnix secureboot enroll --reveal` (the console setup step "Secure Boot and the NVIDIA driver" does the same). It shows an 8-character **one-time password** once. Write it down; it is stored nowhere.
2. Reboot. A blue **MokManager** screen appears before Linux starts; you have about 10 seconds to press a key. If you miss it, the box boots normally and the request stays queued.
3. Choose **Enroll MOK**, then **Continue**, then **Yes**.
4. Type the one-time password. MokManager uses a US keyboard layout, which is why the password leaves out letters that move on other layouts.
5. Choose **Reboot**. Afterwards `awnix secureboot status --brief` shows `ok`.

Lost the password before rebooting? Run `sudo mokutil --revoke-import`, then enroll again. To confirm you are enrolling the right key, compare the SHA-1 fingerprint MokManager shows with the one from `awnix secureboot cert`.

## Turning Secure Boot off instead

Turning Secure Boot off in the firmware also lets the driver load. It is simpler, but it removes a real protection against boot-time tampering. For anything you would describe as hardened or air-gapped, enroll the key and aim for `ok` (or `not-applicable`). `awnix secureboot status` reports the state; nothing enforces it.

## Turning Secure Boot on later

Enroll first, then turn Secure Boot on in the firmware. In the other order the GPU driver does not load until you enroll.

### The garg appliance {variants: garg-appliance}

The garg appliance runs its models with llama.cpp over Vulkan or on the CPU and ships no NVIDIA kernel module, so Secure Boot never blocks it: its status is `not-applicable`.

## Images without the NVIDIA kernel driver

Images that run models with llama.cpp over Vulkan or on the CPU ship no NVIDIA kernel module, so Secure Boot never blocks them. Their status is `not-applicable`, and inference falls back to the CPU when no Vulkan device is present.

## For operators: how the signing works

- The build installs NVIDIA's open kernel modules and signs them against the image's own kernel with a private key mounted only for the signing step. The key is never copied into a layer; the build searches the image for a copy and fails if it finds one.
- An image built without the signing key ships unsigned modules. They load only with Secure Boot off, and the status reports `unsigned-kmod`.
- `awnix-secureboot.service` records the state on every boot and prints it to the serial console. It never blocks boot.
