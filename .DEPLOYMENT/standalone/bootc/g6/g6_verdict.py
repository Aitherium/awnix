#!/usr/bin/env python3
"""g6_verdict.py -- turn a G6 serial log into verdict.json and one G6-VERDICT line.

G6 (AFRL proof plan, Steps 4-5) asks one question with three parts: did a real
`bootc upgrade` from a credential-free local registry boot N+1, did `bootc rollback`
return the node to N's exact digest, and did greenboot fall back on its own from a
faulty N+2 -- with /var agent state intact the whole way. The guest agent
(g6-agent.sh) prints markers to ttyS0; this file judges them. It never runs a VM.

Markers (one per line, anywhere in the log; everything else is ignored):
    G6-PLAN: steps=upgrade,rollback,fallback greenboot_max=3 grub_counting=present
    G6-MARK: boot=<k> role=<n|n1|n2>                       (every boot, early)
    G6: boot=<k> booted=sha256:.. staged=sha256:.. rollback=sha256:.. phase=<p> sentinel=<ok|missing> role=<r>
    G6-STEP: <upgrade|rollback|fallback> rc=<n> [via=..]
    G6-STAGE: n2 rc=<n>
    G6-WANT: stable=<role>
    G6-HOLD: boot=<k> role=n2 ...                          (agent on N+2: waits, never reboots)
    G6-END

An AGENT reboot of N+2 (the pre-2026-09-28 line `G6: still on n2 ... rebooting`) means
the return to N was not automatic: the fallback step is then NO-AUTO-FALLBACK even when
the next boot is N. Measured: hosted runs 36415942227 and 36417253922 recorded PASS, but
their boot 9 reached multi-user on N+2 and only the agent's reboot brought N back.

Usage:
    g6_verdict.py --serial serial.log [--guest-log agent.log] [--digests digests.json]
                  [--out verdict.json] [--container-only]
    g6_verdict.py --self-test

Exit: 0 PASS · 1 UPGRADE-FAILED | ROLLBACK-FAILED | NO-AUTO-FALLBACK | STATE-LOST
      2 UNJUDGED | CONTAINER-ONLY (never 0 on silence). The last stdout line is
      `G6-VERDICT: <V>`.

Python 3.9-compatible on purpose: the awnix guest and CentOS hosts ship 3.9.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional

SCHEMA = 1
FAIL_VERDICTS = ("UPGRADE-FAILED", "ROLLBACK-FAILED", "NO-AUTO-FALLBACK", "STATE-LOST")
EXIT_FOR = {"PASS": 0, "UNJUDGED": 2, "CONTAINER-ONLY": 2}
EXIT_FOR.update({v: 1 for v in FAIL_VERDICTS})
DEFAULT_PLAN = ["upgrade", "rollback", "fallback"]

_KV = re.compile(r"([a-z_0-9]+)=(\S+)")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# Serial logs carry ANSI escapes and getty/kernel noise interleaved mid-line.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _kv(rest: str) -> Dict[str, str]:
    return {k: v for k, v in _KV.findall(rest)}


def parse_markers(text: str) -> Dict[str, object]:
    """Pull every G6 marker out of a serial log, in order.

    The agent writes each marker to ttyS0 AND /dev/kmsg, so a real serial log carries
    every line twice (the kmsg copy prefixed `[   5.61] `). Markers are therefore
    de-duplicated: a MARK per boot number, a boot line per (boot, phase), and a
    STEP/STAGE/WANT per (text, boot it followed). Measured on hosted run 36411166098,
    where counting the copies turned 4 N+2 boots into 8 and failed a real fallback.
    """
    ev: Dict[str, object] = {
        "plan": None, "boots": [], "marks": [], "steps": [], "stages": [],
        "wants": [], "holds": [], "agent_n2_reboots": 0, "end": False,
    }
    seen = set()

    def first(key: tuple) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    for raw in text.splitlines():
        line = _ANSI.sub("", raw).replace("\r", "")
        i = line.find("G6")
        if i < 0:
            continue
        line = line[i:].strip()
        nboots = len(ev["boots"])  # type: ignore[arg-type]
        if line.startswith("G6-PLAN:"):
            ev["plan"] = _kv(line[len("G6-PLAN:"):])
        elif line.startswith("G6-MARK:"):
            kv = _kv(line[len("G6-MARK:"):])
            if first(("mark", kv.get("boot"), kv.get("role"))):
                ev["marks"].append(kv)  # type: ignore[union-attr]
        elif line.startswith("G6-STEP:"):
            rest = line[len("G6-STEP:"):].strip()
            if not first(("step", rest, nboots)):
                continue
            name = rest.split()[0] if rest.split() else ""
            kv = _kv(rest)
            kv["step"] = name
            kv["_boot_index"] = str(nboots)
            ev["steps"].append(kv)  # type: ignore[union-attr]
        elif line.startswith("G6-STAGE:"):
            rest = line[len("G6-STAGE:"):].strip()
            if first(("stage", rest, nboots)):
                ev["stages"].append(_kv(rest))  # type: ignore[union-attr]
        elif line.startswith("G6-WANT:"):
            rest = line[len("G6-WANT:"):].strip()
            if first(("want", rest, nboots)):
                ev["wants"].append(_kv(rest))  # type: ignore[union-attr]
        elif line.startswith("G6-HOLD:"):
            kv = _kv(line[len("G6-HOLD:"):])
            if first(("hold", kv.get("boot"))):
                ev["holds"].append(kv)  # type: ignore[union-attr]
        elif line.startswith("G6-END"):
            ev["end"] = True
        elif line.startswith("G6:") and "still on n2" in line and "reboot" in line:
            # The harness agent rebooted N+2 itself (pre-2026-09-28 g6-agent.sh).
            if first(("agent-n2-reboot", nboots)):
                ev["agent_n2_reboots"] = int(ev["agent_n2_reboots"]) + 1  # type: ignore[call-overload]
        elif line.startswith("G6:"):
            kv = _kv(line[len("G6:"):])
            if "booted" in kv and first(("boot", kv.get("boot"), kv.get("phase"), kv.get("booted"))):
                ev["boots"].append(kv)  # type: ignore[union-attr]
    return ev


def _rc(step: Dict[str, str]) -> Optional[int]:
    try:
        return int(step.get("rc", ""))
    except ValueError:
        return None


def _first_boot_after(boots: List[Dict[str, str]], index: int) -> Optional[Dict[str, str]]:
    return boots[index] if index < len(boots) else None


def judge(ev: Dict[str, object], digests: Dict[str, object], container_only: bool = False) -> Dict[str, object]:
    """Pure judgement: markers + host digests -> verdict dict (schema 1)."""
    boots: List[Dict[str, str]] = ev["boots"]  # type: ignore[assignment]
    marks: List[Dict[str, str]] = ev["marks"]  # type: ignore[assignment]
    steps: List[Dict[str, str]] = ev["steps"]  # type: ignore[assignment]
    plan_kv: Dict[str, str] = ev["plan"] or {}  # type: ignore[assignment]
    plan = [s for s in (plan_kv.get("steps") or ",".join(DEFAULT_PLAN)).split(",") if s]
    try:
        gb_max = int(plan_kv.get("greenboot_max") or digests.get("greenboot_max") or 3)
    except (TypeError, ValueError):
        gb_max = 3

    # N is the deployment the node was INSTALLED with, as the guest itself reports it
    # at boot 1. The registry digest of :n is recorded too, but an install from
    # containers-storage may carry a different manifest digest than the pushed one;
    # rollback correctness means "back to the exact deployment booted at boot 1".
    d_n = boots[0].get("booted") if boots else None
    d_n1 = digests.get("n1") if isinstance(digests.get("n1"), str) else None
    d_n2 = digests.get("n2") if isinstance(digests.get("n2"), str) else None
    # Fall back to role-derived digests when the host file is absent (fixtures, a
    # log handed over without its sidecar). Recorded as such.
    derived = False
    for b in boots:
        if b.get("role") == "n1" and not d_n1:
            d_n1, derived = b.get("booted"), True
        if b.get("role") == "n2" and not d_n2:
            d_n2, derived = b.get("booted"), True

    out: Dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "UNJUDGED",
        "exit_code": 2,
        "reason": "",
        "base_image": digests.get("base_image"),
        "digests": {"n": d_n, "n1": d_n1, "n2": d_n2,
                    "n_registry": digests.get("n_registry"), "derived_from_log": derived},
        "plan": plan,
        "steps": [],
        "boots": max([len(marks), len(boots)] + [int(m.get("boot", 0) or 0) for m in marks]),
        "greenboot_max": gb_max,
        "greenboot_attempts": 0,
        "grub_counting": plan_kv.get("grub_counting") or digests.get("grub_counting"),
        "fallback_reboot": plan_kv.get("fallback_reboot") or digests.get("fallback_reboot"),
        "agent_reboots_on_n2": int(ev.get("agent_n2_reboots") or 0),  # type: ignore[call-overload]
        "state_sentinel_intact": None,
        "kvm": bool(digests.get("kvm")),
        "mode": digests.get("mode") or ("container" if container_only else "vm"),
        "inject": digests.get("inject") or "none",
        "runner": digests.get("runner"),
        "gh_run_id": digests.get("gh_run_id"),
        "started_at": digests.get("started_at"),
        "ended_at": digests.get("ended_at"),
    }

    def finish(verdict: str, reason: str) -> Dict[str, object]:
        out["verdict"] = verdict
        out["exit_code"] = EXIT_FOR[verdict]
        out["reason"] = reason
        return out

    if container_only or out["mode"] == "container":
        return finish("CONTAINER-ONLY", "no KVM: images built and pushed, nothing booted -- never MEASURED")
    if not boots:
        return finish("UNJUDGED", "no G6 boot line in the log: the agent never reported")

    n2_boots = sum(1 for m in marks if m.get("role") == "n2")
    if n2_boots == 0:
        n2_boots = sum(1 for b in boots if b.get("role") == "n2" or (d_n2 and b.get("booted") == d_n2))
    out["greenboot_attempts"] = n2_boots
    sentinels = [b.get("sentinel") for b in boots]
    out["state_sentinel_intact"] = bool(sentinels) and all(s == "ok" for s in sentinels)

    rows: List[Dict[str, object]] = []
    by_name = {}
    for s in steps:
        by_name.setdefault(s["step"], s)

    first_fail: Optional[str] = None
    fail_reason = "first failing step decides"
    missing: List[str] = []
    for name in plan:
        s = by_name.get(name)
        if name == "upgrade":
            expect = d_n1
            if s is None:
                missing.append(name)
                continue
            nxt = _first_boot_after(boots, int(s["_boot_index"]))
            got = nxt.get("booted") if nxt else None
            ok = _rc(s) == 0 and got is not None and got == expect and expect != d_n
            rows.append({"step": name, "expect_digest": expect, "booted_digest": got, "rc": _rc(s), "ok": ok})
            if got is None:
                missing.append(name)
            elif not ok and first_fail is None:
                first_fail = "UPGRADE-FAILED"
        elif name == "rollback":
            expect = d_n
            if s is None:
                missing.append(name)
                continue
            nxt = _first_boot_after(boots, int(s["_boot_index"]))
            got = nxt.get("booted") if nxt else None
            ok = _rc(s) == 0 and got is not None and got == expect
            rows.append({"step": name, "expect_digest": expect, "booted_digest": got, "rc": _rc(s),
                         "via": s.get("via"), "ok": ok})
            if got is None:
                missing.append(name)
            elif not ok and first_fail is None:
                first_fail = "ROLLBACK-FAILED"
        elif name == "fallback":
            last = boots[-1].get("booted")
            if s is None:
                # No verdict from the agent. If N+2 kept booting past the greenboot
                # budget, the log ALREADY proves there was no fallback; anything
                # shorter is a run that simply stopped.
                if n2_boots > gb_max + 1:
                    rows.append({"step": name, "expect_digest": "!= n2", "booted_digest": d_n2,
                                 "rc": None, "ok": False, "n2_boots": n2_boots})
                    if first_fail is None:
                        first_fail = "NO-AUTO-FALLBACK"
                else:
                    missing.append(name)
                continue
            agent_reboots = int(out["agent_reboots_on_n2"])  # type: ignore[call-overload]
            ok = (_rc(s) == 0 and n2_boots >= 1 and n2_boots <= gb_max + 1
                  and last is not None and last != d_n2 and agent_reboots == 0)
            rows.append({"step": name, "expect_digest": "!= n2 (n1 or n)", "booted_digest": last,
                         "rc": _rc(s), "ok": ok, "n2_boots": n2_boots,
                         "agent_reboots_on_n2": agent_reboots})
            if not ok and first_fail is None:
                if agent_reboots and n2_boots >= 1:
                    first_fail = "NO-AUTO-FALLBACK"
                    fail_reason = ("N came back only after the harness agent rebooted N+2 (%d agent "
                                   "reboot(s)): not an automatic fallback" % agent_reboots)
                elif n2_boots == 0:
                    missing.append("fallback (N+2 never booted)")
                else:
                    first_fail = "NO-AUTO-FALLBACK"
    out["steps"] = rows

    digest_list = [d_n, d_n1 if "upgrade" in plan else None, d_n2 if "fallback" in plan else None]
    present = [d for d in digest_list if d]
    bad = [d for d in present if not _DIGEST.match(str(d))]
    if first_fail:
        return finish(first_fail, fail_reason)
    if bad:
        return finish("UNJUDGED", "malformed digest(s): %s" % ", ".join(map(str, bad)))
    if len(set(present)) != len(present):
        return finish("UNJUDGED", "digests are not distinct -- the images did not differ")
    if missing:
        return finish("UNJUDGED", "no result for: %s" % ", ".join(missing))
    if not out["state_sentinel_intact"]:
        return finish("STATE-LOST", "a boot reported sentinel=missing: /var state did not survive")
    if not ev["end"]:
        return finish("UNJUDGED", "no G6-END: the agent never closed the run")
    return finish("PASS", "every planned step booted the expected digest; /var sentinel intact")


def run(serial: str, guest_log: Optional[str], digests_path: Optional[str],
        out_path: Optional[str], container_only: bool) -> int:
    text = ""
    for p in (serial, guest_log):
        if p and os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                t = fh.read()
            ev_t = parse_markers(t)
            # Prefer the serial; use the guest's /var copy only when the serial lost
            # the end (getty re-taking ttyS0 was measured to eat a tail on 2026-09-13).
            if not text or (not parse_markers(text)["end"] and ev_t["end"]):
                text = t
    digests: Dict[str, object] = {}
    if digests_path:
        try:
            with open(digests_path, "r", encoding="utf-8") as fh:
                digests = json.load(fh)
        except (OSError, ValueError) as exc:
            print("g6_verdict: cannot read digests %s: %s" % (digests_path, exc), file=sys.stderr)
    if not text and not container_only:
        verdict = judge({"plan": None, "boots": [], "marks": [], "steps": [], "stages": [],
                         "wants": [], "end": False}, digests)
        verdict["reason"] = "serial log missing or empty: %s" % serial
    else:
        verdict = judge(parse_markers(text), digests, container_only=container_only)
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(verdict, fh, indent=2, sort_keys=True)
            fh.write("\n")
    for row in verdict["steps"]:  # type: ignore[union-attr]
        print("  step %-9s ok=%-5s rc=%s booted=%s" % (row["step"], row["ok"], row["rc"],
                                                     str(row.get("booted_digest"))[:19]))
    print("  reason: %s" % verdict["reason"])
    print("G6-VERDICT: %s" % verdict["verdict"])
    return int(verdict["exit_code"])  # type: ignore[arg-type]


FIXTURES = {
    "pass.serial.log": ("PASS", 0),
    "no-fallback.serial.log": ("NO-AUTO-FALLBACK", 1),
    "rollback-failed.serial.log": ("ROLLBACK-FAILED", 1),
    "truncated.serial.log": ("UNJUDGED", 2),
    # A REAL serial log (hosted KVM run 36411166098): every marker twice, via ttyS0
    # and kmsg. Its sidecar is <name without .serial.log>.digests.json.
    # It recorded PASS until 2026-09-28; its boot 9 was rebooted by the AGENT, so it is
    # now the regression fixture for "an agent reboot is not an automatic fallback".
    "run-36411166098.serial.log": ("NO-AUTO-FALLBACK", 1),
    # The agent's hold on N+2 ran out: nothing in the image rebooted it.
    "hold-expired.serial.log": ("NO-AUTO-FALLBACK", 1),
    # The post-fix good path: greenboot reboots boots 6-8, boot 9 reaches the agent's
    # G6-HOLD, and awnix-greenboot-fallback.service (not the agent) reboots into N.
    "pass-hold.serial.log": ("PASS", 0),
    # The latest pre-fix hosted run (HEAD 6acfd26f9b): recorded PASS, refuted the same way.
    "run-36417253922.serial.log": ("NO-AUTO-FALLBACK", 1),
}


def self_test() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    fx = os.path.join(here, "fixtures")
    digests = os.path.join(fx, "digests.json")
    failures = 0
    for name, (want_v, want_rc) in sorted(FIXTURES.items()):
        path = os.path.join(fx, name)
        own = os.path.join(fx, name.replace(".serial.log", ".digests.json"))
        dg = own if os.path.isfile(own) else digests
        if not os.path.isfile(path):
            print("SELF-TEST: fixture missing: %s" % path)
            return 2
        with tempfile.TemporaryDirectory() as td:
            vj = os.path.join(td, "verdict.json")
            devnull = open(os.devnull, "w")
            saved, sys.stdout = sys.stdout, devnull
            try:
                rc = run(path, None, dg, vj, False)
            finally:
                sys.stdout = saved
                devnull.close()
            with open(vj, "r", encoding="utf-8") as fh:
                got_v = json.load(fh)["verdict"]
        ok = rc == want_rc and got_v == want_v
        failures += 0 if ok else 1
        print("SELF-TEST %-28s want %s/%d got %s/%d %s" % (name, want_v, want_rc, got_v, rc,
                                                          "ok" if ok else "FAIL"))
    # CONTAINER-ONLY is exit 2 even with a perfect log: nothing booted.
    ev = parse_markers(open(os.path.join(fx, "pass.serial.log"), encoding="utf-8").read())
    co = judge(ev, {}, container_only=True)
    ok = co["verdict"] == "CONTAINER-ONLY" and co["exit_code"] == 2
    failures += 0 if ok else 1
    print("SELF-TEST %-28s want CONTAINER-ONLY/2 got %s/%s %s" % ("container-only", co["verdict"],
                                                                 co["exit_code"], "ok" if ok else "FAIL"))
    # A sentinel that goes missing mid-run is STATE-LOST, not PASS.
    lost = parse_markers(open(os.path.join(fx, "pass.serial.log"), encoding="utf-8").read()
                         .replace("phase=verify-rollback sentinel=ok", "phase=verify-rollback sentinel=missing"))
    sl = judge(lost, json.load(open(digests, encoding="utf-8")))
    ok = sl["verdict"] == "STATE-LOST" and sl["exit_code"] == 1
    failures += 0 if ok else 1
    print("SELF-TEST %-28s want STATE-LOST/1 got %s/%s %s" % ("sentinel-lost", sl["verdict"],
                                                             sl["exit_code"], "ok" if ok else "FAIL"))
    # An empty log must never be 0.
    em = judge(parse_markers(""), {})
    ok = em["exit_code"] == 2
    failures += 0 if ok else 1
    print("SELF-TEST %-28s want UNJUDGED/2 got %s/%s %s" % ("empty", em["verdict"], em["exit_code"],
                                                           "ok" if ok else "FAIL"))
    print("SELF-TEST %s" % ("PASS" if failures == 0 else "FAIL (%d)" % failures))
    return 0 if failures == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serial")
    ap.add_argument("--guest-log")
    ap.add_argument("--digests")
    ap.add_argument("--out")
    ap.add_argument("--container-only", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.serial and not a.container_only:
        ap.error("--serial is required (or --container-only / --self-test)")
    return run(a.serial or "", a.guest_log, a.digests, a.out, a.container_only)


if __name__ == "__main__":
    sys.exit(main())
