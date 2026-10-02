#!/usr/bin/env python3
"""awnix-proof -- air-gap proof plan Steps 0-6 as exit-code harnesses on the node.

Install target /usr/libexec/awnix/awnix-proof (run as `awnix proof <verb>`),
with the package in /usr/lib/awnix/proof/ -- NOT YET installed by any image
(APH006 / D-2698); run it from a checkout until then. Evidence lands in
/var/lib/proof/ (<step>.log, <step>.json, proof-audit.jsonl + .anchor).

    awnix proof step0 [--init-key] [--image-seal DIR --image-key HEX]
    awnix proof step1 --expect-digest sha256:D [--account A ...]
    awnix proof egress start [--duration 1800] [--iface any] | stop | analyze <pcap>
    awnix proof step2 [--pcap F] [--tap F ...] [--node-only] [--air-gap-log F] [--min-window-s 1800]
    awnix proof step3 --run-dir DIR --expect-key HEX [--cmd '<agent task command> ...']
    awnix proof step4 --bundle-good G --bundle-flipped F --bundle-wrongkey W --trust-key HEX [--dry-run]
                      [--platform-verify-cmd 'awnix offline-update verify {bundle}']
    awnix proof step4 --post --expect-digest sha256:N1 --banner S
    awnix proof step5 --pre [--dry-run]    then reboot, then    awnix proof step5 --post [--expect-digest sha256:N]
                      [--run-before DIR --run-after DIR [--expect-key HEX] [--deterministic]]
    awnix proof step6 --out /run/media/usb   (alias: export)
    awnix proof status [--json]
    awnix proof --self-test | --list-verbs

Exit codes: 0 PASS, 1 FAIL, 2 COULD-NOT-JUDGE.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

for _cand in (Path(__file__).resolve().parent, Path("/usr/lib/awnix/proof")):
    if (_cand / "awnix_proof").is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break

from awnix_proof import egress, steps  # noqa: E402
from awnix_proof import verify as pv  # noqa: E402

VERBS = ("step0", "step1", "step2", "step3", "step4", "step5", "step6", "egress", "export", "status")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awnix proof", description="awnix air-gap proof harness (Steps 0-6)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    sub = ap.add_subparsers(dest="verb")

    def sp(name: str, **kw: Any) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kw)
        p.add_argument("--json", action="store_true", help="print the step JSON sidecar")
        return p

    p = sp("step0", help="pre-flight: tools, node key, booted digest, image seal")
    p.add_argument("--init-key", action="store_true")
    p.add_argument("--image-seal")
    p.add_argument("--image-key")
    p = sp("step1", help="air-gapped boot: routes, listeners, digest, accounts, sshd")
    p.add_argument("--expect-digest")
    p.add_argument("--account", action="append")
    p = sp("step2", help="no-egress watch over node and tap captures")
    p.add_argument("--pcap")
    p.add_argument("--tap", action="append")
    p.add_argument("--node-only", action="store_true")
    p.add_argument("--air-gap-log")
    p.add_argument("--min-window-s", type=float,
                   help="capture window each pcap must span (default 1800, the plan's 30 minutes; 0 for rehearsal)")
    p = sp("step3", help="offline agent task evidence (seal, awdit, tool calls)")
    p.add_argument("--run-dir")
    p.add_argument("--expect-key")
    p.add_argument("--cmd")
    p.add_argument("--timeout", type=int, default=3600)
    p = sp("step4", help="signed update: good / flipped / wrong-key bundles")
    p.add_argument("--bundle-good")
    p.add_argument("--bundle-flipped")
    p.add_argument("--bundle-wrongkey")
    p.add_argument("--trust-key")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--platform-verify-cmd",
                   help="also run the platform verifier per bundle, e.g. 'awnix offline-update verify {bundle}'")
    p.add_argument("--post", action="store_true")
    p.add_argument("--expect-digest")
    p.add_argument("--banner")
    p.add_argument("--banner-file")
    p = sp("step5", help="rollback: --pre before, --post after the reboot")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pre", action="store_true")
    g.add_argument("--post", action="store_true")
    p.add_argument("--expect-digest")
    p.add_argument("--marker")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-before", help="Step 3 agent-task --out dir from before the update (post only)")
    p.add_argument("--run-after", help="the same task rerun after the rollback (post only)")
    p.add_argument("--expect-key", help="64-hex key the agent-task output must be sealed with")
    p.add_argument("--deterministic", action="store_true",
                   help="Step 0 showed byte-identical reruns: judge model-text equality")
    for name in ("step6", "export"):
        p = sp(name, help="export evidence + audit chains, sealed with the node key")
        p.add_argument("--out")
        p.add_argument("--include", action="append")
    p = sp("egress", help="node capture: start | stop | analyze <pcap>")
    p.add_argument("action", choices=("start", "stop", "analyze"))
    p.add_argument("path", nargs="?")
    p.add_argument("--duration", type=int)
    p.add_argument("--iface")
    p.add_argument("--out")
    sp("status", help="verdict per step and the proof audit chain")
    return ap


def _print_step(step: str, rc: int, as_json: bool) -> None:
    sj = steps.proof_dir() / f"{step}.json"
    doc: Dict[str, Any] = {}
    try:
        doc = json.loads(sj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if as_json:
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return
    for c in doc.get("checks", []):
        tag = "PASS" if c["ok"] is True else "FAIL" if c["ok"] is False else "COULD-NOT-JUDGE"
        print(f"  {tag:<16} {c['name']}: got {c['got']}")
    extra = doc.get("extra", {})
    if "tar" in extra:
        print(f"EXPORT: {extra['tar']} sha256={extra['tar_sha256']} node_pubkey={extra['node_pubkey']}")
    print(f"VERDICT: {doc.get('verdict', '?')} (exit {rc}) log={steps.proof_dir() / (step + '.log')}")


def dispatch(argv: List[str]) -> int:
    args = _parser().parse_args(argv)
    args.argv = ["awnix-proof"] + list(argv)
    if args.list_verbs:
        print("\n".join(VERBS))
        return 0
    if args.self_test:
        return self_test()
    verb = args.verb
    as_json = bool(getattr(args, "json", False))
    try:
        if verb in ("step0", "step1", "step2", "step3", "step6", "export"):
            fn = getattr(steps, "step6" if verb == "export" else verb)
            rc = fn(args)
            _print_step("step6" if verb == "export" else verb, rc, as_json)
            return rc
        if verb == "step4":
            rc = steps.step4(args)
            _print_step("step4-post" if args.post else "step4", rc, as_json)
            return rc
        if verb == "step5":
            rc = steps.step5(args)
            _print_step("step5-pre" if args.pre else "step5", rc, as_json)
            return rc
        if verb == "egress":
            if args.action == "start":
                rc = egress.start(args)
                _print_step("egress-start", rc, as_json)
                return rc
            if args.action == "stop":
                rc = egress.stop(args)
                _print_step("egress-stop", rc, as_json)
                return rc
            if not args.path:
                print("egress analyze needs a capture path", file=sys.stderr)
                return 2
            r = egress.analyze(args.path)
            print(json.dumps(r, indent=2, sort_keys=True) if as_json else
                  f"frames={r['frames']} disallowed={r['disallowed']} dns={r['dns']} tcp_syn={r['tcp_syn']} "
                  f"udp={r['udp']} loopback_skipped={r['loopback_skipped']}")
            return 0 if r["ok"] else 1
        if verb == "status":
            rc, doc = steps.status()
            if as_json:
                print(json.dumps(doc, indent=2, sort_keys=True, default=str))
            else:
                for s, v in doc["steps"].items():
                    print(f"  {s:<11} {v.get('verdict')}  {v.get('ended', '')}")
                a = doc["audit"]
                print(f"audit: {'absent' if a is None else ('chain_ok=' + str(a['chain_ok']) + ' count=' + str(a['count']))}")
            return rc
    except pv.ProofError as exc:
        print(f"COULD-NOT-JUDGE: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A crash is not a verdict: never let it read as PASS, never as FAIL.
        print(f"COULD-NOT-JUDGE: {verb}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    _parser().print_help()
    return 2


# ---------------------------------------------------------------------------
# self-test: hermetic (tempdir, canned command outputs, ephemeral keys)
# ---------------------------------------------------------------------------
_SS_PASS = "udp UNCONN 0 0 127.0.0.1:323 0.0.0.0:*\ntcp LISTEN 0 128 127.0.0.1:8199 0.0.0.0:*\ntcp LISTEN 0 128 [::1]:8199 [::]:*\n"
_SS_FAIL = _SS_PASS + "tcp LISTEN 0 4096 0.0.0.0:9090 0.0.0.0:*\n"
_DIG = "sha256:" + "ab" * 32


def _bootc_json(booted: str, rollback: Optional[str] = None) -> str:
    st: Dict[str, Any] = {"booted": {"image": {"imageDigest": booted}}}
    if rollback:
        st["rollback"] = {"image": {"imageDigest": rollback}}
    return json.dumps({"apiVersion": "org.containers.bootc/v1", "status": st})


def _fixture(dirp: Path, ss: str, bootc: bool = True, extra: Optional[List[Dict[str, Any]]] = None) -> None:
    cmds = [
        {"argv": ["ip", "-o", "link"], "rc": 0, "stdout": "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n2: enp1s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"},
        {"argv": ["ip", "route", "show"], "rc": 0, "stdout": "169.254.0.0/16 dev enp1s0 proto kernel scope link\n"},
        {"argv": ["ip", "-6", "route", "show"], "rc": 0, "stdout": "fe80::/64 dev enp1s0 proto kernel metric 1024\n"},
        {"argv": ["ss", "-H", "-ltun"], "rc": 0, "stdout": ss},
        {"argv": ["passwd", "-S", "root"], "rc": 0, "stdout": "root L 2026-09-01 0 99999 7 -1 (Password locked.)\n"},
        {"argv": ["sshd", "-T"], "rc": 0, "stdout": "port 22\npasswordauthentication no\n"},
    ]
    if bootc:
        cmds.append({"argv": ["bootc", "status", "--json"], "rc": 0, "stdout": _bootc_json(_DIG)})
    cmds += extra or []
    dirp.mkdir(parents=True, exist_ok=True)
    (dirp / "commands.json").write_text(json.dumps({"which": ["ip", "ss", "bootc", "passwd", "tcpdump", "sshd"],
                                                    "commands": cmds}), encoding="utf-8")


def self_test() -> int:
    fails: List[str] = []
    saved = {k: os.environ.get(k) for k in ("AWNIX_PROOF_DIR", "AWNIX_PROOF_FIXTURES", "AWNIX_PROOF_NODE_KEY",
                                              "AWNIX_PROOF_STAGE_CMD", "AWNIX_PROOF_TEST_MODE")}

    def ok(cond: bool, what: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            fails.append(what)

    def call(argv: List[str]) -> int:
        devnull = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = devnull
        try:
            return dispatch(argv)
        finally:
            sys.stdout = old
            devnull.close()

    try:
        with tempfile.TemporaryDirectory(prefix="awnix-proof-st-") as td:
            base = Path(td)
            os.environ["AWNIX_PROOF_DIR"] = str(base / "proof")
            os.environ["AWNIX_PROOF_NODE_KEY"] = str(base / "etc" / "node.key")
            os.environ["AWNIX_PROOF_STAGE_CMD"] = "stage-stub {archive}"
            for name, ss, bootc in (("pass", _SS_PASS, True), ("fail", _SS_FAIL, True), ("nobootc", _SS_PASS, False)):
                _fixture(base / f"fx-{name}", ss, bootc)
            os.environ["AWNIX_PROOF_FIXTURES"] = str(base / "fx-pass")
            os.environ.pop("AWNIX_PROOF_TEST_MODE", None)
            ok(call(["step1", "--expect-digest", _DIG]) == 2, "fixtures without AWNIX_PROOF_TEST_MODE=1 refused (exit 2)")
            os.environ["AWNIX_PROOF_TEST_MODE"] = "1"
            ok(call(["step0", "--init-key"]) == 0, "step0 --init-key PASS")
            ok(call(["step1", "--expect-digest", _DIG]) == 0, "step1 loopback-only listeners PASS (exit 0)")
            ok(call(["step1", "--expect-digest", "sha256:" + "cd" * 32]) == 1, "step1 digest mismatch FAIL (exit 1)")
            os.environ["AWNIX_PROOF_FIXTURES"] = str(base / "fx-fail")
            ok(call(["step1", "--expect-digest", _DIG]) == 1, "step1 0.0.0.0:9090 FAIL (exit 1)")
            os.environ["AWNIX_PROOF_FIXTURES"] = str(base / "fx-nobootc")
            ok(call(["step1", "--expect-digest", _DIG]) == 2, "step1 without bootc COULD-NOT-JUDGE (exit 2)")

            os.environ["AWNIX_PROOF_FIXTURES"] = str(base / "fx-pass")
            trusted, other = os.urandom(32), os.urandom(32)
            steps.build_bundle(base / "b-good", trusted, b"oci-archive-bytes" * 64)
            fb = steps.build_bundle(base / "b-flip", trusted, b"oci-archive-bytes" * 64)
            raw = bytearray((fb / "image.oci.tar").read_bytes())
            raw[100] ^= 0x01
            (fb / "image.oci.tar").write_bytes(bytes(raw))
            steps.build_bundle(base / "b-wrong", other, b"oci-archive-bytes" * 64)
            rc = call(["step4", "--bundle-good", str(base / "b-good"), "--bundle-flipped", str(base / "b-flip"),
                       "--bundle-wrongkey", str(base / "b-wrong"), "--trust-key", pv.ed25519_public(trusted).hex(),
                       "--dry-run"])
            ok(rc == 0, f"step4 good verified, flipped + wrong-key refused (exit {rc})")
            calls = (base / "proof" / "runner-calls.txt").read_text(encoding="utf-8")
            ok("stage-stub" not in calls, "step4 --dry-run never invoked the stage command")

            out = base / "usb"
            ok(call(["step6", "--out", str(out)]) == 0, "step6 export sealed and tarred")
            doc = json.loads((base / "proof" / "step6.json").read_text(encoding="utf-8"))
            pub = doc["extra"]["node_pubkey"]
            rc, rep = pv.verify_export(Path(doc["extra"]["tar"]), pub)
            ok(rc == 1 and rep["fixture_mode"], f"verifier refuses a fixture-mode export (exit {rc})")
            rc, rep = pv.verify_export(Path(doc["extra"]["tar"]), pub, require_steps_pass=False, allow_fixtures=True)
            ok(rc == 0, f"verifier accepts the rehearsal export with --allow-fixtures --integrity-only (exit {rc})")
            rc, _ = pv.tamper_demo(Path(doc["extra"]["tar"]), node_key=pub)
            ok(rc == 0, f"tamper-demo on the export (exit {rc})")
            ok(call(["status"]) == 0, "status: proof audit chain intact")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(f"self-test: {'PASS' if not fails else 'FAIL'} ({len(fails)} failure(s))")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(dispatch(sys.argv[1:]))
