#!/bin/bash
# Fetch the largest Bonsai that FITS and start llama-server, on a box that has the
# PrismML binary baked in (Containerfile.awnix-runner-ai). Runs at FIRST
# BOOT, not build time -- see that Containerfile's comment for why the
# model weights are never baked into the image.
#
# 🚨 QUANTIZATION NOTE: this follows apps/AitherVeil/public/install-bonsai.sh's
# PROVEN, LIVE download (Q2_0, from huggingface.co/prism-ml/Ternary-Bonsai-
# <size>-gguf) rather than AitherOS/config/serving_recipes/bonsai-27b-serve.yaml,
# which documents Q1_0 with no evidence of ever having been fetched or served.
# That is itself a real inconsistency between two docs claiming to describe
# the same model -- flagged, not silently resolved by guessing one is right.
#
# Usage (from cloud-init user-data, as root):
#   /usr/local/sbin/serve-awnix-bonsai.sh [--mesh-provide]
set -euo pipefail

die() { echo "serve-awnix-bonsai: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (drops to 'runner' internally) -- got uid $(id -u)"

MESH_PROVIDE=0
[ "${1:-}" = "--mesh-provide" ] && MESH_PROVIDE=1

BIN="$(find /opt/bonsai/bin -name llama-server -type f | head -1)"
[ -n "$BIN" ] && [ -x "$BIN" ] || die "llama-server missing at /opt/bonsai/bin -- image build did not stage it"

MODEL_DIR=/opt/bonsai/models
PORT="${BONSAI_PORT:-8199}"

# ── pick a model this machine can actually run ──────────────────────────────────────
#
# This fetched 27B unconditionally: 6833 MB of weights, then a guaranteed OOM on any
# card smaller than ~8 GB. The cost was paid BEFORE the failure, which is the worst
# possible ordering -- a first boot that downloads for twenty minutes and then cannot
# start is indistinguishable, to the person watching, from a broken image.
#
# Sizes are the real Q2_0 artefacts, measured 2026-08-21 (all four HTTP 200):
#   1.7B 441MB   4B 1025MB   8B 2081MB   27B 6833MB
#
# The budget is HALF of detected VRAM (or RAM with no GPU). llama.cpp needs room for the
# KV cache and context on top of the weights, so sizing to the weights alone produces a
# model that loads and dies mid-generation -- worse than one that never loaded, because
# it looks like a working install.
# The catalogue is awnix-models.yaml and the chooser is awnix-model-select.py. This
# script deliberately keeps NO table of its own: it had one for about an hour, in
# parallel with the YAML, and two copies of one rule set drift while every test on the
# other copy stays green.
#
# AWNIX_MODEL / BONSAI_SIZE name a model explicitly (BONSAI_SIZE keeps the old spelling
# working: "4B" -> "bonsai-4b"). With neither, the selector detects memory and takes the
# largest Bonsai that fits -- Bonsai only, because the fleet models in the catalogue are
# opt-in and a first boot must not pull several GB of ours unasked.
SELECT=/usr/local/sbin/awnix-model-select.py
[ -x "$SELECT" ] || die "model selector missing at $SELECT -- the image did not ship it"

MODEL_ID="${AWNIX_MODEL:-}"
if [ -z "$MODEL_ID" ] && [ -n "${BONSAI_SIZE:-}" ]; then
  MODEL_ID="bonsai-$(echo "$BONSAI_SIZE" | tr '[:upper:]' '[:lower:]')"
fi

if [ -n "$MODEL_ID" ]; then
  echo "serve-awnix-bonsai: model=$MODEL_ID (explicit)"
  PLAN=$("$SELECT" --model "$MODEL_ID" --plan) || die "no such model: $MODEL_ID"
else
  PLAN=$("$SELECT" --plan) || die "no catalogued model fits this machine.
  Set AWNIX_MODEL=bonsai-1.7b to try the smallest anyway, or run inference elsewhere
  and point this box at it."
  MODEL_ID=$(echo "$PLAN" | jq -r .id)
  echo "serve-awnix-bonsai: selected $MODEL_ID for this machine"
fi

MODEL_FILE=$(echo "$PLAN" | jq -r .file)
NEEDS_JOIN=$(echo "$PLAN" | jq -r .join)
GGUF="$MODEL_DIR/$MODEL_FILE"

if [ ! -s "$GGUF" ]; then
  SIZE_MB=$(echo "$PLAN" | jq -r .size_mb)
  N_URLS=$(echo "$PLAN" | jq -r '.urls | length')
  echo "fetching $MODEL_ID (~${SIZE_MB} MB in ${N_URLS} file(s)) -- the one-time cost"
  echo "this script exists to defer out of the image"

  # Stage under .part and rename only on success. `[ ! -s "$GGUF" ]` above is the ONLY
  # guard against a truncated model, so a partial file must never occupy the real path:
  # llama.cpp handed a half-written GGUF fails in a way that reads as a bad model.
  rm -f "$GGUF.part"
  i=0
  echo "$PLAN" | jq -r '.urls[]' | while IFS= read -r url; do
    i=$((i + 1))
    [ "$N_URLS" -gt 1 ] && echo "  part $i/$N_URLS"
    # -C - resumes; appending each slice in order reassembles the original file.
    sudo -u runner curl -fL --progress-bar "$url" >> "$GGUF.part" \
      || die "download failed on $url"
  done

  # `while` runs in a subshell here, so its exit status is not the loop body's -- check
  # the artefact instead. Trusting the pipeline's status would call a failed fetch a
  # success, which is this repo's most-repeated mistake in a new place.
  [ -s "$GGUF.part" ] || die "download produced nothing for $MODEL_ID"

  ACTUAL_MB=$(( $(stat -c %s "$GGUF.part") / 1048576 ))
  # A concatenation that lost a slice is still a valid-looking file. Allow 5% slack for
  # rounding, and refuse anything materially short rather than serving it.
  MIN_MB=$(( SIZE_MB * 95 / 100 ))
  if [ "$ACTUAL_MB" -lt "$MIN_MB" ]; then
    rm -f "$GGUF.part"
    die "got ${ACTUAL_MB}MB, expected ~${SIZE_MB}MB -- a slice is missing. Refusing to
  serve a truncated model; re-run to retry."
  fi

  mv "$GGUF.part" "$GGUF"
  chown runner:runner "$GGUF"
  echo "  ok: ${ACTUAL_MB}MB at $GGUF"
fi

# --host 127.0.0.1 ON PURPOSE, matching install-bonsai.sh's own reasoning:
# 0.0.0.0 publishes an unauthenticated inference server to the whole
# network. --reasoning-budget 2048 is NOT optional -- Bonsai's Qwen3 chat
# template force-opens a <think> block every turn; llama.cpp's default of
# -1 never closes it, so `content` comes back EMPTY and the model reads as
# broken rather than as a budget problem (measured live building
# install-bonsai.sh; reproduced independently in the browser runtime too).
#
# Context + KV cache, aligned with the fleet's Bonsai quadlet on 2026-08-22 (one
# recipe, two surfaces). Measured there on mainline llama.cpp b10335: Bonsai-27B
# holds KV in only 16 of its 64 layers, so a q4_0 KV cache costs 18 KiB/token --
# 65,536 tokens = 1.15 GB (+150 MB recurrent state). At the old f16 default the
# same window is 4x that, which is why the window was left at 16k. A coding
# agent needs file contents + test output in one window; 16k is where it
# compacts mid-task. Both are knobs: AWNIX_CTX (tokens) and AWNIX_KV (a
# llama.cpp cache type; empty string = engine default, i.e. f16 and no -fa).
AWNIX_CTX="${AWNIX_CTX:-65536}"
AWNIX_KV="${AWNIX_KV-q4_0}"
KV_ARGS=""
[ -n "$AWNIX_KV" ] && KV_ARGS="-fa on -ctk $AWNIX_KV -ctv $AWNIX_KV"
SERVE_ARGS="--model $GGUF --host 127.0.0.1 --port $PORT --ctx-size $AWNIX_CTX $KV_ARGS --reasoning-budget 2048 --alias bonsai-selfhost"
# If the bundled binary refuses the KV flags (an older fork build), fall back to
# the pre-2026-08-22 arguments ONCE and say so, rather than dying with a server
# log nobody reads. A loud downgrade beats a silent "did not come up".
LEGACY_SERVE_ARGS="--model $GGUF --host 127.0.0.1 --port $PORT --ctx-size 16384 --reasoning-budget 2048 --alias bonsai-selfhost"

# The PrismML binary needs GLIBCXX_3.4.30; the base image's own libstdc++
# tops out at 3.4.29, and (measured live) that libstdc++'s own transitive
# GLIBC_2.36/2.38 needs mean the fix can't stop at libstdc++ alone --
# Containerfile.awnix-runner-ai has the full story and bundles a small
# matched runtime chain (loader + libc + libm + libgcc_s + libstdc++) at
# /opt/bonsai/lib. Invoked through THAT loader directly rather than via
# LD_LIBRARY_PATH -- LD_LIBRARY_PATH still leaves libc/the loader itself to
# the OS, which is exactly the mismatch being worked around here. The
# binary's own $ORIGIN-relative RPATH (libggml-*/libllama-*/libmtmd) is
# still honoured through an explicit loader invocation, so --library-path
# only needs to name the bundle.
LOADER=/opt/bonsai/lib/ld-linux-x86-64.so.2
[ -x "$LOADER" ] || die "bundled loader missing at /opt/bonsai/lib -- image build did not stage it"

# 🚨 THE BACKENDS MUST BE VISIBLE FROM THE LOADER'S OWN DIRECTORY, and until
# 2026-08-23 they were not -- so this script could not load a model AT ALL.
#
#   llama_model_load_from_file_impl: no backends are loaded
#
# ggml discovers its backends by scanning the directory of the RUNNING
# EXECUTABLE. Invoked through an explicit loader (which is required here -- the
# comment above explains why LD_LIBRARY_PATH cannot substitute), the executable
# is the LOADER, so ggml scans /opt/bonsai/lib and finds no libggml-*.so. The
# binary's own directory, full of them, is never looked at.
#
# Neither obvious workaround works, both measured:
#   * adding the binary's dir to --library-path        -> still no backends
#     (this is dlopen-by-scan, not a link-time search)
#   * GGML_BACKEND_PATH=<that dir>                     -> "Is a directory"; the
#     variable names ONE .so file, and a colon-list of all 14 also fails.
# Naming a single CPU backend does work, and is wrong: llama.cpp ships fourteen
# CPU variants precisely so it can pick by CPU features at runtime, and pinning
# haswell breaks every older machine.
#
# Symlinking them beside the loader lets ggml's own scan run normally and choose
# for itself. Verified offline (`--network none`): loads, /v1/models answers, and
# a completion comes back.
BINDIR=$(dirname "$BIN")
for _so in "$BINDIR"/libggml*.so*; do
  [ -e "$_so" ] || continue
  ln -sf "$_so" "/opt/bonsai/lib/$(basename "$_so")" 2>/dev/null || true
done

pkill -f "$BIN" 2>/dev/null || true
# shellcheck disable=SC2086
sudo -u runner bash -c "nohup '$LOADER' --library-path /opt/bonsai/lib '$BIN' $SERVE_ARGS >/opt/bonsai/server.log 2>&1 &"

echo "waiting for it to load..."
ok=0
for _ in $(seq 1 120); do
  sleep 1
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  # a refused flag exits immediately; do not wait two minutes to learn that
  if ! pgrep -f "$BIN" >/dev/null 2>&1; then break; fi
done
if [ "$ok" != "1" ] && [ -n "$KV_ARGS" ] && grep -qiE "invalid argument|unknown argument|error while handling argument" /opt/bonsai/server.log 2>/dev/null; then
  echo "WARNING: llama-server refused the KV/context flags ($KV_ARGS, ctx $AWNIX_CTX) -- falling back to the legacy 16k/f16 arguments. Upgrade the bundled binary to get the 64k window."
  # shellcheck disable=SC2086
  sudo -u runner bash -c "nohup '$LOADER' --library-path /opt/bonsai/lib '$BIN' $LEGACY_SERVE_ARGS >/opt/bonsai/server.log 2>&1 &"
  for _ in $(seq 1 120); do
    sleep 1
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  done
fi
[ "$ok" = "1" ] || die "did not come up in two minutes -- see /opt/bonsai/server.log"
echo "serving on 127.0.0.1:$PORT"

# Mesh registration is OPT-IN, never automatic -- adk mesh provide is
# already gated by operator trust approval on the receiving end (the
# "safest existing mechanism" this plan settled on rather than inventing a
# new self-registration endpoint), and this script should not silently
# attempt it just because the box is capable.
if [ "$MESH_PROVIDE" = "1" ]; then
  echo "registering as community inference capacity (adk mesh provide)"
  sudo -u runner bash -c "adk mesh onboard && adk mesh provide --inference-url http://127.0.0.1:$PORT/v1 --model bonsai-selfhost" \
    || echo "mesh registration did not complete -- Bonsai is running locally regardless"
fi
