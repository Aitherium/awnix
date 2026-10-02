---
id: 04-networking
title: Networking
applies_to: ["*"]
---
## Addresses

NetworkManager manages the network. Examples:

```
nmcli connection modify "Wired connection 1" ipv4.method manual ipv4.addresses 192.0.2.10/24 ipv4.gateway 192.0.2.1 ipv4.dns 192.0.2.1
nmcli device wifi connect "MyNetwork" --ask
hostnamectl set-hostname box-01
```

## Firewall

firewalld runs with an `awnix` zone. `sudo firewall-cmd --list-all` shows what is open. The web console, awnix-console(8), needs `9443/tcp`.

Open a port with `firewall-cmd --permanent --add-port=<port>/tcp && firewall-cmd --reload`.

## Proxies

podman and bootc read `HTTPS_PROXY`. Set it for both in a drop-in:

```
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nDefaultEnvironment=HTTPS_PROXY=http://proxy.example:3128\n' | sudo tee /etc/systemd/system.conf.d/proxy.conf
```

## Air-gapped operation

Every vendor endpoint the box dials comes from one file you can override, `/etc/awnix/endpoints.env`. `awnix endpoints show` lists them and where each value came from; point them at your mirrors or leave them unreachable. See awnix-endpoints(8).

## On the garg appliance {variants: garg-appliance}

The product listens on port 8900 (plain HTTP on your LAN; put it behind your own TLS proxy for access beyond a trusted network). The model server (8089) and qdrant (6333) are for the backend on the same box; do not expose them. The product's own endpoints come from `/etc/gargbot/appliance.env` (awnix-endpoints(8)).
