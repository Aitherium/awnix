---
name: awnix-console
section: 8
summary: the on-box web console for setup and administration on port 9443
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-console
verbs_from: awnix-console.py
verbs_mode: list-verbs
files_from: [awnix-console.py, awnix_console/__init__.py, awnix_console/tls.py, awnix-console.service, awnix-console.conf]
status: live
---
## SYNOPSIS

`https://<box>:9443/` · `sudo awnix console code`

## DESCRIPTION

`awnix-console` is one HTTPS server on port 9443 for the life of the box. It runs in one of two modes.

Setup mode
: while `/etc/awnix/setup.json` does not exist. The console walks through first-boot setup. Its one-time code is printed on the local console and the serial console banner.

Console mode
: after setup. The console shows the overview, license, updates, components, endpoints and the admin guide. Its sign-in code is NOT printed on the banner; read it on the box with `sudo awnix console code`.

The certificate is self-signed, so the browser warns on first visit. Compare the fingerprint the browser shows with the `fp=` value on the console banner, `awnix-console: https://<ip>:9443 fp=<sha12>`, before you accept it.

Codes are 10 characters. Five wrong codes lock sign-in for THAT client (its IPv4 address, or its IPv6 /64) for 60 seconds, doubling up to 15 minutes; other clients are not locked. In console mode twenty wrong codes rotate the code; the setup code is never rotated, and the code for the current mode is minted whenever the mode changes. Every change requires the session cookie, a same-origin request and the `X-Awnix-Console: 1` header, and runs one fixed command from an allowlist; the console never runs a shell.

## COMMANDS

`serve`
: run the server; `awnix-console.service` starts it.

`prepare`
: create the sign-in codes, the certificate and the firewall opening for port 9443; the service runs it before `serve`.

`url`
: print the address to open, `https://<ip>:9443/`.

`code`
: print the sign-in code for the current mode (root only).

`status`
: print the mode, bind address, certificate fingerprint and whether the server is listening; `--json` for a machine-readable copy.

`--self-test`
: prove the auth, lockout and allowlist rules offline.

## BIND ADDRESS

The console listens on all addresses. On a cloud instance (any cloud-init datasource other than NoCloud) it listens on 127.0.0.1 only; reach it through an SSH tunnel: `ssh -L 9443:127.0.0.1:9443 admin@<box>`. To change it, set `AWNIX_CONSOLE_BIND=` in `/etc/awnix/console.conf` to an address (for example `AWNIX_CONSOLE_BIND=127.0.0.1` to keep the console off every network, or one interface's address), or back to `auto`, then `sudo systemctl restart awnix-console`. `AWNIX_CONSOLE_PORT=` changes the port. Check the result with `awnix console status`. Any other key name is ignored.

## FILES

`/etc/awnix/console.conf`
: admin overrides of `/usr/lib/awnix/console.conf`.

`/var/lib/awnix-console/tls`
: the self-signed certificate and key.

`/run/awnix/setup-code`
: the setup-mode code.

`/run/awnix-console/token`
: the console-mode code.

## SEE ALSO

awnix(8), awnix-setup(8)
