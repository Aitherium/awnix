# /etc/profile.d/awsh-offline.sh -- keep awsh on this box's loopback.
#
# awsh 1.19+ reads the config layers itself (/usr/lib/awsh/shell.yaml ...). Older awsh
# (1.18.x) reads only ~/.aither/shell.yaml and, with no local backend answering, falls
# back to a cloud gateway. These exports make that fallback land on loopback too, so an
# air-gapped box never dials out whichever awsh version the image carries.
# Every value is a default: anything the user already exported wins.

_awsh_layer_value() {
    # Last `key: value` for $1 across the layers, lowest to highest precedence.
    _awsh_v=""
    for _awsh_f in /usr/lib/awsh/shell.yaml /usr/lib/awsh/shell.d/*.yaml \
                   /etc/awsh/shell.yaml "$HOME/.aither/shell.yaml"; do
        [ -r "$_awsh_f" ] || continue
        _awsh_line=$(tr -d '\r' < "$_awsh_f" | grep -E "^$1:[[:space:]]*" | tail -n 1)
        [ -n "$_awsh_line" ] || continue
        _awsh_v=$(printf '%s' "$_awsh_line" | sed -e "s/^$1:[[:space:]]*//" \
            -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//")
    done
    printf '%s' "$_awsh_v"
}

_awsh_offline=$(_awsh_layer_value offline)
case "${AITHER_OFFLINE:-$_awsh_offline}" in
    1|true|yes|on)
        export AITHER_OFFLINE=1
        _awsh_llm=$(_awsh_layer_value llm_url)
        export AITHER_LLM_URL="${AITHER_LLM_URL:-${_awsh_llm:-http://127.0.0.1:8199/v1}}"
        export AITHER_LOCAL_LLM_URL="${AITHER_LOCAL_LLM_URL:-${AITHER_LLM_URL%/v1}}"
        export AITHER_HARNESS_HOST="${AITHER_HARNESS_HOST:-127.0.0.1}"
        export AITHER_HARNESS_URL="${AITHER_HARNESS_URL:-http://127.0.0.1:8362}"
        # awsh 1.18.x cloud rung: point it at the local agent daemon, not the internet.
        export AITHER_CLOUD_URL="${AITHER_CLOUD_URL:-http://127.0.0.1:9001}"
        export AITHER_CLOUD_MCP_URL="${AITHER_CLOUD_MCP_URL:-http://127.0.0.1:9001/mcp}"
        export AITHER_CLOUD_IDENTITY_URL="${AITHER_CLOUD_IDENTITY_URL:-http://127.0.0.1:8115}"
        ;;
esac
unset _awsh_offline _awsh_llm _awsh_v _awsh_f _awsh_line
unset -f _awsh_layer_value 2>/dev/null || true
