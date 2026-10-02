# awdk on awnix — the local agent, air-gapped

This directory ships `adk` (awdk) as a loopback-only daemon on the awnix ai-full and
garg appliance images. It runs against the box's own llama.cpp server and never
reaches the network.

## Files and where they land

| source | installed at | what |
|---|---|---|
| `awdk-local.service` | `/usr/lib/systemd/system/` | the daemon: `python3.11 -m adk.server --host 127.0.0.1 --port 9001` |
| `awdk-local-health.service` | `/usr/lib/systemd/system/` | one-shot boot proof; prints the serial marker |
| `awdk.env` (ai-full) / `awdk.env.garg` | `/usr/lib/awdk/awdk.env` | vendor environment |
| `air_gap.yaml` | `/usr/lib/awdk/air_gap.yaml` | strict, loopback-only air-gap profile |
| `awnix-awdk.py` | `/usr/libexec/awnix/awnix-awdk` | the `awnix awdk` verb |
| `awdk/examples/airgap_local_agent.py` | `/usr/lib/awdk/examples/` | the example local agent |

Admin overrides go in `/etc/awdk/awdk.env`. It is read after the vendor file, and the
later value wins.

## Ports

There are no external ports. The daemon binds `127.0.0.1:9001`, and the llama.cpp
server binds `127.0.0.1:8199` on ai-full or `127.0.0.1:8089` on garg. Two layers keep
the daemon dark:

1. **In the process.** With `AITHER_AIR_GAP_CONFIG` pointing at `air_gap.yaml`,
   `adk.compliance.egress_guard` refuses non-loopback connects made through httpx,
   urllib, requests and the stdlib socket layer (which the stdlib asyncio loop uses)
   before a byte is sent, and audits the refusal to
   `/var/lib/awdk/compliance/audit.jsonl`. uvloop dials through libuv and would
   bypass the socket layer, so under the air gap `adk.server` runs uvicorn with
   `loop="asyncio"` and the guard resets a uvloop event-loop policy. Child processes
   and hand-built uvloop loops are outside this layer; the kernel layer below covers
   them.
2. **In the kernel.** The unit sets `IPAddressDeny=any` and `IPAddressAllow=localhost`,
   which drops the unit's off-loopback packets even without the guard.

`AITHER_OFFLINE=1` also makes `adk.server` bind loopback whenever `--host` is not
given.

## Operator commands

```sh
awnix awdk status --json        # env files, URLs, adk version (no network)
awnix awdk health --json        # verdict ok|degraded|dark; exit 0/1/2
awnix awdk probe-egress --json  # exit 0 = the guard refused a TEST-NET dial
awnix awdk run-example --corpus /srv/docs --out /var/tmp/agent-evidence.json
```

`health` runs four checks: llama `/v1/models`, the daemon's `/health` (its `air_gap`
block must report `mode=strict` AND `guard_installed=true`), a `/proc/net/tcp{,6}`
scan that must show `:9001` only on loopback, and an egress probe. The probe runs in
a separate interpreter, so it proves the profile seals a process, not that the
daemon's own process is guarded; the `guard_installed` check covers that. Any failed
check gives `degraded`. A daemon that does not answer gives `dark`.

## Evidence the example produces

`run-example` writes a JSON file with `model`, `base_url`,
`rows[{claim, source, page}]`, `sha256_inputs`, `wall_s`, `ttft_s`,
`egress_violations[]`, `air_gap{mode, sealed}` and `verdict`. The exit code is 0 for
cited rows with zero egress, 1 for a failure, a blocked egress or an egress the
enforcer recorded without refusing, and 2 when no model is reachable or the process
cannot be sealed. Only a `strict` enforcer counts as sealed: an `audit` profile
records egress but lets it through, so the example refuses to run under it (exit 2
`unsealed`) unless `--no-seal` is given, and even then any recorded violation fails
the run. Every citation is checked: it must name a page that exists in the file it
cites.

## Boot proof

`awdk-local-health.service` prints one line to the console:

```
awdk-local: ok model=<id> bind=127.0.0.1:9001 airgap=strict
```

`bind=` is the listener address(es) measured in `/proc/net/tcp{,6}`, not the
configured URL, so a wildcard listener shows as `bind=0.0.0.0:9001`.

## What is and is not proven

Everything above is measured at unit level only (pytest against stub servers). No
image ships these files yet (D-2699: the Containerfile COPY/enable is not done, and
the images install awdk from PyPI, whose releases up to 3.8.29 have no
`adk.compliance.egress_guard`). The appliance air gap is not measured until an image
built with this wiring boots with `--network=none` and `awnix awdk health --json`
exits 0. Audit rows are unsigned unless `AITHER_AUDIT_SIGNING_KEY` is provisioned,
and neither env file provisions one, so they are not tamper-evident.

The static checker is `AitherOS/dev/tools/check_awdk_on_awnix.py` (AWK001–AWK007).
