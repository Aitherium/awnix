# /etc/profile.d/aither-awsh-local.sh -- point awsh at THIS box's MicroScheduler.
#
# On a fleet host the model lane is local: MicroScheduler answers https on 127.0.0.1:8150
# and its certificate chains to the fleet CA below. Without this, awsh's auto mode (in
# the published releases up to 1.20) skipped the local scheduler, landed on the public
# cloud gateway, and refused every turn with "Cloud gateway requires sign-in" while the
# local lane was healthy (measured 2026-11-09).
#
# TLS stays VERIFIED: NODE_EXTRA_CA_CERTS adds the fleet CA to node's trust store; nothing
# here relaxes verification. Every value is a default -- an exported value wins.
_aither_ca=/etc/aither/tls/communication-core-lb/ca-chain.pem
if [ -r "$_aither_ca" ]; then
    : "${NODE_EXTRA_CA_CERTS:=$_aither_ca}"
    : "${AITHER_LLM_URL:=https://127.0.0.1:8150/v1}"
    : "${AITHER_INFERENCE_MODE:=raw}"
    export NODE_EXTRA_CA_CERTS AITHER_LLM_URL AITHER_INFERENCE_MODE
fi
unset _aither_ca
