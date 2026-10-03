# /etc/profile.d/awnix-user-ports.sh -- this user's loopback ports, for login shells.
#
# The same lines the systemd user-environment generator gives every user unit
# (/usr/lib/systemd/user-environment-generators/60-awnix-ports), exported here so awsh
# in a terminal dials THIS user's daemons. Sorted before awsh-offline.sh, whose
# `${VAR:-default}` exports then keep these values.
if [ -x /usr/lib/systemd/user-environment-generators/60-awnix-ports ]; then
    _awnix_ports=$(/usr/lib/systemd/user-environment-generators/60-awnix-ports 2>/dev/null)
    for _awnix_kv in $_awnix_ports; do
        case "$_awnix_kv" in
            *=*) export "$_awnix_kv" ;;
        esac
    done
    unset _awnix_ports _awnix_kv
fi
