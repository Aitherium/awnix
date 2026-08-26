# awnix

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awnix/)**  ·  [Source](https://github.com/Aitherium/awnix)  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. **awnix** is one of its 37 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Boot one throwaway box and try to make a change you cannot reproduce.

<!-- aither-header:end -->

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

- **the eleven `aw` tools**, preinstalled and importable (the table below)

No services. No agent. No account. What runs on top is yours.

The `aw` tools are not an exception to that line — they are tools, not services.
Nothing runs, nothing listens, nothing has an account. `awgit` does not start a
daemon; it refuses a commit when someone else holds the lease. The distinction
that matters is *does it run on its own*, and none of these do.

They ship preinstalled because the alternative was worse: this README advertised
all of them while the image contained none, so `awgit` answered
command-not-found on a machine whose whole pitch is that tooling.

---

## Getting an agent onto it

awnix deliberately ships no agent, and that is the honest answer to "how do I run
one": **you layer it on.** A base image that arrives with an agent already running
has decided what you are doing with your machine.

The layer is small, because the capabilities are already here:

```dockerfile
FROM awnix:latest
RUN pip3 install --no-cache-dir awdk         # the agent runtime
# ...your skills, packs and credentials
```

`awdk` finds the `aw` tools by import — they are already on the image — so the
agent gets leases, the call graph, messaging, scoped memory, snapshots and
verified artifacts without any wiring. That is the point of putting them in the
base rather than in the agent: **swap the agent, keep the guarantees.**

What that buys you concretely, from the four problems at the top of this file:

| you inherit | what stops it | already installed |
|---|---|---|
| it changes things you cannot see | atomic updates, `bootc rollback` | bootc |
| two agents editing one file | a lease refused at commit time | `awgit` |
| it will be wrong sometimes | a restore that fully lands or not at all | `awrecover` |
| you cannot prove what it produced | Ed25519 seals, verified fetch | `awseal`, `awshare` |
| it cannot see past its own training | a real browser and a search it can query | `awbrowse`, `awfind` |
| anyone who gets the link gets in | an invitation addressed to one person, revocable alone | `awnboard` |
| you cannot tell a person from a script | a verdict with evidence, where "cannot tell" is not "yes" | `awnest` |

That last row is why the base is worth having at all. An agent with root on a box
and no way to read a page is a very expensive offline function. `awbrowse` and
`awfind` are **clients**, so the browsing engine and the search providers stay on
a service *you* run: the agent gets to look things up without any of your queries
leaving for a vendor you did not choose.

Run the agent as a service with the unit template in `units/`, or interactively
while you are still deciding what it should do.

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

<!-- aither-ecosystem:start GENERATED from ecosystem.yaml by gen_public_pages.py — DO NOT HAND-EDIT. Change the registry instead. -->

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awpack](https://github.com/Aitherium/awpack) | that the pack you want shipped inside somebody's SDK, under whatever licence that SDK happens to carry | the pack as its own versioned artifact, with its own licence, that any agent runtime can install |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| **awnix** _(you are here)_ | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

**awnix** is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | — |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| **awnix** _(you are here)_ | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | — |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | — |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awnix"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
## The Aitherium ecosystem

Every public repository publishes an on-brand GitHub Pages site. Each is independently adoptable.

| repo | what it is |
|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to |
| **awnix** _(you are here)_ | A Linux you can hand to an agent — immutable base, capabilities included |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay |

<!-- aither-ecosystem:end -->
