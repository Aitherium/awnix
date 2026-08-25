#!/bin/sh
# The claim this appliance actually makes: it BOOTS AND ANSWERS WITH NO NETWORK.
#
# Everything else -- files present, imports resolving, a UI that serves -- is
# compatible with a box that cannot complete a sentence. This loads a baked model
# and asks it something, which is the only check that separates the two.
#
# Run with --network none to prove the "no network" half is not a claim.
set -u
fail=0
BIN=$(find /opt/bonsai/bin -name llama-server -type f | head -1)
GGUF=/opt/bonsai/models/Ternary-Bonsai-1.7B-Q2_0.gguf
LOADER=/opt/bonsai/lib/ld-linux-x86-64.so.2

[ -x "$BIN" ]   || { echo "FAIL llama-server missing"; exit 1; }
[ -s "$GGUF" ]  || { echo "FAIL model missing"; exit 1; }

# The bundled loader, because the image's glibc may be older than the binary's.
# serve-awnix-bonsai.sh invokes it this way for the same reason.
# The loader is REQUIRED, not optional: llama-server's libstdc++ needs
# GLIBCXX_3.4.30 and the OS tops out at 3.4.29. LD_LIBRARY_PATH does not
# substitute -- the bundled libstdc++ then wants a newer glibc than the OS has,
# so it fails on GLIBC_ instead. Both measured.
[ -x "$LOADER" ] || { echo "FAIL bundled loader missing"; exit 1; }
D=$(dirname "$BIN")
# ...and because the loader becomes the running executable, ggml's backend scan
# looks in the loader's directory. Link the backends there or it loads none.
for so in "$D"/libggml*.so*; do
  [ -e "$so" ] || continue
  ln -sf "$so" "/opt/bonsai/lib/$(basename "$so")" 2>/dev/null || true
done
RUN="$LOADER --library-path /opt/bonsai/lib:$D $BIN"

echo "  starting llama-server on the baked 1.7B..."
# shellcheck disable=SC2086
$RUN -m "$GGUF" --host 127.0.0.1 --port 8081 -c 512 >/tmp/llama.log 2>&1 &
pid=$!

up=0
for i in $(seq 1 60); do
  if curl -sf -o /dev/null http://127.0.0.1:8081/health 2>/dev/null; then up=1; break; fi
  kill -0 "$pid" 2>/dev/null || { echo "  FAIL llama-server exited early"; tail -20 /tmp/llama.log; exit 1; }
  sleep 2
done
[ "$up" = "1" ] && echo "  ok   llama-server is up" || { echo "  FAIL never became healthy"; tail -20 /tmp/llama.log; kill $pid 2>/dev/null; exit 1; }

# /v1/models -- the endpoint the GobboNet health probe reads (upstream PR #18).
MODELS=$(curl -sf http://127.0.0.1:8081/v1/models 2>/dev/null)
echo "$MODELS" | grep -q '"data"' && echo "  ok   /v1/models answers" || { echo "  FAIL /v1/models"; fail=$((fail+1)); }

# The actual point: a completion.
OUT=$(curl -sf -X POST http://127.0.0.1:8081/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"messages":[{"role":"user","content":"Say the single word: goblin"}],"max_tokens":16,"temperature":0}' 2>/dev/null)
CONTENT=$(echo "$OUT" | sed -n 's/.*"content":"\([^"]*\)".*/\1/p' | head -1)
if [ -n "$CONTENT" ]; then
  echo "  ok   the model answered: $(echo "$CONTENT" | head -c 60)"
else
  echo "  FAIL no completion came back"; echo "$OUT" | head -c 300; fail=$((fail+1))
fi

kill $pid 2>/dev/null
echo
[ "$fail" -eq 0 ] && echo "ANSWERS OFFLINE" || echo "FAILED ($fail)"
exit $fail
