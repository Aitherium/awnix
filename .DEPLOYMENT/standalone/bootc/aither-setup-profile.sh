# shellcheck shell=sh
# Installed as /etc/profile.d/zz-aither-setup.sh. Points the first interactive login at the
# AitherOS first-boot setup (SSH, a `wsl --import`ed distro whose OOBE never ran, a console).
# Sourced, not executed: everything lives in a function so `return` is legal and no
# variable leaks into the login shell. Silent once /var/lib/aither/setup.done exists.
# AITHER_SETUP_SKIP=1 suppresses it for one login.
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
