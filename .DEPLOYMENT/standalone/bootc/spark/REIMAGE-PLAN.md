# DGX Spark: DGX OS to awnix on bare metal (owner decision)

**Status: DESIGN. Nothing here has been run.** Reimaging is a B2 action: it cannot be undone
in place, it takes production inference down, and the only rollback is NVIDIA's recovery
media. No agent runs it. `spark-reimage.sh --apply` refuses when an agent marker is set, and
it needs `AWNIX_REIMAGE_CONFIRM` to equal the machine's DMI serial.

## What the Spark is today (measured 2026-09-28, read-only probe)

| item | value |
|---|---|
| OS | Ubuntu 24.04.4 LTS (DGX OS), kernel `6.17.0-1029-nvidia`, aarch64, 20 cores |
| memory | 127.6 GB unified, about 97-101 GB available |
| disk | `/` 3.7 TB, 3.3 TB used, 279 GB free |
| serving | `gemma4-12b-w8a16` (vLLM, :8124, health 200), `aither-code-embed-spark` (llama.cpp, :8229, unhealthy), `aither-ltx-video-dgx` (:8795), mesh agent, strata node, secrets replica, DNS replica, mesh discovery (:8099) |
| engine | docker only. **podman is not installed** |
| mesh | the host is on headscale (`hs.aitherium.com`) as `dgx-spark`, 100.64.0.38 |

`check_dgx_memory_headroom.py` already reports DGXM002 before any awnix work (vLLM holds
16.5 GB that could be reclaimed). That is the baseline, and the awnix lane must not add to it.

## Before any of this: the container lane

The reversible path proves the image first and needs one owner precondition:

1. **Install podman on the Spark** (`sudo apt-get install podman`, undone with
   `apt-get remove podman`). The agent lane does not install packages on the production
   inference host. The auto-mode classifier refused that on 2026-09-28, so it is the owner's call.
2. `python AitherOS/dev/tools/spark_awnix_node.py build` builds `localhost/awnix:arm64-<sha7>`
   natively. It runs under `nice -n 19 ionice -c 3`, `--memory 12g` and `--cpu-shares 128`,
   and it refuses when MemAvailable is below 24 GB or free disk is below 60 GB.
3. `spark_awnix_node.py run` boots that image as a systemd container: CPU only, no published
   port, no GPU device. `join` puts it on headscale, `ladder` records the ARM64 offline row,
   and `teardown` removes all of it.

Only when step 3 shows `systemctl is-system-running` = running/degraded is there anything
worth putting on metal.

## The driver risk (the reason this is a decision, not a task)

awnix is CentOS Stream 9 (`quay.io/centos-bootc/centos-bootc:stream9`, kernel 5.14 line). The
Spark's GB10 runs today on NVIDIA's own `6.17-nvidia` Ubuntu kernel, which is what DGX OS
ships. As of this writing:

- NVIDIA has published no GB10 driver or CUDA stack for EL9 aarch64 that has been measured
  here. Until one is installed and `nvidia-smi` works on awnix, the Spark is a **20-core CPU
  box with no GPU**.
- The whole production pool depends on that GPU: gemma4 perception, code-embed, LTX video,
  and the Spark half of the pooled DeepSeek. Reimaging before the driver is proven turns the
  fleet's second-largest inference node into a CPU node.

The smallest way to retire the risk is a Containerfile that layers the NVIDIA open kernel
modules for EL9 aarch64 on the awnix image, built and booted in a **VM** on the Spark, with
`nvidia-smi` passing inside it. Only after that does metal make sense.

## Downtime

From `--apply` until the first awnix boot is about 30-60 minutes (install, then reboot). Pool
downtime is that plus the driver work, which is unbounded until the driver risk is retired.
During that window, route perception and embeddings through `awmodels` to the 5090 (the
owner ruling keeps vision on the Spark, so this is a posture change the owner makes).

## Rollback

`bootc install to-existing-root` replaces the bootloader and deploys awnix alongside the old
root's files. **It is not a dual-boot.** Rollback means:

1. Boot the NVIDIA DGX Spark recovery USB. Make it and test-boot it **before** step 5.
2. Reinstall DGX OS.
3. Restore `~/models`, the docker volumes and `/etc/tailscale` from the step-1 backup.
   3.3 TB is in use, so the backup is sized first.

## The command, for the record

```sh
sh .DEPLOYMENT/standalone/bootc/spark/spark-reimage.sh --plan        # prints this sequence
sh .DEPLOYMENT/standalone/bootc/spark/spark-reimage.sh --preflight   # read-only, prints the serial
AWNIX_REIMAGE_CONFIRM=<serial> sh .DEPLOYMENT/standalone/bootc/spark/spark-reimage.sh --apply
```

`check_spark_awnix_node.py` SPK006 fails if the evidence file ever holds a `reimage` step with
an rc. An agent-recorded reimage is a violation by definition.

## Decide

- **Keep the Spark on DGX OS** and run awnix on it only as a container or VM node. This is
  the recommendation until a GB10 driver boots on EL9.
- **Reimage** once the VM proof passes. Accept pool downtime, back up first, and test the
  recovery USB first.
