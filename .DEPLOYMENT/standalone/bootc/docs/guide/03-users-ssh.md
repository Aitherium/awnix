---
id: 03-users-ssh
title: Users and SSH
applies_to: ["*"]
---
## No password ships

awnix images carry no password and no vendor key. The built-in `awnix` account is locked. You get in with the admin user and SSH keys you give the installer, the seed, first-boot setup or cloud-init.

## Add an admin user

At install or first boot, the admin step creates a user in the `wheel` group with your public keys. Later, on the box:

```
sudo useradd -m -G wheel alice
sudo install -d -m 700 -o alice -g alice /home/alice/.ssh
sudo install -m 600 -o alice -g alice alice.pub /home/alice/.ssh/authorized_keys
```

## Cloud and VM instances

cloud-init is enabled. Pass user-data with your user and keys:

```
#cloud-config
users:
  - name: admin
    groups: wheel
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ssh-ed25519 AAAA...example admin@laptop
```

## Harden SSH

Keys are the only credential the image provisions. If you later set a password for console use, keep SSH key-only with a drop-in containing `PasswordAuthentication no` under `/etc/ssh/sshd_config.d/`, then `sudo systemctl reload sshd`.

## Locked out

Use the serial or local console. If no user can sign in, reinstall with a seed that carries your key, or boot the previous deployment from the boot menu.

Booting the previous deployment helps only if the lock-out came with the update. `/var` (and `/home`) is shared by every deployment, but `/etc` is not: each deployment has its own copy, and the previous one still has `/etc` as it was when the update was applied. A user, password, key or `sshd_config.d` drop-in you changed after the update is not there, and whatever locked you out before the update is still there. See "What a rollback keeps" in chapter 6.

## On the garg appliance {variants: garg-appliance}

The same rules apply. Product users (the people who sign in on port 8900) are separate from system users and are managed in the product.
