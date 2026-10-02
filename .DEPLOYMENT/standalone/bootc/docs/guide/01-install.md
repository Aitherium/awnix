---
id: 01-install
title: Install
applies_to: ["*"]
---
## Choose a variant

Every variant is a bootable ISO built from the same chain.

awnix
: the immutable base and the aw* tools. 4 GB RAM, 20 GB disk.

awnix-ai
: the base plus local inference; the model is sized to your hardware on first use. 8 GB RAM recommended.

awnix-full
: every aw* package, no local model.

awnix-ai-full
: the aw* stack and local inference in one image. 8 GB RAM recommended.

gobbonet-appliance
: GobboNet with a local model baked in; answers with no network.

## Download and verify

Download the ISO and the `SHA256SUMS` file from the matching release, then compare:

```
sha256sum awnix-ai-full-x86_64.iso
grep awnix-ai-full-x86_64.iso SHA256SUMS
```

Do not boot an image whose checksum does not match.

## Write a USB stick

On Linux, `write-awnix-usb` writes the ISO and verifies what it wrote. It refuses to write to the disk you booted from.

```
sudo write-awnix-usb --help
```

On Windows or macOS use Fedora Media Writer, balenaEtcher or Rufus (DD mode). `dd if=<iso> of=/dev/sdX bs=4M conv=fsync` also works; double-check the device name first.

## Interactive or seeded install

Booting the ISO starts the installer. With no seed, it asks for the target disk and then installs; first-boot setup asks the rest (chapter 2).

For an unattended install, make a seed stick with `make-awnix-seed` and plug it in next to the installer stick. The seed names the disk, the admin user and SSH keys, and any setup answers; see awnix-seed.json(5). An install over a disk that already has partitions needs `"wipe": true` in the seed, so a seed can never erase a disk by accident. The installer prints `awnix-seed: admin=<user> disk=<device>` to the console when it used a seed.

`make-awnix-seed --help` lists its options.

## Virtual machines

The ISO boots in QEMU/virt-manager, Hyper-V (Generation 2), Proxmox and VMware. Give it at least 4 GB of RAM and 20 GB of disk. The serial console is enabled at 115200 baud, which is useful for headless VMs:

```
qemu-system-x86_64 -m 8G -smp 4 -enable-kvm -cdrom awnix-ai-full-x86_64.iso -drive file=disk.qcow2,if=virtio -serial mon:stdio
```

## On the garg appliance {variants: garg-appliance}

The garg appliance ISO is delivered through your license, not a public download. Install it exactly as above. Put `license.lic` from your purchase on the seed stick (make-awnix-seed copies it there) and the appliance activates itself on first boot. The baked 4B model needs 4 GB of RAM; the 27B tiers need 12 GB (garg-model(8)).

## On the platform appliance {variants: aitheros, aitheros-cloud}

The platform appliance ISO is delivered through your license. Install it as above; its seed may also carry `setup-answers.json` so the platform finishes setup with no questions.
