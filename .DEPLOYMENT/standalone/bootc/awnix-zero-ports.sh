#!/bin/sh
# awnix-zero-ports -- the "zero open ports, no password" claim, as a PROFILE you
# can apply to any awnix variant, check at build time, and prove on a booted box.
#
#   awnix zero-ports apply --profile airgap|base [--open PORT/PROTO ...] [--from DIR]
#   awnix zero-ports verify-static [--profile airgap|base]
#   awnix zero-ports proof [--json] [--out FILE] [--issue] [--account A ...] [--require-no-route]
#   awnix zero-ports --self-test | --list-verbs
#   awnix-zero-ports net-linklocal      (boot helper: awnix-airgap-net.service)
#
# Exit codes (every verb): 0 ok, 1 violation, 2 could not judge.
#
# `proof` runs the demo Step-1 commands from the AFRL proof plan verbatim. The
# count is THIS line, byte-for-byte (check_awnix_zero_ports.py ZP004 pins it):
#
#   ss -H -ltun | awk '$5 !~ /^(127\.|\[::1\]|::1)/' | wc -l
#
# Only loopback listeners are allowed. A firewall that drops traffic does NOT
# make a listener disappear from `ss`; the profile therefore removes or
# rebinds the listeners themselves (sshd -> loopback, cockpit masked, console
# and gargbot -> 127.0.0.1) and ALSO sets a default-deny firewalld zone.
#
# POSIX sh, LF only. Offline apply uses only firewall-offline-cmd, systemctl
# mask/enable, passwd -l and file copies -- it runs inside a container build.
#
# Test seams (tests only): AWNIX_ZP_ROOT is a path prefix for every file this
# script reads or writes; AWNIX_ZP_SRC overrides the directory the profile's
# files are copied from.
set -u

R="${AWNIX_ZP_ROOT:-}"
SRC="${AWNIX_ZP_SRC:-$R/usr/share/awnix/zero-ports}"
ZONE=awnix
PROG=awnix-zero-ports

# Demo Step 2 (no egress for 30 minutes, tap capture) allows only ARP, IPv6 ND and
# MLD on the wire. These units call home or advertise on a timer and ignore the
# profile, so the airgap profile MASKS them: awnix-update.timer (skopeo to ghcr.io
# every 6 h), aither-license.timer (POSTs to the license exchange every 45 min),
# awnix-mesh.timer (mesh heartbeat), garg-update.timer (superseded, masked in case
# an old base still ships it), avahi (mDNS). Updates on an airgap box are offline
# bundles (awnix-offline-update). check_awnix_zero_ports.py ZP010 requires every
# timer the base enables to be listed here or declared offline-safe there.
AIRGAP_MASK="awnix-update.timer aither-license.timer awnix-mesh.timer garg-update.timer avahi-daemon.service avahi-daemon.socket"
# Where the network half of the profile lands (no DHCP, no mDNS/LLMNR, no probe).
NM_CONF=/usr/lib/NetworkManager/conf.d/90-awnix-airgap.conf
NM_WIRED=/usr/lib/NetworkManager/system-connections/awnix-airgap-wired.nmconnection
RESOLVED_CONF=/usr/lib/systemd/resolved.conf.d/90-awnix-airgap.conf
NET_UNIT=awnix-airgap-net.service
NM_CONN_DIR=/etc/NetworkManager/system-connections

# The Step-1 count. Held in a quoted heredoc so the bytes that RUN are the bytes
# that are LOGGED; nothing is re-quoted in between.
STEP1_CMD=$(cat <<'EOF'
ss -H -ltun | awk '$5 !~ /^(127\.|\[::1\]|::1)/' | wc -l
EOF
)

say()  { printf '%s\n' "$*"; }
err()  { printf '%s: %s\n' "$PROG" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    sed -n '2,10p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'
}

# ── JSON helpers (no jq on the box) ─────────────────────────────────────────
jstr() {
    # control characters dropped, backslash and double quote escaped
    printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

# ── accounts ────────────────────────────────────────────────────────────────
# root plus every uid >= 1000 account with a real login shell.
login_accounts() {
    pw="$R/etc/passwd"
    [ -r "$pw" ] || { say root; return 0; }
    awk -F: '
        $1 == "root" { print $1; next }
        $3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false|sync|shutdown|halt)$/ { print $1 }
    ' "$pw"
}

# The accounts the IMAGE shipped with, recorded by `apply --profile airgap` at
# build time. `proof` judges these when no --account is given. The operator that
# awnix-setup creates on first boot (tty1 / serial console, physical presence)
# has a password ON PURPOSE -- it is the only local and recovery login on a box
# whose sshd is loopback-only with PasswordAuthentication no -- so judging every
# login account made every set-up box report FAIL. Remote "no password" is the
# sshd verdict; the shipped accounts must be locked.
SHIPPED_ACCOUNTS=/usr/lib/awnix/zero-ports.accounts
shipped_accounts() {
    if [ -r "$R$SHIPPED_ACCOUNTS" ]; then
        grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$R$SHIPPED_ACCOUNTS"
        return 0
    fi
    login_accounts
}

# L / LK / NP / P / ABSENT. Reads shadow under a test root, passwd -S otherwise.
account_state() {
    u="$1"
    if [ -z "$R" ] && have passwd; then
        st=$(passwd -S "$u" 2>/dev/null | awk '{print $2; exit}')
        [ -n "$st" ] && { say "$st"; return 0; }
    fi
    sh_="$R/etc/shadow"
    if [ -r "$sh_" ]; then
        awk -F: -v u="$u" '
            $1 == u { h = $2
                      if (h == "") print "NP"
                      else if (substr(h,1,1) == "!" || substr(h,1,1) == "*") print "LK"
                      else print "P"
                      found = 1; exit }
            END { if (!found) print "ABSENT" }' "$sh_"
        return 0
    fi
    say UNKNOWN
}

state_locked() { case "$1" in L|LK|ABSENT) return 0 ;; *) return 1 ;; esac; }

# ── sshd ────────────────────────────────────────────────────────────────────
# Effective sshd config. `sshd -T` refuses to run with no host keys (true in a
# container build and before first boot), so fall back to a throwaway key.
SSHD_T=""
sshd_effective() {
    [ -n "$SSHD_T" ] && return 0
    have sshd || return 1
    SSHD_T=$(sshd -T 2>/dev/null) && [ -n "$SSHD_T" ] && return 0
    have ssh-keygen || return 1
    kd=$(mktemp -d 2>/dev/null) || return 1
    ssh-keygen -q -t ed25519 -N '' -f "$kd/hk" >/dev/null 2>&1 || { rm -rf "$kd"; return 1; }
    SSHD_T=$(sshd -T -h "$kd/hk" 2>/dev/null)
    rc=$?
    rm -rf "$kd"
    [ "$rc" -eq 0 ] && [ -n "$SSHD_T" ]
}
sshd_key() { printf '%s\n' "$SSHD_T" | awk -v k="$1" 'tolower($1) == k { print $2; exit }'; }
sshd_listen_nonloopback() {
    printf '%s\n' "$SSHD_T" | awk 'tolower($1) == "listenaddress" {
        a = $2
        if (a !~ /^(127\.|\[::1\]|::1)/) print a }'
}

# ── firewall ────────────────────────────────────────────────────────────────
fw() {
    if [ -z "$R" ] && have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd "$@"
    else
        firewall-offline-cmd "$@"
    fi
}
fw_have() { have firewall-offline-cmd || have firewall-cmd; }

# ── units ───────────────────────────────────────────────────────────────────
unit_masked() {
    # systemd treats a symlink to /dev/null AND an empty unit file as masked.
    f="$R/etc/systemd/system/$1"
    if [ -L "$f" ]; then [ "$(readlink "$f")" = /dev/null ]; return; fi
    [ -f "$f" ] && [ ! -s "$f" ]
}
unit_present() {
    for d in /etc/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
        [ -e "$R$d/$1" ] || [ -L "$R$d/$1" ] && return 0
    done
    return 1
}
# An enablement symlink points at an ABSOLUTE path (/usr/lib/systemd/system/x),
# which does not resolve under a test root, so -e alone is wrong there.
unit_enabled_link() { [ -L "$R/etc/systemd/system/$1" ] || [ -e "$R/etc/systemd/system/$1" ]; }
sysctl_() {
    if [ -n "$R" ]; then systemctl --root="$R" "$@"; else systemctl "$@"; fi
}

# ── console bind (awnix-console's own precedence) ───────────────────────────
console_bind() {
    b=""
    found=0
    for f in "$R/usr/lib/awnix/console.conf" $(ls "$R/usr/lib/awnix/console.d/"*.conf 2>/dev/null | LC_ALL=C sort) "$R/etc/awnix/console.conf"; do
        [ -r "$f" ] || continue
        found=1
        v=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}AWNIX_CONSOLE_BIND=//p' "$f" | tail -n 1 | tr -d "\"'\r ")
        [ -n "$v" ] && b="$v"
    done
    [ "$found" -eq 1 ] || return 1
    say "${b:-auto}"
}
is_loopback_addr() { case "$1" in 127.*|::1|\[::1\]|localhost) return 0 ;; *) return 1 ;; esac; }

current_profile() {
    p=$(cat "$R/usr/lib/awnix/profile" 2>/dev/null | tr -d ' \r\n')
    say "${p:-base}"
}

# ═════════════════════════════════════════════════════════════════════════════
# apply
# ═════════════════════════════════════════════════════════════════════════════
install_file() {  # src dst mode
    [ -r "$1" ] || { err "missing profile file $1"; return 2; }
    mkdir -p "$(dirname "$2")" || return 2
    tr -d '\r' < "$1" > "$2" && chmod "$3" "$2"
}

cmd_apply() {
    profile=""; opens=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --profile) profile="${2:-}"; shift 2 ;;
            --profile=*) profile="${1#*=}"; shift ;;
            --open) opens="$opens ${2:-}"; shift 2 ;;
            --open=*) opens="$opens ${1#*=}"; shift ;;
            --from) SRC="${2:-}"; shift 2 ;;
            *) err "apply: unknown argument $1"; return 2 ;;
        esac
    done
    case "$profile" in airgap|base) ;; *) err "apply: --profile airgap|base is required"; return 2 ;; esac
    for p in $opens; do
        case "$p" in [0-9]*/tcp|[0-9]*/udp) ;; *) err "apply: --open wants PORT/tcp|udp, got $p"; return 2 ;; esac
    done
    fw_have || { err "apply: firewall-offline-cmd not installed (dnf install firewalld first)"; return 2; }
    [ -d "$SRC" ] || { err "apply: profile files not found in $SRC"; return 2; }

    # 1. firewalld: zone awnix, default, nothing open unless asked.
    fw --new-zone="$ZONE" >/dev/null 2>&1 || true
    for s in $(fw --zone="$ZONE" --list-services 2>/dev/null); do
        fw --zone="$ZONE" --remove-service="$s" >/dev/null || return 2
    done
    for p in $(fw --zone="$ZONE" --list-ports 2>/dev/null); do
        fw --zone="$ZONE" --remove-port="$p" >/dev/null || return 2
    done
    if [ "$profile" = base ]; then
        # The base keeps key-only ssh for cloud access, and DHCPv6 so cloud IPv6 works.
        fw --zone="$ZONE" --add-service=ssh >/dev/null || return 2
        fw --zone="$ZONE" --add-service=dhcpv6-client >/dev/null || return 2
    fi
    for p in $opens; do
        fw --zone="$ZONE" --add-port="$p" >/dev/null || return 2
    done
    fw --set-default-zone="$ZONE" >/dev/null || return 2
    mkdir -p "$R/usr/lib/awnix" || return 2
    printf '%s\n' $opens > "$R/usr/lib/awnix/zero-ports.open"

    # 2. sshd: key-only everywhere; loopback-only in the airgap profile.
    install_file "$SRC/sshd-01-awnix.conf" "$R/etc/ssh/sshd_config.d/01-awnix.conf" 0600 || return 2
    if [ "$profile" = airgap ]; then
        install_file "$SRC/sshd-02-airgap-listen.conf" "$R/etc/ssh/sshd_config.d/02-airgap-listen.conf" 0600 || return 2
    else
        rm -f "$R/etc/ssh/sshd_config.d/02-airgap-listen.conf"
    fi

    # 3. cockpit: masked in airgap, loopback in base.
    if [ "$profile" = airgap ]; then
        sysctl_ mask cockpit.socket >/dev/null 2>&1 || {
            mkdir -p "$R/etc/systemd/system" && ln -sf /dev/null "$R/etc/systemd/system/cockpit.socket"; }
    elif unit_present cockpit.socket; then
        mkdir -p "$R/etc/systemd/system/cockpit.socket.d" || return 2
        printf '[Socket]\nListenStream=\nListenStream=127.0.0.1:9090\n' \
            > "$R/etc/systemd/system/cockpit.socket.d/10-awnix-loopback.conf"
    fi

    # 4. accounts: no password anywhere.
    if [ "$profile" = airgap ]; then
        for u in $(login_accounts); do
            passwd -l "$u" >/dev/null 2>&1 || err "apply: passwd -l $u failed (verify-static decides)"
        done
        mkdir -p "$R/usr/lib/awnix" || return 2
        { printf '# accounts shipped in the image, locked by apply --profile airgap; proof judges these\n'
          login_accounts; } > "$R$SHIPPED_ACCOUNTS" || return 2
    else
        rm -f "$R$SHIPPED_ACCOUNTS"
    fi

    # 5. on-box web surfaces -> loopback (airgap).
    if [ "$profile" = airgap ]; then
        install_file "$SRC/console-90-airgap.conf" "$R/usr/lib/awnix/console.d/zz-90-airgap.conf" 0644 || return 2
        if [ -d "$R/usr/lib/gargbot" ] || [ -e "$R/usr/bin/garg-firstboot" ]; then
            install_file "$SRC/gargbot-90-airgap.env" "$R/usr/lib/gargbot/appliance.env.d/90-airgap.env" 0644 || return 2
        fi
        install_file "$SRC/awnix-proof-step1.service" "$R/usr/lib/systemd/system/awnix-proof-step1.service" 0644 || return 2
        sysctl_ enable awnix-proof-step1.service >/dev/null 2>&1 || {
            mkdir -p "$R/etc/systemd/system/multi-user.target.wants" &&
            ln -sf /usr/lib/systemd/system/awnix-proof-step1.service \
                "$R/etc/systemd/system/multi-user.target.wants/awnix-proof-step1.service"; }
    else
        rm -f "$R/usr/lib/awnix/console.d/zz-90-airgap.conf"
    fi

    # 6. egress (airgap): mask the call-home timers, no DHCP / mDNS / LLMNR /
    #    connectivity probe, and rewrite installer DHCP profiles at boot.
    if [ "$profile" = airgap ]; then
        for u in $AIRGAP_MASK; do
            sysctl_ mask "$u" >/dev/null 2>&1 || {
                mkdir -p "$R/etc/systemd/system" && ln -sf /dev/null "$R/etc/systemd/system/$u"; } || return 2
        done
        install_file "$SRC/nm-90-awnix-airgap.conf" "$R$NM_CONF" 0644 || return 2
        install_file "$SRC/nm-awnix-airgap-wired.nmconnection" "$R$NM_WIRED" 0600 || return 2
        install_file "$SRC/resolved-90-awnix-airgap.conf" "$R$RESOLVED_CONF" 0644 || return 2
        install_file "$SRC/$NET_UNIT" "$R/usr/lib/systemd/system/$NET_UNIT" 0644 || return 2
        sysctl_ enable "$NET_UNIT" >/dev/null 2>&1 || {
            mkdir -p "$R/etc/systemd/system/multi-user.target.wants" &&
            ln -sf "/usr/lib/systemd/system/$NET_UNIT" \
                "$R/etc/systemd/system/multi-user.target.wants/$NET_UNIT"; }
    else
        rm -f "$R$NM_CONF" "$R$NM_WIRED" "$R$RESOLVED_CONF" \
              "$R/etc/systemd/system/multi-user.target.wants/$NET_UNIT"
    fi

    printf '%s\n' "$profile" > "$R/usr/lib/awnix/profile" || return 2
    say "$PROG: applied profile $profile (zone $ZONE default${opens:+, open:$opens})"
    return 0
}

# ═════════════════════════════════════════════════════════════════════════════
# verify-static  (build time: the image as built, no running services)
# ═════════════════════════════════════════════════════════════════════════════
V_BAD=0; V_DEAD=0
v_ok()   { say "  ok    $1"; }
v_bad()  { say "  FAIL  $1"; V_BAD=$((V_BAD + 1)); }
v_dead() { say "  DEAD  $1"; V_DEAD=$((V_DEAD + 1)); }

cmd_verify_static() {
    profile=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --profile) profile="${2:-}"; shift 2 ;;
            --profile=*) profile="${1#*=}"; shift ;;
            *) err "verify-static: unknown argument $1"; return 2 ;;
        esac
    done
    [ -n "$profile" ] || profile=$(current_profile)
    case "$profile" in airgap|base) ;; *) err "verify-static: unknown profile $profile"; return 2 ;; esac
    say "$PROG verify-static --profile $profile"

    # firewall
    if fw_have; then
        dz=$(fw --get-default-zone 2>/dev/null)
        if [ "$dz" = "$ZONE" ]; then v_ok "firewalld default zone = $ZONE"; else v_bad "firewalld default zone is '${dz:-?}', want $ZONE"; fi
        allowed=$(cat "$R/usr/lib/awnix/zero-ports.open" 2>/dev/null | tr '\n' ' ')
        svcs=$(fw --zone="$ZONE" --list-services 2>/dev/null)
        if [ "$profile" = airgap ]; then
            [ -z "$svcs" ] && v_ok "zone $ZONE: no services" || v_bad "zone $ZONE opens services: $svcs"
        else
            extra=$(printf '%s\n' $svcs | grep -vx -e ssh -e dhcpv6-client -e '' || true)
            [ -z "$extra" ] && v_ok "zone $ZONE: services = ${svcs:-none}" || v_bad "zone $ZONE opens services beyond ssh/dhcpv6-client: $extra"
        fi
        ports=$(fw --zone="$ZONE" --list-ports 2>/dev/null)
        extra=""
        for p in $ports; do
            case " $allowed " in *" $p "*) ;; *) extra="$extra $p" ;; esac
        done
        [ -z "$extra" ] && v_ok "zone $ZONE: ports = ${ports:-none}" || v_bad "zone $ZONE opens ports not requested with --open:$extra"
        for what in protocols source-ports forward-ports rich-rules; do
            got=$(fw --zone="$ZONE" --list-"$what" 2>/dev/null)
            [ -z "$got" ] && v_ok "zone $ZONE: no $what" || v_bad "zone $ZONE has $what: $got"
        done
    else
        v_bad "firewalld not installed (firewall-offline-cmd absent)"
    fi

    # cockpit
    if [ "$profile" = airgap ]; then
        if unit_masked cockpit.socket; then v_ok "cockpit.socket masked"
        elif unit_present cockpit.socket; then v_bad "cockpit.socket present and not masked"
        else v_ok "cockpit.socket absent"; fi
    elif unit_present cockpit.socket && ! unit_masked cockpit.socket; then
        dropin="$R/etc/systemd/system/cockpit.socket.d/10-awnix-loopback.conf"
        if [ -r "$dropin" ] && grep -qx 'ListenStream=' "$dropin" && grep -qx 'ListenStream=127.0.0.1:9090' "$dropin"; then
            v_ok "cockpit.socket bound to 127.0.0.1:9090"
        else
            v_bad "cockpit.socket listens on every address (no loopback drop-in)"
        fi
    fi

    # sshd
    if have sshd; then
        if sshd_effective; then
            for k in passwordauthentication kbdinteractiveauthentication permitemptypasswords; do
                val=$(sshd_key "$k")
                [ "$val" = no ] && v_ok "sshd $k no" || v_bad "sshd $k is '${val:-?}', want no"
            done
            if [ "$profile" = airgap ]; then
                nl=$(sshd_listen_nonloopback | tr '\n' ' ')
                [ -z "$nl" ] && v_ok "sshd listens on loopback only" || v_bad "sshd listenaddress not loopback: $nl"
            fi
        else
            v_dead "sshd -T could not run (even with a throwaway host key)"
        fi
    else
        v_ok "sshd absent"
    fi

    # accounts
    if [ "$profile" = airgap ]; then
        for u in $(login_accounts); do
            st=$(account_state "$u")
            if state_locked "$st"; then v_ok "account $u: $st"
            elif [ "$st" = UNKNOWN ]; then v_dead "account $u: state unreadable"
            else v_bad "account $u: $st (want L/LK)"; fi
        done
    fi

    # profile marker + surfaces
    if [ "$profile" = airgap ]; then
        grep -qx airgap "$R/usr/lib/awnix/profile" 2>/dev/null && v_ok "profile marker = airgap" || v_bad "/usr/lib/awnix/profile does not say airgap"
        if cb=$(console_bind); then
            is_loopback_addr "$cb" && v_ok "awnix-console bind = $cb" || v_bad "awnix-console bind = $cb (want 127.0.0.1)"
        else
            v_ok "awnix-console not installed"
        fi
        gf="$R/usr/bin/garg-firstboot"
        if [ -e "$gf" ]; then
            [ -r "$R/usr/lib/gargbot/appliance.env.d/90-airgap.env" ] && v_ok "gargbot airgap env present" || v_bad "gargbot airgap env missing"
            if grep -q -- '--host 0\.0\.0\.0' "$gf"; then
                v_bad "garg-firstboot hardcodes uvicorn --host 0.0.0.0 (needs \${GARGBOT_BIND_HOST})"
            else
                v_ok "garg-firstboot has no hardcoded 0.0.0.0 bind"
            fi
            grep -q 'QDRANT__SERVICE__HOST' "$gf" && v_ok "garg-firstboot sets QDRANT__SERVICE__HOST" || v_bad "garg-firstboot leaves qdrant on 0.0.0.0 (no QDRANT__SERVICE__HOST)"
            grep -q 'appliance\.env\.d' "$gf" && v_ok "garg-firstboot reads appliance.env.d" || v_bad "garg-firstboot never reads appliance.env.d (the airgap env is inert)"
        fi
        if [ -r "$R/usr/lib/systemd/system/awnix-proof-step1.service" ] && \
           unit_enabled_link multi-user.target.wants/awnix-proof-step1.service; then
            v_ok "awnix-proof-step1.service installed and enabled"
        else
            v_bad "awnix-proof-step1.service not installed+enabled"
        fi
        # egress (demo Step 2)
        for u in $AIRGAP_MASK; do
            unit_masked "$u" && v_ok "$u masked" || v_bad "$u not masked (it calls home inside the Step 2 window)"
        done
        if [ -r "$R$NM_CONF" ] && grep -qx 'no-auto-default=\*' "$R$NM_CONF" && grep -qx 'enabled=false' "$R$NM_CONF"; then
            v_ok "NetworkManager: no auto DHCP profile, no connectivity probe"
        else
            v_bad "NetworkManager airgap conf missing or allows an auto DHCP profile ($NM_CONF)"
        fi
        if [ -r "$R$NM_WIRED" ] && [ "$(grep -cx 'method=link-local' "$R$NM_WIRED")" -eq 2 ]; then
            v_ok "wired profile is link-local only (v4 and v6)"
        else
            v_bad "wired link-local profile missing or not link-local for v4 AND v6 ($NM_WIRED)"
        fi
        if [ -r "$R/usr/lib/systemd/system/$NET_UNIT" ] && unit_enabled_link "multi-user.target.wants/$NET_UNIT"; then
            v_ok "$NET_UNIT installed and enabled (installer DHCP profiles go link-local)"
        else
            v_bad "$NET_UNIT not installed+enabled; the installer's DHCP profile would send DHCPDISCOVER"
        fi
    fi

    if [ "$V_DEAD" -gt 0 ]; then say "VERDICT: COULD-NOT-JUDGE ($V_DEAD dead, $V_BAD failed)"; return 2; fi
    if [ "$V_BAD" -gt 0 ]; then say "VERDICT: FAIL ($V_BAD violation(s))"; return 1; fi
    say "VERDICT: PASS"
    return 0
}

# ═════════════════════════════════════════════════════════════════════════════
# net-linklocal  (boot, before NetworkManager: awnix-airgap-net.service)
# ═════════════════════════════════════════════════════════════════════════════
# One keyfile on stdout with DHCP/auto turned link-local. A missing method (or a
# missing [ipv4]/[ipv6] section) is NM's default, auto, so it becomes link-local
# too -- unless the section carries addresses (then NM infers manual).
nm_linklocal() {
    awk '
    function endsec() {
        if (sec == "ipv4" && !m4 && !a4) print "method=link-local"
        if (sec == "ipv6" && !m6 && !a6) print "method=link-local"
    }
    /^[ \t]*\[[^]]*\][ \t]*$/ {
        endsec()
        sec = $0; gsub(/^[ \t]*\[|\][ \t]*$/, "", sec)
        if (sec == "ipv4") h4 = 1
        if (sec == "ipv6") h6 = 1
        print; next
    }
    sec == "ipv4" && /^[ \t]*address/ { a4 = 1 }
    sec == "ipv6" && /^[ \t]*address/ { a6 = 1 }
    sec == "ipv4" && /^[ \t]*method[ \t]*=/ {
        m4 = 1; v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t]*$/, "", v)
        if (v == "auto") { print "method=link-local"; next }
    }
    sec == "ipv6" && /^[ \t]*method[ \t]*=/ {
        m6 = 1; v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t]*$/, "", v)
        if (v == "auto" || v == "dhcp") { print "method=link-local"; next }
    }
    { print }
    END {
        endsec()
        if (!h4) { print ""; print "[ipv4]"; print "method=link-local" }
        if (!h6) { print ""; print "[ipv6]"; print "method=link-local" }
    }' "$1"
}

cmd_net_linklocal() {
    d="$R$NM_CONN_DIR"
    [ $# -eq 0 ] || { err "net-linklocal: takes no arguments"; return 2; }
    [ -d "$d" ] || { say "$PROG: net-linklocal: no $NM_CONN_DIR, nothing to rewrite"; return 0; }
    n=0; rc=0
    for f in "$d"/*.nmconnection; do
        [ -f "$f" ] || continue
        if grep -qE '^[[:space:]]*type[[:space:]]*=[[:space:]]*loopback[[:space:]]*$' "$f"; then continue; fi
        # hidden and *.tmp: NetworkManager's keyfile plugin skips both, so a crash
        # mid-rewrite never leaves a second profile behind
        tmp="$d/.$(basename "$f").awnix.tmp"
        if ! nm_linklocal "$f" > "$tmp"; then rm -f "$tmp"; err "net-linklocal: could not rewrite $f"; rc=2; continue; fi
        if cmp -s "$f" "$tmp"; then rm -f "$tmp"; continue; fi
        chmod 0600 "$tmp" && mv -f "$tmp" "$f" || { rm -f "$tmp"; err "net-linklocal: could not replace $f"; rc=2; continue; }
        n=$((n + 1))
        say "$PROG: net-linklocal: $(basename "$f") -> link-local (no DHCP)"
    done
    say "$PROG: net-linklocal: $n profile(s) rewritten"
    return "$rc"
}

# ═════════════════════════════════════════════════════════════════════════════
# proof  (runtime: the booted box, the demo Step-1 commands verbatim)
# ═════════════════════════════════════════════════════════════════════════════
LOG=""
logrun() {  # logrun "<shell command>" -> output on stdout, rc in LAST_RC, both logged
    out=$(sh -c "$1" 2>&1)
    LAST_RC=$?
    if [ -n "$LOG" ]; then
        { printf '$ %s\n' "$1"; [ -n "$out" ] && printf '%s\n' "$out"; printf 'rc=%s\n' "$LAST_RC"; } >> "$LOG"
    fi
    printf '%s' "$out"
}

cmd_proof() {
    as_json=0; out=""; issue=0; accounts=""; need_noroute=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --json) as_json=1; shift ;;
            --out) out="${2:-}"; shift 2 ;;
            --out=*) out="${1#*=}"; shift ;;
            --issue) issue=1; shift ;;
            --account) accounts="$accounts ${2:-}"; shift 2 ;;
            --account=*) accounts="$accounts ${1#*=}"; shift ;;
            --require-no-route) need_noroute=1; shift ;;
            *) err "proof: unknown argument $1"; return 2 ;;
        esac
    done
    if [ -n "$out" ]; then
        mkdir -p "$(dirname "$out")" 2>/dev/null
        LOG="$out"
        : > "$LOG" || { err "proof: cannot write $out"; return 2; }
        printf '# awnix-zero-ports proof (demo Step 1) %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
    fi

    verdict=""
    if ! have ss || ! ss -H -ltun >/dev/null 2>&1; then
        verdict=COULD-NOT-JUDGE
    fi

    noroute=null
    if have ip; then
        logrun 'ip -o link' >/dev/null
        routes=$(logrun 'ip route'; echo "rc=$LAST_RC")
        case "$routes" in
            *rc=0) if printf '%s\n' "$routes" | grep -q '^default'; then noroute=false; else noroute=true; fi ;;
        esac
    fi

    count=-1
    if [ -z "$verdict" ]; then
        c=$(logrun "$STEP1_CMD" | tr -d ' \t\r\n')
        case "$c" in ''|*[!0-9]*) verdict=COULD-NOT-JUDGE ;; *) count=$c ;; esac
    fi
    listeners=""
    if [ -z "$verdict" ]; then
        listeners=$(logrun 'ss -H -ltunp' | awk '
            function esc(s) { gsub(/\\/, "/", s); gsub(/"/, "\047", s); return s }
            NF >= 5 {
                lb = ($5 ~ /^(127\.|\[::1\]|::1)/) ? "true" : "false"
                p = ""; for (i = 7; i <= NF; i++) p = p (i > 7 ? " " : "") $i
                printf "%s{\"proto\":\"%s\",\"local\":\"%s\",\"process\":\"%s\",\"loopback\":%s}", (n++ ? "," : ""), esc($1), esc($5), esc(p), lb
            }')
    fi

    digest=""
    if have bootc; then
        bs=$(logrun 'bootc status --format=json 2>/dev/null || bootc status --json')
        if have python3; then
            digest=$(printf '%s' "$bs" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(((d.get("status") or {}).get("booted") or {}).get("image",{}).get("imageDigest") or "")
except Exception:
    print("")' 2>/dev/null)
        fi
    fi

    zone=""
    fw_have && zone=$(logrun 'firewall-cmd --get-default-zone 2>/dev/null || firewall-offline-cmd --get-default-zone')

    acct_src="--account"
    if [ -z "$accounts" ]; then
        accounts=$(shipped_accounts)
        if [ -r "$R$SHIPPED_ACCOUNTS" ]; then acct_src="$SHIPPED_ACCOUNTS"; else acct_src="every login account"; fi
    fi
    [ -n "$LOG" ] && printf '# accounts judged (from %s): %s\n' "$acct_src" "$(printf '%s ' $accounts)" >> "$LOG"
    pw_json=""; pw_bad=0
    for u in $accounts; do
        if [ -z "$R" ] && have passwd; then logrun "passwd -S $u" >/dev/null; fi
        st=$(account_state "$u")
        state_locked "$st" || pw_bad=$((pw_bad + 1))
        pw_json="$pw_json${pw_json:+,}{\"user\":$(jstr "$u"),\"state\":$(jstr "$st")}"
    done

    pwauth=absent
    if have sshd; then
        if [ -n "$LOG" ]; then printf '$ sshd -T | grep -i passwordauthentication\n' >> "$LOG"; fi
        if sshd_effective; then
            pwauth=$(sshd_key passwordauthentication); pwauth=${pwauth:-unknown}
        else
            pwauth=unknown
        fi
        [ -n "$LOG" ] && printf 'passwordauthentication %s\n' "$pwauth" >> "$LOG"
    fi

    ok=false
    if [ -z "$verdict" ]; then
        if [ "$count" -eq 0 ] && [ "$pw_bad" -eq 0 ] && { [ "$pwauth" = no ] || [ "$pwauth" = absent ]; } &&
           { [ "$need_noroute" -eq 0 ] || [ "$noroute" = true ]; }; then
            ok=true; verdict=PASS
        elif [ "$pwauth" = unknown ] && [ "$count" -eq 0 ] && [ "$pw_bad" -eq 0 ]; then
            verdict=COULD-NOT-JUDGE
        else
            verdict=FAIL
        fi
    fi
    [ -n "$LOG" ] && printf 'count=%s accounts_unlocked=%s passwordauthentication=%s no_default_route=%s\nVERDICT: %s\n' \
        "$count" "$pw_bad" "$pwauth" "$noroute" "$verdict" >> "$LOG"

    if [ "$issue" -eq 1 ]; then
        sha=""
        [ -n "$LOG" ] && have sha256sum && sha=$(sha256sum "$LOG" | cut -c1-12)
        line="awnix-zero-ports: verdict=$verdict count=$count profile=$(current_profile)${sha:+ log-sha256=$sha}"
        say "$line"
        if mkdir -p "$R/run/issue.d" 2>/dev/null; then
            printf '%s\n\n' "$line" > "$R/run/issue.d/50-awnix-zero-ports.issue" 2>/dev/null
            have agetty && agetty --reload >/dev/null 2>&1
        fi
    fi

    if [ "$as_json" -eq 1 ]; then
        printf '{"schema":1,"count":%s,"listeners":[%s],"default_zone":%s,"passwd":[%s],"sshd_passwordauthentication":%s,"bootc_digest":%s,"no_default_route":%s,"ok":%s,"verdict":%s,"profile":%s,"step1_cmd":%s}\n' \
            "$count" "$listeners" "$(jstr "$zone")" "$pw_json" "$(jstr "$pwauth")" \
            "$( [ -n "$digest" ] && jstr "$digest" || printf null)" "$noroute" "$ok" \
            "$(jstr "$verdict")" "$(jstr "$(current_profile)")" "$(jstr "$STEP1_CMD")"
    elif [ "$issue" -eq 0 ]; then
        say "non-loopback listeners: $count"
        say "accounts not locked:    $pw_bad"
        say "sshd passwordauth:      $pwauth"
        say "default route absent:   $noroute"
        say "VERDICT: $verdict"
    fi
    case "$verdict" in PASS) return 0 ;; FAIL) return 1 ;; *) return 2 ;; esac
}

# ═════════════════════════════════════════════════════════════════════════════
# --self-test  (hermetic: stubbed ss/sshd/passwd/firewall, a temp root)
# ═════════════════════════════════════════════════════════════════════════════
self_test() {
    T=$(mktemp -d 2>/dev/null) || { err "self-test: mktemp failed"; return 2; }
    trap 'rm -rf "$T"' EXIT INT TERM
    fails=0
    chk() {
        if [ "$1" = "$2" ]; then say "  ok    $3"; return 0; fi
        say "  FAIL  $3 (want $1, got $2)"; fails=$((fails + 1))
        [ -r "$T/out" ] && sed 's/^/        | /' "$T/out" | grep -v '|   ok' | tail -n 8
        return 0
    }
    # a python that actually runs (a Windows store alias named python3 exits 49)
    PY=""
    for p in python3 python; do
        if have "$p" && "$p" -c 'import json' >/dev/null 2>&1; then PY="$p"; break; fi
    done
    B="$T/bin"; mkdir -p "$B"

    cat > "$B/ss" <<'EOS'
#!/bin/sh
[ -n "${ZP_SS_FAIL:-}" ] && exit 1
cat "$ZP_SS_FIXTURE"
EOS
    cat > "$B/sshd" <<'EOS'
#!/bin/sh
cat "$ZP_SSHD_FIXTURE"
EOS
    cat > "$B/ip" <<'EOS'
#!/bin/sh
[ "${1:-}" = route ] && { [ -n "${ZP_ROUTE:-}" ] && echo "$ZP_ROUTE"; exit 0; }
echo "1: lo: <LOOPBACK,UP> mtu 65536"
EOS
    # firewall-offline-cmd stub: zone state in files under $ZP_FW.
    cat > "$B/firewall-offline-cmd" <<'EOS'
#!/bin/sh
d="$ZP_FW"; mkdir -p "$d"; zone=""
for a in "$@"; do
  case "$a" in
    --zone=*) zone="${a#*=}" ;;
    --new-zone=*) touch "$d/zone.${a#*=}.services" "$d/zone.${a#*=}.ports" ;;
    --get-default-zone) cat "$d/default" 2>/dev/null || echo public ;;
    --set-default-zone=*) echo "${a#*=}" > "$d/default" ;;
    --list-services) cat "$d/zone.$zone.services" 2>/dev/null | tr '\n' ' ' | sed 's/ $//'; echo ;;
    --list-ports) cat "$d/zone.$zone.ports" 2>/dev/null | tr '\n' ' ' | sed 's/ $//'; echo ;;
    --list-*) echo ;;
    --add-service=*) echo "${a#*=}" >> "$d/zone.$zone.services" ;;
    --add-port=*) echo "${a#*=}" >> "$d/zone.$zone.ports" ;;
    --remove-service=*) grep -vx "${a#*=}" "$d/zone.$zone.services" > "$d/t"; mv "$d/t" "$d/zone.$zone.services" ;;
    --remove-port=*) grep -vx "${a#*=}" "$d/zone.$zone.ports" > "$d/t"; mv "$d/t" "$d/zone.$zone.ports" ;;
  esac
done
exit 0
EOS
    cat > "$B/firewall-cmd" <<'EOS'
#!/bin/sh
exit 1
EOS
    cat > "$B/systemctl" <<'EOS'
#!/bin/sh
root=""; verb=""; unit=""
for a in "$@"; do case "$a" in --root=*) root="${a#*=}" ;; mask|enable) verb="$a" ;; *) unit="$a" ;; esac; done
case "$verb" in
  mask) mkdir -p "$root/etc/systemd/system"; ln -sf /dev/null "$root/etc/systemd/system/$unit" ;;
  enable) mkdir -p "$root/etc/systemd/system/multi-user.target.wants"
          # MSYS ln refuses a dangling target; a plain file stands in (test hosts only)
          ln -sf "/usr/lib/systemd/system/$unit" "$root/etc/systemd/system/multi-user.target.wants/$unit" 2>/dev/null ||
            printf '%s\n' "/usr/lib/systemd/system/$unit" > "$root/etc/systemd/system/multi-user.target.wants/$unit" ;;
esac
EOS
    cat > "$B/passwd" <<'EOS'
#!/bin/sh
[ "$1" = -l ] || exit 1
f="$AWNIX_ZP_ROOT/etc/shadow"
awk -F: -v OFS=: -v u="$2" '$1 == u && substr($2,1,1) != "!" { $2 = "!" $2 } { print }' "$f" > "$f.t" && mv "$f.t" "$f"
EOS
    chmod +x "$B"/*

    # -- 1. the Step-1 filter, line by line (the cases the spec names) --
    say "step-1 filter:"
    for case_ in \
        "0|udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*" \
        "0|udp UNCONN 0 0 [::1]:323 [::]:*" \
        "0|tcp LISTEN 0 128 127.0.0.1:9443 0.0.0.0:*" \
        "1|tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*" \
        "1|tcp LISTEN 0 128 *:9443 *:*" \
        "1|tcp LISTEN 0 4096 [::]:9090 [::]:*" \
        "1|udp UNCONN 0 0 [fe80::1%eth0]:546 [::]:*"; do
        want=${case_%%|*}; line=${case_#*|}
        printf '%s\n' "$line" > "$T/ss.fx"
        got=$(ZP_SS_FIXTURE="$T/ss.fx" PATH="$B:$PATH" sh -c "$STEP1_CMD" | tr -d ' ')
        chk "$want" "$got" "count($line)"
    done

    # -- 2. apply + verify-static + proof on a temp root --
    say "apply/verify/proof on a temp root:"
    mkroot() {
        rm -rf "$T/root" "$T/fw"; mkdir -p "$T/root/etc/ssh/sshd_config.d" "$T/root/usr/lib/systemd/system" \
            "$T/root/usr/lib/awnix" "$T/src"
        printf 'root:x:0:0:root:/root:/bin/bash\nbin:x:1:1:bin:/bin:/sbin/nologin\nawnix:x:1000:1000::/home/awnix:/bin/bash\nsvc:x:1001:1001::/:/sbin/nologin\n' > "$T/root/etc/passwd"
        printf 'root:$6$abc:1::::::\nbin:*:1::::::\nawnix:$6$def:1::::::\nsvc:!!:1::::::\n' > "$T/root/etc/shadow"
        printf '[Socket]\nListenStream=9090\n' > "$T/root/usr/lib/systemd/system/cockpit.socket"
        printf 'AWNIX_CONSOLE_BIND=auto\n' > "$T/root/usr/lib/awnix/console.conf"
        for f in sshd-01-awnix.conf sshd-02-airgap-listen.conf console-90-airgap.conf gargbot-90-airgap.env awnix-proof-step1.service \
                 resolved-90-awnix-airgap.conf awnix-airgap-net.service; do
            printf 'x\n' > "$T/src/$f"
        done
        printf 'AWNIX_CONSOLE_BIND=127.0.0.1\n' > "$T/src/console-90-airgap.conf"
        printf '[main]\nno-auto-default=*\n[connectivity]\nenabled=false\n' > "$T/src/nm-90-awnix-airgap.conf"
        printf '[connection]\ntype=ethernet\n[ipv4]\nmethod=link-local\n[ipv6]\nmethod=link-local\n' > "$T/src/nm-awnix-airgap-wired.nmconnection"
    }
    run() { AWNIX_ZP_ROOT="$T/root" AWNIX_ZP_SRC="$T/src" ZP_FW="$T/fw" ZP_SS_FIXTURE="$T/ss.fx" \
            ZP_SSHD_FIXTURE="$T/sshd.fx" PATH="$B:$PATH" sh "$0" "$@" >"$T/out" 2>&1; echo $?; }
    printf 'passwordauthentication no\nkbdinteractiveauthentication no\npermitemptypasswords no\nlistenaddress 127.0.0.1:22\nlistenaddress [::1]:22\n' > "$T/sshd.fx"
    printf 'udp UNCONN 0 0 127.0.0.1:323 0.0.0.0:*\n' > "$T/ss.fx"

    mkroot
    chk 1 "$(run verify-static --profile airgap)" "verify-static fails on an un-applied root"
    chk 0 "$(run apply --profile airgap)" "apply --profile airgap"
    chk 0 "$(run verify-static --profile airgap)" "verify-static passes after apply"
    [ -L "$T/root/etc/systemd/system/cockpit.socket" ] && chk 1 1 "cockpit.socket masked" || chk 1 0 "cockpit.socket masked"
    [ -L "$T/root/etc/systemd/system/awnix-update.timer" ] && chk 1 1 "awnix-update.timer masked" || chk 1 0 "awnix-update.timer masked"
    [ -L "$T/root/etc/systemd/system/aither-license.timer" ] && chk 1 1 "aither-license.timer masked" || chk 1 0 "aither-license.timer masked"
    rm -f "$T/root/etc/systemd/system/aither-license.timer"
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL when aither-license.timer is unmasked"
    run apply --profile airgap >/dev/null
    mv "$T/root$NM_WIRED" "$T/wired.bak"
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL without the link-local wired profile"
    mv "$T/wired.bak" "$T/root$NM_WIRED"
    chk 0 "$(run verify-static --profile airgap)" "verify-static PASS again"
    # net-linklocal: the installer's DHCP keyfile goes link-local; static and loopback stay
    nd="$T/root$NM_CONN_DIR"; mkdir -p "$nd"
    printf '[connection]\nid=enp1s0\ntype=ethernet\n\n[ipv4]\nmethod=auto\n\n[ipv6]\naddr-gen-mode=eui64\nmethod=auto\n' > "$nd/enp1s0.nmconnection"
    printf '[connection]\nid=st\ntype=ethernet\n\n[ipv4]\naddress1=10.0.0.5/24\nmethod=manual\n' > "$nd/st.nmconnection"
    printf '[connection]\nid=bare\ntype=ethernet\n' > "$nd/bare.nmconnection"
    printf '[connection]\nid=lo\ntype=loopback\n' > "$nd/lo.nmconnection"
    cp "$nd/lo.nmconnection" "$T/lo.orig"
    chk 0 "$(run net-linklocal)" "net-linklocal exits 0"
    grep -q 'method=auto' "$nd/enp1s0.nmconnection"; chk 1 $? "net-linklocal: no method=auto left in the DHCP profile"
    chk 2 "$(grep -cx 'method=link-local' "$nd/enp1s0.nmconnection")" "net-linklocal: v4 and v6 link-local"
    chk 2 "$(grep -cx 'method=link-local' "$nd/bare.nmconnection")" "net-linklocal: a profile with no ip sections gets link-local"
    grep -qx 'method=manual' "$nd/st.nmconnection" && grep -qx 'address1=10.0.0.5/24' "$nd/st.nmconnection"
    chk 0 $? "net-linklocal: a static v4 profile keeps method=manual and its address"
    chk 1 "$(grep -cx 'method=link-local' "$nd/st.nmconnection")" "net-linklocal: only its missing v6 goes link-local"
    cmp -s "$nd/lo.nmconnection" "$T/lo.orig"; chk 0 $? "net-linklocal: a loopback profile is untouched"
    cp "$nd/enp1s0.nmconnection" "$T/e1"
    run net-linklocal >/dev/null
    cmp -s "$nd/enp1s0.nmconnection" "$T/e1"; chk 0 $? "net-linklocal is idempotent"
    chk 0 "$(run proof)" "proof PASS on loopback-only listeners"
    chk 0 "$(run proof --json --require-no-route)" "proof --json --require-no-route PASS"
    if [ -n "$PY" ]; then
        "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["schema"]==1 and d["count"]==0 and d["ok"] is True' "$T/out" 2>/dev/null
        chk 0 $? "proof --json is valid JSON with count 0"
    fi
    printf 'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n' >> "$T/ss.fx"
    chk 1 "$(run proof)" "proof FAIL on 0.0.0.0:22"
    if [ -n "$PY" ]; then
        run proof --json >/dev/null
        "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["count"]==1 and d["ok"] is False and len(d["listeners"])==2' "$T/out" 2>/dev/null
        chk 0 $? "proof --json escapes the ss process column"
    fi
    chk 1 "$(run proof --out "$T/step1.log")" "proof --out FAIL is logged"
    grep -qx 'VERDICT: FAIL' "$T/step1.log"; chk 0 $? "step1.log records VERDICT: FAIL"
    printf 'udp UNCONN 0 0 127.0.0.1:323 0.0.0.0:*\n' > "$T/ss.fx"
    chk 0 "$(run proof --out "$T/step1.log")" "proof --out PASS"
    grep -qxF "$ $STEP1_CMD" "$T/step1.log"; chk 0 $? "step1.log carries the Step-1 command verbatim"
    grep -qx 'VERDICT: PASS' "$T/step1.log"; chk 0 $? "step1.log ends with VERDICT"
    ZP_ROUTE="default via 10.0.0.1 dev eth0" ; export ZP_ROUTE
    chk 1 "$(run proof --require-no-route)" "proof --require-no-route FAIL with a default route"
    unset ZP_ROUTE
    ZP_SS_FAIL=1; export ZP_SS_FAIL
    chk 2 "$(run proof)" "proof COULD-NOT-JUDGE when ss fails"
    unset ZP_SS_FAIL
    printf 'passwordauthentication yes\nkbdinteractiveauthentication no\npermitemptypasswords no\nlistenaddress 0.0.0.0:22\n' > "$T/sshd.fx"
    chk 1 "$(run proof)" "proof FAIL on passwordauthentication yes"
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL on a public sshd listenaddress"
    printf 'passwordauthentication no\nkbdinteractiveauthentication no\npermitemptypasswords no\nlistenaddress 127.0.0.1:22\n' > "$T/sshd.fx"
    sed 's/^awnix:!/awnix:/' "$T/root/etc/shadow" > "$T/s" && mv "$T/s" "$T/root/etc/shadow"
    chk 1 "$(run proof)" "proof FAIL on an account with a password"
    chk 0 "$(run proof --account root)" "proof --account limits the accounts judged"
    run apply --profile airgap >/dev/null
    grep -qx awnix "$T/root$SHIPPED_ACCOUNTS" 2>/dev/null; chk 0 $? "apply records the shipped accounts"
    # awnix-setup on first boot: an operator with a password, created AFTER the build
    printf 'admin:x:1001:1001::/home/admin:/bin/bash\n' >> "$T/root/etc/passwd"
    printf 'admin:$6$op:1::::::\n' >> "$T/root/etc/shadow"
    chk 0 "$(run proof)" "proof PASS with a setup-created operator password (shipped accounts locked)"
    chk 1 "$(run proof --account admin)" "proof --account admin still sees the operator password"
    grep -v '^admin:' "$T/root/etc/passwd" > "$T/p" && mv "$T/p" "$T/root/etc/passwd"
    grep -v '^admin:' "$T/root/etc/shadow" > "$T/p" && mv "$T/p" "$T/root/etc/shadow"
    printf 'AWNIX_CONSOLE_BIND=0.0.0.0\n' > "$T/root/usr/lib/awnix/console.d/zz-99-late.conf"
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL when a later console drop-in binds 0.0.0.0"
    rm -f "$T/root/usr/lib/awnix/console.d/zz-99-late.conf"
    mkdir -p "$T/root/usr/bin"; printf 'uvicorn --host 0.0.0.0 --port 8900\n' > "$T/root/usr/bin/garg-firstboot"
    run apply --profile airgap >/dev/null
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL on garg-firstboot --host 0.0.0.0"
    printf '. /usr/lib/gargbot/appliance.env.d/x\nQDRANT__SERVICE__HOST="${GARG_QDRANT_HOST:-127.0.0.1}"\nuvicorn --host "${GARGBOT_BIND_HOST:-0.0.0.0}"\n' > "$T/root/usr/bin/garg-firstboot"
    chk 0 "$(run verify-static --profile airgap)" "verify-static PASS once garg-firstboot honours the env"
    printf 'ssh\n' >> "$T/fw/zone.awnix.services"
    chk 1 "$(run verify-static --profile airgap)" "verify-static FAIL when the zone opens ssh"

    mkroot
    chk 0 "$(run apply --profile base)" "apply --profile base"
    chk 0 "$(run verify-static --profile base)" "verify-static --profile base"
    grep -qx 'ListenStream=127.0.0.1:9090' "$T/root/etc/systemd/system/cockpit.socket.d/10-awnix-loopback.conf" 2>/dev/null
    chk 0 $? "base: cockpit.socket loopback drop-in"
    chk 0 "$(run apply --profile airgap --open 8900/tcp)" "apply --open 8900/tcp"
    chk 0 "$(run verify-static --profile airgap)" "verify-static accepts a requested --open port"
    chk 2 "$(run apply --profile airgap --open bogus)" "apply refuses a malformed --open"
    chk 2 "$(run apply)" "apply refuses a missing --profile"

    if [ "$fails" -gt 0 ]; then say "self-test: $fails FAILED"; return 1; fi
    say "self-test: all passed"
    return 0
}

case "${1:-}" in
    apply) shift; cmd_apply "$@"; exit $? ;;
    verify-static) shift; cmd_verify_static "$@"; exit $? ;;
    proof) shift; cmd_proof "$@"; exit $? ;;
    net-linklocal) shift; cmd_net_linklocal "$@"; exit $? ;;
    --self-test) self_test; exit $? ;;
    --list-verbs) printf 'apply\nverify-static\nproof\n'; exit 0 ;;
    -h|--help|help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
