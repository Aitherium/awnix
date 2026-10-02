#!/bin/sh
# spark-probe.sh -- READ-ONLY state of the DGX Spark before any awnix work.
# Changes nothing: no install, no pull, no restart, and by default NO FILE on the node --
# evidence goes to stdout as "EV {json}" lines and the summary as one "PROBE {json}" line,
# which the host (spark_awnix_node.py probe) collects. Scratch lives in a mktemp dir that
# is removed on exit. Runs from stdin too: `cat spark-lib.sh spark-probe.sh | ssh spark sh -s`.
# Exit 0 probed; 2 could not judge (no /proc/meminfo, no python3 to write the JSON).
set -u
: "${EVIDENCE:=-}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
POOL_DIR=$T
if [ -z "${SPARK_LIB_LOADED:-}" ]; then
  HERE=$(cd "$(dirname "$0")" && pwd)
  . "$HERE/spark-lib.sh"
fi
OUT="$T/probe.json"
[ -r /proc/meminfo ] || { echo "no /proc/meminfo" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 missing" >&2; exit 2; }
ev probe-uname sh -c "uname -m > $T/arch; uname -r > $T/kernel"
ev probe-nproc sh -c "nproc > $T/nproc"
ev probe-meminfo sh -c "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo > $T/mem"
ev probe-loadavg sh -c "cat /proc/loadavg > $T/load"
ev probe-disk sh -c "df -Pk $HOME > $T/df"
EV_NOTES="GPU residents (unified memory: not visible in ps RSS)" \
  ev probe-gpu-apps sh -c "nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader > $T/gpu 2>&1"
ev probe-docker-ps sh -c "docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' > $T/docker 2>&1"
if command -v podman >/dev/null 2>&1; then
  ev probe-podman-ps sh -c "podman ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' > $T/podman 2>&1"
  ev probe-podman-images sh -c "podman images --format '{{.Repository}}:{{.Tag}}|{{.Size}}' > $T/podimg 2>&1"
else
  ev_write probe-podman 127 "command -v podman" "podman not installed on the host"
  : > "$T/podman"; : > "$T/podimg"
fi
ev probe-tailscale sh -c "tailscale status --self --peers=false > $T/ts 2>&1; tailscale ip -4 > $T/tsip 2>&1"
pool_health probe

python3 - "$T" "$OUT" "$POOL_DIR/pool-probe.txt" <<'PY'
import json, os, sys
t, out, pool = sys.argv[1:4]
def rd(n):
    try:
        return open(os.path.join(t, n)).read().strip()
    except OSError:
        return ""
mem = dict(l.split(":", 1) for l in rd("mem").splitlines() if ":" in l)
df = rd("df").splitlines()
load = rd("load").split()
doc = {
    "schema": 1,
    "arch": rd("arch"), "kernel": rd("kernel"), "nproc": int(rd("nproc") or 0),
    "mem_total_kb": int(mem.get("MemTotal", "0 kB").split()[0]),
    "mem_available_kb": int(mem.get("MemAvailable", "0 kB").split()[0]),
    "loadavg": [float(x) for x in load[:3]] if load else None,
    "disk_free_kb": int(df[1].split()[3]) if len(df) > 1 else None,
    "gpu_apps": [l for l in rd("gpu").splitlines() if l],
    "docker": [l for l in rd("docker").splitlines() if l],
    "podman": [l for l in rd("podman").splitlines() if l],
    "podman_images": [l for l in rd("podimg").splitlines() if l],
    "tailscale_self": rd("ts"), "tailscale_ip4": rd("tsip"),
    "pool": [l.split() for l in open(pool).read().splitlines()] if os.path.exists(pool) else [],
}
json.dump(doc, open(out, "w"), indent=2)
print("PROBE " + json.dumps(doc, separators=(",", ":")))
PY
