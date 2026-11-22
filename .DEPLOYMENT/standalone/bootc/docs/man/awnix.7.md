---
name: awnix
section: 7
summary: overview of the awnix immutable Linux, its variants and its administration pages
applies_to: ["*"]
verbs_mode: none
files_from: [docs/awman.py, Containerfile.awnix]
status: live
---
## SYNOPSIS

`man awnix` · `awnix help` · `/usr/share/doc/awnix/html/index.html`

## DESCRIPTION

awnix is an immutable, image-based Linux built with bootc. The operating system is a container image: `/usr` is read-only, `/etc` and `/var` persist across updates, and every update is a whole new image that can be rolled back with `awnix update rollback`. `/var` is shared by every deployment; `/etc` is merged into each new deployment when it is applied, so a rollback returns `/etc` to how it was at that point (awnix-update(8)).

Every variant is built from one chain, so a fix in the base reaches all of them. The base carries podman, cloud-init, firewalld, greenboot and the aw* tools. The larger variants add a local inference runtime, the full aw* stack, or an appliance product on top.

Administration is done with one command, `awnix`, whose verbs are documented in awnix(8), and with the on-box web console on port 9443, documented in awnix-console(8).

## VARIANTS

awnix
: the immutable base and the aw* tools.

awnix-ai
: the base plus a llama.cpp runtime and the model catalogue; weights are fetched on first use.

awnix-full
: the base plus every aw* package.

awnix-ai-full
: the aw* stack and local inference in one image.

gobbonet-appliance
: GobboNet with a local model baked in, answering with no network on first boot.

## Local inference and the KV cache

The inference variants (`awnix-ai`, `awnix-ai-full`, `gobbonet-appliance`, and the separately licensed appliance) serve models with a llama.cpp runtime. Context memory is bounded by quantising the KV cache: `serve-awnix-bonsai.sh` runs `-ctk q4_0 -ctv q4_0` by default. Measured on mainline llama.cpp b10335 with Bonsai-27B — a model whose KV lives in only 16 of its 64 layers — a q4_0 cache costs 18 KiB/token, so 65,536 tokens is 1.15 GB (+150 MB recurrent state); the f16 default is 4x that, which is why the window was 16k before this. `AWNIX_CTX` (tokens) and `AWNIX_KV` (cache type) are the knobs.

## What is not in the image

`aitherkvcache` is a standalone research brick, not part of any awnix variant. The only KV compression an awnix image actually applies is the llama.cpp-native q4_0 cache above. The brick's published numbers are tensor-level — roughly 3.8-7.1x over FP16 for TurboQuant and 14-26x for TriAttention at F=8-16 — with an explicit quality caveat at its F=12 default (cosine 0.91, but mean top-32 attention overlap 0.41); no end-to-end awnix measurement exists.

The awnix model chooser does use the `Aitherium/aitherkvcache` GitHub *release store* as a mirror: four opt-in fleet weights (`fleet-orchestrator-v18-q8`, `fleet-gemma4-12b-q4`, `fleet-qwen36-27b-q4`, `fleet-deepseek-v4flash-iq2m`) are published as chunked release assets there and fetched from it (see `awnix-models.yaml`). That is a model-file mirror, not the KV library.

## Licensed appliances {variants: garg-appliance, aitheros, aitheros-cloud}

garg-appliance
: GargBot Professional, self-hosted. See garg-appliance(7).

aitheros, aitheros-cloud
: the AitherOS platform appliance, with and without the desktop and GPU layers.

Licensed variants read their license from `/etc/aither/appliance.lic` (appliance.lic(5)) and pull private images with a short-lived credential derived from it. See aitheros(1).

## FILES

`/usr/share/man`
: these pages, rendered from one source by awman at image build.

`/usr/share/doc/awnix/html`
: the same pages and the admin guide as offline HTML.

`/usr/share/doc/awnix/guide.json`
: the admin guide the web console shows under its Guide tab.

## SEE ALSO

awnix(8), awnix-update(8), awnix-update.conf(5), awnix-component(8), awnix-console(8), awnix-endpoints(8), awnix-setup(8), awnix-seed.json(5), bootc(8)

## SEE ALSO FOR LICENSED APPLIANCES {variants: garg-appliance, aitheros, aitheros-cloud}

aitheros(1), appliance.lic(5)

## SEE ALSO ON THE GARG APPLIANCE {variants: garg-appliance}

garg-appliance(7), garg-update(8), garg-model(8), garg-firstboot(8)
