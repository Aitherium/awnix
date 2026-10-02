#!/bin/sh
# awnix reset -- typed-confirmation erase (scope data|factory). Never exposed over HTTP.
exec /usr/libexec/awnix/awnix-backup reset "$@"
