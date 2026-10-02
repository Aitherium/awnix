#!/bin/sh
# awnix restore -- the dispatcher finds this verb by file; the work is in awnix-backup.
exec /usr/libexec/awnix/awnix-backup restore "$@"
