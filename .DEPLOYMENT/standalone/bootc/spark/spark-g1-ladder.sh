#!/bin/sh
# spark-g1-ladder.sh PRISM_TAR PRISM_SHA256 MODEL_GGUF MODEL_SHA256 [RUNS] -- the G1 / Step 7
# ARM64 row:
# offline CPU inference on the DGX Spark, in a --network=none container.
#
#   server  PrismML llama.cpp (prism-b10685 ubuntu-arm64 CPU build, the pin every awnix
#           image carries) in ubuntu:24.04, --network=none, -ngl 0, --cpus 8 --memory 4g,
#           bound to 127.0.0.1 inside its own empty namespace
#   client  python:3.12-slim joined to the SAME namespace (--network=container:srv): it can
#           reach 127.0.0.1:8080 and nothing else; it first proves egress FAILS (TCP to
#           1.1.1.1:443 and 8.8.8.8:53, DNS), then runs RUNS (default 5) fixed completions
#   memory  VmHWM of the server (PID 1 in its container), read from /proc after the runs
#
# Writes $AWNIX_SPARK_HOME/evidence/ladder-runs.json. The row is labelled "clean host" only
# when all three loadavg samples before the start are < 1.0 (the Step 7 rule), otherwise
# "loaded host". Watts are ABSENT: there is no power meter on the Spark.
# Images are pulled BEFORE the offline run (the pull is a separate, recorded step).
# Exit 0 row measured; 1 egress succeeded / server failed / prism or model sha mismatch / pool regressed;
# 2 could not judge (no podman, inputs missing).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/spark-lib.sh"
PRISM_TAR=${1:-}; PRISM_SHA=${2:-}; MODEL=${3:-}; SHA=${4:-}; RUNS=${5:-5}
MODEL_ID=${MODEL_ID:-bonsai-1.7b}
SRV=awnix-ladder-srv
SRV_IMG=${SRV_IMG:-docker.io/library/ubuntu:24.04}
CLI_IMG=${CLI_IMG:-docker.io/library/python:3.12-slim}
L="$AWNIX_SPARK_HOME/ladder"
OUT="$AWNIX_SPARK_HOME/evidence/ladder-runs.json"
command -v podman >/dev/null 2>&1 || { ev_write ladder 2 "command -v podman" "podman absent"; exit 2; }
[ -f "$PRISM_TAR" ] && [ -n "$PRISM_SHA" ] && [ -f "$MODEL" ] && [ -n "$SHA" ]   || { echo "usage: $0 PRISM_TAR PRISM_SHA256 MODEL_GGUF MODEL_SHA256 [RUNS]" >&2; exit 2; }

# Both inputs are pinned: the llama-server binary is EXECUTED, so it is verified like the model.
PGOT=$(sha256sum "$PRISM_TAR" | awk '{print $1}')
ev_write ladder-prism-sha "$([ "$PGOT" = "$PRISM_SHA" ] && echo 0 || echo 1)" "sha256sum $(basename "$PRISM_TAR")" "got=$PGOT want=$PRISM_SHA"
[ "$PGOT" = "$PRISM_SHA" ] || exit 1

GOT=$(sha256sum "$MODEL" | awk '{print $1}')
ev_write ladder-model-sha "$([ "$GOT" = "$SHA" ] && echo 0 || echo 1)" "sha256sum $(basename "$MODEL")" "got=$GOT want=$SHA"
[ "$GOT" = "$SHA" ] || exit 1

gate_memory || exit 1
LOAD_BEFORE=""
for _i in 1 2 3; do LOAD_BEFORE="$LOAD_BEFORE $(cut -d' ' -f1 /proc/loadavg)"; sleep 5; done
ev_write ladder-load-before 0 "cut -d' ' -f1 /proc/loadavg (x3, 5s apart)" "load1=$LOAD_BEFORE"
pool_health before

rm -rf "$L"; mkdir -p "$L/prism"
ev ladder-prism-extract tar -xzf "$PRISM_TAR" -C "$L/prism" || exit 2
BIN=$(find "$L/prism" -name llama-server -type f | head -1)
[ -n "$BIN" ] || { ev_write ladder-prism-extract 2 "find llama-server" "not in tarball"; exit 2; }
BINDIR=$(dirname "$BIN")
GOMP=$(ls /usr/lib/aarch64-linux-gnu/libgomp.so.1 2>/dev/null || true)
ev ladder-pull podman pull -q "$SRV_IMG" || exit 2
ev ladder-pull podman pull -q "$CLI_IMG" || exit 2

cat > "$L/client.py" <<'PY'
import json, socket, statistics, sys, time, urllib.request
BASE = "http://127.0.0.1:8080"
RUNS = int(sys.argv[1])
def egress():
    out = {}
    for label, host, port in (("tcp_1.1.1.1:443", "1.1.1.1", 443), ("tcp_8.8.8.8:53", "8.8.8.8", 53)):
        s = socket.socket(); s.settimeout(5)
        try:
            s.connect((host, port)); out[label] = "CONNECTED (NETWORK PRESENT!)"
        except Exception as e:
            out[label] = "FAILED as expected: %r" % (e,)
        finally:
            s.close()
    try:
        socket.getaddrinfo("huggingface.co", 443); out["dns_huggingface.co"] = "RESOLVED (NETWORK PRESENT!)"
    except Exception as e:
        out["dns_huggingface.co"] = "FAILED as expected: %r" % (e,)
    return out
def ready(deadline=300):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    return round(time.time() - t0, 2)
        except Exception:
            pass
        time.sleep(1)
    return None
def run(prompt, n):
    body = json.dumps({"prompt": prompt, "n_predict": n, "stream": True, "temperature": 0.0,
                       "seed": 42, "cache_prompt": False}).encode()
    req = urllib.request.Request(BASE + "/completion", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter(); ttft = None; tim = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:])
            if d.get("content") and ttft is None:
                ttft = time.perf_counter() - t0
            if d.get("stop"):
                tim = d.get("timings") or {}; break
    wall = time.perf_counter() - t0
    tim = tim or {}
    return {"ttft_s": round(ttft, 4) if ttft else None, "tok_s": tim.get("predicted_per_second"),
            "decode_tokens": tim.get("predicted_n"), "prompt_ms": tim.get("prompt_ms"),
            "wall_s": round(wall, 3)}
def summ(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return None
    return {"median": round(statistics.median(xs), 4), "min": round(min(xs), 4), "max": round(max(xs), 4),
            "spread": round(max(xs) - min(xs), 4)}
res = {"egress": egress(), "server_ready_after_s": ready()}
if res["server_ready_after_s"] is None:
    print(json.dumps(res)); sys.exit(3)
with urllib.request.urlopen(BASE + "/props", timeout=10) as r:
    p = json.load(r)
res["props"] = {"model_path": p.get("model_path"), "build_info": p.get("build_info")}
prompt = ("You are an air-gapped edge node. In three short sentences, explain why running an AI model "
          "locally without any network connection matters for a disconnected field deployment.\n\nAnswer:")
res["warmup"] = run("Hello", 8)
res["runs"] = [run(prompt, 128) for _ in range(RUNS)]
res["summary"] = {k: summ([r[k] for r in res["runs"]]) for k in ("tok_s", "ttft_s", "wall_s")}
print(json.dumps(res))
PY

podman rm -f "$SRV" >/dev/null 2>&1
GOMP_MOUNT=""
[ -n "$GOMP" ] && GOMP_MOUNT="-v $GOMP:/opt/gomp/libgomp.so.1:ro"
# shellcheck disable=SC2086
ev ladder-server podman run -d --name "$SRV" --network=none --cpus 8 --memory 4g \
  -v "$BINDIR:/opt/prism:ro" $GOMP_MOUNT -v "$MODEL:/models/model.gguf:ro" \
  -e LD_LIBRARY_PATH=/opt/prism:/opt/gomp "$SRV_IMG" \
  /opt/prism/llama-server -m /models/model.gguf --host 127.0.0.1 --port 8080 -ngl 0 -t 8 -c 4096 || exit 1
NM=$(podman inspect "$SRV" --format '{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}')
ev_write ladder-isolation "$([ "${NM%%|*}" = none ] && echo 0 || echo 1)" "podman inspect $SRV --format '{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}'" "NetworkMode=${NM%%|*} PortBindings=${NM#*|}"

EV_CMD="podman run --rm --network=container:$SRV -v client.py:/client.py:ro $CLI_IMG python /client.py $RUNS" \
  ev ladder-client sh -c "podman run --rm --network=container:$SRV -v '$L/client.py:/client.py:ro' '$CLI_IMG' python /client.py $RUNS > '$L/client.json'"
CRC=$?
HWM=$(podman exec "$SRV" sh -c 'grep ^VmHWM /proc/1/status' 2>/dev/null | awk '{print $2}')
ev_write ladder-vmhwm "$([ -n "$HWM" ] && echo 0 || echo 1)" "podman exec $SRV grep ^VmHWM /proc/1/status" "vmhwm_kb=$HWM"
LOAD_AFTER=$(cut -d' ' -f1-3 /proc/loadavg)
ev ladder-server-rm podman rm -f "$SRV"
pool_health after
PREG=0; pool_regressed || PREG=1

python3 - "$L/client.json" "$OUT" "$MODEL_ID" "$SHA" "${NM%%|*}" "${HWM:-}" "$LOAD_BEFORE" "$LOAD_AFTER" "$CRC" "$PREG" "$RUNS" <<'PY'
import json, platform, sys
cj, out, mid, sha, nm, hwm, lb, la, crc, preg, runs = sys.argv[1:12]
try:
    c = json.load(open(cj))
except Exception as e:
    c = {"error": "client output unreadable: %r" % (e,)}
lb = [float(x) for x in lb.split()]
clean = bool(lb) and all(x < 1.0 for x in lb)
eg = c.get("egress") or {}
doc = {
    "schema": 1, "target": "dgx-spark-gb10-arm64-cpu", "model_id": mid, "model_sha256": sha,
    "arch": platform.machine(), "network_mode": nm, "threads": 8, "ngl": 0, "runs_requested": int(runs),
    "egress": eg, "egress_all_failed": bool(eg) and all(str(v).startswith("FAILED") for v in eg.values()),
    "props": c.get("props"), "server_ready_after_s": c.get("server_ready_after_s"),
    "runs": c.get("runs") or [], "summary": c.get("summary"),
    "vmhwm_kb": int(hwm) if hwm.isdigit() else None,
    "load_before": lb, "load_after": [float(x) for x in la.split()],
    "host_label": "clean host" if clean else "loaded host",
    "watts": None, "watts_note": "ABSENT: no power meter on the Spark",
    "client_rc": int(crc), "pool_regressed": preg == "1",
}
json.dump(doc, open(out, "w"), indent=2)
ok = int(crc) == 0 and doc["egress_all_failed"] and nm == "none" and len(doc["runs"]) == int(runs) and preg != "1"
print(json.dumps({"ok": ok, "summary": doc["summary"], "vmhwm_kb": doc["vmhwm_kb"], "host_label": doc["host_label"]}))
sys.exit(0 if ok else 1)
PY
