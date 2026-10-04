#!/bin/bash
# Build-time proof that this image can LOAD a model, not merely start llama-server.
#
# Measured 2026-09-27: the ai and ai-full images passed every check (binary runs,
# GLIBCXX resolves, catalogue present) and shipped unable to load ANY model -- the
# pinned PrismML build refuses the legacy Q2_0 GGUFs the catalogue named (#9468).
# The only check that could have caught it is the one the user's machine runs on
# first boot, so this runs exactly that: the real serve script, the real catalogue,
# the smallest model, one completion. The download is deleted before this RUN ends,
# so the image carries no weights.
set -eu
MODEL="${AWNIX_CHECK_MODEL:-bonsai-1.7b}"
PORT=$((21000 + $$ % 900))
LOG=/tmp/awnix-model-load-check.log
cleanup() {
  pkill -f /opt/bonsai/bin/ 2>/dev/null || true
  find /opt/bonsai/models -mindepth 1 -delete 2>/dev/null || true
  find /var/lib/bonsai/models -mindepth 1 -delete 2>/dev/null || true
  rm -f /var/lib/bonsai/server.log
  rm -f /opt/bonsai/server.log
  rm -rf "${SHIM:-/nonexistent}"
}
trap cleanup EXIT

# Inside a build RUN every `sudo` fails PAM account lookup, root included
# ("Authentication service cannot retrieve authentication info"; measured in CI run
# 36339449906), so the serve script's privilege drop cannot run and the download never
# starts. This check proves the model LOAD; the build already runs as root, so a
# pass-through `sudo` stands in here only. Real boots use the real sudo.
SHIM=$(mktemp -d)
printf '#!/bin/bash
[ "${1:-}" = -u ] && shift 2
exec "$@"
' > "$SHIM/sudo"
chmod 755 "$SHIM/sudo"
PATH="$SHIM:$PATH" BONSAI_USER=root AWNIX_MODEL="$MODEL" BONSAI_PORT="$PORT" bash /usr/local/sbin/serve-awnix-bonsai.sh >"$LOG" 2>&1 &
pid=$!
up=0
for _ in $(seq 1 200); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/health" && { up=1; break; }
  kill -0 "$pid" 2>/dev/null || break
  sleep 3
done
if [ "$up" != 1 ]; then
  echo "awnix-model-load-check: $MODEL never became servable -- this image cannot load a model" >&2
  tail -25 "$LOG" >&2
  exit 1
fi
answer=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"Name the capital of France in one word."}],"max_tokens":200}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())' 2>/dev/null || true)
if [ -z "$answer" ]; then
  echo "awnix-model-load-check: $MODEL loaded but returned no completion" >&2
  tail -25 "$LOG" >&2
  exit 1
fi
echo "awnix-model-load-check: ok -- $MODEL loaded and answered ($(printf %s "$answer" | tail -c 60))"
