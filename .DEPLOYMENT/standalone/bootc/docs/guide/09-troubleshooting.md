---
id: 09-troubleshooting
title: Troubleshooting
applies_to: ["*"]
---
## Start with doctor

```
sudo awnix doctor
sudo awnix doctor --bundle
```

`doctor` runs every present verb's own check (updates, components, endpoints, console) and prints one report. `--bundle` writes a support bundle archive with the report, `bootc status`, failed units and recent logs. Secrets, licenses and credentials are left out. Attach that file to a support request.

## Where to look

```
systemctl --failed
journalctl -b -p warning
sudo bootc status --json
journalctl -u greenboot-healthcheck -b
```

The serial console (115200 baud) shows the boot banner, including the console address and fingerprint, even when the network is down.

## Symptoms

The web console does not answer
: check `systemctl status awnix-console`, and that `9443/tcp` is open in firewalld. On cloud instances it listens on 127.0.0.1 only; use an SSH tunnel (awnix-console(8)).

An update is refused
: `awnix update status` names the reason: `unsigned-refused` (the image is not signed by a trusted identity), `auth-refused` or `no-credential` (the registry refused the pull), `offline`.

The box went back to the old image
: greenboot rolled back a failing update. `journalctl -u greenboot-healthcheck -b -1` shows which check failed.

A script fails with `bad interpreter`
: it was saved with Windows line endings. Convert it with `sed -i 's/\r$//' script`.

## Licensed appliances {variants: garg-appliance, aitheros, aitheros-cloud}

License problems
: `aitheros license doctor` explains the state in `/var/lib/aither/license/status.json`. `expired` and `invalid` mean a new license is needed. `offline` recovers on its own when the network returns. A wrong system clock makes a valid license read as expired; check `timedatectl`.

## On the garg appliance {variants: garg-appliance}

The product is dark after a reboot
: `systemctl status garg-firstboot` and the logs in `/var/log/gargbot` (`backend.log`, `llama.log`, `qdrant.log`).

The model will not load
: check `/var/log/gargbot/model.log`. The baked models are `PQ2_0` files; a `Q2_0` file of the same model is a different format that this runtime refuses. `garg-model resolve` shows the file first boot will serve.

Updates return 401
: the box has no registry credential. Install the license (chapter 2) and run `garg-update check`.
