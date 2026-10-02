---
name: awnix-mesh
section: 8
summary: join this box to a workspace mesh as a client-only node, report it, and leave it
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-mesh
verbs_from: awnix-mesh.py
verbs_mode: list-verbs
files_from: [awnix-mesh.py, awnix-mesh.nm-dispatcher, Containerfile.awnix]
status: live
---
## SYNOPSIS

`awnix mesh join` {*TOKEN*|`@`*FILE*|`-`} [`--portal` *URL*] [`--node-name` *NAME*] [`--role` `worker`|`edge`] [`--cafile` *PEM*] [`--dry-run`]

`awnix mesh status` [`--json`] · `awnix mesh leave` · `awnix mesh retry` [`--quiet`] · `awnix mesh capabilities` [`--json`]

`awnix-mesh --self-test` · `awnix-mesh --list-verbs`

## DESCRIPTION

`awnix mesh` enrolls the box in your workspace's mesh with an enroll token from **Connect a device** in the portal. The token is exchanged for a 30-day capability token (renewed automatically), the node joins the overlay with its WireGuard public key and receives its overlay address and your workspace's peers (the workspace comes from the token, never from the request), and its CPU, memory, architecture and GPUs are registered so work can be placed on it.

The overlay is a NetworkManager WireGuard connection named `aithernet0`, so wireguard-tools is not needed. The node is **client-only**: it writes no listen port and advertises no endpoint of its own. The connection sits in its own firewalld zone `awnix-mesh` (target DROP, no services, no ports), which accepts nothing inbound; a port an admin opens in the host zone never opens on the overlay.

The portal comes from the endpoints chain, key `AWNIX_MESH_PORTAL` (see awnix-endpoints(8)). An empty value switches the mesh off. Plain `http://` is accepted only for a loopback portal.

**Air gap.** When the air gap is on (`/etc/aither/air_gap.yaml` says `enabled: true`, or the image profile is `airgap`), a join is refused unless every address of the portal host is inside `allowed_subnets`; loopback is always allowed. This is how a node joins a private enclave conductor and never the public control plane. If the air gap comes on after a join, or the portal revokes the node, the next `retry` tears the overlay down so no keepalive leaves the box.

## COMMANDS

`join`
: enroll the box. Pass the token as `@FILE` or `-` (stdin): a token on the command line shows in the process list. Offline, the join is recorded as pending and exits 2; it finishes by itself when the network returns. `--portal` points at a private enclave conductor, `--cafile` trusts its CA, `--dry-run` prints the plan and writes nothing.

`status`
: the state (unjoined, pending, joined, refused, offline, left or error), the overlay address, the peers and the registered capabilities. `--json` prints the status document, which never contains a token.

`leave`
: tell the portal the node is gone (when it can reach it), then remove the overlay connection, the credential and the WireGuard key. Leaving works offline too.

`retry`
: finish a pending join; on a joined node, send the heartbeat and renew the capability token when fewer than 7 days are left. On a box that never joined it does nothing and exits 0. `awnix-mesh.timer` runs it every 10 minutes.

`capabilities`
: what would be registered: arch, CPU, memory, GPUs and accelerators. VRAM the driver does not report is null, never guessed.

## EXIT STATUS

`0`
: joined, or nothing to do (unjoined, left).

`1`
: refused or invalid: a rejected token, the air gap, a malformed argument, or the mesh switched off.

`2`
: offline, pending, or could not judge. The path unit and the timer retry it.

## SECURITY

A joined node holds one WireGuard UDP socket, which `ss -u` shows; it accepts nothing that is not a reply to its own session. The zero-open-ports claim covers the unjoined box and the air-gap profile.

## FILES

`/etc/awnix/mesh/`
: `credential`, the capability token and renewal secret, and `wg.key`, the WireGuard private key (both 0600, kept across updates).

`/var/lib/awnix/mesh/`
: `status.json`, the status document (no secrets), and `pending.json`, a join waiting for the network (0600).

`/usr/lib/NetworkManager/dispatcher.d/90-awnix-mesh`
: starts the retry when a connection comes up.

## SEE ALSO

awnix(8), awnix-endpoints(8), awnix-console(8), nmcli(1), firewalld.zones(5)
