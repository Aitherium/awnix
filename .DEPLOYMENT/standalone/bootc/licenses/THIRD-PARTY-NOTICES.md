<!-- GENERATED FILE — do not edit.
     Source:    AitherOS/config/upstreams.yaml
     Generator: AitherOS/dev/tools/gen_third_party_notices.py
     Gate:      AitherOS/dev/tools/check_upstream_attribution.py (UPA003)
     A hand-edit here renders perfectly and is reverted by the next run. -->

# Third-party notices

AitherOS is built on other people's work. This file lists it.

It is generated from a single registry, so it cannot drift from what the build
actually pins, and it is ordered by what we owe rather than by name.

**If your project is here and the attribution is wrong, incomplete, or you would
rather it read differently — open an issue and we will fix it.**

## Redistributed

We ship these to other people -- inside an image, a package, an appliance or an ISO. The licence text travels with the artifact; where it does not, that is a defect, not a formality.

### AOS-1 Agent Orchestration Standard

- **Upstream**: https://github.com/tomtomsatnav/agent-orchestration-standard
- **Licence**: MIT
- **Version pinned**: cc4ed67
- **Pinned at**: `AitherOS/lib/aos1/__init__.py`
- **Modified**: no — redistributed unchanged.

The AOS-1 spec, JSON Schemas and conformance fixtures, vendored byte-identical under AitherOS/lib/aos1/spec; lib/aos1/validator.py is our Python port of its validator. Genesis serves it (the BeadSpace orchestration overlay).

### CPython

- **Upstream**: https://github.com/python/cpython
- **Licence**: PSF-2.0
- **Version pinned**: 3.11 / 3.12 (per image)
- **Pinned at**: `.DEPLOYMENT/images/arc-playground/Dockerfile`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The interpreter every service in this platform runs on.

### Diagram Design

- **Upstream**: https://github.com/cathrynlavery/diagram-design
- **Licence**: MIT
- **Version pinned**: 2.6.33
- **Pinned at**: `AitherOS/lib/media/diagram_design/VENDORED.sha256.json`
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/diagram-design
- **Intake verdict**: ADOPT

An editorial diagram system for coding agents: 41 layout grammars (architecture, sequence, sankey, wardley, gantt...), a semantic colour role table, a taste checklist, stdlib Mermaid/draw.io/Excalidraw extractors and an output self-check. Vendored byte-identical (pinned by sha256) under lib/media/diagram_design/vendor and driven by lib/media/editorial_diagram.py, which reskins to AitherDesign and gates model output with the upstream self-check.

> The deletion rule, one accent on 1-2 focal nodes, and a 4px grid as a non-negotiable are what make a machine-drawn diagram stop looking machine-drawn. Those, and the discipline of verifying geometry instead of trusting it, are Cathryn Lavery's.

### GobboNet

- **Upstream**: https://github.com/ElodineOfficial/GobboNet
- **Licence**: MIT
- **Version pinned**: v1.5.3
- **Pinned at**: `.PRODUCTS/.GOBBONET/upstream`
- **Modified**: yes — we carry local changes to this project, and most licences ask that this be stated.

The upstream of the GobboNet fork we vendor, modify and serve.

### i-have-adhd

- **Upstream**: https://github.com/ayghri/i-have-adhd
- **Licence**: MIT
- **Pinned at**: `awskills/skills/i-have-adhd.md`
- **Modified**: yes — we carry local changes to this project, and most licences ask that this be stated.
- **Our fork**: https://github.com/wizzense/i-have-adhd
- **Intake verdict**: ADAPT

A coding-agent skill that shapes output for a reader with ADHD (next action first, numbered steps, lists capped at five, one closing next step). Its ten rules and pre-send check are adapted, with the MIT notice carried inline, into the public awskills skill i-have-adhd (vendored into the awdk Claude plugin), the plugin's aither output style, and awknowledge law 30, which extends them from chat replies to inbox cards, terminal home screens and status lines.

> "Lead with the next action" and "cap the visible list, never the data" are Ayoub Ghriss's; they turned a vague wish for shorter output into rules a screen can be tested against.

### llama.cpp (PrismML fork)

- **Upstream**: https://github.com/PrismML-Eng/llama.cpp
- **Licence**: MIT
- **Version pinned**: prism-b12128-d7b2896
- **Pinned at**: `.DEPLOYMENT/containers/llamacpp-prism/Containerfile`
- **Modified**: no — redistributed unchanged.
- **Licence text ships at**: `/usr/share/licenses/llama-cpp-prism/LICENSE`

PrismML's llama.cpp branch `prism`: the ONLY runtime that serves Bonsai 2 (PTQ1_0 / PQ2_0 ternary tensors + the Walsh-Hadamard activation rotation in ggml-cuda/fwht.cu; PRs #148 and #150). Stock llama.cpp loads those files and emits gibberish. The 5090 lane `aither-llamacpp-bonsai` will run llama-server built from this fork at a pinned commit once the image is pushed (the cut-over is staged in compose, not applied).

> PrismML's fork is what lets a 27B model answer from a single consumer GPU at under 2 bits per weight. We build it unmodified from a pinned commit.

### Node.js

- **Upstream**: https://github.com/nodejs/node
- **Licence**: MIT
- **Version pinned**: 20 / 22 (per image)
- **Pinned at**: `.PRODUCTS/.CHELLE/backend/Dockerfile`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The runtime behind every frontend build and the awkit surfaces.

### Obscura

- **Upstream**: https://github.com/h4ckf0r0day/obscura
- **Licence**: Apache-2.0
- **Version pinned**: 0.2.1
- **Pinned at**: `awnix/Containerfile`
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/obscura
- **Intake verdict**: ADAPT
- **Licence text ships at**: `/usr/share/licenses/obscura/LICENSE`

A CPU-only browser engine in Rust speaking CDP. It is the default engine behind AitherBrowser, which is why the fleet can render pages without waking a GPU — and it reads several sites (npm, stackoverflow, sites behind a Cloudflare challenge) that headless Chromium times out on.

> Obscura is why AitherBrowser can render the web on a machine with no GPU to spare. We consume the release binary unmodified and have changed nothing about it.

### PyTorch

- **Upstream**: https://github.com/pytorch/pytorch
- **Licence**: BSD-3-Clause
- **Pinned at**: `AitherOS/docker/Dockerfile.training`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The training and inference substrate for everything we fine-tune.

### Repowise

- **Upstream**: https://github.com/repowise-dev/repowise
- **Licence**: AGPL-3.0-only
- **Version pinned**: >=0.31.0
- **Pinned at**: `AitherOS/requirements.txt`
- **Our fork**: https://github.com/wizzense/repowise
- **Intake verdict**: ADOPT

The codebase intelligence engine behind the `repowise_*` tools and awgraph -- dependency graph, git history, auto-docs and architectural decisions over a repo this size, which is what makes code navigation here a query rather than a grep sweep.

> Repowise is why "who calls this" is a query here instead of a grep sweep across a tree this size. Thanks to Raghav Chamadiya and contributors.

### vLLM

- **Upstream**: https://github.com/vllm-project/vllm
- **Licence**: Apache-2.0
- **Version pinned**: 0.7.3
- **Pinned at**: `AitherOS/Dockerfile.Services`
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/vllm

The inference server behind most of the GPU fleet.

> vLLM is why this fleet can serve real models on the hardware it has.

### vLLM TurboQuant

- **Upstream**: https://github.com/mitkox/vllm-turboquant
- **Licence**: Apache-2.0
- **Pinned at**: `docker/Dockerfile.vllm-tq`
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/vllm-turboquant

A quantization layer over vLLM for distributed inference.

> TurboQuant is what makes the quantized serving path on this fleet practical.

## Integrated

We pin and run these but ship nobody else a copy. Credit here is courtesy rather than obligation, which is exactly why it needs writing down.

### AssetRipper

- **Upstream**: https://github.com/AssetRipper/AssetRipper
- **Licence**: GPL-3.0
- **Version pinned**: 2.0.0
- **Pinned at**: `AitherOS/packages/darklink/darklink/assetripper.py`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Unity asset extractor. Darkmatter Link's `games import` downloads the pinned official release (2.0.0 AssetRipper_win_x64.zip, sha256 from GitHub's release digest) into %LOCALAPPDATA%/AitherOS/tools/assetripper on the owner's machine and runs it as a SEPARATE process (headless, loopback REST) over a game's *_Data dir; we never link, import, vendor or redistribute its code (GPL-3.0 stays on its side of the process boundary).

> AssetRipper is why a Unity game's meshes, textures and audio can be studied at all without the original project.

### cloudflared

- **Upstream**: https://github.com/cloudflare/cloudflared
- **Licence**: Apache-2.0
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The tunnel connector every public hostname rides.

### Docling

- **Upstream**: https://github.com/docling-project/docling
- **Licence**: MIT
- **Pinned at**: `awdk/pyproject.toml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

IBM's document converter (PDF, DOCX, PPTX, XLSX, HTML -> Markdown/JSON). Declared as the awdk `docs` extra so `adk ingest` converts document files instead of skipping them; the model-free native PDF pipeline is used, so the extra pulls no torch.

> Docling is the reason a PDF dropped into `adk ingest` is text and not a silent skip. Thanks to the Docling contributors.

### FreeLLMAPI

- **Upstream**: https://github.com/tashfeenahmed/freellmapi
- **Licence**: MIT
- **Version pinned**: v0.16.2
- **Pinned at**: `awdk/adk/freellmapi.py`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Self-hosted proxy that puts many providers' free tiers behind one OpenAI-compatible /v1 endpoint, with routing, failover and per-key quota tracking. awdk's optional NON-LOCAL route, labelled "Free cloud (your own free-tier keys)": bring-your-own-keys only, off until the user enables it, never pooled with platform keys and never a fleet backend.

> Thanks to Tashfeen Ahmed for FreeLLMAPI, which gives awdk users a free non-local route on their own provider accounts.

### Git for Global Law (corpus + MCP server)

- **Upstream**: https://github.com/wizzense/global-law
- **Licence**: CC0-1.0
- **Version pinned**: 47a4f1a36ff92d20cd4f573143b89cf54ad58f44
- **Pinned at**: `.RESEARCH/INTAKE/global-law/DOSSIER.md`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The statutes of ~200 jurisdictions as one dated git history, with a stdlib read-only MCP server. Cloned --recurse-submodules to E:/aither-data/global-law, mounted read-only into aither-mcpgateway; lib/agents/packs/global_law runs the corpus's own tools/mcp_server.py over stdio as the law_* tools.

### Grounding DINO

- **Upstream**: https://github.com/IDEA-Research/GroundingDINO
- **Licence**: Apache-2.0
- **Version pinned**: IDEA-Research/grounding-dino-base@12bdfa3120f3e7ec7b434d90674b3396eccf88eb
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Open-vocabulary text -> boxes; feeds SAM 2.1 in AssetVision /segment.

### Harness² (google-research/harness2)

- **Upstream**: https://github.com/google-research/harness2
- **Licence**: Apache-2.0
- **Version pinned**: 1cfe254e8d25b15f37048c3730134686abe60335
- **Pinned at**: `AitherOS/dev/tools/harness2/run_on_awnix.sh`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADAPT

Recursive harness refinement (propose, probe and compose edits to an agent's system prompt, guardrails, memory and skills with weights fixed). We run it from a pinned clone in a throwaway, egress-fenced awnix container, its model channel replaced by MicroScheduler and its grader by AitherBench oracles; never Vertex, never redistributed.

> Harness² turned "tune the harness, not the weights" into a loop with evidence behind every edit; it is what lets our own models improve their scaffolding.

### headroom

- **Upstream**: https://github.com/headroomlabs-ai/headroom
- **Licence**: Apache-2.0
- **Version pinned**: 0.25.0
- **Pinned at**: `docker/Dockerfile.Headroom`
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/headroom
- **Intake verdict**: REFERENCE

Pre-send compression of tool outputs, logs and RAG chunks. The fleet runs the real `headroom-ai` package inside the locally built sidecar image (`aitheros-headroom`, no workflow publishes it) and awdk mirrors the sidecar contract in `adk/compression.py`. Registered late: it is pip-pinned, so the compose FROM-scan never saw it.

> headroom is the ~46% the fleet measured off every JSON-shaped tool result before it reaches a model. Thanks to Headroom Labs.

### Tencent Hunyuan3D 2.1

- **Upstream**: https://huggingface.co/tencent/Hunyuan3D-2.1
- **Licence**: LicenseRef-Tencent-Hunyuan-3D-2.1-Community
- **Version pinned**: Comfy-Org/hunyuan3D_2.1_repackaged@606def5076d38b387b005caf93b5336c56ca99f9
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Image -> 3D shape, as ComfyUI's native Hunyuan3D v2.1 nodes; the asset vision to3d backend. Internal only (territory clause).

### Ling-3.0-tiny (GGUF)

- **Upstream**: https://huggingface.co/inclusionAI/Ling-3.0-tiny-GGUF
- **Licence**: MIT
- **Pinned at**: `awdk/adk/cli.py`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADAPT

An 8B sparse mixture-of-experts language model (about 1B parameters active per token, llama.cpp architecture `bailingmoe3`), published as GGUF by its author. The optional small-box tier of `adk bonsai-local`, pinned by sha256.

> inclusionAI published the model and its GGUF under MIT, which is the only reason a CPU-only box can run a model of this class here at all.

### llama.cpp

- **Upstream**: https://github.com/ggml-org/llama.cpp
- **Licence**: MIT
- **Version pinned**: server-cuda
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.

The CUDA llama-server image the pool lane runs.

### nginx

- **Upstream**: https://github.com/nginx/nginx
- **Licence**: BSD-2-Clause
- **Version pinned**: alpine
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The static server several compose sidecars pull.

### Playwright

- **Upstream**: https://github.com/microsoft/playwright
- **Licence**: Apache-2.0
- **Pinned at**: `AitherOS/apps/AitherVeil/package.json`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The browser automation library behind AitherBrowser's session lane and the e2e checks that assert a rendered page rather than a status code.

### PostgreSQL

- **Upstream**: https://github.com/postgres/postgres
- **Licence**: PostgreSQL
- **Version pinned**: 16-alpine
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The authoritative session and directory store.

### REA (Reverse Engineer Anything)

- **Upstream**: https://github.com/morluto/rea
- **Licence**: MIT
- **Version pinned**: 6.7.0
- **Pinned at**: `AitherOS/lib/agents/packs/rea/tools.py`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

One MCP server + CLI that reverse-engineers native binaries (Ghidra/Hopper/ IDA providers), JS/Electron apps, .NET, Android, firmware and live browser behavior, returning typed Evidence records. We run rea-agents as a pinned subprocess (npx rea-agents@6.7.0) behind the gateway; we do NOT vendor or redistribute its 460k-line source. Our rea pack adds a fail-closed reconstruction-obligation ledger and a target-authorization policy.

> REA's insistence that every finding carries its evidence and its limits — and that a reconstruction only closes when a verifier of comparable authority passes — is the discipline we built our ledger on.

### Redis

- **Upstream**: https://github.com/redis/redis
- **Licence**: RSALv2 OR SSPLv1
- **Version pinned**: 7-alpine (live: 7.4.11)
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The FluxBus backbone -- streams, pub/sub, shared state, rate limits.

### RF-DETR

- **Upstream**: https://github.com/roboflow/rf-detr
- **Licence**: Apache-2.0
- **Version pinned**: Roboflow/rf-detr-medium@1b5b672408f86dd38e05dd3cf3f2e0834e545a59
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The AssetVision detect backend (COCO, RF-DETR-M via transformers).

### SAM 2.1 (Segment Anything 2)

- **Upstream**: https://github.com/facebookresearch/sam2
- **Licence**: Apache-2.0
- **Version pinned**: facebook/sam2.1-hiera-large@665f8e2ad61cf5f53d65644ff27c8ee525124610
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Box-prompted masks; AssetVision's default segmenter (with Grounding DINO boxes) while SAM 3 awaits Meta's approval.

### Scrapling

- **Upstream**: https://github.com/D4Vinci/Scrapling
- **Licence**: BSD-3-Clause
- **Pinned at**: `awdk/pyproject.toml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Adaptive scraping framework with TLS-impersonating and stealth-browser fetchers. Declared as the awdk `scrape` extra behind `adk/webfetch.py`'s ladder (httpx first, Scrapling on a bot wall, stealth browser on request), with our SSRF guard in front of every engine.

> Scrapling is why a standalone awdk agent can read a Cloudflare-fronted docs page off-box. Thanks to Karim Shoair and contributors.

### SigLIP 2

- **Upstream**: https://huggingface.co/google/siglip2-base-patch16-224
- **Licence**: Apache-2.0
- **Version pinned**: google/siglip2-base-patch16-224@75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The AssetVision classify backend (zero-shot taxonomy + material tags).

## Evaluated

Read, forked or formally taken through intake, and they left no trace in the build. Listed because a named absence can be chased and a silent one is rediscovered from zero.

### caveman

- **Upstream**: https://github.com/JuliusBrussee/caveman
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Terse-output skill for coding agents plus a BSL-licensed compression engine. The house report cap already exists; the measurable half ships as awdk's `report_150w` pattern. Nothing copied.

### Edge0

- **Upstream**: https://github.com/Edge0-AI/edge0
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/Aitherium/Edge0
- **Intake verdict**: ADAPT

A streaming-MoE inference framework (SSD expert offload + Recover-LoRA + a trained "prerouter" that predicts expert routing one step ahead). The Python framework is MLX/Apple-Silicon only; since 2026-09-30 the repo also carries Windows and Android engines on pinned mainline llama.cpp that read GGUF. No wasm, no WebGPU. Two 4-bit releases, Edge0-8B-A1B (Ling 3.0 tiny hybrid) and Edge0-35B-A3B (Qwen3.6-35B-A3B).

> Edge0 is why this intake happened at all, and its `backends/` facade is the reason there is something to contribute back: it reserves a peer backend slot selected by `EDGE0_BACKEND` and says outright that a new backend implementing the same surface "can reuse all of the framework code". Our fork targets exactly that seam. The idea we are taking — a sparse MoE so an in-browser model's quality and download size stop moving together — is theirs, and we say so.

### Fabric

- **Upstream**: https://github.com/danielmiessler/Fabric
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADAPT

A library of 256 prompt "patterns" (`<name>/system.md`) and a CLI to pipe input through one. awdk adopts the on-disk convention as `adk patterns` (own pattern texts; an importer reads a user's Fabric clone with provenance). No Fabric text ships in the wheel.

> The pattern-directory convention is Daniel Miessler's; adopting it unchanged is what makes every pattern in that ecosystem importable here.

### Hermes Agent

- **Upstream**: https://github.com/NousResearch/hermes-agent
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/hermes-agent
- **Intake verdict**: ADAPT

Nous Research's self-improving agent (skills created from experience, agentskills.io SKILL.md standard). We have a migration bridge into AitherOS and `aither install hermes`; the one adaptation is SKILL.md import/export on awdk's SkillStore so skills move both ways.

> Hermes made "the agent that learns skills" a product claim other agents now have to answer.

### HyperFrames

- **Upstream**: https://github.com/heygen-com/hyperframes
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

HTML/CSS + GSAP -> deterministic MP4, with agent skills as the authoring surface. A renderer for the platform video plane (VideoDirector/Remotion), not an awdk capability; first candidate when that plane is next opened.

### Roboflow Inference

- **Upstream**: https://github.com/roboflow/inference
- **Licence**: Apache-2.0 AND LicenseRef-Roboflow-Enterprise
- **Our fork**: https://github.com/wizzense/inference
- **Intake verdict**: REFERENCE

Roboflow's inference server (model zoo + workflows). NOT deployed: its image carries the enterprise directories and AGPL YOLO weights. The asset vision lane loads the two models it needs (RF-DETR, SAM 3) directly through transformers instead.

### mem0

- **Upstream**: https://github.com/mem0ai/mem0
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Memory layer for agents (LLM fact extraction, ADD/UPDATE/DELETE/NONE reconciliation, scoped vector store). awdk's `memory_wiki.py` already consolidates and supersedes; no adapter until a user with a mem0 store asks.

> The four-outcome reconciliation vocabulary is the clearest statement of what a memory write should decide.

### NVIDIA Cosmos

- **Upstream**: https://github.com/NVIDIA/Cosmos
- **Licence**: OpenMDW-1.1
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

World-foundation models for video generation and physical-AI simulation. The repository weights are OpenMDW-1.1; the managed Cosmos service is sold under NVIDIA AI Enterprise. We do not hold that licence, so the row is evaluated, not adopted -- there is no free self-host lane.

### NVIDIA CUDA Toolkit

- **Upstream**: https://docs.nvidia.com/cuda/
- **Licence**: NVIDIA CUDA Toolkit EULA
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

The CUDA toolkit and its documentation corpus, exposed through the hand-written nvidia_cuda toolpack (cuda_docs_*). Free to use under the CUDA EULA; a free NVIDIA account is needed for the docs MCP login.

### NVIDIA DALI

- **Upstream**: https://github.com/NVIDIA/DALI
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

GPU-accelerated data loading and augmentation. Free and open (Apache-2.0); not adopted -- our training loaders are not yet GPU-bound.

### NVIDIA DeepStream

- **Upstream**: https://developer.nvidia.com/deepstream-sdk
- **Licence**: NVIDIA DeepStream SDK License (production use)
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Streaming video analytics SDK. The sample code is open; PRODUCTION deployment requires NVIDIA AI Enterprise, which we do not hold. The nvcr.io image is 401 anonymously, so its live tag list is could-not-judge rather than "no releases".

### NVIDIA Dynamo

- **Upstream**: https://github.com/ai-dynamo/dynamo
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Disaggregated inference serving for datacenter GPUs. Free and open (Apache-2.0); not adopted because our inference route is MicroScheduler, and a second serving plane would split VRAM accounting.

### NVIDIA Isaac Sim

- **Upstream**: https://github.com/NVIDIA-Omniverse/IsaacSim
- **Licence**: NVIDIA Isaac Sim License (production)
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Robotics simulation and deployment. Repository code is Apache-2.0 but production Isaac Sim is under NVIDIA AI Enterprise, which we do not hold. The release API answers 404 (no GitHub releases), so the snapshot records could-not-judge with the status rather than inventing a version.

### NVIDIA JetPack

- **Upstream**: https://developer.nvidia.com/embedded/jetpack
- **Licence**: NVIDIA JetPack SDK License
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

The edge runtime for Jetson modules. Free, but it targets dedicated hardware we do not run; recorded so the question is not reopened from zero.

### NVIDIA NeMo Framework

- **Upstream**: https://github.com/NVIDIA/NeMo
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Supervised fine-tuning and ASR/NLP training framework. The Python package is Apache-2.0 and free to self-host. The managed NeMo microservice lane is NVIDIA AI Enterprise; we use the open package or nothing.

### NVIDIA Nemotron

- **Upstream**: https://huggingface.co/nvidia/Nemotron-4-340B-Instruct
- **Licence**: NVIDIA Open Model License
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Instruction-tuned models built for agent planning and dispatch. Weights are free under the NVIDIA Open Model License; serving needs an NGC key. The nvidia_nemotron toolpack exists and is fail-soft without one.

### NVIDIA NIM

- **Upstream**: https://build.nvidia.com/
- **Licence**: NVIDIA AI Enterprise (self-host) / NVIDIA API Terms (hosted)
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

OpenAI-compatible model microservices. The hosted build.nvidia.com lane is free with an NGC key; SELF-HOSTING requires NVIDIA AI Enterprise, which we do not hold. The nvidia_nim pack therefore reports "not authenticated" and falls back to the native route -- never a claim that self-host is available.

### NVIDIA Nsight Systems

- **Upstream**: https://docs.nvidia.com/nsight-systems/
- **Licence**: NVIDIA Nsight Systems license (free)
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

System-wide GPU/CPU profiler. Free to use; recorded because profiling is how a VRAM-attribution claim is proved rather than asserted.

### NVIDIA OpenShell

- **Upstream**: https://github.com/NVIDIA/OpenShell
- **Licence**: Apache-2.0 (repository)
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Managed shell/execution surface for agent environments. Repository is Apache-2.0; the managed deployment is NVIDIA AI Enterprise, which we do not hold.

### NVIDIA Riva

- **Upstream**: https://docs.nvidia.com/nim/riva/
- **Licence**: NVIDIA AI Enterprise
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Speech recognition, synthesis and NLU microservices. Production Riva is NVIDIA AI Enterprise only -- we do not hold that licence, so the pack is unavailable by design. The nvcr.io image is 401 anonymously; the snapshot records that as could-not-judge.

### NVIDIA NeMo Switchyard

- **Upstream**: https://github.com/search?q=org%3ANVIDIA+switchyard&type=repositories
- **Licence**: UNKNOWN — No NVIDIA project or documentation page named Switchyard was located on 2026-11-09 (GitHub org search returned no repository); there is nothing to read a licence from yet.
- **Modified**: no — redistributed unchanged.

A component named in the absorption plan for which no upstream was located. Kept as a named gap: an identity that could not be resolved is recorded, not silently dropped, and the snapshot row carries the failed search as its source_url.

### NVIDIA TAO Toolkit

- **Upstream**: https://pypi.org/project/nvidia-tao/
- **Licence**: NVIDIA TAO Toolkit License
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Transfer-learning toolkit for vision models. The package itself is free under NVIDIA's proprietary-but-no-fee TAO licence; SOME model families additionally require NVIDIA AI Enterprise, so adoption would need a per-model check, not a blanket yes.

### NVIDIA TensorRT-LLM

- **Upstream**: https://github.com/NVIDIA/TensorRT-LLM
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Ahead-of-time compilation and optimized kernels for LLM inference. Free and open (Apache-2.0). Not adopted: MicroScheduler already owns the engine lifecycle, and a second engine plane would double the VRAM accounting.

### OpenMontage

- **Upstream**: https://github.com/calesthio/OpenMontage
- **Licence**: AGPL-3.0-only
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Agentic video production (pipelines, checkpoints, pacing scoring, a live cost board) rendering through HyperFrames. AGPL -- never vendored; the production-management ideas are recorded for a platform video session.

### OpenSpec

- **Upstream**: https://github.com/Fission-AI/OpenSpec
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADAPT

Spec-driven development for coding agents: a change = proposal + delta specs (ADDED/MODIFIED/REMOVED requirements with WHEN/THEN scenarios) + tasks, archived into living specs. awdk's `specflow` toolpack reimplements the shape in Python; template prose is ours.

> The change-delta shape and the WHEN/THEN requirement unit are OpenSpec's.

### PageIndex

- **Upstream**: https://github.com/VectifyAI/PageIndex
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/PageIndex
- **Intake verdict**: ADAPT

Vectorless, reasoning-based RAG: a heading/TOC tree with per-node LLM summaries, retrieved by tree descent. awdk's `adk/graph_rag/page_index.py` reimplements the idea on `adk.llm.LLMRouter`; upstream's hard pins (litellm, PyPDF2, pypdfium2) make it undependable directly.

> The tree-search-instead-of-embeddings bet is VectifyAI's, and it is the right one for long single documents.

### NVIDIA Personal AI Router (PAIR)

- **Upstream**: https://github.com/NVIDIA/Personal-AI-Router
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/Personal-AI-Router
- **Intake verdict**: ADAPT

NVIDIA's LAN inference router (Go services + Electron): mDNS node discovery keyed by UUID, EAP-NOOB PIN pairing into pinned-cert mTLS, Ollama/OpenAI-compatible proxies that build an ordered failover list per request (model-inventory gate, then pending + smoothed GPU pressure, then stable id) with process-local reservations. The scheduler/reservation shape is reimplemented in awrouter's failover.py; the LM Studio probe joins awdk's enrollment ladder. No code copied.

> PAIR's scheduler is the clearest small statement of load-aware failover for a handful of consumer GPUs -- pressure bands with hysteresis, neutral-on-stale telemetry, and reservations that spread a burst before any report can land. Those three ideas are theirs.

### phantom-kv

- **Upstream**: https://github.com/lordx64/phantom-kv
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Our fork**: https://github.com/wizzense/phantom-kv
- **Intake verdict**: REFERENCE

Refusal removal for language models as a loadable KV-cache graft: no weight edits and no refusal-direction projection, a small cache artifact is swapped in at runtime ("pills") and unloaded to restore the base model byte-identically. transformers/PyTorch checkpoints (its scoreboard is Qwen3-4B-Instruct).

### SAM 3D Objects

- **Upstream**: https://github.com/facebookresearch/sam-3d-objects
- **Licence**: LicenseRef-SAM-License-2025-11-19
- **Intake verdict**: REFERENCE

Single image -> 3D object. Not run: HF weights access-gated (403 for our token; requesting access accepts Meta's terms on the owner's behalf) and upstream asks for a 32 GB GPU. Declared first in asset_vision.yaml to3d.backend_order so it is picked up the day it is cleared.

### SAM 3 (Segment Anything with Concepts)

- **Upstream**: https://github.com/facebookresearch/sam3
- **Licence**: LicenseRef-SAM-License-2025-11-19
- **Intake verdict**: ADOPT

Text-prompted concept segmentation ("sword", "barrel" -> every instance's mask). A ready-to-enable AssetVision backend (config/asset_vision.yaml `sam3`, enabled: false) pending Meta's approval. Internal only (ship_ok false).

### spec-kit

- **Upstream**: https://github.com/github/spec-kit
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

GitHub's greenfield SDD toolkit (constitution -> specify -> plan -> tasks). Two ideas folded into awdk's `specflow`: a project constitution the proposal is checked against, and prioritized independently-testable user stories. No code or text copied.

> The constitution-as-artifact idea is spec-kit's.

### TrendRadar

- **Upstream**: https://github.com/sansan0/TrendRadar
- **Licence**: GPL-3.0-only
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Hot-topic aggregator over ~35 (mostly Chinese) platforms with keyword rules, push channels and an MCP server. GPL -- never vendored. The platform's NewsWire has trending + velocity already; its missing MCP surface is noted for a platform session.

### Triton Inference Server

- **Upstream**: https://github.com/triton-inference-server/server
- **Licence**: BSD-3-Clause
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Multi-framework model serving. Free (BSD-3-Clause); evaluated as an alternative serving front end and not adopted for the same reason as Dynamo.

## Evaluated and declined

Taken through intake and not adopted. The verdict is recorded so the same repo is not re-litigated by the next person who finds it -- declining a project is not a judgement on it.

### AI Engineering Hub

- **Upstream**: https://github.com/patchy631/ai-engineering-hub
- **Licence**: MIT
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REJECT

A corpus of independent RAG/agent tutorial projects, each bound to a different vendor SDK. No shared library, no seam to read; rejected so the hub is not re-intaken -- a specific demo's upstream project would be.

### Daytona

- **Upstream**: https://github.com/daytonaio/daytona
- **Licence**: UNKNOWN — The public repository is a README stub since 2026-06 ("core development has moved to a private codebase"); no LICENSE file exists in the tree. Nothing to read, nothing to depend on.
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REJECT

Was a sandbox service for AI-generated code. Rejected as a dependency because the source is gone; the one idea kept (sandbox TTL auto-teardown) is ticketed against awdk's own daemon.

### Ultralytics YOLO

- **Upstream**: https://github.com/ultralytics/ultralytics
- **Licence**: AGPL-3.0
- **Intake verdict**: REJECT

YOLO detectors. Rejected for the asset vision lane: AGPL-3.0 -- internal experiments only, never in anything that ships. RF-DETR (Apache-2.0) covers detection.

---

## What the columns mean

- **Usage** — `shipped` we redistribute the artifact; `integrated` we pin and run
  it without shipping a copy; `evaluated` we read or forked it and it is not in
  the build; `rejected` we took it through intake and declined.
- **Licence** — read from a file or an API response, never assumed. `UNKNOWN`
  means nobody has checked yet and says so, which is the honest state; guessing
  would fail open, because the next reader would assume somebody had.
- **Version** — what the pin file actually pins, asserted against the registry.

Corrections welcome.
