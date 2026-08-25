#!/bin/sh
# End-to-end verification of the gobbonet-appliance image, run INSIDE it.
#
# Written as a FILE rather than a nested `podman run sh -c "..."` on purpose: the
# quoting survives neither the Windows->WSL hop nor sh -lc, and a swallowed
# command substitution reports MISSING for something that is present -- which is
# a false alarm that reads exactly like a broken image.
#
# Exit 0 = every claim this variant makes is true of the artifact.
fail=0
ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }

# 1. the runtime
[ -x /usr/bin/python3.11 ] && ok "python3.11" || bad "python3.11 missing"
command -v curl >/dev/null && ok "curl" || bad "curl missing"
command -v jq   >/dev/null && ok "jq"   || bad "jq missing"

# 2. inference
BIN=$(find /opt/bonsai/bin -name llama-server -type f 2>/dev/null | head -1)
[ -n "$BIN" ] && [ -x "$BIN" ] && ok "llama-server at $BIN" || bad "llama-server missing"
[ -x /usr/local/sbin/serve-awnix-bonsai.sh ] && ok "serve script" || bad "serve script missing"
[ -f /usr/local/sbin/awnix-models.yaml ] && ok "model catalogue" || bad "catalogue missing"

# 3. the baked weights -- the whole "boots offline" claim
for f in Ternary-Bonsai-1.7B-Q2_0.gguf Ternary-Bonsai-4B-Q2_0.gguf; do
  p="/opt/bonsai/models/$f"
  if [ -s "$p" ]; then
    sz=$(stat -c%s "$p")
    mb=$((sz / 1024 / 1024))
    # A GGUF starts with the magic bytes "GGUF". A truncated or error-page
    # download is the failure this guards: it has a size and is not a model.
    magic=$(head -c 4 "$p")
    if [ "$magic" = "GGUF" ]; then ok "$f (${mb} MB, GGUF magic)"
    else bad "$f is ${mb} MB but does not start with GGUF -- not a model"; fi
  else
    bad "$f absent"
  fi
done

# 4. the UI
[ -f /opt/gobbonet/chat.html ] && ok "gobbonet chat.html" || bad "chat.html missing"
[ -d /opt/gobbonet/js ] && ok "gobbonet js/" || bad "js/ missing"

# 5. the agent layer -- launcher and scoped memory are the same package
python3.11 -c "import adk" 2>/dev/null && ok "awdk imports" || bad "awdk does not import"
python3.11 -c "from adk.packs.gobbonet import campaign_memory, cards, retrieval" 2>/dev/null \
  && ok "gobbonet pack imports" || bad "gobbonet pack does not import"
python3.11 -c "import awm" 2>/dev/null && ok "awm (scoped memory)" || bad "awm missing"
command -v adk >/dev/null && ok "adk on PATH" || bad "adk not on PATH"

# 6. the aw* stack
python3.11 - <<'PY'
mods = ["awgit","awgraph","awrelay","awm","awshare","awnode","awprism","awreason","awrecurse","awrepl","awsync"]
missing = []
for m in mods:
    try: __import__(m)
    except Exception: missing.append(m)
print(("  ok   aw* stack: %d/%d import" % (len(mods)-len(missing), len(mods))) if not missing
      else ("  FAIL aw* missing: " + ", ".join(missing)))
PY

echo
[ "$fail" -eq 0 ] && echo "APPLIANCE VERIFIED" || echo "FAILED ($fail)"
exit $fail
