# awnix

**A Linux you can hand to an agent.**

Not a distro with an AI assistant bolted on. A bootable, immutable base built for
machines where software writes software — and where you still need to know what
happened, undo it, and prove it.

```bash
podman build -t awnix:latest -f Containerfile .
```

That's the whole install. The OS is a container image: you build it, you sign it,
you roll it back.

---

## Why this exists

Give an agent real access to a machine and you inherit four problems on day one:

**It will change things you can't see.** Not maliciously — thoroughly. An
immutable `/usr` and atomic updates mean every change is a new image you can
diff, and a bad one is `bootc rollback`, not an evening.

**It will run alongside other agents.** Two processes editing the same file is
not a merge conflict, it's lost work. That's why the tooling below has leases.

**It will be wrong sometimes.** The question isn't whether, it's whether you find
out cheaply. Snapshots and verified artifacts make "what did it actually do" a
command instead of an investigation.

**It will need credentials, and shouldn't get yours.** awnix ships **no
password at all** — the account is locked, access is by SSH key, and there is
nothing to leak because nothing was ever set.

If you're running agents on someone else's platform, all four are handled by
trusting the platform. awnix is the other bet: you can verify everything, so you
have to trust nothing.

---

## What you actually get

- **podman, buildah, skopeo, crun** — rootless containers, no daemon
- **cloud-init** — so a key-only image is genuinely reachable
- **Cockpit** on 9090 — a web console for a machine with no desktop
- **firewalld** with an `awnix` zone and **zero ports opened**
- an unprivileged `awnix` account, **locked**, with NOPASSWD sudo
- **bootc** — atomic updates, real rollback

No services. No agent. No account. What runs on top is yours.

---

## The credential model, and why it's this one

The `awnix` account has **no password**. Access is by SSH key: cloud-init injects
your launch keypair on a cloud image; on bare metal you place one.

Two details that look optional and are not:

**cloud-init is what makes key-only work.** Remove it and you have an image with
no password *and* no key path — unreachable rather than insecure, which is worse
than the shared credential it replaced. If you fork this and drop cloud-init,
give yourself another way in first.

**NOPASSWD sudo is the companion, not a convenience.** A locked account cannot
sudo at all if sudo demands a password. Key-only login without it gives you a
machine you can reach and cannot administer. `ec2-user` and `ubuntu` work the
same way, for the same reason.

Password authentication is off in sshd. With no password anywhere it's dead
weight, and image scanners flag it — correctly.

---

## Running something

awnix ships unit **templates**, not units. A base image that hands you a fleet
has already decided what you're running.

```bash
sed -e 's/{{ name }}/myapp/g' \
    -e 's|{{ image }}|ghcr.io/you/myapp:latest|g' \
    -e 's/{{ port }}/8080/g' \
    units/service.container.j2 > /etc/containers/systemd/myapp.container

systemctl daemon-reload && systemctl start myapp
firewall-cmd --zone=awnix --add-port=8080/tcp --permanent
```

The template carries four opinions, each one something that's expensive to learn
the hard way:

| | |
|---|---|
| **state under `/var`** | an immutable `/usr` loses anything else on update |
| **a real healthcheck** | without one, a wedged container looks exactly like a healthy one to every supervisor on the box |
| **`Restart=unless-stopped`** | nothing revives an exited container, and this still honours a manual stop |
| **`After=network-online.target`** | the difference between a network that's *configured* and one that merely *exists* |

---

## Make it bootable

```bash
podman run --rm -it --privileged \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v "$PWD/output":/output \
  quay.io/centos-bootc/bootc-image-builder:latest \
  --type ami --local awnix:latest
```

`--type` also takes `iso`, `qcow2`, `vmdk`, `raw`.

## Update it

```bash
sudo bootc upgrade   # stage
sudo reboot          # atomic switch
sudo bootc rollback  # if it went badly
```

---

## The aw family

awnix is the ground floor. The rest are standalone tools — each installs on its
own, each works offline, none needs an account:

| | |
|---|---|
| [**awgit**](https://github.com/Aitherium/awgit) | git with **leases**, so two agents in one worktree don't silently overwrite each other |
| [**awgraph**](https://github.com/Aitherium/awgraph) | a semantic code graph — AST + tree-sitter, call graphs, so an agent reads structure instead of grepping |
| [**awseal**](https://github.com/Aitherium/awseal) | sign a directory so a stranger can verify it. The key that verifies is not the key that forges |
| [**awshare**](https://github.com/Aitherium/awshare) | publish an artifact and fetch it back **verified** — content-addressed bundles |
| [**awrelay**](https://github.com/Aitherium/awrelay) | agent messaging: findings, alerts, coordination, without a SaaS in the middle |
| [**awm**](https://github.com/Aitherium/awm) | scoped agent memory — `tenant:user:project`, so a write can't leak across a boundary |
| [**awrecover**](https://github.com/Aitherium/awrecover) | labelled snapshots with a restore that **fully lands or doesn't land** |

The pattern, if you want one sentence: *every one replaces a thing you'd
otherwise have to trust with a thing you can check.*

---

## Scope

awnix is the OS layer. Deliberately small, deliberately opinionated about the
few things that are expensive to get wrong — credentials, state placement,
restart behaviour — and silent about everything else.

Apache-2.0. See [LICENSE](LICENSE).
