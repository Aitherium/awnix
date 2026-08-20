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

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| **awnix** _(you are here)_ | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — a bootable, immutable Linux base for machines where software writes software.

<!-- aitherium-ecosystem:start -->
## Aitherium open-source ecosystem

This repo is one piece of a connected set. All public, MIT/BSL-licensed:

| repo | what it is | pages |
|---|---|---|
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awnode](https://github.com/Aitherium/awnode) | Lightweight local gateway — your apps to backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awnix](https://github.com/Aitherium/awnix) | A bootable, immutable Linux base for agent-run machines | [docs](https://aitherium.github.io/awnix/) |
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Free agent skills, scripts & automations | [docs](https://aitherium.github.io/awskills/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework | [docs](https://aitherium.github.io/AitherZero/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git | [docs](https://aitherium.github.io/awgit/) |
| [awgraph](https://github.com/Aitherium/awgraph) | Code knowledge graph for AI agents | [docs](https://aitherium.github.io/awgraph/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Agent-to-agent messaging over any chat server | [docs](https://aitherium.github.io/awrelay/) |
| [awm](https://github.com/Aitherium/awm) | A small world model (LeWM JEPA + MLP) to bootstrap your own | [docs](https://aitherium.github.io/awm/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension: federated AI search & desktop bridge | — |
| [homebrew-tap](https://github.com/Aitherium/homebrew-tap) | `brew tap aitherium/tap` | — |

Built by [Aitherium](https://aitherium.com).
<!-- aitherium-ecosystem:end -->
