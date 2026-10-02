---
name: awnix-backup
section: 8
summary: back up, restore and factory-reset an awnix appliance
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-backup
verbs_from: awnix-backup.py
verbs_mode: list-verbs
files_from: [awnix-backup.py, awnix-backup.tmpfiles.conf, Containerfile.awnix]
status: live
---
## SYNOPSIS

`awnix backup create` [`--to` *DIR*] [`--label` *L*] [`--seal --key` *FILE*] [`--no-verify`] [`--live`] [`--keep` *N*] [`--json`]

`awnix backup list` [`--from` *DIR*] [`--json`] · `awnix backup verify --label` *L* [`--from` *DIR*] [`--expect-key` *HEX*] · `awnix backup drop --label` *L*

`awnix restore --label` *L* [`--from` *DIR*] [`--expect-key` *HEX*|`--expect-key-file` *F*] [`--allow-unsigned`] [`--only` *ID,...*] [`--force-variant`] [`--json`]

`awnix restore --undo` *TS*

`awnix reset --scope` {`data`|`factory`} [`--dry-run`] [`--confirm` 'erase *HOSTNAME*'] [`--include-models`] [`--keep-backups`] [`--no-reboot`] [`--json`]

## VERBS

- `backup`
- `restore`
- `reset`

## DESCRIPTION

**backup create** copies the appliance's data into one archive. What is copied is declared by profiles in `/usr/lib/awnix/backup.d/*.json` (a same-named file in `/etc/awnix/backup.d/` overrides one): the base profile applies to every variant and an appliance variant adds its own. Services are stopped while their data is copied (`--live` skips that), SQLite databases are copied with the online-backup API, and the archive is then restored into a scratch directory and compared item by item: a backup is reported `verified` only after that. The default store is `/var/lib/awnix/backups`; `--to` writes to any directory, such as a USB drive, with no network. `--seal --key FILE` signs the archive with awseal.

Credentials (registry tokens, secret keys, TLS keys, the link token) are never backed up; after a restore they are derived again from the restored license.

**restore** verifies the backup first and refuses a damaged or tampered archive before anything changes. Each item is copied beside its live path and renamed into place; the previous data is kept under `/var/lib/awnix/restore-replaced/TS`, and `awnix restore --undo TS` puts it back. A restore writes only the items this box's own profiles declare: the backup's manifest never chooses a path. A backup read from removable media or a share needs a seal and `--expect-key` (the publisher's awseal public key); `--allow-unsigned` overrides that for media you made yourself.

**reset** erases data. `--scope data` removes customer data (chats, documents, the vector index, uploads, logs, backups) and keeps the license, setup and configuration. `--scope factory` also removes configuration, credentials, the license and the admin user, then reboots into setup mode. The command asks for the phrase `erase HOSTNAME` (without a terminal, pass it with `--confirm`). Reset is never available from the web console. Afterwards `/var/lib/awnix/reset-receipt.json` records the scope and every removed path with its counts, and contains no customer data.

## COMMANDS

`backup`
: `create`, `list`, `verify` and `drop` a backup.

`restore`
: verify, then restore a backup; `--undo TS` reverts a restore.

`reset`
: erase customer data (`--scope data`) or return the box to setup (`--scope factory`).

## SANITIZATION

Reset unlinks files and trims the filesystem (`fstrim`). That is **NIST SP 800-88 Clear (logical)**. It is not Purge and not cryptographic erase: the default install does not encrypt the disk, so there is no key to destroy. For Purge, destroy or securely erase the drive with its own tools.

## EXIT STATUS

`0`
: done (backup verified, restore healthy, reset finished).

`1`
: refused or unverifiable (missing label, tampered archive, variant mismatch, wrong phrase, unhealthy after restore).

`2`
: could not judge (a required tool is missing, no terminal and no `--confirm`, an invalid profile, or a restore that failed mid-way and was rolled back).

## FILES

`/usr/lib/awnix/backup.d/`
: the vendor backup profiles.

`/etc/awnix/backup.d/`
: your overrides.

`/var/lib/awnix/backups`
: the default backup store.

`/var/lib/awnix/restore-replaced/`
: data a restore replaced, kept for `--undo`.

`/var/lib/awnix/reset-receipt.json`
: what the last reset removed.

## SEE ALSO

awnix(8), awnix-update(8)
