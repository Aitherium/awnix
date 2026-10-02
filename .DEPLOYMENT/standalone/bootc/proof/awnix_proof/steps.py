"""Air-gap proof plan Steps 0-6 on the node, each an exit-code harness.

Every step:
  * writes /var/lib/proof/<step>.log -- `$ argv`, output, `rc=N` per command,
    one `CHECK` line per criterion, and a final `VERDICT: PASS|FAIL|COULD-NOT-JUDGE`;
  * writes the sidecar <step>.json {schema:1, step, verdict, exit, started, ended,
    checks[{name, cmd, rc, want, got, ok}], notes, extra};
  * appends an awdit record `proof.<step>` (binding the log's sha256) to
    /var/lib/proof/proof-audit.jsonl and moves its .anchor.

Verdicts: any check ok=False -> FAIL (exit 1); otherwise any ok=None (could not
judge) or no checks at all -> COULD-NOT-JUDGE (exit 2); otherwise PASS (exit 0).
A missing tool is never a pass.

Env seams: AWNIX_PROOF_DIR, AWNIX_PROOF_NODE_KEY, AWNIX_PROOF_FIXTURES,
AWNIX_PROOF_STAGE_CMD, AWNIX_PROOF_STATE_MARKER, AWNIX_PROOF_BOOT_ID_FILE.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import PROOF_DIR_DEFAULT
from . import verify as pv
from .runner import FixtureRefusedError, Result, Runner, fixture_dir_from_env

#: The Step 1 filter, byte-for-byte as the proof plan and g2 (ZP004) pin it.
SS_FILTER_LITERAL = "ss -H -ltun | awk '$5 !~ /^(127\\.|\\[::1\\]|::1)/' | wc -l"
_LOOPBACK_RE = re.compile(r"^(127\.|\[::1\]|::1)")

VERDICT_EXIT = {"PASS": 0, "FAIL": 1, "COULD-NOT-JUDGE": 2}
FLIPPED_REASONS = ("content-mismatch", "archive-digest-mismatch", "signature-invalid")


def proof_dir() -> Path:
    return Path(os.environ.get("AWNIX_PROOF_DIR") or PROOF_DIR_DEFAULT)


def audit_path() -> Path:
    return proof_dir() / "proof-audit.jsonl"


def node_key_path() -> Path:
    return Path(os.environ.get("AWNIX_PROOF_NODE_KEY") or "/etc/awnix/proof/node.key")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _norm_digest(d: Optional[str]) -> str:
    d = (d or "").strip().lower()
    return d[len("sha256:"):] if d.startswith("sha256:") else d


class StepLog:
    def __init__(self, path: Path):
        self.path = path
        self.fh = path.open("w", encoding="utf-8", newline="\n")

    def line(self, text: str) -> None:
        self.fh.write(text.rstrip("\n") + "\n")
        self.fh.flush()

    def command(self, res: Result, quiet: bool = False) -> None:
        self.line("$ " + shlex.join(res.argv))
        if res.stdout and not quiet:
            for ln in res.stdout.rstrip("\n").splitlines():
                self.line(ln)
        if res.stderr:
            for ln in res.stderr.rstrip("\n").splitlines():
                self.line("stderr: " + ln)
        self.line(f"rc={res.rc}")

    def close(self) -> None:
        if not self.fh.closed:
            self.fh.close()


class StepRun:
    """One step's evidence: log, checks, sidecar JSON and the audit record."""

    def __init__(self, step: str, argv: Optional[List[str]] = None):
        try:
            fixture_dir_from_env()
        except FixtureRefusedError as exc:
            raise pv.ProofError(str(exc)) from exc
        self.step = step
        self.dir = proof_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.log = StepLog(self.dir / f"{step}.log")
        self.runner = Runner(self.log, self.dir)
        self.started = time.time()
        self.checks: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        self.extra: Dict[str, Any] = {}
        self.log.line(f"# awnix-proof {step} started {_iso(self.started)} host={socket.gethostname()}")
        if self.runner.fixture_mode:
            self.log.line(f"# FIXTURE MODE: commands are canned from {self.runner.fixture_dir} -- not a real run")
        if argv is not None:
            self.log.line("# argv: " + shlex.join(argv))

    def check(self, name: str, ok: Optional[bool], want: str, got: Any,
              cmd: Optional[str] = None, rc: Optional[int] = None) -> Optional[bool]:
        self.checks.append({"name": name, "cmd": cmd, "rc": rc, "want": want, "got": got, "ok": ok})
        tag = "PASS" if ok is True else "FAIL" if ok is False else "COULD-NOT-JUDGE"
        self.log.line(f"CHECK {name}: {tag} want={want!s} got={got!s}")
        return ok

    def cmd_check(self, name: str, res: Result, ok: Optional[bool], want: str, got: Any) -> Optional[bool]:
        return self.check(name, None if res.missing else ok, want, got if not res.missing else "command not found",
                          cmd=shlex.join(res.argv), rc=res.rc)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.log.line("NOTE: " + text)

    def verdict(self) -> str:
        if any(c["ok"] is False for c in self.checks):
            return "FAIL"
        if not self.checks or any(c["ok"] is None for c in self.checks):
            return "COULD-NOT-JUDGE"
        return "PASS"

    def finish(self) -> int:
        verdict = self.verdict()
        ended = time.time()
        self.log.line(f"# ended {_iso(ended)} wall_s={ended - self.started:.3f}")
        self.log.line(f"VERDICT: {verdict}")
        self.log.close()
        log_sha = pv.sha256_file(self.log.path)
        audit: Dict[str, Any] = {}
        try:
            rec = pv.append_record(audit_path(), f"proof.{self.step}", verdict=verdict,
                                  exit=VERDICT_EXIT[verdict], log=self.log.path.name, log_sha256=log_sha,
                                  fixture_mode=self.runner.fixture_mode)
            audit = {"file": audit_path().name, "hash": rec["hash"]}
        except OSError as exc:
            with self.log.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"AUDIT-APPEND-FAILED: {exc}\nVERDICT: COULD-NOT-JUDGE\n")
            verdict = "COULD-NOT-JUDGE"
        doc = {
            "schema": 1,
            "step": self.step,
            "verdict": verdict,
            "exit": VERDICT_EXIT[verdict],
            "started": _iso(self.started),
            "ended": _iso(ended),
            "wall_s": round(ended - self.started, 3),
            "checks": self.checks,
            "notes": self.notes,
            "extra": self.extra,
            "log": self.log.path.name,
            "log_sha256": log_sha,
            "audit": audit,
            "fixture_mode": self.runner.fixture_mode,
        }
        (self.dir / f"{self.step}.json").write_text(json.dumps(doc, indent=2, sort_keys=True, default=str),
                                                    encoding="utf-8")
        return VERDICT_EXIT[verdict]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def bootc_status(run: StepRun) -> Tuple[Optional[Dict[str, Any]], Result]:
    res = run.runner.run(["bootc", "status", "--json"])
    if res.rc != 0:
        return None, res
    try:
        return json.loads(res.stdout), res
    except ValueError:
        return None, res


def slot_digest(status: Optional[Dict[str, Any]], slot: str) -> Optional[str]:
    try:
        d = status["status"][slot]["image"]["imageDigest"]  # type: ignore[index]
        return str(d) if d else None
    except (KeyError, TypeError):
        return None


def parse_ss(stdout: str) -> List[Dict[str, str]]:
    out = []
    for line in stdout.splitlines():
        cols = line.split()
        if len(cols) >= 5:
            out.append({"proto": cols[0], "local": cols[4], "line": line.strip()})
    return out


def _sha256_text_file(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_boot_id() -> Optional[str]:
    p = Path(os.environ.get("AWNIX_PROOF_BOOT_ID_FILE") or "/proc/sys/kernel/random/boot_id")
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Step 0 -- pre-flight
# ---------------------------------------------------------------------------
def step0(args: Any) -> int:
    run = StepRun("step0", getattr(args, "argv", None))
    for tool in ("ip", "ss", "bootc", "passwd", "tcpdump"):
        have = run.runner.which(tool)
        run.check(f"tool:{tool}", True if have else None, "installed", "installed" if have else "absent")
    if not run.runner.which("sshd"):
        run.note("sshd not installed: Step 1 records passwordauthentication as not applicable")

    key = node_key_path()
    if not key.exists() and getattr(args, "init_key", False):
        pv.generate_key(key)
        run.note(f"generated node proof key {key} (0600)")
    if key.exists():
        try:
            pub = pv.ed25519_public(pv.load_seed(key)).hex()
            for dest in (key.parent / "pubkey.hex", run.dir / "pubkey.hex"):
                dest.write_text(pub + "\n", encoding="utf-8")
            run.check("node-key", True, "present; public key recorded on paper by the witness", pub)
        except pv.ProofError as exc:
            run.check("node-key", False, "a readable PKCS#8 Ed25519 key", str(exc))
    else:
        run.check("node-key", None, "present (run `awnix proof step0 --init-key`)", f"absent at {key}")

    st, res = bootc_status(run)
    booted = slot_digest(st, "booted")
    run.extra["booted_digest"] = booted
    run.cmd_check("bootc-booted-digest", res, True if booted else None, "a booted image digest", booted)

    seal_dir = getattr(args, "image_seal", None)
    if seal_dir:
        sres = pv.verify_seal(Path(seal_dir), expect_key=getattr(args, "image_key", None))
        run.extra["image_seal"] = sres
        run.check("image-seal", sres["ok"] if sres["present"] and sres["key_trusted"] is not None else None,
                  "awseal verify ok against --image-key",
                  f"signature_ok={sres['signature_ok']} content_ok={sres['content_ok']} key_trusted={sres['key_trusted']}")
        dtxt = Path(seal_dir) / "digest.txt"
        if dtxt.is_file() and booted:
            sealed = dtxt.read_text(encoding="utf-8").strip().split("@")[-1]
            run.check("image-seal-digest", _norm_digest(sealed) == _norm_digest(booted),
                      "sealed digest == booted digest", sealed)
    else:
        run.note("no --image-seal: image signature/SBOM verification (G9) not judged here")
    osr = Path("/etc/os-release")
    if osr.is_file():
        run.extra["os_release"] = osr.read_text(encoding="utf-8", errors="replace")[:2000]
    return run.finish()


# ---------------------------------------------------------------------------
# Step 1 -- physically air-gapped boot
# ---------------------------------------------------------------------------
def step1(args: Any) -> int:
    run = StepRun("step1", getattr(args, "argv", None))
    r = run.runner.run(["ip", "-o", "link"])
    ifaces = []
    for ln in r.stdout.splitlines():
        parts = ln.split(":")
        if len(parts) > 2:
            ifaces.append({"name": parts[1].strip(), "up": ",UP" in ln or "<UP" in ln})
    run.extra["interfaces"] = ifaces
    run.cmd_check("ip-link", r, r.rc == 0 or None, "interfaces listed", [i["name"] for i in ifaces])

    for fam, argv in (("v4", ["ip", "route", "show"]), ("v6", ["ip", "-6", "route", "show"])):
        rr = run.runner.run(argv)
        defaults = [ln.strip() for ln in rr.stdout.splitlines() if ln.split()[:1] == ["default"]]
        run.cmd_check(f"no-default-route-{fam}", rr, (len(defaults) == 0) if rr.rc == 0 else None,
                      "no default route", defaults or "none")

    ss = run.runner.run(["ss", "-H", "-ltun"])
    listeners = parse_ss(ss.stdout) if ss.rc == 0 else []
    nonlo = [x for x in listeners if not _LOOPBACK_RE.match(x["local"])]
    run.extra["listeners"] = listeners
    run.extra["non_loopback_listeners"] = nonlo
    run.log.line(f"# filter (equivalent): {SS_FILTER_LITERAL}  => {len(nonlo)}")
    run.cmd_check("non-loopback-listeners", ss, (len(nonlo) == 0) if ss.rc == 0 else None,
                  f"0 from `{SS_FILTER_LITERAL}`", f"{len(nonlo)}" + (": " + ", ".join(x["proto"] + " " + x["local"] for x in nonlo) if nonlo else ""))

    st, res = bootc_status(run)
    booted = slot_digest(st, "booted")
    run.extra["booted_digest"] = booted
    expect = getattr(args, "expect_digest", None)
    if res.missing or booted is None:
        run.cmd_check("booted-digest", res, None, f"== {expect}", booted or f"unreadable (rc={res.rc})")
    elif not expect:
        run.check("booted-digest", None, "--expect-digest (the paper record) is required", booted)
    else:
        run.check("booted-digest", _norm_digest(booted) == _norm_digest(expect), f"== {expect}", booted,
                  cmd="bootc status --json", rc=res.rc)

    accounts = list(getattr(args, "account", None) or []) or ["root"]
    for acct in accounts:
        pr = run.runner.run(["passwd", "-S", acct])
        fields = pr.stdout.split()
        state = fields[1] if len(fields) > 1 else "?"
        run.cmd_check(f"account-locked:{acct}", pr, (state in ("L", "LK")) if pr.rc == 0 else None,
                      "L (locked)", state)

    sr = run.runner.run(["sshd", "-T"], quiet=True)
    if sr.missing:
        run.check("sshd-passwordauthentication", True, "no (or sshd absent)", "sshd absent", cmd="sshd -T", rc=sr.rc)
    else:
        vals = [ln.split(None, 1)[1].strip().lower() for ln in sr.stdout.splitlines()
                if ln.lower().startswith("passwordauthentication ") and len(ln.split(None, 1)) > 1]
        val = vals[0] if vals else None
        run.log.line(f"# sshd -T | grep -i passwordauthentication => {val}")
        run.check("sshd-passwordauthentication", (val == "no") if sr.rc == 0 and val else None,
                  "no", val or f"unreadable (rc={sr.rc})", cmd="sshd -T", rc=sr.rc)
    return run.finish()


# ---------------------------------------------------------------------------
# Step 2 -- no-egress watch
# ---------------------------------------------------------------------------
def step2(args: Any) -> int:
    from . import egress

    run = StepRun("step2", getattr(args, "argv", None))
    min_window = float(getattr(args, "min_window_s", None) if getattr(args, "min_window_s", None) is not None
                       else egress.DEFAULT_DURATION_S)
    run.extra["min_window_s"] = min_window
    if egress.capture_running():
        run.check("capture-stopped", None, "`awnix proof egress stop` before judging", "tcpdump still running")
    node_pcap = Path(getattr(args, "pcap", None) or proof_dir() / "egress-node.pcap")
    caps: List[Tuple[str, Path]] = [("node", node_pcap)] + [("tap", Path(t)) for t in (getattr(args, "tap", None) or [])]
    run.extra["egress"] = {}
    for role, cap in caps:
        name = f"egress:{role}:{cap.name}"
        if not cap.is_file():
            run.check(name, None, "capture file present", f"missing: {cap}")
            continue
        try:
            r = pv.analyze_capture(cap, role=role)
        except pv.CaptureError as exc:
            run.check(name, None, "a readable capture", str(exc))
            continue
        run.extra["egress"][f"{role}:{cap.name}"] = r
        run.log.line(f"# analyzed {cap} sha256={r['sha256']} frames={r['frames']} loopback_skipped={r['loopback_skipped']} "
                     f"span_s={r['span_s']}")
        for fd in r["first_disallowed"]:
            run.log.line(f"#   disallowed frame {fd['index']}: {fd['what']}")
        # An empty capture proves nothing (wrong port, unplugged mirror, a file cut to
        # its header): COULD-NOT-JUDGE, never PASS. Disallowed frames still FAIL.
        verdict: Optional[bool] = False if r["disallowed"] else (True if r["frames"] else None)
        run.check(name, verdict, "frames>0 and disallowed=0 (only ARP; ND/MLD to link-local/multicast)",
                  f"frames={r['frames']} disallowed={r['disallowed']} dns={r['dns']} tcp_syn={r['tcp_syn']} udp={r['udp']}")
        # The plan's watch is 30 minutes. First-to-last frame must span >= 90% of the
        # window (tcpdump -G stops AT the window, so the last frame lands inside it).
        span = r["span_s"]
        need = round(0.9 * min_window, 3)
        run.check(f"window:{role}:{cap.name}", None if span is None or span < need else True,
                  f"first-to-last frame >= {need}s (90% of --min-window-s {min_window:g})",
                  f"span_s={span}")
    if not getattr(args, "tap", None):
        if getattr(args, "node_only", False):
            run.note("no tap capture: node-side evidence only (the node is not independent of itself)")
        else:
            run.check("tap-capture", None, "an independent tap capture (--tap FILE)",
                      "absent; pass --node-only to accept node-side evidence")
    agl = getattr(args, "air_gap_log", None)
    if agl:
        p = Path(agl)
        if p.is_file():
            n = sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
            run.extra["air_gap_blocked_attempts"] = n
            run.note(f"air-gap policy log {p}: {n} blocked in-process attempt(s) recorded")
            if pv.anchor_path(p).is_file():
                cr = pv.verify_chain(p)
                run.check("air-gap-log-chain", cr["chain_ok"], "chain_ok=true", cr)
        else:
            run.note(f"air-gap policy log {p} absent")
    return run.finish()


# ---------------------------------------------------------------------------
# Step 3 -- offline agent task (evidence check over the g11 output dir)
# ---------------------------------------------------------------------------
def step3(args: Any) -> int:
    run = StepRun("step3", getattr(args, "argv", None))
    cmd = getattr(args, "cmd", None)
    if cmd:
        r = run.runner.run(shlex.split(cmd), timeout=int(getattr(args, "timeout", 3600) or 3600))
        run.cmd_check("agent-task-exit", r, r.rc == 0, "exit 0", r.rc)
    rd = Path(getattr(args, "run_dir", None) or "")
    if not getattr(args, "run_dir", None) or not rd.is_dir():
        run.check("run-dir", None, "the agent task --out directory", f"missing: {rd}")
        return run.finish()
    key = getattr(args, "expect_key", None)
    s = pv.verify_seal(rd, expect_key=key)
    run.extra["seal"] = s
    run.check("output-signed", bool(s["present"] and s["signature_ok"] and s["content_ok"]),
              "awseal.json present, signature_ok and content_ok", f"present={s['present']} signature_ok={s['signature_ok']} content_ok={s['content_ok']}")
    run.check("output-signer", s["key_trusted"], "signer == --expect-key", s["public_key"])
    log = rd / "awdit.log"
    cr = pv.verify_chain(log)
    run.extra["awdit"] = cr
    run.check("awdit-chain", cr["chain_ok"] if log.is_file() else None, "chain_ok=true", cr)
    rj = rd / "run.json"
    if rj.is_file() and log.is_file():
        try:
            n = int(json.loads(rj.read_text(encoding="utf-8")).get("tool_calls"))
            calls = sum(1 for rec in pv.read_records(log) if rec.get("event") == "tool_call")
            run.check("one-audit-entry-per-tool-call", calls == n, f"tool_call records == run.json tool_calls ({n})", calls)
        except (ValueError, TypeError, AttributeError) as exc:
            run.check("one-audit-entry-per-tool-call", None, "run.json tool_calls readable", str(exc))
    else:
        run.check("one-audit-entry-per-tool-call", None, "run.json + awdit.log present", "missing")
    return run.finish()


# ---------------------------------------------------------------------------
# Step 4 -- signed update across the gap
# ---------------------------------------------------------------------------
def _bundle_archive(bundle: Path) -> Optional[Path]:
    uj = bundle / "update.json"
    if uj.is_file():
        try:
            name = json.loads(uj.read_text(encoding="utf-8")).get("archive")
            if name and "/" not in name and "\\" not in name and (bundle / name).is_file():
                return bundle / name
        except (ValueError, AttributeError):
            return None
    if (bundle / "image.oci.tar").is_file():
        return bundle / "image.oci.tar"
    tars = sorted(bundle.glob("*.tar"))
    return tars[0] if len(tars) == 1 else None


def verify_bundle(bundle: Path, trust_key: str) -> Dict[str, Any]:
    """Refuse before staging unless seal, signer and archive digest all check out."""
    out: Dict[str, Any] = {"bundle": str(bundle), "ok": False, "reason": None, "archive": None}
    s = pv.verify_seal(bundle, expect_key=trust_key)
    out["seal"] = {k: s[k] for k in ("present", "signature_ok", "content_ok", "key_trusted", "diff", "public_key", "error")}
    if not s["present"]:
        out["reason"] = "seal-missing"
    elif s["error"]:
        out["reason"] = "seal-unreadable"
    elif not s["signature_ok"]:
        out["reason"] = "signature-invalid"
    elif not s["key_trusted"]:
        out["reason"] = "untrusted-key"
    elif not s["content_ok"]:
        out["reason"] = "content-mismatch"
    if out["reason"]:
        return out
    archive = _bundle_archive(bundle)
    if archive is None:
        chunks = sorted(bundle.glob("*.awchunk.json"))
        if chunks:
            # g5 split form: image.oci.tar.awchunk.json + parts. Every part is in
            # the seal's file map, so a trusted signature over content_ok already
            # binds the bytes; stitching is the platform's job (stage command).
            out.update(ok=True, reason="verified", chunked=True, chunk_manifest=chunks[0].name)
            return out
        out["reason"] = "archive-missing"
        return out
    out["archive"] = str(archive)
    meta = s["meta"] or {}
    expected = meta.get("digest") or meta.get("archive_sha256")
    uj = bundle / "update.json"
    if not expected and uj.is_file():
        try:
            expected = json.loads(uj.read_text(encoding="utf-8")).get("archive_sha256")
        except (ValueError, AttributeError):
            expected = None
    actual = pv.sha256_file(archive)
    out["archive_sha256"] = actual
    out["expected_sha256"] = expected
    if not expected:
        out["reason"] = "archive-digest-unknown"
    elif _norm_digest(expected) != actual:
        out["reason"] = "archive-digest-mismatch"
    else:
        out["ok"] = True
        out["reason"] = "verified"
    return out


def _expand_template(tmpl: str, bundle: Path, archive: Optional[str]) -> List[str]:
    toks = shlex.split(tmpl)
    if any("{bundle}" in t or "{archive}" in t for t in toks):
        return [t.replace("{bundle}", str(bundle)).replace("{archive}", archive or "") for t in toks]
    return toks + [archive or str(bundle)]


def stage_argv(bundle: Path, archive: Optional[str]) -> Optional[List[str]]:
    """The staging command, or None when nothing can stage this bundle.

    $AWNIX_PROOF_STAGE_CMD (e.g. `awnix offline-update apply {bundle}`) wins;
    otherwise `bootc switch --transport oci-archive <archive>`, which needs a
    single archive file (a chunked bundle has none).
    """
    tmpl = os.environ.get("AWNIX_PROOF_STAGE_CMD")
    if tmpl:
        return _expand_template(tmpl, bundle, archive)
    if not archive:
        return None
    return ["bootc", "switch", "--transport", "oci-archive", archive]


def platform_verify_argv(bundle: Path, tmpl: Optional[str]) -> Optional[List[str]]:
    """$AWNIX_PROOF_VERIFY_CMD / --platform-verify-cmd, e.g. `awnix offline-update verify {bundle}`."""
    tmpl = tmpl or os.environ.get("AWNIX_PROOF_VERIFY_CMD")
    return _expand_template(tmpl, bundle, None) if tmpl else None


def _banner_candidates(extra: Optional[str]) -> List[Path]:
    c = [Path(extra)] if extra else []
    c += [Path("/etc/issue"), Path("/etc/motd"), Path("/usr/lib/os-release"), Path("/etc/os-release")]
    c += sorted(Path("/run/motd.d").glob("*")) if Path("/run/motd.d").is_dir() else []
    return c


def step4(args: Any) -> int:
    if getattr(args, "post", False):
        run = StepRun("step4-post", getattr(args, "argv", None))
        st, res = bootc_status(run)
        booted = slot_digest(st, "booted")
        expect = getattr(args, "expect_digest", None)
        if booted is None or not expect:
            run.cmd_check("booted-digest", res, None, f"== {expect} (N+1)", booted)
        else:
            run.check("booted-digest", _norm_digest(booted) == _norm_digest(expect), f"== {expect} (N+1)", booted)
        banner = getattr(args, "banner", None)
        if banner:
            hits = [str(p) for p in _banner_candidates(getattr(args, "banner_file", None))
                    if p.is_file() and banner in p.read_text(encoding="utf-8", errors="replace")]
            run.check("banner-changed", bool(hits), f"banner {banner!r} present", hits or "not found")
        else:
            run.check("banner-changed", None, "--banner S", "not given")
        return run.finish()

    run = StepRun("step4", getattr(args, "argv", None))
    trust = getattr(args, "trust_key", None)
    if not trust or not re.fullmatch(r"[0-9a-fA-F]{64}", trust):
        run.check("trust-key", None, "--trust-key <64 hex>", trust or "missing")
        return run.finish()
    dry = bool(getattr(args, "dry_run", False))
    cases = (("good", getattr(args, "bundle_good", None)),
             ("flipped", getattr(args, "bundle_flipped", None)),
             ("wrongkey", getattr(args, "bundle_wrongkey", None)))
    run.extra["bundles"] = {}
    for case, path in cases:
        if not path or not Path(path).is_dir():
            run.check(f"bundle:{case}", None, "bundle directory given", path or "missing")
            continue
        b = Path(path)
        res = verify_bundle(b, trust)
        run.extra["bundles"][case] = res
        event = "proof.step4.verified" if res["ok"] else "proof.step4.refused"
        pv.append_record(audit_path(), event, case=case, bundle=str(b), reason=res["reason"],
                        archive_sha256=res.get("archive_sha256"))
        run.log.line(f"# bundle {case}: {res['reason']} (audited as {event})")
        pv_argv = platform_verify_argv(b, getattr(args, "platform_verify_cmd", None))
        if pv_argv:
            # The platform's own verifier (g5 `awnix offline-update verify`) must
            # agree with this harness: 0 for the good bundle, 1 (refused) for the
            # others. The harness judging only itself would prove little.
            pr = run.runner.run(pv_argv, timeout=1800)
            want_rc = 0 if case == "good" else 1
            run.cmd_check(f"platform-verify:{case}", pr, pr.rc == want_rc, f"exit {want_rc}", pr.rc)
        if case == "good":
            run.check("bundle:good", res["ok"], "verified", res["reason"])
            if res["ok"]:
                if dry:
                    run.note("--dry-run: good bundle verified, not staged")
                else:
                    argv = stage_argv(b, res.get("archive"))
                    if argv is None:
                        run.check("stage:good", None, "a stage command (chunked bundle needs $AWNIX_PROOF_STAGE_CMD)",
                                  "none")
                        continue
                    sr = run.runner.run(argv, timeout=3600)
                    pv.append_record(audit_path(), "proof.step4.staged" if sr.rc == 0 else "proof.step4.stage-failed",
                                    case=case, argv=argv, rc=sr.rc)
                    run.cmd_check("stage:good", sr, sr.rc == 0, "exit 0, then reboot and run --post", sr.rc)
        elif case == "flipped":
            run.check("bundle:flipped", (not res["ok"]) and res["reason"] in FLIPPED_REASONS,
                      "refused before staging (" + "|".join(FLIPPED_REASONS) + ")", res["reason"])
        else:
            run.check("bundle:wrongkey", (not res["ok"]) and res["reason"] == "untrusted-key",
                      "refused before staging (untrusted-key)", res["reason"])
    return run.finish()


# ---------------------------------------------------------------------------
# Step 5 -- rollback
# ---------------------------------------------------------------------------
def _marker_path(args: Any) -> Path:
    return Path(getattr(args, "marker", None) or os.environ.get("AWNIX_PROOF_STATE_MARKER")
                or "/var/lib/aither/proof-state-marker")


def step5(args: Any) -> int:
    state_file = proof_dir() / "step5-state.json"
    marker = _marker_path(args)
    if getattr(args, "pre", False):
        run = StepRun("step5-pre", getattr(args, "argv", None))
        st, res = bootc_status(run)
        booted, rollback = slot_digest(st, "booted"), slot_digest(st, "rollback")
        run.cmd_check("bootc-status", res, True if booted else None, "booted digest (N+1)", booted)
        run.check("rollback-target", True if rollback else (None if st is None else False),
                  "a rollback deployment (N) exists", rollback)
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(os.urandom(16).hex() + "\n", encoding="utf-8")
            run.note(f"created /var state marker {marker}")
        state = {"schema": 1, "pre_ts": time.time(), "booted": booted, "rollback": rollback,
                 "marker": str(marker), "marker_sha256": _sha256_text_file(marker), "boot_id": _read_boot_id()}
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        run.extra.update(state)
        if getattr(args, "dry_run", False):
            run.note("--dry-run: bootc rollback not run")
        else:
            rr = run.runner.run(["bootc", "rollback"])
            run.cmd_check("bootc-rollback", rr, rr.rc == 0, "exit 0, then reboot once and run --post", rr.rc)
        return run.finish()

    run = StepRun("step5", getattr(args, "argv", None))
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
        run.check("pre-state", None, "step5 --pre ran first", f"missing {state_file}")
    st, res = bootc_status(run)
    booted = slot_digest(st, "booted")
    expect = getattr(args, "expect_digest", None) or state.get("rollback")
    if booted is None or not expect:
        run.cmd_check("booted-digest", res, None, f"== {expect} (N)", booted)
    else:
        run.check("booted-digest", _norm_digest(booted) == _norm_digest(expect), f"== {expect} (N)", booted)
    if state:
        now_sha = _sha256_text_file(Path(state.get("marker") or marker))
        run.check("state-intact", (now_sha == state.get("marker_sha256")) if now_sha else False,
                  f"marker sha256 == {state.get('marker_sha256')}", now_sha or "marker missing")
        bid = _read_boot_id()
        run.check("rebooted", (bid != state.get("boot_id")) if bid and state.get("boot_id") else None,
                  f"boot_id != {state.get('boot_id')}", bid)
        wall = time.time() - float(state.get("pre_ts", time.time()))
        run.extra["rollback_wall_s"] = round(wall, 1)
        run.note(f"rollback wall time since --pre: {wall:.1f}s (reboot count is the witness's to record)")
    before, after = getattr(args, "run_before", None), getattr(args, "run_after", None)
    if before or after:
        _compare_payloads(run, before, after, getattr(args, "expect_key", None),
                          bool(getattr(args, "deterministic", False)))
    return run.finish()


def _load_payload(run: StepRun, label: str, d: Optional[str], key: Optional[str]) -> Optional[Dict[str, Any]]:
    """A g11 agent-task --out dir: signed (awseal) and carrying payload.json {model_text, model_digest}."""
    if not d or not Path(d).is_dir():
        run.check(f"payload:{label}", None, "an agent-task output dir", d or "missing")
        return None
    s = pv.verify_seal(Path(d), expect_key=key)
    run.check(f"payload-signed:{label}", bool(s["signature_ok"] and s["content_ok"]) and s["key_trusted"] is not False,
              "awseal verifies" + (" against --expect-key" if key else ""),
              {k: s[k] for k in ("signature_ok", "content_ok", "key_trusted")})
    try:
        return json.loads((Path(d) / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        run.check(f"payload:{label}", None, "payload.json readable", str(exc))
        return None


def _compare_payloads(run: StepRun, before: Optional[str], after: Optional[str], key: Optional[str],
                      deterministic: bool) -> None:
    """Proof plan Step 5: compare the signed PAYLOAD (model text + model digest), never the envelope."""
    a = _load_payload(run, "before", before, key)
    b = _load_payload(run, "after", after, key)
    if a is None or b is None:
        return
    run.check("payload-model-digest", bool(a.get("model_digest")) and a.get("model_digest") == b.get("model_digest"),
              "same model digest before and after rollback", f"{a.get('model_digest')} / {b.get('model_digest')}")
    ha = hashlib.sha256(str(a.get("model_text", "")).encode("utf-8")).hexdigest()
    hb = hashlib.sha256(str(b.get("model_text", "")).encode("utf-8")).hexdigest()
    run.extra["payload_text_sha256"] = {"before": ha, "after": hb}
    if deterministic:
        run.check("payload-text-equal", ha == hb, "byte-identical model text (Step 0 showed determinism)", f"{ha} / {hb}")
    else:
        run.note("--deterministic not given: model-text equality is recorded, not judged (proof plan Step 5)")


# ---------------------------------------------------------------------------
# Step 6 -- audit export
# ---------------------------------------------------------------------------
_EXPORT_SUFFIXES = (".log", ".json", ".jsonl", ".pcap", ".pcapng", ".anchor", ".hex")
_EXPORT_SKIP = {"step6.log", "step6.json", "egress.pid"}


def step6(args: Any) -> int:
    run = StepRun("step6", getattr(args, "argv", None))
    key = node_key_path()
    out_arg = getattr(args, "out", None)
    if not out_arg:
        run.check("out-dir", None, "--out DIR (the USB mount)", "missing")
        return run.finish()
    if not key.is_file():
        run.check("node-key", None, f"node key at {key} (step0 --init-key)", "absent")
        return run.finish()
    seed = pv.load_seed(key)
    pub = pv.ed25519_public(seed).hex()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(out_arg)
    exp = out / f"proof-{ts}"
    exp.mkdir(parents=True, exist_ok=False)
    pv.append_record(audit_path(), "proof.step6.export", export=exp.name, pubkey=pub)
    copied = []
    for p in sorted(proof_dir().iterdir()):
        if p.is_file() and p.name not in _EXPORT_SKIP and p.name.endswith(_EXPORT_SUFFIXES) and not p.name.endswith(".key"):
            shutil.copy2(p, exp / p.name)
            copied.append(p.name)
    inc_dir = exp / "include"
    for inc in getattr(args, "include", None) or []:
        ip = Path(inc)
        for src in (ip, pv.anchor_path(ip)):
            if src.is_file():
                inc_dir.mkdir(exist_ok=True)
                shutil.copy2(src, inc_dir / src.name)
                copied.append("include/" + src.name)
            elif src == ip:
                run.note(f"--include {ip} not found; not exported")
    (exp / "pubkey.hex").write_text(pub + "\n", encoding="utf-8")
    run.log.line(f"# exported {len(copied)} file(s): {', '.join(copied)}")
    fixture_steps = []
    for name in copied:
        if name.startswith("step") and name.endswith(".json"):
            try:
                if json.loads((exp / name).read_text(encoding="utf-8")).get("fixture_mode"):
                    fixture_steps.append(name)
            except (OSError, ValueError, AttributeError):
                pass
    fixture_mode = bool(run.runner.fixture_mode or fixture_steps)
    if fixture_mode:
        run.note(f"FIXTURE MODE evidence in this export ({', '.join(fixture_steps) or 'step6'}); the verifier will refuse it")
    pv.seal_dir(exp, seed, subject="awnix-proof-export",
               meta={"node_pubkey": pub, "hostname": socket.gethostname(), "created_by": "awnix-proof step6",
                     "fixture_mode": fixture_mode})
    vr = pv.verify_seal(exp, expect_key=pub)
    run.check("export-seal-selfverify", vr["ok"], "signature_ok, content_ok, key_trusted", {k: vr[k] for k in ("signature_ok", "content_ok", "key_trusted")})
    for cf in pv._chain_files(exp):
        cr = pv.verify_chain(cf)
        run.check(f"chain:{cf.relative_to(exp).as_posix()}", cr["chain_ok"], "chain_ok=true", f"count={cr['count']} kind={cr['kind']}")
    tar_path = out / f"proof-{ts}.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(exp, arcname=exp.name)
    sha = pv.sha256_file(tar_path)
    run.extra.update({"export_dir": str(exp), "tar": str(tar_path), "tar_sha256": sha, "node_pubkey": pub, "files": copied})
    run.check("export-tar", True, "written", f"{tar_path} sha256={sha}")
    return run.finish()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
STATUS_STEPS = ("step0", "step1", "step2", "step3", "step4", "step4-post", "step5-pre", "step5", "step6")


def status() -> Tuple[int, Dict[str, Any]]:
    d = proof_dir()
    steps: Dict[str, Any] = {}
    for s in STATUS_STEPS:
        p = d / f"{s}.json"
        if p.is_file():
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
                steps[s] = {"verdict": j.get("verdict"), "ended": j.get("ended")}
            except ValueError:
                steps[s] = {"verdict": "UNREADABLE"}
    chain = pv.verify_chain(audit_path()) if audit_path().is_file() else None
    doc = {"schema": 1, "proof_dir": str(d), "steps": steps, "audit": chain}
    if chain is None:
        return 2, doc
    return (0 if chain["chain_ok"] else 1), doc


# ---------------------------------------------------------------------------
# test / rehearsal helper: build a g5-shaped signed bundle
# ---------------------------------------------------------------------------
def build_bundle(dest: Path, seed: bytes, archive_bytes: bytes, variant: str = "base",
                 version: str = "rehearsal") -> Path:
    """update.json + image.oci.tar + awseal.json (meta.digest = archive sha256)."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "image.oci.tar").write_bytes(archive_bytes)
    sha = hashlib.sha256(archive_bytes).hexdigest()
    (dest / "update.json").write_text(json.dumps({
        "schema": 1, "kind": "awnix-offline-update", "variant": variant, "version": version,
        "archive": "image.oci.tar", "archive_sha256": sha, "archive_size": len(archive_bytes),
    }, indent=2, sort_keys=True), encoding="utf-8")
    pv.seal_dir(dest, seed, subject="awnix-offline-update", meta={"digest": sha})
    return dest
