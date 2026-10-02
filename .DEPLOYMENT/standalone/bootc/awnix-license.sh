#!/bin/sh
# /usr/libexec/awnix/awnix-license -- `awnix license ...` is `aitheros license ...`.
# One implementation (aitheros-cli.py); this shim only forwards.
exec /usr/bin/aitheros license "$@"
