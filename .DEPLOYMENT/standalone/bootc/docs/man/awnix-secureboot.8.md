---
name: awnix-secureboot
section: 8
summary: report Secure Boot and NVIDIA driver state, and enroll the awnix module-signing key
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-secureboot
verbs_from: awnix-secureboot.py
verbs_mode: list-verbs
files_from: [awnix-secureboot.py, awnix-secureboot.service, Containerfile.awnix]
status: live
---
## SYNOPSIS

`awnix secureboot status` [`--json`] [`--brief`] [`--write`]

`awnix secureboot enroll` [`--reveal`|`--hash-file` *FILE*] [`--cert` *DER*]

`awnix secureboot doctor` [`--json`] · `awnix secureboot cert` [`--json`]

`awnix-secureboot --self-test` · `awnix-secureboot --list-verbs`

## DESCRIPTION

With UEFI Secure Boot on, Linux loads a kernel module only if a key the firmware or shim trusts has signed it. awnix GPU images sign the NVIDIA modules at build time with the awnix Machine Owner Key (MOK). The image ships only the public certificate at `/usr/share/awnix/secureboot/awnix-mok.der`; the private key never exists on the box. An image without the NVIDIA kernel driver reports `not-applicable`.

`awnix-secureboot.service` records the state on every boot and prints `awnix-secureboot: sb=... gpu=... kmod=... key=... state=...` to the serial console. It never blocks boot.

## COMMANDS

`status`
: the firmware (uefi or bios), the Secure Boot state, kernel lockdown, the NVIDIA GPU, the driver type, the module's signer and whether it loaded, and whether the key is enrolled or pending. `--write` saves `/var/lib/awnix/secureboot/status.json`; `--brief` prints one line and the next action. The states are `not-applicable`, `sb-off`, `ok`, `needs-enroll`, `enroll-pending`, `unsigned-kmod`, `kmod-rejected`, `driver-not-loaded` and `unknown`.

`enroll`
: queue the awnix certificate with `mokutil --import`. `--reveal` generates a one-time 8-character password (safe to type on a US keymap) and shows it once; it goes to `openssl passwd -6 -stdin` on stdin and is never stored or put on a command line. `--hash-file FILE` uses a crypt hash you prepared. Reboot afterwards and finish in MokManager.

`doctor`
: run checks SBD001 to SBD008 and print a verdict (ok, action-needed or unknown) with exactly one next action.

`cert`
: the shipped certificate's path and its SHA-256 and SHA-1 fingerprints (MokManager shows the SHA-1 one).

## EXIT STATUS

`0`
: ok, or not applicable.

`1`
: an action is needed, or the request was refused.

`2`
: could not judge.

## FILES

`/usr/share/awnix/secureboot/awnix-mok.der`
: the public module-signing certificate.

`/var/lib/awnix/secureboot/status.json`
: the state recorded at boot.

`/usr/lib/systemd/system/awnix-secureboot.service`
: records the state on every boot.

## SEE ALSO

mokutil(1), awnix(8), awnix-setup(8)
