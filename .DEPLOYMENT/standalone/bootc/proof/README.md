# awnix proof harness

Proof-plan Steps 0-6 (`.AITHERIUM/BUSINESS/GOV/AFRL-EXTREME-COMPUTING-FY27/05-PROOF-PLAN.md`)
as exit-code harnesses on the node, plus a one-file offline verifier for the
verifier laptop.

| file | where it runs | install target (NOT YET INSTALLED by any image) |
|---|---|---|
| `awnix-proof.py` | the awnix node | `/usr/libexec/awnix/awnix-proof` (`awnix proof <verb>`) |
| `awnix_proof/` | the awnix node | `/usr/lib/awnix/proof/awnix_proof/` |
| `awnix_proof_verify.py` | the node **and** the verifier laptop | `/usr/lib/awnix/proof/awnix_proof_verify.py` and a copy on the laptop |

**Not installed yet.** Neither `Containerfile.awnix` nor
`Containerfile.garg-appliance` copies this directory; until they do, run it from
a checkout (`python3 awnix-proof.py <verb>`). APH006 reports the gap, allowlisted
under D-2698 (the Containerfiles belong to the image workstream).

Exit codes everywhere: **0 PASS · 1 FAIL · 2 COULD-NOT-JUDGE**. A missing tool,
file or flag is never a pass.

## Evidence layout (`/var/lib/proof`, mode 0700)

- `<step>.log`: a `# argv:` line, then for each command `$ argv`, its output and
  `rc=N`, then one `CHECK name: PASS|FAIL|COULD-NOT-JUDGE want=… got=…` per
  criterion. The last line is `VERDICT: PASS|FAIL|COULD-NOT-JUDGE`.
- `<step>.json`: `{schema:1, step, verdict, exit, started, ended, wall_s,
  checks[{name, cmd, rc, want, got, ok}], notes, extra, log_sha256, audit}`.
- `proof-audit.jsonl` + `.anchor`: an awdit chain in the same format as
  `AitherOS/packages/awdit`. Each step appends `proof.<step>` and binds the
  sha256 of its log. Step 4 also appends `proof.step4.refused|verified|staged`
  for each bundle.

## Runbook

| proof-plan step | command on the node | PASS means |
|---|---|---|
| 0 pre-flight | `awnix proof step0 --init-key [--image-seal DIR --image-key HEX]` | tools present; the node proof key exists (the witness writes `pubkey.hex` on paper); the booted digest can be read; the image seal verifies (G9) |
| 1 air-gapped boot | `awnix proof step1 --expect-digest sha256:<paper> --account root --account <svc>` | no default route (v4/v6); `ss -H -ltun` shows 0 non-loopback listeners; booted digest == paper; every account `L`; `sshd -T` gives `passwordauthentication no` |
| 2 no-egress watch | `awnix proof egress start` (30 min default), run Steps 3-5, `awnix proof egress stop`, then `awnix proof step2 --tap /media/usb/egress-tap.pcap [--air-gap-log F] [--min-window-s 1800]` | every capture has frames, spans >= 90% of the window (first to last frame), and has 0 disallowed frames. Allowed: ARP; ND (133-137, hop limit 255) and MLD (130-132, 143, hop limit 1) to a link-local or multicast destination. Any IPv6 fragment counts. Loopback is skipped only on the node capture and only when the link layer says loopback (`-i any` SLL/SLL2); 127/8 or ::1 on a wire counts. An empty or short capture is COULD-NOT-JUDGE. Without `--tap` the verdict is COULD-NOT-JUDGE unless `--node-only` |
| 3 offline agent task | `awnix proof step3 --run-dir OUT --expect-key HEX [--cmd 'afrl_offline_agent_task.py …']` | the output seal verifies against the key, awdit.log chain is ok, one `tool_call` record per `run.json` tool call |
| 4 signed update | `awnix proof step4 --bundle-good G --bundle-flipped F --bundle-wrongkey W --trust-key HEX [--platform-verify-cmd 'awnix offline-update verify {bundle}']`, reboot, then `awnix proof step4 --post --expect-digest sha256:N1 --banner S` | G verifies and stages; F is refused (`content-mismatch`/`archive-digest-mismatch`) and W is refused (`untrusted-key`) **before** any stage command runs, and each refusal is audited; after the reboot the booted digest is N+1 and the banner is present |
| 5 rollback | `awnix proof step5 --pre`, reboot once, then `awnix proof step5 --post [--run-before A --run-after B --expect-key HEX [--deterministic]]` | booted == the rollback target recorded at `--pre`; the `/var` marker sha is unchanged; the boot_id changed; wall time recorded; the payload model digest is equal (text equality only with `--deterministic`) |
| 6 audit export | `awnix proof step6 --out /run/media/<usb>` | the export dir is awseal-signed with the node key, every chain in it verifies, and `proof-<ts>.tar` plus its sha256 are printed |

Staging for Step 4 runs `$AWNIX_PROOF_STAGE_CMD` (with `{bundle}`/`{archive}`
substituted) when that is set, otherwise
`bootc switch --transport oci-archive <archive>`. A chunked g5 bundle has no
single archive, so it needs the stage command, e.g.
`AWNIX_PROOF_STAGE_CMD='awnix offline-update apply {bundle}'`.

`awnix proof status [--json]` shows each step's verdict and the audit chain
state.

## Verifier laptop (Step 6, offline)

The laptop needs only Python 3.10+. If `cryptography` is missing, a pure-Python
Ed25519 (RFC 8032) is used; `AWNIX_PROOF_FORCE_PUREPY=1` forces that path.

```bash
python3 awnix_proof_verify.py --self-test
python3 awnix_proof_verify.py verify /media/usb/proof-<ts>.tar --node-key <pubkey.hex from paper> [--json]
python3 awnix_proof_verify.py tamper-demo /media/usb/proof-<ts>.tar --node-key <hex> [--flip-index K] [--delete-index J]
```

`verify` prints `{seal:{signature_ok, content_ok, key_trusted, diff},
chains:[{file, chain_ok, first_broken_index, kind, count}], steps, egress,
unsealed, fixture_mode}`. `kind` is `altered | gap | truncated | unparseable |
anchor-mismatch`, and `first_broken_index` is 0-based.

`verify` exits 0 only when ALL hold: seal signature and content ok under the
paper key; no file outside the seal's signed file map (`unsealed`); every chain
ok; every step verdict PASS; every sealed capture readable, non-empty and with 0
disallowed frames (a capture named `*tap*` is judged as the wire capture); no
fixture-mode evidence. Steps, chains and captures are read ONLY from sealed
paths. `--integrity-only` answers just "is this the node's untampered export?"
(seal, key, chains, unsealed files). `--allow-fixtures` accepts a rehearsal
export and still reports it.

## Fixture (rehearsal) mode

`AWNIX_PROOF_FIXTURES=<dir>` replaces every command with canned output for
tests. It is refused (exit 2) unless `AWNIX_PROOF_TEST_MODE=1` is also set, and
it stamps `fixture_mode: true` into each step JSON, each awdit record and the
step6 seal meta. The verifier fails such an export unless `--allow-fixtures`.
An export made in fixture mode is a rehearsal of the harness, never evidence.

`tamper-demo` makes copies of the export. In one it flips one byte of record k,
in another it deletes record j, and in a third it cuts the tail. It exits 0 only
if all of these hold: the clean copy gives `chain_ok=true`; the flip gives
`altered` at k; the delete gives `gap` at j; the cut gives `truncated`, because
the anchor still records the full count.

## Known limits

- The anchor sits on the same disk as the chain. Someone with root on the node
  can rewrite both, and the export's awseal seal is then the protection. That is
  why the witness writes the node public key on paper at Step 0.
- The node capture is supporting evidence only. The tap capture is
  authoritative.
- These harnesses judge a booted image. Until they have run on one, Steps 1, 2,
  4 and 5 are BUILT-UNMEASURED. Step 1 fails as built while `cockpit.socket` or
  a console listens on 0.0.0.0 (G2).

Gate: `python AitherOS/dev/tools/check_awnix_proof_harness.py` (APH001-006).
