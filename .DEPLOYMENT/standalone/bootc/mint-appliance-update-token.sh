#!/bin/bash
# mint-appliance-update-token.sh [key.pem] [out-auth.json]
#
# Mint a GitHub App installation token scoped to packages:read for the
# GargBot appliance's `bootc upgrade`, and write it as a containers auth.json.
# App "Aitherium" (4528160), installed on the Aitherium org (152226155).
# The key is the vault's GITHUB_APP_PRIVATE_KEY, staged to a mode-600 file by
#   python AitherOS/dev/tools/aither_secret.py GITHUB_APP_PRIVATE_KEY --to-file <key.pem>
# Prints fingerprints and HTTP codes only - never a value. Refuses to write an
# auth.json from a token that carries MORE than packages:read (measured
# 2026-09-13: before the org accepted the permission, the unfiltered mint
# handed back administration:write + contents:write - the wrong thing to put
# on a third-party device).
set -u
KEY="${1:-/var/tmp/garg-rig/app/key.pem}"
OUT="${2:-/var/tmp/garg-rig/auth.json}"
APP_ID=4528160
INST=152226155
[ -r "$KEY" ] || { echo "KEY-MISSING: $KEY"; exit 2; }
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

echo "== key fingerprint (GitHub shows SHA256 of the public key DER):"
echo -n "   SHA256:"; openssl rsa -in "$KEY" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 -binary | openssl base64 -A; echo
echo "   page says: SHA256:dKKoVgcFU6o1oLvEGU5jHJeIzCr3DYVg+7Ktuble6IY="

now=$(date +%s)
hdr=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
pl=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now-60)) $((now+540)) "$APP_ID" | b64url)
sig=$(printf '%s.%s' "$hdr" "$pl" | openssl dgst -sha256 -sign "$KEY" -binary | b64url)
JWT="$hdr.$pl.$sig"

echo "== app identity (JWT -> /app):"
curl -s -o /tmp/app.json -w "   HTTP %{http_code}\n" -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" https://api.github.com/app
python3 -c "import json; d=json.load(open('/tmp/app.json')); print('   slug=%s id=%s perms.packages=%s' % (d.get('slug'), d.get('id'), (d.get('permissions') or {}).get('packages','none')))" 2>/dev/null || head -c 200 /tmp/app.json

echo "== installation $INST permissions:"
curl -s -o /tmp/inst.json -w "   HTTP %{http_code}\n" -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" "https://api.github.com/app/installations/$INST"
python3 -c "import json; d=json.load(open('/tmp/inst.json')); print('   account=%s packages=%s' % ((d.get('account') or {}).get('login'), (d.get('permissions') or {}).get('packages','none')))" 2>/dev/null || head -c 200 /tmp/inst.json

echo "== mint installation token (request packages:read):"
code=$(curl -s -o /tmp/tok.json -w "%{http_code}" -X POST -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
  -d '{"permissions":{"packages":"read"}}' "https://api.github.com/app/installations/$INST/access_tokens")
echo "   HTTP $code"
if [ "$code" != "201" ]; then
  echo "   MINT-REFUSED: $(python3 -c "import json; print(json.load(open('/tmp/tok.json')).get('message'))" 2>/dev/null)"
  echo "   -> the org installation has not accepted packages:read yet:"
  echo "      https://github.com/organizations/Aitherium/settings/installations/$INST"
  rm -f /tmp/tok.json /tmp/app.json /tmp/inst.json
  echo "NO-TOKEN"; exit 1
fi
OUT="$OUT" python3 - <<'PY'
import json, base64, hashlib
d = json.load(open('/tmp/tok.json'))
t = d['token']
print("   token fp=%s len=%d expires=%s perms=%s" % (hashlib.sha256(t.encode()).hexdigest()[:12], len(t), d.get('expires_at'), d.get('permissions')))
auth = {"auths": {"ghcr.io": {"auth": base64.b64encode(("x-access-token:" + t).encode()).decode()}}}
perms = d.get('permissions') or {}
if set(perms) - {'packages', 'metadata'} or perms.get('packages') != 'read':
    print("   REFUSING: token permissions are broader than packages:read -> not written")
    raise SystemExit(3)
import os
open(os.environ['OUT'], 'w').write(json.dumps(auth))
PY
chmod 600 "$OUT"; rm -f /tmp/tok.json /tmp/app.json /tmp/inst.json
echo "== ghcr pull-bearer probe with the minted token:"
T=$(python3 -c "import json,base64; print(base64.b64decode(json.load(open('$OUT'))['auths']['ghcr.io']['auth']).decode().split(':',1)[1])")
curl -s -o /dev/null -w "   ghcr token endpoint: HTTP %{http_code}\n" -u "x-access-token:$T" "https://ghcr.io/token?scope=repository:aitherium/garg-appliance:pull&service=ghcr.io"
echo "AUTH-JSON-READY $OUT"
