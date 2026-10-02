---
id: 12-mesh
title: Join a mesh
applies_to: ["*"]
---
## What a mesh is

A mesh links your machines into one private network, so work can be placed on whichever machine fits it: the one with the GPU, or the one next to the data. An awnix box joins as a **client-only** node. It opens no port and connects out to your mesh. The mesh stays off until you join; a box that never joins is not changed.

## Get a token

In the portal, open **Connect a device** and copy the enroll token. A token works once and expires after 24 hours.

## Join

Put the token in a file, or pipe it in, so it does not show in the process list:

```
sudo awnix mesh join @/root/enroll.txt
cat enroll.txt | sudo awnix mesh join -
```

The box exchanges the token, sets up the `aithernet0` overlay and registers its CPU, memory and GPUs. Check the result with `awnix mesh status` (or `--json`). The console's **Mesh** tab shows the same state and has a join form.

## No network yet

If the box is offline, `join` records the join as **pending** and exits 2. It finishes by itself when a network connection comes up: a NetworkManager hook and a 10-minute timer both retry it. You do not need to run the command again.

## Private enclave (air gap)

On the air-gap profile a join to the public control plane is refused, and a box that had already joined drops its overlay at the next retry. To join your own conductor inside the enclave, pass its address; every address it resolves to must be inside the air-gap `allowed_subnets`:

```
sudo awnix mesh join @/root/enroll.txt --portal https://conductor.enclave.lan --cafile /etc/pki/enclave-ca.pem
```

To set it once for the box, put `AWNIX_MESH_PORTAL=https://conductor.enclave.lan` in `/etc/awnix/endpoints.env`.

## Leave

`sudo awnix mesh leave` removes the overlay, the credential and the WireGuard key. If the portal cannot be reached, the box still leaves locally, and you remove it from the portal by hand.

### The garg appliance {variants: garg-appliance}

A garg appliance joins the same way, from the console's Mesh tab or `awnix mesh join`. The mesh stays off until you join, and an appliance that never joins is not changed.

## What stays closed

The node writes no listen port, and the overlay sits in its own firewalld zone `awnix-mesh`, which accepts nothing inbound; ports you open in the host zone do not open on the overlay. A joined node holds one outbound WireGuard UDP socket and does not listen for new connections. The zero-open-ports proof covers the unjoined box and the air-gap profile.
