# shellcheck shell=sh
# Installed as /etc/profile.d/zz-aither-setup.sh. Points the first interactive login at the
# AitherOS first-boot setup (SSH, a `wsl --import`ed distro whose OOBE never ran, a console).
# Sourced, not executed: everything lives in a function so `return` is legal and no
# variable leaks into the login shell. Silent once /var/lib/aither/setup.done exists.
# AITHER_SETUP_SKIP=1 suppresses it for one login. Never offered on the live fleet host
# (AITHER_SETUP_FORCE=1 overrides).
#
# Every login (bare metal, VM and WSL) offers to run the setup right here. On WSL the
# device-code sign-in opens in the Windows browser; the desk's "Set up Aither" window
# remains an alternative for people who start from Windows.
_aither_setup_offer() {
    [ -f /var/lib/aither/setup.done ] && return 0
    case $- in *i*) ;; *) return 0 ;; esac
    [ -t 0 ] && [ -t 1 ] || return 0
    [ -n "${AITHER_SETUP_SKIP:-}" ] && return 0
    [ -x /usr/bin/aither-setup ] || return 0
    # The LIVE fleet host is never set up here (2026-11-09: a root `wsl` into the fleet
    # distro ran the wizard, which tried to renumber the fleet's service accounts).
    # Same signals as aither-setup's own guard: distro name, fleet marker files, quadlets.
    if [ "${AITHER_SETUP_FORCE:-}" != 1 ]; then
        _aither_fleet=""
        _aither_name=${AITHER_FLEET_DISTRO:-${AITHER_WSL_DISTRO:-${FLEET_DISTRO:-${AWDESK_FLEET_DISTRO:-awnix}}}}
        if [ -n "${WSL_DISTRO_NAME:-}" ] && [ "$WSL_DISTRO_NAME" = "$_aither_name" ]; then
            _aither_fleet="WSL distro $WSL_DISTRO_NAME is the fleet distro"
        elif [ -e /etc/aither/awnix-migration.env ] ||
            [ -e /usr/libexec/aither/aither-attach-fleet-data.sh ]; then
            _aither_fleet="fleet marker file present"
        else
            for _aither_q in /etc/containers/systemd/aitheros-*.container; do
                [ -e "$_aither_q" ] && _aither_fleet="fleet quadlets present"
                break
            done
        fi
        if [ -n "$_aither_fleet" ]; then
            printf 'aither-setup: skipped on the fleet host (%s); force: %s\n' \
                "$_aither_fleet" 'AITHER_SETUP_FORCE=1 aither-setup'
            unset _aither_fleet _aither_name _aither_q
            return 0
        fi
        unset _aither_fleet _aither_name _aither_q
    fi
    # WSL gets the SAME in-place offer as bare metal (owner, 2026-09-29: "why could this not
    # be done directly from awnix/wsl?"). The sign-in is a device code, and aither-setup
    # opens its URL in the Windows browser through interop, so nothing forces a detour
    # through the Start-menu window; that stays an alternative, not the only door.
    if [ "$(id -u)" -eq 0 ]; then
        printf '\nThis AitherOS machine has not been set up yet (user, sign-in, agent).\n'
        printf 'Run aither-setup now? [Y/n] '
        _aither_ans=""
        read -r _aither_ans || _aither_ans=n
        case "$_aither_ans" in
            n | N | no | NO) printf 'Later: aither-setup\n' ;;
            *) /usr/bin/aither-setup ;;
        esac
        unset _aither_ans
    else
        printf '\nFirst-boot setup has not run on this machine. Run: sudo aither-setup\n'
    fi
    return 0
}
_aither_setup_offer
unset -f _aither_setup_offer
