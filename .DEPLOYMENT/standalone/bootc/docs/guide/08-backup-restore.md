---
id: 08-backup-restore
title: Backup and restore
applies_to: ["*"]
---
## What to back up

`/etc`
: your configuration: users, network, `/etc/awnix` (setup, update and endpoint settings).

`/var/lib`
: service and component data, including `/var/lib/awnix`.

`/home`
: user data, including rootless container volumes.

Nothing under `/usr` needs a backup; it comes back with the image.

## Backups contain secrets

A backup of `/etc` and `/var/lib` holds every credential on the box: password hashes in `/etc/shadow`, the SSH host keys, the account link token in `/etc/awnix`, and the registry credential in `/etc/ostree/auth.json`. Anyone who can read the archive can impersonate the box. Create archives readable by root only, keep them on encrypted storage, and never leave one in a shared or world-readable directory.

## Taking a backup

Stop the services that write the data you are copying, or use each store's own snapshot tool. Run the whole backup as root with a restrictive umask, so every archive is created mode 0600 and owned by root:

```
sudo -i
umask 077
mkdir -p /root/backup
podman volume export myvolume --output /root/backup/myvolume.tar
tar -C / -czf /root/backup/etc-backup.tgz etc
tar -C / -czf /root/backup/var-lib-backup.tgz var/lib
ls -l /root/backup
```

`ls -l` must show `-rw-------` and `root` on every file. Copy the archives off the box over SSH (`scp` or `rsync`) to storage only you can read.

## Restore order

1. Install the same variant, on the same channel.
2. Restore `/etc`, then reboot.
3. Restore `/var/lib` and `/home` with the services stopped.
4. Run `awnix component sync`, then `awnix doctor`.

Practise the restore on a spare VM before you need it.

## License and credentials {variants: garg-appliance, aitheros, aitheros-cloud}

Back up `/etc/aither` (the license, `appliance.lic`) and `/var/lib/aither/license`. Both are secrets: the box keeps them mode 0600, and your copy must stay just as private, made with `umask 077` as above. The registry credential does not need a backup; `aitheros license refresh` makes a new one from the license.

## On the garg appliance {variants: garg-appliance}

Everything the product stores is under `/var/lib/gargbot`:

- SQLite databases: `gargbot.db`, `sessions.db`, `aitherchat.db`, `agent_memory.db`. Copy them with the backend stopped, or online with `sqlite3 <db> ".backup <dest>"`.
- qdrant: create a snapshot through its API on port 6333; snapshots land in `/var/lib/gargbot/qdrant/snapshots`.
- `uploads`, `exports`, `artifacts`: plain files.
- `/var/lib/gargbot/env`: the secret key. Without it every user is signed out after a restore. Anyone holding a copy can forge a sign-in for any user, so keep it only in a root-only archive (`umask 077`) on encrypted storage.

Restore with `garg-firstboot.service` stopped, then start it again.
