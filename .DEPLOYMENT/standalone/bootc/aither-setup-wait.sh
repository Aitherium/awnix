#!/bin/sh
# /usr/lib/aither/aither-setup-wait -- the WSL [oobe] command (/etc/wsl-distribution.conf).
#
# The one-click path: the AitherOS desk ("Set up Aither" in the Start menu or tray)
# touches /run/aither-setup/desk-present before it applies the setup through
# `wsl -u root --exec aither-setup --seed ...`. When that marker is there, this
# terminal does NOT ask anything: it says where to finish and waits for setup.done.
# With no desk (a .wsl double-clicked on a machine without it) it runs the terminal
# setup, `aither-setup --oobe`, exactly as the image did before.
#
# WSL treats the distro as installed only when this exits 0; aither-setup --oobe keeps
# its own exit contract (non-zero only when no user could be created).
MARKER=${AITHER_SETUP_MARKER:-/var/lib/aither/setup.done}
DESK=${AITHER_SETUP_DESK:-/run/aither-setup/desk-present}
GRACE=${AITHER_SETUP_WAIT_GRACE:-6}

[ -f "$MARKER" ] && exit 0

# First boot with the desk installed on Windows but not open (a customer double-clicked
# awnix.wsl): open "Set up Aither" through WSL interop, via the desk:// protocol the
# desk registers. Only when that protocol IS registered -- otherwise Windows would pop an
# "open with" dialog -- and never with anything from input. The window then touches
# $DESK, and this terminal waits instead of asking.
WIN=${AITHER_SETUP_WIN_ROOT:-/mnt/c/Windows/System32}
if [ ! -e "$DESK" ] && [ -x "$WIN/reg.exe" ] && [ -x "$WIN/cmd.exe" ] \
    && "$WIN/reg.exe" query 'HKCU\Software\Classes\desk' >/dev/null 2>&1; then
    "$WIN/cmd.exe" /c start "" "desk://setup" >/dev/null 2>&1 || true
    GRACE=${AITHER_SETUP_WAIT_DESK_GRACE:-45}
fi

# A desk that is starting the distro needs a moment to announce itself.
i=0
while [ ! -e "$DESK" ] && [ "$i" -lt "$GRACE" ]; do
    sleep 1
    i=$((i + 1))
done

if [ -e "$DESK" ]; then
    printf '\nFinish setting up AitherOS in the "Set up Aither" window.\n'
    printf 'This terminal continues on its own when setup is done (Ctrl+C: set up here instead).\n'
    trap 'exec /usr/bin/aither-setup --oobe' INT
    while [ ! -f "$MARKER" ]; do
        # The desk removes the marker when it closes without finishing.
        [ -e "$DESK" ] || exec /usr/bin/aither-setup --oobe
        sleep 2
    done
    printf 'AitherOS is set up. Open a new terminal to log in as your user.\n'
    exit 0
fi

exec /usr/bin/aither-setup --oobe
