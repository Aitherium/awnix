#!/bin/bash
# awnix-update -- signed, channel-pinned updates for every awnix image (garg included).
#
# Installed as /usr/libexec/awnix/awnix-update and reached as `awnix update <verb>`
# through the dispatcher (/usr/bin/awnix, owned by the console gap).
#
#   awnix update status [--json]      the last verdict + what bootc says right now
#   awnix update check                resolve the channel, VERIFY, stage (never reboots)
#   awnix update apply                reboot into the staged update IF this program verified it
#   awnix update rollback             queue the previous deployment, reboot (detached)
#   awnix update channel stable|beta  pick the channel in /etc/awnix/update.conf
#   awnix update auto-apply on|off    let the timer reboot into a verified update
#   awnix update doctor               can this box update, and is the chain intact?
#   awnix update --self-test          every rule below, against stubbed bootc/skopeo/cosign
#   awnix update --list-verbs
#
# WHY A DIGEST AND A SIGNATURE, NOT `bootc upgrade`. `bootc upgrade` follows a TAG and
# trusts whatever the registry hands back. Keyless cosign cannot be expressed in
# containers/image's policy.json (its fulcio block only matches subjectEmail and a
# GitHub OIDC certificate carries a URI SAN), so the enforcement lives here: resolve
# <repo>:<channel> to ONE digest, `cosign verify` THAT digest against the workflow
# identity in /usr/share/awnix/signers.conf, then `bootc switch <repo>@<digest>`. What
# was verified is exactly what is staged. bootc-fetch-apply-updates.timer is masked so
# nothing else pulls, and it would reboot on its own besides.
#
# The registry credential is READ, never written: /etc/ostree/auth.json belongs to
# `aitheros license refresh` (the activation gap). When a license exists, check asks
# it to refresh first; a refused license stops the check before any pull.
#
# Exit: 0 judged (current, staged, or license-refused) · 1 a real failure or a refused
# image · 2 could not judge (offline, no credential, no release on the channel yet, usage).
set -u

STATUS="${AWNIX_UPDATE_STATUS:-/var/lib/awnix/update-status.json}"
# The ONE digest this program itself verified and staged. `bootc upgrade`, `bootc switch
# <tag>` or the stock bootc-fetch-apply-updates.timer can stage an image too, and none of
# them verify anything -- so "something is staged" is never proof, only a match here is.
VERIFIED="${AWNIX_UPDATE_VERIFIED:-/var/lib/awnix/verified-staged-digest}"
CONF="${AWNIX_UPDATE_CONF:-/etc/awnix/update.conf}"
AUTH="${AWNIX_UPDATE_AUTH:-/etc/ostree/auth.json}"
SIGNERS="${AWNIX_SIGNERS:-/usr/share/awnix/signers.conf}"
RELEASE_ENV="${AWNIX_RELEASE_ENV:-/usr/lib/awnix/release.env}"
LICENSE_FILE="${AWNIX_LICENSE_FILE:-/etc/aither/appliance.lic}"
POLICY="${AWNIX_POLICY:-/etc/containers/policy.json}"
REGISTRIES_D="${AWNIX_REGISTRIES_D:-/etc/containers/registries.d/aitherium.yaml}"
BOOTC="${AWNIX_BOOTC:-bootc}"
SKOPEO="${AWNIX_SKOPEO:-skopeo}"
COSIGN="${AWNIX_COSIGN:-cosign}"
AITHEROS="${AWNIX_AITHEROS:-aitheros}"
# The license lifecycle gate (aither-license-lifecycle(7)): absent on a stale image.
RENEWAL="${AWNIX_RENEWAL:-/usr/libexec/awnix/awnix-renewal}"
SYSTEMD_RUN="${AWNIX_SYSTEMD_RUN:-systemd-run}"
SYSTEMCTL="${AWNIX_SYSTEMCTL:-systemctl}"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
# Every network call is bounded: awnix-update.service is a oneshot, and a check that hangs
# keeps the unit "activating", so awnix-update.timer never fires again on that box.
NET_TIMEOUT="${AWNIX_UPDATE_NET_TIMEOUT:-300}"
# Portability contract: vendor < admin < process env, plain KEY=VALUE, no expansion.
ENDPOINT_FILES="${AWNIX_ENDPOINT_FILES:-/usr/lib/awnix/endpoints.env /etc/awnix/endpoints.env /usr/lib/gargbot/appliance.env /etc/gargbot/appliance.env}"

CHANNEL=stable
AUTO_APPLY=0

say()  { printf '%s\n' "$*"; }
bounded() {  # bounded <cmd...> -- run under NET_TIMEOUT when coreutils timeout exists
    if command -v timeout >/dev/null 2>&1; then timeout --kill-after=10 "$NET_TIMEOUT" "$@"; else "$@"; fi
}
warn() { printf 'awnix-update: %s\n' "$*" >&2; }
now()  { date -u +%FT%TZ; }

# ── python: bootc speaks JSON, and a sed JSON parser is how status files lie ────────
PY="${AWNIX_PYTHON:-}"
if [ -z "$PY" ]; then
    for c in python3.11 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY=$c; break; fi
    done
fi

# KEY=VALUE only. A sourced file can run code; update.conf is admin-edited, so read it.
read_kv() {  # read_kv <file> <key>  -> value of the LAST assignment, or nothing
    [ -r "$1" ] || return 0
    sed -n "s/^[[:space:]]*$2=[\"']\{0,1\}\([^\"']*\)[\"']\{0,1\}[[:space:]]*\$/\1/p" "$1" | tail -1 | tr -d '\r'
}

load_conf() {
    local v
    v=$(read_kv "$CONF" CHANNEL); [ -n "$v" ] && CHANNEL=$v
    v=$(read_kv "$CONF" AUTO_APPLY); [ -n "$v" ] && AUTO_APPLY=$v
    case "$AUTO_APPLY" in 1|yes|true|on) AUTO_APPLY=1 ;; *) AUTO_APPLY=0 ;; esac
}

set_conf() {  # set_conf <KEY> <VALUE> -- rewrite one key, keep every other line
    local dir tmp
    dir=$(dirname "$CONF"); mkdir -p "$dir" 2>/dev/null || return 1
    tmp="$CONF.tmp.$$"
    if [ -r "$CONF" ]; then grep -v "^[[:space:]]*$1=" "$CONF" > "$tmp" 2>/dev/null || true; else : > "$tmp"; fi
    printf '%s=%s\n' "$1" "$2" >> "$tmp" && mv "$tmp" "$CONF"
}

endpoint() {  # endpoint <KEY> <default> -- the portability chain, process env wins
    local v f val
    eval "v=\${$1:-}"
    if [ -n "$v" ]; then printf '%s' "$v"; return; fi
    val=""
    for f in $ENDPOINT_FILES; do
        v=$(read_kv "$f" "$1"); [ -n "$v" ] && val=$v
    done
    printf '%s' "${val:-$2}"
}

bootc_json() { "$BOOTC" status --json 2>/dev/null || "$BOOTC" status --format=json 2>/dev/null; }

bootc_field() {  # bootc_field <booted_digest|staged_digest|rollback|booted_ref|booted_transport>
    [ -n "$PY" ] || return 0
    bootc_json | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin) or {}
except Exception:
    sys.exit(0)
st = d.get("status") or {}
def img(slot):
    return ((st.get(slot) or {}).get("image") or {})
want = sys.argv[1]
if want == "booted_digest":
    print(img("booted").get("imageDigest") or "")
elif want == "staged_digest":
    print(img("staged").get("imageDigest") or "")
elif want == "rollback":
    print("true" if st.get("rollback") else "false")
elif want == "booted_ref":
    print((img("booted").get("image") or {}).get("image") or "")
elif want == "booted_transport":
    print((img("booted").get("image") or {}).get("transport") or "")
' "$1" 2>/dev/null
}

image_repo() {
    # The repo this box follows: what it booted from, else what its image says it is.
    local ref transport
    transport=$(bootc_field booted_transport)
    ref=$(bootc_field booted_ref)
    case "$transport" in registry|docker|"") ;; *) ref="" ;; esac
    ref=${ref#docker://}
    ref=${ref%%@*}
    # strip a :tag, but not a registry :port
    case "${ref##*/}" in *:*) ref=${ref%:*} ;; esac
    [ -n "$ref" ] || ref=$(read_kv "$RELEASE_ENV" AWNIX_IMAGE_REPO)
    # An admin override (a mirror, or the update drill's local registry) wins over both.
    local override
    override=$(read_kv "$CONF" IMAGE_REPO)
    [ -n "$override" ] && ref=$override
    printf '%s' "$ref"
}

signer_for() {  # signer_for <repo> -> identity regexp from signers.conf
    [ -r "$SIGNERS" ] || return 0
    awk -v r="$1" '$0 !~ /^[[:space:]]*#/ && $1 == r { print $2; exit }' "$SIGNERS" | tr -d '\r'
}

# ── the status file: one writer, atomic, parsed by python so it is always JSON ─────
S_BOOTED=""; S_AVAIL=""; S_STAGED=""; S_SIGNER=""; S_ROLLBACK=false
# The shared contract (update-channels-and-status, read by the console's types.ts and by
# /var/lib/gargbot/update-status.json readers) fixes `state` to: current|staged|
# unsigned-refused|auth-refused|license-refused|offline|error|no-credential|never-checked.
# Finer verdicts ride in `reason`, so no reader meets a state it does not know.
contract_state() {  # contract_state <verdict> -> the contract enum value for it
    case "$1" in
        rolled-back)         printf staged ;;            # the previous deployment is queued to boot
        staged-unverified)   printf unsigned-refused ;;  # a foreign stage that did not verify: refused
        channel-unpublished) printf offline ;;           # could not judge (exit 2): nothing to resolve yet
        *)                   printf '%s' "$1" ;;
    esac
}

record() {  # record <verdict> <detail>
    S_BOOTED=${S_BOOTED:-$(bootc_field booted_digest)}
    S_STAGED=$(bootc_field staged_digest)
    S_ROLLBACK=$(bootc_field rollback); S_ROLLBACK=${S_ROLLBACK:-false}
    mkdir -p "$(dirname "$STATUS")" 2>/dev/null
    if [ -n "$PY" ]; then
        U_STATE="$(contract_state "$1")" U_REASON="$1" U_DETAIL="$2" U_AT="$(now)" U_CHANNEL="$CHANNEL" U_BOOTED="$S_BOOTED" \
        U_AVAIL="$S_AVAIL" U_STAGED="$S_STAGED" U_SIGNER="$S_SIGNER" U_RB="$S_ROLLBACK" U_AUTO="$AUTO_APPLY" \
        "$PY" -c '
import json, os
e = os.environ
doc = {
    "checked_at": e["U_AT"], "state": e["U_STATE"], "channel": e["U_CHANNEL"],
    "booted_digest": e["U_BOOTED"] or None, "available_digest": e["U_AVAIL"] or None,
    "staged_digest": e["U_STAGED"] or None, "signer_identity": e["U_SIGNER"] or None,
    "rollback_available": e["U_RB"] == "true", "auto_apply": e["U_AUTO"] == "1",
    "detail": e["U_DETAIL"], "reason": e["U_REASON"],
}
print(json.dumps(doc, sort_keys=True))
' > "$STATUS.tmp" 2>/dev/null && mv "$STATUS.tmp" "$STATUS"
    else
        warn "no python; status file not written"
    fi
    say "$1: $2"
}

schedule_reboot() {
    # Detached, so the console (or ssh session) that asked gets its answer first.
    if [ -n "${AWNIX_NO_REBOOT:-}" ]; then say "would reboot in 5s"; return 0; fi
    "$SYSTEMD_RUN" --on-active=5 --unit=awnix-update-reboot "$SYSTEMCTL" reboot >/dev/null 2>&1 \
        || { warn "could not schedule the reboot"; return 1; }
    say "rebooting in 5 seconds"
}

# ── check ──────────────────────────────────────────────────────────────────────────
resolve_digest() {  # resolve_digest <repo> <tag>  -> sha256:..., rc 0; err text on stderr
    local raw rc auth=()
    [ -s "$AUTH" ] && auth=(--authfile "$AUTH")
    raw=$(bounded "$SKOPEO" inspect --raw "${auth[@]}" "docker://$1:$2" 2>&1); rc=$?
    [ $rc -eq 124 ] && raw="skopeo timed out after ${NET_TIMEOUT}s"
    [ $rc -eq 0 ] || { printf '%s' "$raw" >&2; return 1; }
    printf '%s' "$raw" | RAW_ARCH="${AWNIX_ARCH:-amd64}" "$PY" -c '
import hashlib, json, os, sys
raw = sys.stdin.buffer.read()
try:
    d = json.loads(raw)
except Exception:
    sys.exit(3)
mt = d.get("mediaType", "")
if "manifests" in d:  # an index: bootc boots the platform manifest, so compare that
    for m in d["manifests"]:
        p = m.get("platform") or {}
        if p.get("architecture") == os.environ["RAW_ARCH"] and p.get("os", "linux") == "linux":
            print(m["digest"]); sys.exit(0)
    sys.exit(4)
print("sha256:" + hashlib.sha256(raw).hexdigest())
'
}

verified_digest() { [ -r "$VERIFIED" ] && tr -d ' \r\n' < "$VERIFIED"; }

mark_verified() {  # mark_verified <digest> -- only after cosign said yes AND bootc staged it
    mkdir -p "$(dirname "$VERIFIED")" 2>/dev/null
    printf '%s\n' "$1" > "$VERIFIED.tmp" 2>/dev/null && mv "$VERIFIED.tmp" "$VERIFIED"
}

# verify_digest <repo> <digest>: 0 signed by the configured signer (S_SIGNER set) ·
# 1 refused (V_STATE/V_DETAIL say why) · 2 could not reach the signature store.
V_STATE=""; V_DETAIL=""
verify_digest() {
    local repo=$1 digest=$2 signer vout tmpd err rc
    signer=$(signer_for "$repo")
    if [ -z "$signer" ]; then
        V_STATE=unsigned-refused
        V_DETAIL="no signer is configured for $repo in $SIGNERS -- refusing an image nobody vouches for"
        return 1
    fi
    command -v "$COSIGN" >/dev/null 2>&1 || [ -x "$COSIGN" ] || {
        V_STATE=error; V_DETAIL="cosign is not installed -- refusing to stage an unverified image"; return 1; }
    tmpd=$(mktemp -d) || { V_STATE=error; V_DETAIL="no temp dir"; return 1; }
    [ -s "$AUTH" ] && cp "$AUTH" "$tmpd/config.json" 2>/dev/null
    local vargs=()
    case "$signer" in
        key:*)
            # A pinned public key (Phase B co-sign, and the update drill's ephemeral key).
            vargs=(--key "${signer#key:}")
            [ "$(read_kv "$CONF" COSIGN_IGNORE_TLOG)" = 1 ] && vargs+=(--insecure-ignore-tlog=true) ;;
        *)  vargs=(--certificate-identity-regexp "$signer" --certificate-oidc-issuer "$OIDC_ISSUER") ;;
    esac
    # A systemd system unit runs with no $HOME, and keyless verify needs a writable TUF
    # cache for the sigstore trust root; give it one on /var (persistent across deploys).
    vout=$(DOCKER_CONFIG="$tmpd" HOME="${HOME:-/root}" TUF_ROOT="${TUF_ROOT:-/var/lib/awnix/sigstore}" \
            bounded "$COSIGN" verify --output json "${vargs[@]}" \
            "$repo@$digest" 2>"$tmpd/err"); rc=$?
    err=$(tail -1 "$tmpd/err" 2>/dev/null); rm -rf "$tmpd"
    [ $rc -eq 124 ] && err="cosign timed out after ${NET_TIMEOUT}s"
    if [ $rc -ne 0 ]; then
        case "$err" in
            *resolve*|*"timed out"*|*onnection*|*"no such host"*)
                V_STATE=offline; V_DETAIL="could not reach the signature store to verify $digest: $err"; return 2 ;;
        esac
        V_STATE=unsigned-refused; V_DETAIL="$repo@$digest has no valid signature from $signer ($err)"; return 1
    fi
    S_SIGNER=$(printf '%s' "$vout" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    o = (d[0].get("optional") or {}) if d else {}
    print(o.get("Subject") or o.get("subject") or "")
except Exception:
    print("")
' 2>/dev/null)
    S_SIGNER=${S_SIGNER:-$signer}
    return 0
}

cmd_check() {
    local repo digest err rc tmpd
    load_conf
    case "$CHANNEL" in stable|beta) ;; *) record error "CHANNEL=$CHANNEL in $CONF is not stable|beta"; return 1 ;; esac
    [ -n "$PY" ] || { warn "no python3 on this box -- cannot parse registry or bootc JSON"; return 2; }

    # 1. The license decides whether this box may pull at all (activation owns it).
    if [ -e "$LICENSE_FILE" ]; then
        bounded "$AITHEROS" license refresh --quiet >/dev/null 2>&1; rc=$?
        [ $rc -eq 124 ] && rc=2  # timed out: could not judge, carry on
        case "$rc" in
            0) ;;
            1) record license-refused "the license was refused (invalid, expired or revoked) -- updates stop, the product keeps running; see 'aitheros status'"
               return 0 ;;
            *) : ;;  # offline or could not judge: carry on with the credential we have
        esac
    fi
    # 1b. The lifecycle: past the 30-day grace after expiry, VENDOR updates pause (the
    #     product keeps serving). `awnix renewal gate updates` exits 1 to pause and 2 when it
    #     cannot judge (carry on, exactly as an offline refresh does).
    if [ -x "$RENEWAL" ]; then
        bounded "$RENEWAL" gate updates >/dev/null 2>&1; rc=$?
        if [ "$rc" -eq 1 ]; then
            record license-refused "the license lapsed past its 30-day grace -- updates pause until a renewed license is imported; the product keeps running (see 'awnix renewal status')"
            return 0
        fi
    fi

    # 2. Staged already? Nothing to fetch.
    S_BOOTED=$(bootc_field booted_digest)
    repo=$(image_repo)
    [ -n "$repo" ] || { record error "cannot tell which image this box follows (bootc status has no registry ref and $RELEASE_ENV has no AWNIX_IMAGE_REPO)"; return 1; }

    # 3. channel -> ONE digest
    tmpd=$(mktemp -d) || { record error "no temp dir"; return 1; }
    digest=$(resolve_digest "$repo" "$CHANNEL" 2>"$tmpd/err"); rc=$?
    err=$(cat "$tmpd/err" 2>/dev/null); rm -rf "$tmpd"
    if [ $rc -ne 0 ] || [ -z "$digest" ]; then
        case "$err" in
            *401*|*403*|*denied*|*nauthorized*|*"authentication required"*)
                if [ -s "$AUTH" ]; then
                    record auth-refused "the registry refused this box's credential for $repo:$CHANNEL -- run 'aitheros license refresh'"; return 1
                fi
                record no-credential "$repo is private and there is no credential at $AUTH -- activate a license ('aitheros login --license ...')"; return 2 ;;
            *resolve*|*"timed out"*|*timeout*|*unreachable*|*onnection*|*"no such host"*)
                record offline "could not reach the registry: $(printf '%s' "$err" | tail -1)"; return 2 ;;
            *"manifest unknown"*|*MANIFEST_UNKNOWN*|*"not found"*|*404*)
                # Nothing has been promoted to this channel yet. Not this box's failure and
                # not a reason to follow an unproven tag: keep running, look again later.
                record channel-unpublished "nothing is published to $repo:$CHANNEL yet -- this box keeps what it runs and checks again later"
                return 2 ;;
        esac
        record error "could not resolve $repo:$CHANNEL: $(printf '%s' "$err" | tail -1)"; return 1
    fi
    S_AVAIL=$digest

    if [ -n "$S_BOOTED" ] && [ "$digest" = "$S_BOOTED" ]; then
        record current "this box runs $repo:$CHANNEL ($digest)"; return 0
    fi
    if [ "$(bootc_field staged_digest)" = "$digest" ]; then
        # Staged already -- but by whom? Only a digest THIS program verified counts; one
        # staged by a raw bootc upgrade/switch or the stock fetch timer is verified now,
        # and never called verified until cosign says so.
        if [ "$(verified_digest)" != "$digest" ]; then
            verify_digest "$repo" "$digest"; rc=$?
            if [ $rc -ne 0 ]; then
                if [ $rc -eq 2 ]; then record offline "$V_DETAIL"; return 2; fi
                record staged-unverified "$repo@$digest was staged by something other than awnix update and does not verify -- it will not be applied ($V_DETAIL)"
                return 1
            fi
            mark_verified "$digest"
        fi
        record staged "$repo@$digest is verified and staged; it takes effect on the next reboot (awnix update apply)"
        [ "$AUTO_APPLY" = 1 ] && cmd_apply
        return 0
    fi

    # 4. VERIFY the digest we are about to stage. No signer, no cosign, no stage.
    verify_digest "$repo" "$digest"; rc=$?
    if [ $rc -ne 0 ]; then
        if [ $rc -eq 2 ]; then record offline "$V_DETAIL"; return 2; fi
        record "$V_STATE" "$V_DETAIL -- refused, nothing staged"; return 1
    fi

    # 5. Stage exactly what was verified. bootc switch never reboots on its own.
    err=$("$BOOTC" switch "$repo@$digest" 2>&1); rc=$?
    if [ $rc -ne 0 ] || [ "$(bootc_field staged_digest)" != "$digest" ]; then
        record error "verified $digest but could not stage it: $(printf '%s' "$err" | tail -1)"; return 1
    fi
    mark_verified "$digest"
    record staged "$repo@$digest is signed by $S_SIGNER and staged; it takes effect on the next reboot (awnix update apply)"
    [ "$AUTO_APPLY" = 1 ] && cmd_apply
    return 0
}

cmd_apply() {
    local st
    st=$(bootc_field staged_digest)
    [ -n "$st" ] || { warn "nothing is staged -- run 'awnix update check' first"; return 1; }
    # Reboot only into what awnix-update itself verified. Anything else was staged by a
    # path that checks no signature; `awnix update check` verifies it (or refuses it).
    if [ "$(verified_digest)" != "$st" ]; then
        warn "the staged image ($st) was not verified by awnix update -- run 'awnix update check'; refusing to reboot into it"
        return 1
    fi
    schedule_reboot
}

cmd_rollback() {
    local out rc
    load_conf
    [ "$(bootc_field rollback)" = "true" ] || { warn "there is no previous deployment to roll back to"; return 1; }
    out=$("$BOOTC" rollback 2>&1); rc=$?
    [ $rc -eq 0 ] || { record error "bootc rollback failed: $(printf '%s' "$out" | tail -1)"; return 1; }
    record rolled-back "the previous deployment is queued; rebooting into it"
    schedule_reboot
}

cmd_channel() {
    # No argument: print the current channel (the setup step's current_cmd reads this).
    if [ -z "${1:-}" ]; then load_conf; say "$CHANNEL"; return 0; fi
    case "${1:-}" in
        stable|beta) ;;
        *) warn "channel must be stable or beta (got '${1:-}')"; return 1 ;;
    esac
    set_conf CHANNEL "$1" || { warn "could not write $CONF"; return 1; }
    say "channel: $1 (takes effect on the next 'awnix update check')"
    [ "$1" = beta ] && say "beta receives every CI build before it is proven; stable moves only after the upgrade/rollback proof passes"
    return 0
}

cmd_auto_apply() {
    local v
    if [ -z "${1:-}" ]; then load_conf; if [ "$AUTO_APPLY" = 1 ]; then say on; else say off; fi; return 0; fi
    case "${1:-}" in on|1|yes|true) v=1 ;; off|0|no|false) v=0 ;; *) warn "auto-apply must be on or off"; return 1 ;; esac
    set_conf AUTO_APPLY "$v" || { warn "could not write $CONF"; return 1; }
    say "auto-apply: $([ "$v" = 1 ] && echo on || echo off)"
}

cmd_status() {
    load_conf
    if [ "${1:-}" = "--json" ]; then
        [ -n "$PY" ] || { say '{"state":"error","detail":"no python3"}'; return 2; }
        # The file is the last verdict; channel/auto_apply/rollback are re-read live so a
        # just-changed setting shows before the next check runs.
        U_FILE="$STATUS" U_CHANNEL="$CHANNEL" U_AUTO="$AUTO_APPLY" U_RB="$(bootc_field rollback)" \
        U_BOOTED="$(bootc_field booted_digest)" U_STAGED="$(bootc_field staged_digest)" "$PY" -c '
import json, os
e = os.environ
try:
    with open(e["U_FILE"], encoding="utf-8") as fh:
        doc = json.load(fh)
except Exception:
    doc = {"state": "never-checked", "checked_at": None, "available_digest": None,
           "signer_identity": None, "detail": "no update check has run on this box yet"}
doc["channel"] = e["U_CHANNEL"]
doc["auto_apply"] = e["U_AUTO"] == "1"
if e["U_RB"]:
    doc["rollback_available"] = e["U_RB"] == "true"
if e["U_BOOTED"]:
    doc["booted_digest"] = e["U_BOOTED"]
doc["staged_digest"] = e["U_STAGED"] or doc.get("staged_digest")
for k in ("booted_digest", "staged_digest", "rollback_available"):
    doc.setdefault(k, None)
print(json.dumps(doc, sort_keys=True))
'
        return 0
    fi
    say "channel     $CHANNEL"
    say "auto-apply  $([ "$AUTO_APPLY" = 1 ] && echo on || echo off)"
    say "booted      $(bootc_field booted_digest)"
    say "staged      $(bootc_field staged_digest)"
    if [ -r "$STATUS" ]; then say "last        $(cat "$STATUS")"; else say "last        never-checked"; fi
    return 0
}

http_code() { curl -s -o /dev/null --max-time 20 -w '%{http_code}' "$@" 2>/dev/null || printf 000; }

cmd_doctor() {
    local bad=0 c repo reg weights feedback origin
    load_conf
    repo=$(image_repo)
    say "info  image $repo  channel $CHANNEL  auto-apply $AUTO_APPLY"
    if command -v "$COSIGN" >/dev/null 2>&1; then say "ok    cosign present"; else say "FAIL  cosign missing -- no update can be verified, none will be staged"; bad=1; fi
    if command -v "$SKOPEO" >/dev/null 2>&1; then say "ok    skopeo present"; else say "FAIL  skopeo missing -- channels cannot be resolved"; bad=1; fi
    if [ -n "$PY" ] && "$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$POLICY" 2>/dev/null; then say "ok    $POLICY parses"
    else say "FAIL  $POLICY missing or not JSON"; bad=1; fi
    if [ -r "$REGISTRIES_D" ]; then say "ok    $REGISTRIES_D present"; else say "FAIL  $REGISTRIES_D missing (sigstore attachments not looked up)"; bad=1; fi
    if [ -n "$repo" ] && [ -n "$(signer_for "$repo")" ]; then say "ok    signer configured for $repo"
    else say "FAIL  no signer for '$repo' in $SIGNERS -- every update will be refused"; bad=1; fi
    c=$("$SYSTEMCTL" is-enabled bootc-fetch-apply-updates.timer 2>/dev/null)
    case "$c" in masked) say "ok    bootc-fetch-apply-updates.timer masked" ;;
                 *) say "FAIL  bootc-fetch-apply-updates.timer is '$c' -- it pulls unverified AND reboots"; bad=1 ;; esac
    c=$("$SYSTEMCTL" is-enabled awnix-update.timer 2>/dev/null)
    if [ "$(tr -d ' \r\n' < "${AWNIX_PROFILE_FILE:-/usr/lib/awnix/profile}" 2>/dev/null)" = airgap ]; then
        # awnix-zero-ports masks it: the timer would reach ghcr.io inside the no-egress window.
        case "$c" in masked) say "ok    awnix-update.timer masked (airgap profile: offline bundles only)" ;;
                     *) say "FAIL  awnix-update.timer is '$c' on the airgap profile -- it calls the registry"; bad=1 ;; esac
    else
        case "$c" in enabled) say "ok    awnix-update.timer enabled" ;; *) say "FAIL  awnix-update.timer is '$c' -- this box never looks"; bad=1 ;; esac
    fi
    origin=$(bootc_field booted_ref)
    case "$origin" in *@sha256:*) say "ok    booted from a verified digest" ;;
                      "") say "info  booted ref unknown" ;;
                      *) say "WARN  booted from a tag ($origin) -- a raw 'bootc upgrade' would bypass verification; use 'awnix update'" ;; esac
    if [ -s "$AUTH" ]; then say "ok    registry credential present ($AUTH)"
    else say "info  no registry credential -- fine for a public image, fatal for a private one"; fi
    reg=$(endpoint GARG_REGISTRY_PING "$(endpoint AWNIX_REGISTRY https://ghcr.io)/v2/")
    case "$reg" in */v2/) ;; *) reg="${reg%/}/v2/" ;; esac
    c=$(http_code "$reg")
    case "$c" in 200|401) say "ok    update registry reachable ($c) $reg" ;; *) say "FAIL  update registry unreachable ($c) -- $reg"; bad=1 ;; esac
    weights=$(endpoint GARG_WEIGHTS_PING "")
    if [ -n "$weights" ]; then
        c=$(http_code -r 0-3 "$weights")
        case "$c" in 200|206) say "ok    model mirror reachable ($c)" ;; *) say "FAIL  model mirror unreachable ($c) -- $weights"; bad=1 ;; esac
    fi
    feedback=$(endpoint GARG_FEEDBACK_PING "")
    if [ -n "$feedback" ]; then
        # HEAD, never POST: a diagnostic must not file an (empty) feedback submission.
        # 401/403/404/405 all prove the intake answers; only no answer or a 5xx fails.
        c=$(http_code -I "$feedback")
        case "$c" in 000|5??) say "FAIL  feedback intake unreachable ($c) -- $feedback"; bad=1 ;; *) say "ok    feedback intake reachable ($c)" ;; esac
    fi
    [ -r "$STATUS" ] && say "last  $(cat "$STATUS")"
    return $bad
}

# ── self-test: hermetic, stubbed, never touches the network ────────────────────────
# The self-tests assert on the exit status of a [ test ] on purpose.
# shellcheck disable=SC2319
self_test() {
    local tmp rc=0 got
    [ -n "$PY" ] || { say "SELF-TEST DEAD: no python"; return 2; }
    tmp=$(mktemp -d) || { say "SELF-TEST DEAD: no temp dir"; return 2; }
    STATUS="$tmp/status.json"; VERIFIED="$tmp/verified"; CONF="$tmp/update.conf"; AUTH="$tmp/auth.json"; SIGNERS="$tmp/signers.conf"
    RELEASE_ENV="$tmp/release.env"; LICENSE_FILE="$tmp/appliance.lic"; POLICY="$tmp/policy.json"
    REGISTRIES_D="$tmp/aitherium.yaml"; ENDPOINT_FILES="$tmp/endpoints.env"
    BOOTC="$tmp/bootc"; SKOPEO="$tmp/skopeo"; COSIGN="$tmp/cosign"; AITHEROS="$tmp/aitheros"
    SYSTEMD_RUN="$tmp/systemd-run"; SYSTEMCTL="$tmp/systemctl"; unset AWNIX_NO_REBOOT
    RENEWAL="$tmp/awnix-renewal"   # absent until arm 10b: never the box's real gate
    local REPO=ghcr.io/aitherium/awnix
    local D_N="sha256:1111111111111111111111111111111111111111111111111111111111111111"
    printf '%s ^https://github\\.com/Aitherium/awnix/\\.github/workflows/build-and-publish-images\\.yml@refs/heads/main$\n' "$REPO" > "$SIGNERS"
    printf 'AWNIX_VARIANT=awnix\nAWNIX_IMAGE_REPO=%s\n' "$REPO" > "$RELEASE_ENV"
    # bootc: state in files. booted = $tmp/b, staged = $tmp/s, rollback = $tmp/r
    cat > "$BOOTC" <<EOF
#!/bin/bash
T="$tmp"
case "\$1" in
  status)
    b=\$(cat "\$T/b" 2>/dev/null); s=\$(cat "\$T/s" 2>/dev/null); r=\$(cat "\$T/r" 2>/dev/null)
    st=null; [ -n "\$s" ] && st="{\"image\":{\"imageDigest\":\"\$s\"}}"
    rb=null; [ -n "\$r" ] && rb='{"image":{}}'
    echo "{\"status\":{\"booted\":{\"image\":{\"image\":{\"image\":\"$REPO:stable\",\"transport\":\"registry\"},\"imageDigest\":\"\$b\"}},\"staged\":\$st,\"rollback\":\$rb}}" ;;
  switch) echo "\$2" >> "\$T/switched"; [ -e "\$T/switch-fails" ] && exit 1; echo "\${2#*@}" > "\$T/s" ;;
  rollback) touch "\$T/rolled"; ;;
esac
EOF
    # skopeo: prints the manifest in \$tmp/manifest or fails with \$tmp/skopeo-err
    cat > "$SKOPEO" <<EOF
#!/bin/bash
touch "$tmp/skopeo-called"
[ -e "$tmp/skopeo-hang" ] && exec sleep 30
for a in "\$@"; do case "\$a" in docker://*) echo "\$a" > "$tmp/skopeo-ref" ;; esac; done
if [ -s "$tmp/skopeo-err" ]; then cat "$tmp/skopeo-err" >&2; exit 1; fi
printf '%s' "\$(cat "$tmp/manifest")"
EOF
    cat > "$COSIGN" <<EOF
#!/bin/bash
echo "\$*" > "$tmp/cosign-args"
[ -n "\$DOCKER_CONFIG" ] && [ -e "\$DOCKER_CONFIG/config.json" ] && touch "$tmp/cosign-had-auth"
if [ -e "$tmp/cosign-bad" ]; then echo "Error: no matching signatures" >&2; exit 1; fi
echo '[{"optional":{"Subject":"https://github.com/Aitherium/awnix/.github/workflows/build-and-publish-images.yml@refs/heads/main"}}]'
EOF
    cat > "$AITHEROS" <<EOF
#!/bin/bash
echo "\$*" > "$tmp/aitheros-args"; exit \$(cat "$tmp/lic-rc" 2>/dev/null || echo 0)
EOF
    printf '#!/bin/bash\necho "$*" > "%s/reboot-scheduled"\n' "$tmp" > "$SYSTEMD_RUN"
    # shellcheck disable=SC2016  # the stub expands $2 when IT runs
    printf '#!/bin/bash\ncase "$2" in bootc-fetch-apply-updates.timer) echo masked ;; *) echo enabled ;; esac\n' > "$SYSTEMCTL"
    chmod +x "$BOOTC" "$SKOPEO" "$COSIGN" "$AITHEROS" "$SYSTEMD_RUN" "$SYSTEMCTL"

    t() { if [ "$2" = "$3" ]; then :; else say "self-test FAIL: $1 (want '$3', got '$2')"; rc=1; fi; }
    state() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state",""))' "$STATUS" 2>/dev/null; }
    field() { "$PY" -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$STATUS" "$1" 2>/dev/null; }
    reset() { rm -f "$tmp/s" "$tmp/r" "$tmp/switched" "$tmp/switch-fails" "$tmp/skopeo-err" "$tmp/skopeo-called" \
                  "$tmp/cosign-bad" "$tmp/cosign-had-auth" "$tmp/reboot-scheduled" "$tmp/rolled" "$tmp/lic-rc" \
                  "$LICENSE_FILE" "$STATUS" "$VERIFIED" "$AUTH" "$CONF" "$tmp/cosign-args"; echo "$D_N" > "$tmp/b"; AUTO_APPLY=0; CHANNEL=stable; S_BOOTED=""; S_AVAIL=""; S_SIGNER=""; }
    manifest() { printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"digest":"sha256:%s"}}' "$1" > "$tmp/manifest"; }
    digest_of_manifest() { "$PY" -c 'import hashlib,sys; print("sha256:"+hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp/manifest"; }

    # 1. A public image with no credential resolves anonymously: current.
    reset; manifest aaa; digest_of_manifest > "$tmp/b"
    cmd_check >/dev/null 2>&1; t "the booted digest is current" "$?" "0"; t "  and says so" "$(state)" "current"
    grep -q "docker://$REPO:stable" "$tmp/skopeo-ref"; t "  the channel tag is what gets resolved" "$?" "0"

    # 2. A new, correctly signed digest is verified then STAGED by digest.
    reset; manifest bbb; want=$(digest_of_manifest)
    cmd_check >/dev/null 2>&1; t "a signed new digest stages" "$?" "0"; t "  state staged" "$(state)" "staged"
    t "  bootc switch got repo@digest, not a tag" "$(cat "$tmp/switched")" "$REPO@$want"
    grep -q -- "--certificate-oidc-issuer $OIDC_ISSUER" "$tmp/cosign-args"; t "  cosign pinned the GitHub OIDC issuer" "$?" "0"
    grep -q -- "$REPO@$want" "$tmp/cosign-args"; t "  cosign verified the SAME digest" "$?" "0"
    case "$(field signer_identity)" in https://github.com/Aitherium/awnix/*) got=ok ;; *) got=bad ;; esac
    t "  the signer identity is recorded" "$got" "ok"
    [ -e "$tmp/reboot-scheduled" ]; t "  check never reboots by default" "$?" "1"

    # 3. An unsigned digest is refused and NOTHING is staged.
    reset; manifest ccc; touch "$tmp/cosign-bad"
    cmd_check >/dev/null 2>&1; t "an unsigned digest is a failure" "$?" "1"; t "  state unsigned-refused" "$(state)" "unsigned-refused"
    [ -e "$tmp/switched" ]; t "  bootc switch was never called" "$?" "1"

    # 4. A repo with no signer line is refused before cosign even runs.
    reset; manifest ddd; : > "$SIGNERS.bak"; cp "$SIGNERS" "$SIGNERS.bak"; : > "$SIGNERS"
    cmd_check >/dev/null 2>&1; t "no signer configured is refused" "$(state)" "unsigned-refused"
    [ -e "$tmp/switched" ]; t "  and nothing staged" "$?" "1"; cp "$SIGNERS.bak" "$SIGNERS"

    # 5. Private repo, no credential: could-not-judge, named.
    reset; manifest eee; echo "Error: unauthorized: authentication required (401)" > "$tmp/skopeo-err"
    cmd_check >/dev/null 2>&1; t "private with no credential is could-not-judge" "$?" "2"; t "  state no-credential" "$(state)" "no-credential"
    # 6. With a credential that is refused: auth-refused, a failure.
    printf '{"auths":{"ghcr.io":{"auth":"eA=="}}}' > "$AUTH"; echo "Error: 403 denied" > "$tmp/skopeo-err"
    cmd_check >/dev/null 2>&1; t "a refused credential is a failure" "$?" "1"; t "  state auth-refused" "$(state)" "auth-refused"
    # 7. Offline.
    echo "Error: dial tcp: lookup ghcr.io: no such host" > "$tmp/skopeo-err"
    cmd_check >/dev/null 2>&1; t "offline is could-not-judge" "$?" "2"; t "  state offline" "$(state)" "offline"
    # 8. cosign reads the credential through a DOCKER_CONFIG copy, never the original path.
    rm -f "$tmp/skopeo-err"; manifest fff
    cmd_check >/dev/null 2>&1; [ -e "$tmp/cosign-had-auth" ]; t "cosign got the registry credential" "$?" "0"

    # 9. License refused: stop before any pull, exit 0, state license-refused.
    reset; manifest ggg; echo "AITHER1.x.y" > "$LICENSE_FILE"; echo 1 > "$tmp/lic-rc"
    cmd_check >/dev/null 2>&1; t "a refused license is judged (exit 0)" "$?" "0"; t "  state license-refused" "$(state)" "license-refused"
    [ -e "$tmp/skopeo-called" ]; t "  and the registry was never asked" "$?" "1"
    t "  the refresh was 'license refresh --quiet'" "$(cat "$tmp/aitheros-args")" "license refresh --quiet"
    # 10. License offline (2): carry on with the existing credential.
    echo 2 > "$tmp/lic-rc"
    cmd_check >/dev/null 2>&1; t "an offline license refresh still checks" "$(state)" "staged"
    # 10b. The lifecycle gate: lapsed past grace (1) pauses before any pull; 2 carries on.
    reset; manifest hhh; rm -f "$LICENSE_FILE"
    printf '#!/bin/sh\nexit 1\n' > "$RENEWAL"; chmod +x "$RENEWAL"
    cmd_check >/dev/null 2>&1; t "a lapsed license pauses updates (exit 0)" "$?" "0"; t "  state license-refused" "$(state)" "license-refused"
    [ -e "$tmp/skopeo-called" ]; t "  and the registry was never asked" "$?" "1"
    printf '#!/bin/sh\nexit 2\n' > "$RENEWAL"
    cmd_check >/dev/null 2>&1; t "a gate that cannot judge still checks" "$(state)" "staged"
    rm -f "$RENEWAL"

    # 11. apply: nothing staged refuses; staged schedules a DETACHED reboot and returns 0.
    reset
    cmd_apply >/dev/null 2>&1; t "apply with nothing staged refuses" "$?" "1"
    echo "sha256:feed" > "$tmp/s"
    cmd_apply >/dev/null 2>&1; t "apply refuses a staged digest awnix-update never verified" "$?" "1"
    [ -e "$tmp/reboot-scheduled" ]; t "  and schedules no reboot" "$?" "1"
    echo "sha256:feed" > "$VERIFIED"
    cmd_apply >/dev/null 2>&1; t "apply with a verified staged update returns 0 first" "$?" "0"
    grep -q -- "--on-active=5" "$tmp/reboot-scheduled"; t "  the reboot is scheduled, not immediate" "$?" "0"

    # 11b. Staged by someone else (raw bootc upgrade / the stock fetch timer) at exactly the
    # channel digest: verify it before calling it verified, never short-circuit.
    reset; manifest abc; digest_of_manifest > "$tmp/s"; touch "$tmp/cosign-bad"
    printf 'CHANNEL=stable\nAUTO_APPLY=1\n' > "$CONF"
    cmd_check >/dev/null 2>&1; t "an unverified foreign stage is refused" "$?" "1"
    t "  state unsigned-refused (contract enum)" "$(state)" "unsigned-refused"
    t "  reason staged-unverified" "$(field reason)" "staged-unverified"
    [ -e "$tmp/cosign-args" ]; t "  cosign was actually run on it" "$?" "0"
    [ -e "$tmp/reboot-scheduled" ]; t "  AUTO_APPLY did not reboot into it" "$?" "1"
    rm -f "$tmp/cosign-bad" "$CONF"
    cmd_check >/dev/null 2>&1; t "  a foreign stage that DOES verify is accepted" "$(state)" "staged"
    t "  and is recorded as verified" "$(cat "$VERIFIED")" "$(cat "$tmp/s")"

    # 11c. A channel nobody has promoted to yet is could-not-judge, not an error.
    reset; manifest ddd
    echo "Error: reading manifest stable in ghcr.io/aitherium/awnix: manifest unknown" > "$tmp/skopeo-err"
    cmd_check >/dev/null 2>&1; t "an unpublished channel is could-not-judge" "$?" "2"
    t "  state offline (contract enum)" "$(state)" "offline"
    t "  reason channel-unpublished" "$(field reason)" "channel-unpublished"
    # 11d. A hung registry call is bounded, never an "activating" unit forever.
    if command -v timeout >/dev/null 2>&1; then
        reset; manifest ddd; touch "$tmp/skopeo-hang"
        NET_TIMEOUT=1 cmd_check >/dev/null 2>&1; t "a hung skopeo is bounded (could-not-judge)" "$?" "2"
        t "  state offline" "$(state)" "offline"; rm -f "$tmp/skopeo-hang"
    fi
    # 12. AUTO_APPLY=1 applies a freshly staged update.
    reset; manifest hhh; printf 'CHANNEL=stable\nAUTO_APPLY=1\n' > "$CONF"
    cmd_check >/dev/null 2>&1; [ -e "$tmp/reboot-scheduled" ]; t "AUTO_APPLY=1 schedules the reboot" "$?" "0"

    # 13. rollback needs a previous deployment, then queues it and reboots detached.
    reset
    cmd_rollback >/dev/null 2>&1; t "rollback with no previous deployment refuses" "$?" "1"
    echo x > "$tmp/r"
    cmd_rollback >/dev/null 2>&1; t "rollback returns 0" "$?" "0"
    [ -e "$tmp/rolled" ] && [ -e "$tmp/reboot-scheduled" ]; t "  bootc rollback ran and a reboot was scheduled" "$?" "0"
    t "  state staged (contract enum)" "$(state)" "staged"; t "  reason rolled-back" "$(field reason)" "rolled-back"

    # 14. channel / auto-apply write the conf and nothing else.
    reset; printf '# admin note\nCHANNEL=stable\nFOO=bar\n' > "$CONF"
    cmd_channel beta >/dev/null 2>&1; t "channel beta is accepted" "$?" "0"
    t "  CHANNEL is now beta" "$(read_kv "$CONF" CHANNEL)" "beta"
    t "  other lines survive" "$(read_kv "$CONF" FOO)" "bar"
    cmd_channel lts >/dev/null 2>&1; t "channel lts is refused" "$?" "1"
    t "  and the conf is untouched" "$(read_kv "$CONF" CHANNEL)" "beta"
    cmd_auto_apply on >/dev/null 2>&1; t "auto-apply on writes 1" "$(read_kv "$CONF" AUTO_APPLY)" "1"
    cmd_auto_apply sometimes >/dev/null 2>&1; t "auto-apply garbage is refused" "$?" "1"
    t "channel with no argument prints the current one" "$(cmd_channel)" "beta"
    t "auto-apply with no argument prints on/off" "$(cmd_auto_apply)" "on"
    # a beta conf makes check resolve :beta
    manifest iii; rm -f "$tmp/skopeo-ref"; cmd_check >/dev/null 2>&1
    grep -q "docker://$REPO:beta" "$tmp/skopeo-ref"; t "the beta channel resolves :beta" "$?" "0"

    # 15. status --json always parses, including before the first check.
    reset
    got=$(cmd_status --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d["state"], d["channel"], d["auto_apply"])' 2>/dev/null)
    t "status --json before any check is never-checked" "$got" "never-checked stable False"
    manifest jjj; cmd_check >/dev/null 2>&1
    got=$(cmd_status --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d["state"], bool(d["staged_digest"]))' 2>/dev/null)
    t "status --json after a stage names the staged digest" "$got" "staged True"

    # 16. A stage that bootc does not actually record is an error, never 'staged'.
    reset; manifest kkk; touch "$tmp/switch-fails"
    cmd_check >/dev/null 2>&1; t "a failed switch is an error" "$(state)" "error"

    # 17. doctor passes on a complete, stubbed box (no network: pings pointed at a dead port).
    reset; echo '{"default":[{"type":"insecureAcceptAnything"}]}' > "$POLICY"; echo "docker: {}" > "$REGISTRIES_D"
    printf 'GARG_REGISTRY_PING=http://127.0.0.1:9/v2/\n' > "$tmp/endpoints.env"
    got=$(cmd_doctor 2>&1)
    case "$got" in *"FAIL  cosign"*|*"FAIL  no signer"*|*"FAIL  bootc-fetch"*|*"policy.json missing"*) t "doctor sees the chain intact" "broken" "intact" ;; esac
    case "$got" in *"127.0.0.1:9/v2/"*) : ;; *) t "doctor reads the registry ping from the endpoints chain" "missing" "present" ;; esac

    rm -rf "$tmp"
    [ "$rc" = 0 ] && say "self-test OK - current, signed-stage, unsigned-refused, no-signer, no-credential, auth-refused, offline, channel-unpublished, foreign-stage, license-refused, apply, auto-apply, rollback, channel and status rules all hold"
    return "$rc"
}

list_verbs() { printf '%s\n' status check apply rollback channel auto-apply doctor; }

case "${1:-status}" in
    status)      shift; cmd_status "${1:-}" ;;
    check)       cmd_check ;;
    apply)       load_conf; cmd_apply ;;
    rollback)    cmd_rollback ;;
    channel)     cmd_channel "${2:-}" ;;
    auto-apply)  cmd_auto_apply "${2:-}" ;;
    doctor)      cmd_doctor ;;
    --self-test) self_test ;;
    --list-verbs) list_verbs ;;
    -h|--help|help) sed -n '2,16p' "$0"; exit 0 ;;
    *) say "usage: awnix update {status [--json]|check|apply|rollback|channel stable|beta|auto-apply on|off|doctor|--self-test|--list-verbs}"; exit 2 ;;
esac
