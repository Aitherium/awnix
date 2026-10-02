---
name: awnix-offline-update
section: 8
summary: verify a signed update bundle from removable media, then stage it
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-offline-update
verbs_from: awnix-offline-update.py
verbs_mode: list-verbs
files_from: [awnix-offline-update.py, awnix-offline-update.tmpfiles.conf, Containerfile.awnix]
status: live
---
## SYNOPSIS

`awnix offline-update scan` [`--json`] [*DIR* ...]

`awnix offline-update verify` *BUNDLE* [`--json`] [`--allow-downgrade`]

`awnix offline-update stage` *BUNDLE* [`--json`] [`--allow-downgrade`] · `awnix offline-update apply` *BUNDLE* [`--json`]

`awnix offline-update status` [`--json`] · `awnix offline-update trust` {`list`|`add` *HEX64*} · `awnix offline-update gc` [`--json`]

`awnix-offline-update --self-test` · `awnix-offline-update --list-verbs`

## DESCRIPTION

An air-gapped box gets its updates on removable media. The media is hostile until proven otherwise, so nothing on it reaches `bootc` until the bundle, copied to a root-only directory first (never read in place), answers four questions: who signed it (an Ed25519 awseal signature by a key in the trust store), whether it is what they signed (every byte matches the seal), whether it is for this box (the archive and OCI manifest digests match the signed update.json, and the variant matches this image), and whether it is not older than what this box already ran (a validly signed old bundle is refused as `rollback` unless `--allow-downgrade` is given, which is audited).

Every refusal is appended to an audit hash chain before the command exits; if that append fails, the answer is exit 2 and nothing is staged. Only a verified bundle reaches the one `bootc switch --transport oci-archive` call; nothing here runs `bootc upgrade`.

A bundle is a directory holding `update.json`, the image as `image.oci.tar` (or chunked parts) and `awseal.json`, the seal over every file.

## COMMANDS

`scan`
: list the bundles on mounted media (`/run/media`, `/var/mnt`), unverified.

`verify`
: full verification; nothing is staged.

`stage`
: verify, then stage the image for the next boot.

`apply`
: stage, then schedule a reboot (detached).

`status`
: the last verdict and the rollback floor.

`trust`
: `trust list` shows the trusted signer keys; `trust add HEX64` trusts one more signer on this box only.

`gc`
: remove staging copies `bootc` no longer references.

## EXIT STATUS

`0`
: ok.

`1`
: refused (untrusted key, tampered bytes, wrong variant, rollback) or failed.

`2`
: could not judge (a required library or the audit log is unavailable).

## FILES

`/usr/share/awnix/offline-trust.d`
: the vendor signer keys (`*.pub`, Ed25519 hex).

`/etc/awnix/offline-trust.d`
: signer keys an admin added on this box.

`/var/lib/awnix/offline`
: the root-only staging copies.

`/var/log/awnix`
: the audit hash chain, `update-audit.jsonl`.

## SEE ALSO

awnix-update(8), awnix(8)
