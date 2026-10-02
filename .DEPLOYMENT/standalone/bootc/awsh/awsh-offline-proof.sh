#!/bin/sh
# awsh-offline-proof.sh -- run inside a container started with --network=none
# and --cap-add SYS_PTRACE.
#
# HOSTED CI ONLY (awsh-offline-proof.yml). Proves, with real exit codes:
#   1. the container has no route off the box (ip route shows no default route);
#   2. with a loopback model server, the harness daemon (127.0.0.1:8362) and the agent
#      daemon (127.0.0.1:9001), both started with their user units' argv and env,
#      `awnix-awsh doctor --json` exits 0 and `awsh doctor --json` exits 0;
#   2b. `awsh -p` answers a question from the loopback model server;
#   3. nothing listens on a non-loopback address (harness, agent, stub model all up);
#   4. EGRESS ATTEMPTS: the doctors, the one-shot and both daemons run under
#      `strace -f -e trace=connect`. Every connect() to a non-loopback address is an
#      attempt to leave the box, recorded even though --network=none makes it fail.
#      The count must be 0. A positive control (one deliberate connect to 203.0.113.1)
#      must be counted, or the trace is judged blind (could not judge);
#   5. negative case: `offline: false` plus a cloud url in /etc/awsh/shell.yaml makes
#      both doctors exit 1.
# Every command goes to $OUT/proof.log as `$ argv` then `rc=N`. The last line of stdout
# is `AWSH-OFFLINE-PROOF: PASS|FAIL|COULD-NOT-JUDGE`. Exit 0 pass, 1 fail, 2 could not judge.
#
# What this does NOT prove: that an awnix or garg IMAGE carries these files (the image is
# a node:22 stand-in until Containerfile.awnix installs them; check_awnix_awsh AWH005), or
# that systemd starts the units (no systemd here; the argv and env are copied by hand).
set -u

OUT="${OUT:-/out}"
PY="${AWDK_PY:-python3}"
CLI=/usr/libexec/awnix/awnix-awsh
mkdir -p "$OUT"
LOG="$OUT/proof.log"
: > "$LOG"
# connect() is the attempt. execve names any child process that makes one, and the
# Python audit hook below names the resolver call and its stack, so a non-zero count can
# be traced to its cause.
STRACE_OPTS="-f -qq -s 160 -e trace=connect,sendto,sendmsg,sendmmsg,execve"
{ echo "--- /etc/hosts"; cat /etc/hosts; echo "--- /etc/resolv.conf"; cat /etc/resolv.conf
  echo "--- /etc/nsswitch.conf hosts"; grep '^hosts' /etc/nsswitch.conf; } >> "$LOG" 2>&1
# sitecustomize (loaded by every Python below via PYTHONPATH): log each non-loopback
# resolver/connect call with its Python stack to $OUT/py-audit.log. Observation only.
AUDIT_DIR="$OUT/.py-audit"
mkdir -p "$AUDIT_DIR"
cat > "$AUDIT_DIR/sitecustomize.py" <<'EOF'
import os, sys, traceback
_OUT = os.path.join(os.environ.get("AWSH_PROOF_OUT", "/out"), "py-audit.log")
_EV = ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
       "socket.gethostbyaddr", "socket.connect")
def _loop(h):
    h = str(h or "")
    return h in ("localhost", "::1", "0.0.0.0", "") or h.startswith("127.")
def _hook(event, args):
    if event not in _EV:
        return
    try:
        host = args[1][0] if event == "socket.connect" and isinstance(args[1], tuple) \
            else (args[1] if event == "socket.connect" else args[0])
        if _loop(host) or (event == "socket.connect" and not isinstance(args[1], tuple)):
            return
        with open(_OUT, "a") as fh:
            fh.write("pid=%d %s %r argv=%r\n%s\n" % (os.getpid(), event, args[:2], sys.argv,
                     "".join(traceback.format_stack(limit=16)[:-1])))
    except Exception:
        pass
sys.addaudithook(_hook)
EOF
export AWSH_PROOF_OUT="$OUT"
export PYTHONPATH="$AUDIT_DIR${PYTHONPATH:+:$PYTHONPATH}"

step() {  # step NAME CMD...   -> runs CMD, logs argv + output + rc, sets $RC
    name="$1"; shift
    printf '$ %s\n' "$*" >> "$LOG"
    "$@" > "$OUT/$name.out" 2>> "$LOG"
    RC=$?
    cat "$OUT/$name.out" >> "$LOG"
    printf 'rc=%s\n' "$RC" >> "$LOG"
    echo "$RC" > "$OUT/$name.rc"
}

traced() {  # traced NAME CMD...  -> step NAME under strace, trace in $OUT/NAME.strace
    tname="$1"; shift
    # shellcheck disable=SC2086
    step "$tname" strace $STRACE_OPTS -o "$OUT/$tname.strace" "$@"
}

count_nonloop() {  # count_nonloop FILE...  -> prints the number of non-loopback connects
    "$PY" - "$@" <<'EOF'
import ipaddress, re, sys
pat = re.compile(r'inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)"')
hits = []
for path in sys.argv[1:]:
    try:
        with open(path, errors="replace") as fh:
            text = fh.read()
    except OSError:
        continue
    for m in pat.finditer(text):
        addr = m.group(1) or m.group(2)
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            hits.append(addr)
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        if not (mapped or ip).is_loopback:
            hits.append(addr)
print(len(hits))
for h in hits:
    sys.stderr.write("non-loopback connect: %s\n" % h)
EOF
}

# shellcheck disable=SC1091
. /etc/profile.d/awsh-offline.sh

# 1. no route off the box
step ip-route ip route
DEFAULT_ROUTES=$(grep -c '^default' "$OUT/ip-route.out" || true)

# 4 (control). the trace must SEE a non-loopback attempt, or a 0 below means nothing.
traced control "$PY" -c "import socket; s = socket.socket(); s.settimeout(1); s.connect_ex(('203.0.113.1', 443))"
CONTROL=$(count_nonloop "$OUT/control.strace" 2>> "$LOG")
echo "control_nonloopback_connects=$CONTROL (must be >= 1)" >> "$LOG"

# a loopback stub model server on the vendor llm port (8199)
"$PY" - > "$OUT/stub-model.log" 2>&1 <<'EOF' &
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path in ("/v1/models", "/models"):
            self._send(json.dumps({"data": [{"id": "proof-stub-model"}]}).encode())
        elif self.path in ("/health", "/v1/health"):
            self._send(b'{"status":"ok"}')
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        # OpenAI-compatible chat: SSE when asked to stream (awsh's raw path), else JSON.
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404); self.end_headers(); return
        try:
            stream = bool(json.loads(raw or b"{}").get("stream"))
        except ValueError:
            stream = False
        if stream:
            chunk = {"id": "c1", "object": "chat.completion.chunk", "model": "proof-stub-model",
                     "choices": [{"index": 0, "delta": {"content": "OFFLINE-OK"}}]}
            sep = chr(10) * 2  # the SSE event separator, spelled without a backslash escape
            self._send(("data: " + json.dumps(chunk) + sep + "data: [DONE]" + sep).encode(),
                       "text/event-stream")
        else:
            self._send(json.dumps({
                "id": "c1", "object": "chat.completion", "model": "proof-stub-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "OFFLINE-OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode())
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", 8199), H).serve_forever()
EOF
STUB_PID=$!

step first-run "$CLI" first-run --json

wait_up() {  # wait_up URL SECONDS -> sets $UP (1/0) and $WAITED
    UP=0; WAITED=0
    while [ "$WAITED" -lt "$2" ]; do
        if "$PY" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=2)" "$1" 2>/dev/null; then
            UP=1; return
        fi
        WAITED=$((WAITED + 1)); sleep 1
    done
}

# the harness daemon: awsh-harness.service's env and argv, traced
# shellcheck disable=SC2086
env AITHER_OFFLINE=1 AITHER_HARNESS_HOST=127.0.0.1 AITHER_HARNESS_BIND_HOST=127.0.0.1 \
    AITHER_HARNESS_PORT=8362 PYTHONUNBUFFERED=1 \
    strace $STRACE_OPTS -o "$OUT/harness.strace" \
    "$PY" -m adk.cli harness serve --host 127.0.0.1 --port 8362 \
    > "$OUT/harness.log" 2>&1 &
HARNESS_PID=$!
wait_up http://127.0.0.1:8362/health 90
HARNESS_UP=$UP
echo "harness_up=$HARNESS_UP after ${WAITED}s" >> "$LOG"

# the agent daemon: awsh-agent.service's env, then its EnvironmentFile, then its argv
(
    export AITHER_OFFLINE=1 AITHER_HOST=127.0.0.1 AITHER_PORT=9001 AITHER_LLM_BACKEND=vllm
    export AITHER_LLM_BASE_URL=http://127.0.0.1:8199/v1 AITHER_LLM_STRICT_BASE_URL=1
    export AITHER_PREFER_LOCAL=true PYTHONUNBUFFERED=1
    set -a
    # shellcheck disable=SC1090,SC1091
    if [ -r "$HOME/.aither/awsh-agent.env" ]; then . "$HOME/.aither/awsh-agent.env"; fi
    set +a
    # shellcheck disable=SC2086
    exec strace $STRACE_OPTS -o "$OUT/agent.strace" \
        "$PY" -m adk.server --host 127.0.0.1 --port 9001 --backend vllm --identity aither
) > "$OUT/agent.log" 2>&1 &
AGENT_PID=$!
wait_up http://127.0.0.1:9001/health 150
AGENT_UP=$UP
echo "agent_up=$AGENT_UP after ${WAITED}s" >> "$LOG"

# 2. both doctors, positive case, traced
traced awnix-awsh-doctor "$CLI" doctor --json
POS_AWNIX=$RC
traced awsh-doctor awsh doctor --json
POS_AWSH=$RC

# 2b. a one-shot question answered by the LOCAL model, with no network. USE_ADK=0 and
#     AUTOSTART_ADK=0 pin it to the offline local-llm rung (raw /v1 against :8199), so
#     the answer provably comes from the loopback stub.
traced awsh-oneshot env AITHERSHELL_AUTOSTART_ADK=0 AITHERSHELL_USE_ADK=0 \
    awsh -p "reply with one word" --output-format json
ONESHOT=$RC
if [ "$ONESHOT" = 0 ] && ! grep -q 'OFFLINE-OK' "$OUT/awsh-oneshot.out"; then ONESHOT=99; fi

# 3. listeners, with harness + agent + stub all up: nothing but loopback
step ss ss -H -ltun
NON_LOOPBACK=$(awk '$5 !~ /^(127\.|\[::1\]|::1)/' "$OUT/ss.out" | wc -l | tr -d ' ')
AGENT_LISTENING=$(awk '$5 ~ /:9001$/' "$OUT/ss.out" | wc -l | tr -d ' ')

# 4. egress attempts across every traced process (doctors, one-shot, harness, agent)
EGRESS=$(count_nonloop "$OUT/awnix-awsh-doctor.strace" "$OUT/awsh-doctor.strace" \
    "$OUT/awsh-oneshot.strace" "$OUT/harness.strace" "$OUT/agent.strace" 2>> "$LOG")
TRACED_CONNECTS=$(cat "$OUT/awsh-oneshot.strace" "$OUT/awsh-doctor.strace" 2>/dev/null \
    | grep -c 'connect(' || true)
echo "non_loopback_connects=$EGRESS traced_connects(awsh)=$TRACED_CONNECTS" >> "$LOG"

# 5. negative case: offline off + a cloud url
printf 'offline: false\nmcp_url: https://gateway.example.com/mcp\n' > /etc/awsh/shell.yaml
step neg-awnix-awsh-doctor env AITHER_OFFLINE=0 "$CLI" doctor --json
NEG_AWNIX=$RC
step neg-awsh-doctor env AITHER_OFFLINE=0 awsh doctor --json
NEG_AWSH=$RC
rm -f /etc/awsh/shell.yaml

kill "$AGENT_PID" "$HARNESS_PID" "$STUB_PID" 2>/dev/null || true
pkill -f 'adk.server' 2>/dev/null || true
pkill -f 'adk.cli harness' 2>/dev/null || true

verdict=PASS
code=0
# Could not judge: a route off the box, the harness never came up, or a trace that
# cannot see the control's deliberate non-loopback connect (a 0 would mean nothing).
if [ "$HARNESS_UP" != 1 ] || [ "$DEFAULT_ROUTES" != 0 ] || [ "${CONTROL:-0}" -lt 1 ] \
    || [ "${TRACED_CONNECTS:-0}" -lt 1 ]; then
    verdict=COULD-NOT-JUDGE; code=2
fi
if [ "$code" = 0 ]; then
    if [ "$POS_AWNIX" != 0 ] || [ "$POS_AWSH" != 0 ] || [ "$NON_LOOPBACK" != 0 ] \
        || [ "$ONESHOT" != 0 ] || [ "$NEG_AWNIX" != 1 ] || [ "$NEG_AWSH" != 1 ] \
        || [ "$AGENT_UP" != 1 ] || [ "$AGENT_LISTENING" -lt 1 ] || [ "$EGRESS" != 0 ]; then
        verdict=FAIL; code=1
    fi
fi

"$PY" - "$OUT" "$verdict" "$code" "$DEFAULT_ROUTES" "$NON_LOOPBACK" \
    "$POS_AWNIX" "$POS_AWSH" "$NEG_AWNIX" "$NEG_AWSH" "$HARNESS_UP" "$ONESHOT" \
    "$AGENT_UP" "$AGENT_LISTENING" "${EGRESS:--1}" "${CONTROL:-0}" "${TRACED_CONNECTS:-0}" <<'EOF'
import json, sys
(out, verdict, code, routes, nonloop, pa, pw, na, nw, up, one,
 agent_up, agent_listen, egress, control, traced) = sys.argv[1:]
def load(name):
    try:
        with open("%s/%s.out" % (out, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
def num(s):
    try:
        return int(s)
    except ValueError:
        return -1
doc = load("awnix-awsh-doctor") or {}
rep = {
    "schema": 2, "verdict": verdict, "exit_code": num(code), "network": "none",
    "image": "node:22-bookworm stand-in (NOT an awnix or garg image)",
    "default_routes": num(routes), "non_loopback_listeners": num(nonloop),
    "harness_up": up == "1", "agent_up": agent_up == "1",
    "agent_listeners_9001": num(agent_listen),
    "non_loopback_connects": num(egress),
    "non_loopback_connects_scope": "strace connect() over both doctors, the "
                                   "one-shot, the harness daemon and the agent daemon",
    "trace_control_nonloopback_connects": num(control),
    "traced_connects_awsh": num(traced),
    "awnix_awsh_doctor_rc": num(pa), "awsh_doctor_rc": num(pw),
    "awsh_oneshot_rc": num(one),
    "negative_awnix_awsh_doctor_rc": num(na), "negative_awsh_doctor_rc": num(nw),
    "nonloopback_urls": doc.get("nonloopback_urls"), "harness_bind": doc.get("harness_bind"),
    "agent_bind": doc.get("agent_bind"), "model": doc.get("model"),
}
with open("%s/verdict.json" % out, "w", encoding="utf-8") as fh:
    json.dump(rep, fh, indent=2)
print(json.dumps(rep, indent=2))
EOF

echo "AWSH-OFFLINE-PROOF: $verdict"
exit "$code"
