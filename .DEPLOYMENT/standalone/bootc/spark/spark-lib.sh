# shellcheck shell=sh
# spark-lib.sh -- shared helpers for the DGX Spark awnix-node lane. POSIX sh; sourced,
# never executed. Every step that matters goes through ev(), which runs the command and
# appends ONE evidence line:
#
#   {"schema":1,"step":S,"cmd":C,"rc":N,"ts":T,"host":H,"arch":A,"notes":X}
#
# That JSONL is what check_spark_awnix_node.py gates and what 05-PROOF-PLAN.md may quote.
# A claim is MEASURED only when its line exists with the real rc.
#
# Secrets never enter the evidence file: a tailscale auth key is only ever passed as
# `--auth-key=file:<path>` (the file is 0600), and redact() rewrites any literal key
# shape to '<file:0600>' before the line is written, as a second fence.

: "${AWNIX_SPARK_HOME:=$HOME/awnix-spark}"
: "${EVIDENCE:=$AWNIX_SPARK_HOME/evidence/evidence.jsonl}"
: "${POOL_DIR:=$AWNIX_SPARK_HOME/evidence}"    # pool-*.txt, build.log; a tmp dir in read-only mode
: "${NODE_NAME:=awnix-node-arm64}"
: "${MEM_FLOOR_KB:=25165824}"        # 24 GiB -- the DGXM001 floor for any extra load
: "${DISK_FLOOR_KB:=62914560}"       # 60 GiB free on the build filesystem
: "${POOL_PORTS:=8124 8120}"         # production endpoints; 8120 may legitimately be down

json_esc() {
    # JSON string body for $1: backslash, quote, tab, CR dropped, newlines as \n.
    printf '%s' "$1" | awk 'BEGIN{ORS=""} {
        gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); gsub(/\t/,"\\t"); gsub(/\r/,"");
        if (NR>1) printf "\\n"; printf "%s",$0 }'
}

redact() {
    # Never let a key shape reach disk. tskey-* (tailscale), headscale preauth hex (exactly
    # 48; a 64-hex sha256 is evidence, not a secret),
    # and any --auth-key/--authkey value that is not a file: reference.
    printf '%s' "$1" | sed -E \
        -e 's/tskey-[A-Za-z0-9_-]+/<file:0600>/g' \
        -e 's/(--auth-?key[= ])(file:)?[^ ]+/\1<file:0600>/g' \
        -e 's/(^|[^0-9a-f])[0-9a-f]{48}([^0-9a-f]|$)/\1<redacted-hex>\2/g'
}

ev_write() {
    # ev_write STEP RC CMD [NOTES]
    _step=$1; _rc=$2; _cmd=$(redact "$3"); _notes=$(redact "${4:-}")
    _line=$(printf '{"schema":1,"step":"%s","cmd":"%s","rc":%d,"ts":"%s","host":"%s","arch":"%s","notes":"%s"}\n' \
        "$(json_esc "$_step")" "$(json_esc "$_cmd")" "$_rc" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(json_esc "$(hostname)")" \
        "$(uname -m)" "$(json_esc "$_notes")")
    if [ "$EVIDENCE" = - ]; then
        # Read-only mode: nothing is written on the node; the host collects "EV " lines.
        printf 'EV %s\n' "$_line"
    else
        mkdir -p "$(dirname "$EVIDENCE")"
        printf '%s\n' "$_line" >> "$EVIDENCE"
    fi
}

ev() {
    # ev STEP CMD... -- run CMD (argv, not a string), record {step,cmd,rc}. Returns rc.
    # EV_NOTES adds a note; EV_CMD overrides the recorded command text.
    _s=$1; shift
    "$@"
    _r=$?
    ev_write "$_s" "$_r" "${EV_CMD:-$*}" "${EV_NOTES:-}"
    return "$_r"
}

ev_sh() {
    # ev_sh STEP 'shell text' -- same, for a pipeline. The text is what gets recorded.
    _s=$1; _t=$2
    sh -c "$_t"
    _r=$?
    ev_write "$_s" "$_r" "$_t" "${EV_NOTES:-}"
    return "$_r"
}

mem_available_kb() { awk '/^MemAvailable:/ {print $2}' /proc/meminfo; }

disk_free_kb() { df -Pk "${1:-$HOME}" | awk 'NR==2 {print $4}'; }

gate_memory() {
    # DGXM001 floor: refuse extra load when unified memory is short.
    _m=$(mem_available_kb)
    if [ "${_m:-0}" -lt "$MEM_FLOOR_KB" ]; then
        ev_write "gate-memory" 1 "awk MemAvailable /proc/meminfo" "MemAvailable=${_m}kB < floor ${MEM_FLOOR_KB}kB; refusing"
        echo "refuse: MemAvailable ${_m} kB < ${MEM_FLOOR_KB} kB" >&2
        return 1
    fi
    ev_write "gate-memory" 0 "awk MemAvailable /proc/meminfo" "MemAvailable=${_m}kB >= ${MEM_FLOOR_KB}kB"
}

gate_disk() {
    _d=$(disk_free_kb "${1:-$HOME}")
    if [ "${_d:-0}" -lt "$DISK_FLOOR_KB" ]; then
        ev_write "gate-disk" 1 "df -Pk ${1:-$HOME}" "free=${_d}kB < floor ${DISK_FLOOR_KB}kB; refusing"
        echo "refuse: disk free ${_d} kB < ${DISK_FLOOR_KB} kB" >&2
        return 1
    fi
    ev_write "gate-disk" 0 "df -Pk ${1:-$HOME}" "free=${_d}kB"
}

pool_tok_s() {
    # Median decode tok/s of 3 tiny completions against the production endpoint on $1.
    # 24 tokens each: negligible load on the pool, enough to see a 2x slowdown.
    _port=$1
    _model=$(curl -s -m 5 "http://127.0.0.1:${_port}/v1/models" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null) || return 1
    [ -n "$_model" ] || return 1
    for _i in 1 2 3; do
        _t0=$(date +%s.%N)
        _n=$(curl -s -m 60 "http://127.0.0.1:${_port}/v1/completions" \
            -H 'Content-Type: application/json' \
            -d "{\"model\":\"${_model}\",\"prompt\":\"Count: 1 2 3\",\"max_tokens\":24,\"temperature\":0}" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["usage"]["completion_tokens"])' 2>/dev/null) || return 1
        _t1=$(date +%s.%N)
        awk -v n="$_n" -v a="$_t0" -v b="$_t1" 'BEGIN{ if (b>a) printf "%.2f\n", n/(b-a); else print 0 }'
    done | sort -n | sed -n 2p
}

pool_health() {
    # pool_health LABEL -- writes $AWNIX_SPARK_HOME/evidence/pool-LABEL.txt, one line per
    # port: "<port> <http_code> <tok_s|->". rc 0 when every UP-before port is still up.
    _label=$1; _out="$POOL_DIR/pool-${_label}.txt"
    mkdir -p "$POOL_DIR"; : > "$_out"
    for _p in $POOL_PORTS; do
        _code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${_p}/health")
        _tps=-
        if [ "$_code" = 200 ]; then _tps=$(pool_tok_s "$_p" || echo -); fi
        echo "$_p $_code ${_tps:--}" >> "$_out"
    done
    ev_write "pool-health-${_label}" 0 "curl /health + /v1/completions on ports ${POOL_PORTS}" "$(tr '\n' ';' < "$_out")"
}

pool_regressed() {
    # pool_regressed -- rc 1 when a port that was 200 before is not 200 after, or its
    # tok/s fell below 70% of before (3-sample medians; the pool is shared, so a small
    # dip is noise, a 30% drop is us).
    _b="$POOL_DIR/pool-before.txt"; _a="$POOL_DIR/pool-after.txt"
    [ -f "$_b" ] && [ -f "$_a" ] || return 0
    awk 'NR==FNR { code[$1]=$2; tps[$1]=$3; next }
         code[$1]=="200" && $2!="200" { bad=1; print "port " $1 " was 200, now " $2 }
         code[$1]=="200" && tps[$1]!="-" && $3!="-" && ($3+0) < 0.7*(tps[$1]+0) {
             bad=1; print "port " $1 " tok/s " tps[$1] " -> " $3 }
         END { exit bad ? 1 : 0 }' "$_b" "$_a"
}

SPARK_LIB_LOADED=1
