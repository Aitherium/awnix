#!/usr/bin/env python3
"""Disk preflight — refuse to start a build that will die mid-way (owner, 2026-08-27).

Measured that day: the awnix-full layer died at
    At least 187MB more space needed on the / filesystem.
twenty minutes into a build, after the base layer had completed. No signal
beforehand — the build tool started trusting the disk, the disk was at 100%,
and the failure arrived as an ENOSPC at the innermost dnf step, naming the
filesystem rather than the build. That is the class this exists to end: a
build lane checks free space BEFORE it starts, clears what it safely can when
short, and exits 1 (refusing to start) when it still cannot — never a build
that burns twenty minutes and then dies.

  CLEAR order (safest only; each measured after the last):
    1. dangling images   podman image prune -f  (failed-build leftovers ONLY —
       never touches an image any container references, running or stopped)
    2. unused volumes    ONLY with --prune-volumes  (volumes can hold deliberate
       data — a flag, never a default)

  DELIBERATELY NOT USED: `podman system prune` — it removes STOPPED CONTAINERS.
  This host keeps ~610 stopped `bak-*` containers as the rollback hoard
  (owner decision; `podman system prune` on this box is an owner hard-deny).
  The clear path must never arm it.

ENGINE-CHECK mode (--engine-check): a second, separate question in the same
plane — can the container engine INITIALIZE its storage at all? Measured
2026-08-31 (D-2358): the overlay driver's metacopy probe started failing
deterministically (`chmod .../metacopy-check<pid>/merged/f: permission
denied` → `configure storage` error) while every container stayed
Up/healthy and the disk had hundreds of GB free — so every disk gate passed
and NO new storage op (build, restart, image load, new container, even
`podman ps`) was possible. Disk SPACE and storage INIT are different
questions; only a probe that asks "does podman answer?" sees the second.
Runs `podman ps -q` through the same WSL ladder.

Exit: 0 = engine storage init works | 1 = VIOLATION, storage init broken
(the D-2358 class — page, every build/restart is blocked) | 2 = could not
judge (wsl itself broken — the E_UNEXPECTED relay class — or podman errors
for an unclassified reason).

    check_disk_preflight.py --need 15
    check_disk_preflight.py --need 15 --clear
    check_disk_preflight.py --engine-check
    check_disk_preflight.py --self-test
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

#: The podman store lives on the fleet filesystem — inside the WSL distro on
#: the workstation, or the host on a Linux runner. Same ladder as
#: build_awnix_images._host_prefix.
DISTRO = "Debian"


class DeadError(RuntimeError):
    """Could not judge. Exit 2 — never 0."""


def _host_prefix() -> list[str]:
    if shutil.which("wsl"):
        return ["wsl", "-d", DISTRO, "-u", "root"]
    return []


def _shell_argv() -> list[str]:
    """How to run a bash snippet HERE: through the WSL hop, or natively.

    `_host_prefix()` returns [] when there is no `wsl` on PATH, and that empty
    list used to be handed straight to `subprocess.run` as argv -- which raises
    `IndexError: list index out of range` from deep inside subprocess, five
    frames from anything naming WSL.

    Measured 2026-09-08: that is what has been failing the awnix ISO lane. The
    lane was correctly repinned off this Windows workstation onto the AWS Linux
    runner (`aitheros-aws-8`), and this Windows-shaped preflight rode along --
    so the ISO build died 5 seconds in, before bootc was ever reached, with a
    traceback that names `subprocess.py` and not the missing assumption. Three
    consecutive red runs, and the lane's real subject was never exercised.

    On a Linux runner there is no hop to make: `df -BG /` is the SAME
    measurement, taken directly. So an absent WSL means "run it here", not
    "run it with no program".
    """
    prefix = _host_prefix()
    if prefix:
        return prefix
    shell = shutil.which("bash") or shutil.which("sh")
    if not shell:
        # No WSL and no POSIX shell: this host cannot answer the question at
        # all. DEAD (exit 2), never a pass -- the exit contract exists for
        # exactly this, and it is what should have happened originally.
        raise DeadError(
            "no `wsl` and no bash/sh on PATH, so the disk preflight cannot "
            "measure any filesystem on this host"
        )
    return [shell]


def _wsl(script: str, timeout: int = 120) -> tuple[int, str]:
    """BYTES not text=True (Windows' text pipe turns \\n into \\r\\n, which lands
    inside the command and breaks bash). Same plumbing as the build tool."""
    try:
        r = subprocess.run(
            _shell_argv(),
            input=script.replace("\r\n", "\n").encode("utf-8"),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A busy or wedging distro does not answer `df` in 30s. Letting this
        # escape made the tool CRASH, and rebuild-staged.sh printed its own
        # refusal -- "not enough free disk to build" -- for a measurement that
        # never happened. Measured 2026-09-20 on aitheros-shop-backend with
        # 294 GB free on the host: the next reader is sent to free disk that is
        # not full. A probe that cannot run says so (free_gb -> None -> exit 2);
        # it does not name a cause.
        return 124, f"timeout: no answer in {timeout}s"
    out = (r.stdout.decode("utf-8", errors="replace")
           + r.stderr.decode("utf-8", errors="replace"))
    return r.returncode, out


def free_gb() -> float | None:
    """Free space on the podman store filesystem, in GB, or None if unjudgeable."""
    code, out = _wsl("df -BG / | awk 'NR==2 {print $4}'", timeout=30)
    if code != 0:
        return None
    text = out.strip()
    if text.endswith("G"):
        try:
            return float(text[:-1])
        except ValueError:
            return None
    return None


def engine_storage_ok() -> tuple[int, str]:
    """Can podman initialize its storage? (0, text) | (1, text) | (2, text).

    Runs `podman ps -q` — the cheapest command that MUST open the store.
    Storage-init failures surface as the "configure storage" error family
    (metacopy probe, permission denied, layer-not-known); a wedged wsl relay
    surfaces as the E_UNEXPECTED family. The first is a VIOLATION (the
    storage plane is broken), the second is DEAD (could not judge).
    """
    try:
        code, out = _wsl("podman ps -q 2>&1", timeout=60)
    except subprocess.TimeoutExpired:
        return 2, "DEAD: podman ps timed out (60s) — could not judge"
    if code == 0:
        n = out.strip().splitlines()
        return 0, f"engine storage init OK ({len(n)} container(s) visible)"
    low = out.lower()
    if ("configure storage" in low or "metacopy" in low
            or "storage driver" in low):
        return (1, f"VIOLATION: podman cannot initialize storage — every build/"
                   f"restart/new-container op is blocked: {out.strip()[-300:]}")
    if (code == 4294967295 or "unexpected" in low
            or ("failed" in low and "wsl" in low)):
        return 2, f"DEAD: could not judge (wsl/podman error): {out.strip()[-200:]}"
    return 2, f"DEAD: podman failed for an unclassified reason: {out.strip()[-200:]}"


def clear_reclaimable(*, prune_volumes: bool) -> list[str]:
    """Clear reclaimable podman space. Returns what was done.

    Dangling-image prune ONLY — `podman system prune` is deliberately absent:
    it removes stopped containers, and this host's ~610 stopped `bak-*`
    containers are the owner's rollback hoard (hard-deny). An image any
    container references, running or stopped, is never dangling, so
    `image prune -f` cannot touch it.
    """
    done: list[str] = []

    code, out = _wsl("podman image prune -f 2>&1 | tail -2", timeout=900)
    if code != 0:
        done.append(f"image prune failed: {out.strip()[-200:]}")
    else:
        done.append("dangling images pruned")

    if prune_volumes:
        code, out = _wsl("podman volume prune -f 2>&1 | tail -2", timeout=900)
        if code != 0:
            done.append(f"volume prune failed: {out.strip()[-200:]}")
        else:
            done.append("unused volumes pruned (--prune-volumes was explicit)")
    return done


def preflight(need_gb: float, *, clear: bool, prune_volumes: bool) -> int:
    """Returns 0 = go, 1 = don't start, 2 = could not judge."""
    free = free_gb()
    if free is None:
        print(f"DEAD: could not measure free space on the podman store "
              f"(wsl {DISTRO}) — refusing to bless a build on an unmeasured disk.")
        return 2

    print(f"disk preflight: {free:.1f}G free, need >= {need_gb:.1f}G")
    if free >= need_gb:
        return 0

    if not clear:
        print(f"DISK SHORT: {free:.1f}G free, {need_gb:.1f}G needed. "
              f"Run with --clear to reclaim space, or free disk manually — "
              f"do NOT start this build.")
        return 1

    print(f"DISK SHORT ({free:.1f}G) — clearing reclaimable space first...")
    for step in clear_reclaimable(prune_volumes=prune_volumes):
        print(f"  - {step}")
    free_after = free_gb()
    if free_after is None:
        print("DEAD: could not re-measure after clearing — do not start the build.")
        return 2
    print(f"after clear: {free_after:.1f}G free")
    if free_after >= need_gb:
        return 0
    print(f"STILL SHORT after clearing ({free_after:.1f}G < {need_gb:.1f}G). "
          f"Unused volumes were NOT pruned (they can hold deliberate data) — "
          f"re-run with --prune-volumes, or free disk manually. Do NOT start.")
    return 1


def _self_test() -> int:
    """Prove the tool can still fail in every direction."""
    from unittest import mock

    failures = 0

    # 0. A HOST WITH NO WSL must still measure a filesystem, not crash.
    #    This is the arm that would have caught the awnix ISO lane failing three
    #    times in a row: `_host_prefix()` returns [] off Windows, that empty list
    #    reached `subprocess.run` as argv, and the lane died with
    #    `IndexError: list index out of range` raised inside subprocess.py --
    #    five frames away from anything mentioning WSL, and before bootc ran at
    #    all. The rule is that an absent WSL means "measure here", never "run no
    #    program"; only a genuine absence of ANY shell is DEAD.
    _real_which = shutil.which
    with mock.patch.object(sys.modules[__name__].shutil, "which",
                           side_effect=lambda n, *a, **k: (
                               None if n == "wsl" else _real_which(n, *a, **k))):
        try:
            argv = _shell_argv()
        except DeadError:
            argv = None  # acceptable only where no shell exists at all
        if argv is not None and not argv:
            print("SELF-TEST FAIL: no-WSL host produced an EMPTY argv — "
                  "subprocess.run() will raise IndexError, which is the awnix "
                  "ISO lane failure")
            failures += 1
        if argv:
            try:
                _wsl("true", timeout=30)
            except IndexError:
                print("SELF-TEST FAIL: no-WSL host still raises IndexError")
                failures += 1

    # 1. Short without --clear refuses (exit 1).
    with mock.patch.object(sys.modules[__name__], "free_gb", return_value=3.0):
        if preflight(10.0, clear=False, prune_volumes=False) != 1:
            print("SELF-TEST FAIL: short-without-clear did not refuse")
            failures += 1

    # 2. Short with --clear that frees enough passes (exit 0).
    calls = {"n": 0}

    def _freed():
        calls["n"] += 1
        return 3.0 if calls["n"] == 1 else 12.0

    with mock.patch.object(sys.modules[__name__], "free_gb", side_effect=_freed), \
         mock.patch.object(sys.modules[__name__], "clear_reclaimable",
                           return_value=["mocked clear"]):
        if preflight(10.0, clear=True, prune_volumes=False) != 0:
            print("SELF-TEST FAIL: clear-then-enough did not pass")
            failures += 1

    # 3. Clear that does NOT free enough still refuses (exit 1).
    with mock.patch.object(sys.modules[__name__], "free_gb", return_value=3.0), \
         mock.patch.object(sys.modules[__name__], "clear_reclaimable",
                           return_value=["mocked clear"]):
        if preflight(10.0, clear=True, prune_volumes=False) != 1:
            print("SELF-TEST FAIL: still-short-after-clear did not refuse")
            failures += 1

    # 4. Unmeasurable disk is DEAD (exit 2), never a pass.
    with mock.patch.object(sys.modules[__name__], "free_gb", return_value=None):
        if preflight(10.0, clear=True, prune_volumes=False) != 2:
            print("SELF-TEST FAIL: unmeasurable disk did not exit DEAD")
            failures += 1

    # 4b. A probe that TIMES OUT is unmeasurable, not a crash and not a cause.
    #     Before 2026-09-20 subprocess.TimeoutExpired escaped _wsl, the tool died
    #     with a traceback, and its caller printed "not enough free disk to build"
    #     with 294 GB free on the host.
    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="df", timeout=30)

    with mock.patch.object(subprocess, "run", _boom):
        if _wsl("df -BG /", timeout=30)[0] != 124:
            print("SELF-TEST FAIL: a timing-out probe did not return the timeout code")
            failures += 1
        if free_gb() is not None:
            print("SELF-TEST FAIL: a timing-out probe produced a free-space NUMBER")
            failures += 1
        if preflight(10.0, clear=False, prune_volumes=False) != 2:
            print("SELF-TEST FAIL: a timing-out probe did not exit DEAD")
            failures += 1

    # 5. Engine storage init: healthy store passes (0).
    with mock.patch.object(sys.modules[__name__], "_wsl",
                           return_value=(0, "abc\n123\n")):
        if engine_storage_ok() != (0, "engine storage init OK (2 container(s) visible)"):
            print("SELF-TEST FAIL: healthy storage did not pass engine-check")
            failures += 1

    # 6. The D-2358 class — storage init broken → VIOLATION (1), the pager.
    broken = ("1", "Error: configure storage: changing permissions on file "
                  "for metacopy check: chmod .../merged/f: permission denied")
    with mock.patch.object(sys.modules[__name__], "_wsl",
                           return_value=broken):
        code, _ = engine_storage_ok()
        if code != 1:
            print("SELF-TEST FAIL: broken storage init did not VIOLATION")
            failures += 1

    # 7. Wedged wsl relay (E_UNEXPECTED) is DEAD (2), never a violation.
    with mock.patch.object(sys.modules[__name__], "_wsl",
                           return_value=(4294967295, "Wsl/Service/E_UNEXPECTED")):
        code, _ = engine_storage_ok()
        if code != 2:
            print("SELF-TEST FAIL: wedged wsl relay did not exit DEAD")
            failures += 1

    # 8. An unclassified podman failure is DEAD (2) — never a clean pass.
    with mock.patch.object(sys.modules[__name__], "_wsl",
                           return_value=(1, "some other podman error")):
        code, _ = engine_storage_ok()
        if code != 2:
            print("SELF-TEST FAIL: unclassified podman error did not exit DEAD")
            failures += 1

    if failures:
        print(f"SELF-TEST FAIL: {failures} arm(s) failed")
        return 1
    print("SELF-TEST PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--need", type=float,
                    help="GB of free space the caller requires before starting")
    ap.add_argument("--need-default", type=float, default=10.0,
                    help=argparse.SUPPRESS)
    ap.add_argument("--clear", action="store_true",
                    help="attempt to reclaim space (dangling images, stopped "
                         "containers, build cache) when short")
    ap.add_argument("--prune-volumes", action="store_true",
                    help="ALSO prune unused volumes — volumes can hold deliberate "
                         "data; this flag is explicit for a reason")
    ap.add_argument("--engine-check", action="store_true",
                    help="ask whether podman can INITIALIZE its storage "
                         "(exit 1 = broken — every build/restart blocked), "
                         "instead of the free-space question")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if args.engine_check:
        code, text = engine_storage_ok()
        print(text)
        return code

    if args.need is None:
        args.need = args.need_default

    # Sanity: a build that needs <1G is a build that didn't think about disk.
    if args.need < 1.0:
        print(f"--need {args.need}G is below the 1G floor — a build that "
              f"cannot say what it needs should not be started either.")
        return 2

    return preflight(args.need, clear=args.clear, prune_volumes=args.prune_volumes)


if __name__ == "__main__":
    sys.exit(main())
