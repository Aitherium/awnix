---
name: awnix-endpoints
section: 8
summary: show, probe and apply every vendor endpoint this box dials
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-endpoints
verbs_from: awnix-endpoints.py
verbs_mode: list-verbs
files_from: [awnix-endpoints.py, awnix-endpoints.env]
status: live
---
## SYNOPSIS

`awnix endpoints` {`show`|`probe`} [`--json`]

`sudo awnix endpoints apply`

## DESCRIPTION

Every URL an awnix box dials for linking, device sign-in, packages or images is read from one chain of plain `KEY=VALUE` files, with no expansion. Later entries win:

- the vendor defaults in `/usr/lib/awnix/endpoints.env`
- your overrides in `/etc/awnix/endpoints.env`
- the process environment

To run air-gapped, or against your own mirror, set the keys in `/etc/awnix/endpoints.env`. `show` prints each value and where it came from; `probe` also tries to reach it; `apply` restarts what has to re-read the files.

The awnix chain is read by every `awnix` command on its next run, so a change to it needs nothing else.

## KEYS

`AWNIX_LINK_HOST`, `AWNIX_LINK_VERIFY_URL`, `AWNIX_DEVICE_CODE_URL`
: account linking and the device sign-in flow.

`AWNIX_GITHUB_ORG`, `AWNIX_PYPI`, `AWNIX_REGISTRY`
: where components and images come from.

`AITHER_LICENSE_EXCHANGE_URL`
: where a licensed appliance exchanges its license for a registry credential.

`AWNIX_MESH_PORTAL`
: where `awnix mesh join` enrolls this box; empty switches the mesh off. See awnix-mesh(8).

## Garg appliance chain {variants: garg-appliance}

The garg appliance product reads a second chain, in the same order: `/usr/lib/gargbot/appliance.env`, then `/etc/gargbot/appliance.env`, then the process environment. It carries `GARGBOT_DEPLOYMENT_MODE=standalone`, the product's service URLs (all local), and `GARG_REGISTRY_PING`, `GARG_WEIGHTS_PING` and `GARG_FEEDBACK_PING`, which `garg-update doctor` probes.

The garg backend reads this chain once, when it starts. After you change `/etc/gargbot/appliance.env`, run `sudo awnix endpoints apply`. A bare `systemctl restart garg-firstboot` is not enough: it skips a backend that is already answering on port 8900.

`apply` is a product outage. It stops the backend (SIGTERM, then SIGKILL after 15 seconds), restarts `garg-firstboot.service` and waits up to 90 seconds for port 8900 to answer again. Every signed-in user loses the product until it is back, so run it in a maintenance window. It exits 1 if the backend does not come back within 90 seconds; `garg-update doctor` and `journalctl -u garg-firstboot` then show why.

## COMMANDS

`show [--json]`
: every key, its value and its source: default, vendor-env, admin-env or process-env.

`probe [--json]`
: the same, plus whether each endpoint answers: ok, unreachable or off.

`apply`
: root only. Restarts every service that reads an endpoint chain only when it starts, so it picks up your change, and waits for it to answer again. Where a product service is restarted this interrupts it; the variant sections of this page say what stops and for how long. On a box where nothing needs restarting it does nothing and exits 0. Exit 1 if a restarted service does not come back, 2 if not run as root or `systemctl` is missing.

`--self-test`
: prove the precedence rules offline.

## FILES

`/usr/lib/awnix/endpoints.env`
: vendor defaults; replaced by updates.

`/etc/awnix/endpoints.env`
: your overrides; kept across updates.

## SEE ALSO

awnix(8), awnix-update(8)
