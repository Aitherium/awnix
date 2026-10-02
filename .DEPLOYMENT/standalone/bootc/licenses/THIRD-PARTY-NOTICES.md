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
- **Pinned at**: `.PRODUCTS/.GOBBONET/upstream`
- **Modified**: yes — we carry local changes to this project, and most licences ask that this be stated.

The upstream of the GobboNet fork we vendor, modify and serve.

### llama.cpp (PrismML fork)

- **Upstream**: https://github.com/PrismML-Eng/llama.cpp
- **Licence**: MIT
- **Version pinned**: prism-b10687-5d80cff
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

### Redis

- **Upstream**: https://github.com/redis/redis
- **Licence**: RSALv2 OR SSPLv1
- **Version pinned**: 7-alpine (live: 7.4.11)
- **Pinned at**: `.DEPLOYMENT/compose/docker-compose.aitheros.yml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

The FluxBus backbone -- streams, pub/sub, shared state, rate limits.

### Scrapling

- **Upstream**: https://github.com/D4Vinci/Scrapling
- **Licence**: BSD-3-Clause
- **Pinned at**: `awdk/pyproject.toml`
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: ADOPT

Adaptive scraping framework with TLS-impersonating and stealth-browser fetchers. Declared as the awdk `scrape` extra behind `adk/webfetch.py`'s ladder (httpx first, Scrapling on a bot wall, stealth browser on request), with our SSRF guard in front of every engine.

> Scrapling is why a standalone awdk agent can read a Cloudflare-fronted docs page off-box. Thanks to Karim Shoair and contributors.

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

A streaming-MoE inference framework (SSD expert offload + Recover-LoRA + a trained "prerouter" that predicts expert routing one step ahead). MLX/Apple-Silicon only — no GGUF, no wasm, no WebGPU. Two 4-bit releases, Edge0-8B-A1B (Ling 3.0 tiny hybrid) and Edge0-35B-A3B (Qwen3.5-MoE).

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

### mem0

- **Upstream**: https://github.com/mem0ai/mem0
- **Licence**: Apache-2.0
- **Modified**: no — redistributed unchanged.
- **Intake verdict**: REFERENCE

Memory layer for agents (LLM fact extraction, ADD/UPDATE/DELETE/NONE reconciliation, scoped vector store). awdk's `memory_wiki.py` already consolidates and supersedes; no adapter until a user with a mem0 store asks.

> The four-outcome reconciliation vocabulary is the clearest statement of what a memory write should decide.

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
