# awnix

A bootable, immutable Linux base for running containerised services.

It boots. It runs containers rootlessly. It updates atomically. It ships **no
services of its own** — what you run on top is yours.

Built on [bootc](https://containers.github.io/bootc/) and CentOS Stream 9, so
the operating system is a container image: you build it, you sign it, you roll
it back.

```bash
podman build -t awnix:latest -f Containerfile .
```

Then turn it into something bootable:

```bash
podman run --rm -it --privileged \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v "$PWD/output":/output \
  quay.io/centos-bootc/bootc-image-builder:latest \
  --type ami --local awnix:latest
```

`--type` also takes `iso`, `qcow2`, `vmdk` and `raw`.

## What you get

- **podman, buildah, skopeo, crun** — rootless containers, no daemon
- **cloud-init** — so a key-only image is actually reachable
- **Cockpit** on 9090 — a web console for a machine with no desktop
- **firewalld** with an `awnix` zone and **no ports opened**
- an unprivileged `awnix` account, **locked**, with NOPASSWD sudo

## The credential model, and why it is this one

The `awnix` account ships with **no password at all**. Access is by SSH key:
cloud-init injects your launch keypair on a cloud image, and on bare metal you
place one.

Two details that look optional and are not:

**cloud-init is what makes key-only work.** Remove it and you have an image with
no password *and* no key path — unreachable rather than insecure, which is worse
than the shared credential it replaced. If you fork this and drop cloud-init,
give yourself another way in first.

**NOPASSWD sudo is the companion, not a convenience.** An account with a locked
password cannot sudo at all if sudo demands one. Key-only login without it gives
you a machine you can reach and cannot administer. `ec2-user` and `ubuntu` work
the same way, for the same reason.

Password authentication is disabled in sshd. With no password anywhere it is
dead weight, and image scanners flag it — correctly.

## Running something

awnix ships unit **templates**, not units. `units/service.container.j2` is the
shape of a service — Quadlet form, health contract, restart policy, where state
belongs. Render it with your own values:

```bash
sed -e 's/{{ name }}/myapp/g' \
    -e 's|{{ image }}|ghcr.io/you/myapp:latest|g' \
    -e 's/{{ port }}/8080/g' \
    units/service.container.j2 > /etc/containers/systemd/myapp.container

systemctl daemon-reload && systemctl start myapp
firewall-cmd --zone=awnix --add-port=8080/tcp --permanent
```

The template carries a few opinions worth keeping:

- **state under `/var`** — bootc gives you an immutable `/usr`, so anything
  written to the image root is gone on the next atomic update
- **a real healthcheck** — without one, a wedged container looks exactly like a
  healthy one to every supervisor on the box
- **`Restart=unless-stopped`**, never `no` — nothing revives an exited
  container, and this still honours a manual stop
- **`After=network-online.target`**, not `network.target` — the difference
  between a network that is configured and one that merely exists

## Updating

```bash
sudo bootc upgrade   # stage the new image
sudo reboot          # atomic switch
sudo bootc rollback  # if it went badly
```

## Scope

awnix is the OS layer. It is intentionally small and intentionally opinionated
about the few things that are expensive to get wrong — credentials, state
placement, restart behaviour — and silent about everything else.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
