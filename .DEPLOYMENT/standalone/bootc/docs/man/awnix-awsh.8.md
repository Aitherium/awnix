---
name: awnix-awsh
section: 8
summary: check and set up awsh for offline use on an awnix machine
applies_to: ["*"]
cli: /usr/libexec/awnix/awnix-awsh
verbs_from: awsh/awnix-awsh.py
verbs_mode: list-verbs
files_from: [awsh/awnix-awsh.py, awsh/awsh-offline.sh, Containerfile.awnix]
status: live
---
## SYNOPSIS

`awnix awsh doctor` [`--json`] · `awnix awsh status` [`--json`]

`awnix awsh first-run` [`--quiet`] [`--json`]

`awnix awsh config show` [`--json`] · `awnix awsh config get` *KEY*

`awnix awsh --self-test` · `awnix awsh --list-verbs`

## DESCRIPTION

awsh is the terminal agent shell. On awnix it runs offline by default: it talks to the model server on this machine and never to a cloud gateway. Two user services back it, and both listen on 127.0.0.1 only: `awsh-harness.service` (127.0.0.1:8362, the harness daemon awsh drives) and `awsh-agent.service` (127.0.0.1:9001, the local agent). The model server is 127.0.0.1:8199 on the base and ai-full images; an appliance image points awsh at its own model server with a drop-in.

## COMMANDS

`doctor`
: check that awsh can work with no network: offline mode on, every configured URL on this machine, the model server answering `/v1/models`, the harness listening only on loopback and answering `/health`, the agent (if it listens) only on loopback, and `~/.aither/harness_token` with mode 0600. It never connects outside this machine. `--json` prints `{verdict, checks[{id, ok, detail}], nonloopback_urls[], llm_url, model, harness_bind}`; `nonloopback_urls` must be empty. It is a static scan of the configuration, not a trace of connections.

`status`
: offline mode, the model, harness and agent URLs, whether the harness token exists, and the state of the two user services.

`first-run`
: create `~/.aither` (0700) and `~/.aither/harness_token` (0600) if absent and write `~/.aither/awsh-agent.env` with the model URL from the configuration. Idempotent; it never writes a cloud key, and offline it refuses a non-loopback `llm_url`. Both user services run it before they start.

`config`
: `config show` lists each layer and where every value came from; `config get KEY` prints one value and exits 1 when it is not set.

## CONFIGURATION

Layers, lowest precedence first: `/usr/lib/awsh/shell.yaml` (the image's, do not edit), `/usr/lib/awsh/shell.d/*.yaml` in name order (an appliance's drop-in), `/etc/awsh/shell.yaml` (this machine: edit this one), `~/.aither/shell.yaml` (per user), then the environment (`AITHER_OFFLINE`, `AITHER_LLM_URL`, `AITHER_API_URL`). Each file holds flat `key: value` lines; `offline: true` means awsh never tries a cloud endpoint.

To point awsh at a different local model server, add `llm_url: http://127.0.0.1:8080/v1` to `/etc/awsh/shell.yaml`, run `awnix awsh first-run`, then `systemctl --user restart awsh-agent`.

## EXIT STATUS

`0`
: pass.

`1`
: fail.

`2`
: could not judge (a listener table or a configuration file could not be read). A check that could not run is never reported as a pass.

## FILES

`/usr/libexec/awnix/awnix-awsh`
: this command.

`/usr/lib/awsh/shell.yaml`
: the image's awsh configuration.

`/etc/profile.d/awsh-offline.sh`
: keeps login shells offline.

`/usr/lib/systemd/user/awsh-harness.service`
: the harness daemon (loopback).

`/usr/lib/systemd/user/awsh-agent.service`
: the local agent (loopback).

## SEE ALSO

awnix(8), awsh(1)
