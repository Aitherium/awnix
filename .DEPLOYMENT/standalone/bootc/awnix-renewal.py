#!/usr/bin/python3.11
"""awnix-renewal -- the license expiry and renewal lifecycle (installed as
/usr/libexec/awnix/awnix-renewal, run as `awnix renewal ...`).

Verbs:

  awnix renewal status [--json]            the phase, days left, clock and gates (read-only)
  awnix renewal evaluate [--json]          the same, and WRITE lifecycle.json, the motd
                                           fragment and one journal line
  awnix renewal gate updates|private-pulls|components|packs [--json]
  awnix renewal gate offline-bundle --built-at EPOCH [--json]
  awnix renewal policy [--json]            the vendor policy in force
  awnix-renewal --self-test | --list-verbs

The contract is aither-license-lifecycle(7). In one paragraph: an expired license NEVER
stops the appliance serving. Nothing here stops, masks or read-only-mounts anything; the
only effects are the answers `gate` gives to the callers that ask (awnix update,
awnix component, the offline-bundle stage) and the reminders. After the expiry there is a
grace (30 days by default); after the grace, online updates, private pulls and new
licensed installs are refused until a renewed license is imported. An offline bundle
built before exp+grace stays installable forever (the perpetual fallback). A revoked
license gets no grace.

The phase is derived from /var/lib/aither/license/status.json (written by `aitheros`,
the ONLY verifier) and never re-verifies the envelope. `state` in that file is never
changed; the phase is a separate field. Time is judged from a trusted clock: a high-water
mark in /var/lib/aither/license/clock.json that only advances while NTP is synchronised,
or by the monotonic delta within one boot, floored at the image build time
(AWNIX_BUILD_EPOCH in /usr/lib/awnix/release.env). Winding the clock back cannot un-expire
a license; an unsynchronised forward jump (an RTC reset to 2099) cannot lapse one on its
own, and one synchronised reading more than 400 days ahead is not latched until a later
synchronised reading corroborates it. An `expired` status.json is never re-derived as active.

Exit codes. status: 0 serving normally (perpetual/active/renew-soon/grace/unlicensed),
1 lapsed/revoked/invalid (the box STILL serves -- 1 only says renewal is needed),
2 could not judge. gate: 0 allowed, 1 denied, 2 could not judge. evaluate: 0 written,
1 could not write, 2 written but the phase is unknown.

`--root DIR`, `--now`, `--mono`, `--boot-id` and `--ntp` are test seams, honoured only
when DIR is not `/`. stdlib only, Python 3.10-compatible.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

VERBS = ("status", "evaluate", "gate", "policy")
GATE_NAMES = ("updates", "private-pulls", "components", "packs", "offline-bundle")
# gate verb -> the stops_after_grace id it answers for
GATE_POLICY_ID = {"updates": "updates", "private-pulls": "private-pulls",
                  "components": "component-install", "packs": "packs"}
PHASES = ("perpetual", "active", "renew-soon", "grace", "lapsed", "revoked", "unlicensed",
          "invalid", "unknown")
REMINDERS = ("none", "info", "warn", "urgent", "grace", "lapsed")
CLOCK_STATES = ("ok", "unsynced", "rolled-back", "behind-build")
LICENSE_STATES = ("unlicensed", "valid", "expired", "invalid", "revoked", "refused", "offline")
SERVING_PHASES = ("perpetual", "active", "renew-soon", "grace")
DAY = 86400
MOTD_NAME = "50-aither-license"
JOURNAL_TAG = "AITHER-LICENSE"

# The policy baked into this program. /usr/share/aither/license-lifecycle.json overrides
# it; ALR001 asserts the two are equal (the "doc" key aside), so a missing file changes
# nothing.
DEFAULT_POLICY: Dict[str, Any] = {
    "schema": 1,
    "grace_days": 30,
    "renew_soon_days": 30,
    "reminder_days": [60, 30, 14, 7, 3, 1],
    "perpetual_fallback": True,
    "keeps_serving": [
        {"id": "serving", "what": "Every service already running keeps running: no unit is "
                                  "stopped, masked or made read-only."},
        {"id": "inference", "what": "Local models keep answering on the box."},
        {"id": "components", "what": "Baked and already-installed components keep running "
                                     "and are never removed."},
        {"id": "console", "what": "The on-box console and its sign-in keep working."},
        {"id": "local-api", "what": "The local API and data export keep working; your data "
                                    "is never locked."},
        {"id": "rollback", "what": "bootc rollback to any deployment already on the disk "
                                   "keeps working."},
        {"id": "license-renew", "what": "License import and renewal keep working, online and "
                                        "offline."},
    ],
    "stops_after_grace": [
        {"id": "updates", "what": "Online image updates (awnix update check/apply) are refused."},
        {"id": "private-pulls", "what": "Private registry pulls stop: no new pull credential "
                                        "is issued."},
        {"id": "component-install", "what": "Installing a licensed component that is not "
                                            "already on the box is refused."},
        {"id": "packs", "what": "New licensed packs cannot be added."},
        {"id": "support", "what": "Vendor support and new offline bundles built after the "
                                  "grace end are not provided."},
    ],
    "revoke_immediate": ["updates", "private-pulls", "component-install", "packs", "support"],
    "clock": {"rollback_tol_s": 300, "unsynced_window_h": 24},
}
SYNCED_JUMP_CONFIRM_S = 400 * 86400  # one synced reading may move the mark this far
REQUIRED_KEEPS_SERVING = ("serving", "inference", "components", "console", "local-api",
                          "rollback", "license-renew")
REQUIRED_STOPS = ("updates", "private-pulls", "component-install")


class JudgeError(Exception):
    """Something needed to judge is missing or malformed: exit 2."""


# ── context: every path hangs off --root; test seams only when root != / ────────

def _same_path(a: str, b: str) -> bool:
    # '//x' is implementation-defined on POSIX and a UNC prefix on Windows:
    # collapse leading slashes first so '//' is judged as '/'
    a, b = ("/" + x.lstrip("/") if x.startswith("/") else x for x in (a, b))
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


class Ctx:
    def __init__(self, root: str = "/", now: Optional[float] = None, mono: Optional[float] = None,
                 boot_id: Optional[str] = None, ntp: Optional[str] = None) -> None:
        self.root = root or "/"
        # realpath, not abspath: POSIX abspath keeps a leading '//' (abspath('//') is
        # '//'), which would switch the test seams on against the real root
        self.test = not _same_path(self.root, "/")
        seams = {"--now": now, "--mono": mono, "--boot-id": boot_id, "--ntp": ntp}
        used = [k for k, v in seams.items() if v is not None]
        if used and not self.test:
            raise JudgeError(f"{', '.join(used)} is honoured only with --root DIR (not /)")
        self._now, self._mono, self._boot, self._ntp = now, mono, boot_id, ntp

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel.lstrip("/"))

    @property
    def status_path(self) -> str:
        return self.p("/var/lib/aither/license/status.json")

    @property
    def lifecycle_path(self) -> str:
        return self.p("/var/lib/aither/license/lifecycle.json")

    @property
    def clock_path(self) -> str:
        return self.p("/var/lib/aither/license/clock.json")

    @property
    def policy_path(self) -> str:
        return self.p("/usr/share/aither/license-lifecycle.json")

    @property
    def release_env(self) -> str:
        return self.p("/usr/lib/awnix/release.env")

    @property
    def motd_path(self) -> str:
        return self.p("/run/motd.d/" + MOTD_NAME)

    def wall(self) -> float:
        return float(self._now) if self._now is not None else time.time()

    def mono(self) -> float:
        if self._mono is not None:
            return float(self._mono)
        clk = getattr(time, "CLOCK_BOOTTIME", None)  # counts suspend; Linux only
        if clk is not None:
            try:
                return time.clock_gettime(clk)
            except OSError:
                pass
        return time.monotonic()

    def boot_id(self) -> str:
        if self._boot is not None:
            return self._boot
        try:
            with open(self.p("/proc/sys/kernel/random/boot_id"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def synced(self) -> bool:
        """True only when the system clock is known to be NTP-synchronised."""
        if self._ntp is not None:
            return self._ntp == "synced"
        if self.test:
            return os.path.exists(self.p("/run/systemd/timesync/synchronized"))
        tdc = shutil.which("timedatectl")
        if tdc:
            try:
                out = subprocess.run([tdc, "show", "-p", "NTPSynchronized", "--value"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                return out.stdout.strip() == "yes"
            except (OSError, subprocess.SubprocessError):
                pass
        return os.path.exists("/run/systemd/timesync/synchronized")


# ── small helpers ────────────────────────────────────────────────────────────

def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def day(ts: Optional[float]) -> str:
    s = iso(ts)
    return s[:10] if s else "?"


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise JudgeError(f"{path}: unreadable ({exc})") from exc


def atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── policy ───────────────────────────────────────────────────────────────────

def validate_policy(pol: Any) -> List[str]:
    """Problems with a policy document ([] = good). Shared with check_license_lifecycle."""
    errs: List[str] = []
    if not isinstance(pol, dict):
        return ["policy is not a JSON object"]
    if pol.get("schema") != 1:
        errs.append(f"schema {pol.get('schema')!r} != 1")
    g = pol.get("grace_days")
    if not isinstance(g, int) or isinstance(g, bool) or not 0 <= g <= 365:
        errs.append(f"grace_days {g!r} is not an int in 0..365")
    rs = pol.get("renew_soon_days")
    if not isinstance(rs, int) or isinstance(rs, bool) or rs < 1:
        errs.append(f"renew_soon_days {rs!r} is not a positive int")
    rd = pol.get("reminder_days")
    if (not isinstance(rd, list) or not rd
            or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in rd)):
        errs.append("reminder_days is not a non-empty list of positive ints")
    if not isinstance(pol.get("perpetual_fallback"), bool):
        errs.append("perpetual_fallback is not a bool")
    for key, required in (("keeps_serving", REQUIRED_KEEPS_SERVING),
                          ("stops_after_grace", REQUIRED_STOPS)):
        rows = pol.get(key)
        if not isinstance(rows, list) or not all(
                isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("what"), str)
                and r["what"].strip() for r in rows):
            errs.append(f"{key} is not a list of {{id, what}}")
            continue
        ids = [r["id"] for r in rows]
        missing = [i for i in required if i not in ids]
        if missing:
            errs.append(f"{key} lacks required id(s): {', '.join(missing)}")
        if len(set(ids)) != len(ids):
            errs.append(f"{key} has duplicate ids")
    ks = {r.get("id") for r in pol.get("keeps_serving") or [] if isinstance(r, dict)}
    st = {r.get("id") for r in pol.get("stops_after_grace") or [] if isinstance(r, dict)}
    both = sorted(i for i in ks & st if i)
    if both:
        errs.append(f"id(s) both keep serving and stop: {', '.join(both)}")
    ri = pol.get("revoke_immediate")
    if not isinstance(ri, list) or not all(isinstance(x, str) for x in ri):
        errs.append("revoke_immediate is not a list of ids")
    elif set(ri) & ks:
        errs.append("revoke_immediate names a keeps_serving id: "
                    + ", ".join(sorted(set(ri) & ks)))
    clk = pol.get("clock")
    if not isinstance(clk, dict):
        errs.append("clock is not an object")
    else:
        for k in ("rollback_tol_s", "unsynced_window_h"):
            v = clk.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errs.append(f"clock.{k} {v!r} is not a non-negative int")
    return errs


def load_policy(ctx: Ctx) -> Tuple[Dict[str, Any], str]:
    doc = read_json(ctx.policy_path)
    if doc is None:
        return json.loads(json.dumps(DEFAULT_POLICY)), "builtin"
    errs = validate_policy(doc)
    if errs:
        raise JudgeError(f"{ctx.policy_path}: " + "; ".join(errs))
    return doc, ctx.policy_path


# ── the trusted clock ────────────────────────────────────────────────────────

def read_floor(ctx: Ctx) -> Tuple[int, str]:
    """The image build time: time on this box can never be earlier than this."""
    try:
        with open(ctx.release_env, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k == "AWNIX_BUILD_EPOCH":
                    n = _int_or_none(v.strip().strip('"').strip("'"))
                    if n and n > 0:
                        return n, "AWNIX_BUILD_EPOCH"
    except OSError:
        pass
    return 0, "none"


def judge_clock(ctx: Ctx, pol: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (clock report, clock.json document to persist)."""
    tol = int(pol["clock"]["rollback_tol_s"])
    window = int(pol["clock"]["unsynced_window_h"]) * 3600
    wall, mono, boot, synced = ctx.wall(), ctx.mono(), ctx.boot_id(), ctx.synced()
    floor, floor_src = read_floor(ctx)
    try:
        prev = read_json(ctx.clock_path)
    except JudgeError:
        prev = None  # a corrupt clock file restarts from the build floor; never blocks
    prev = prev if isinstance(prev, dict) else {}
    prev_hw = float(prev.get("high_water") or 0)
    base = max(prev_hw, float(floor))
    advanced = base
    prev_mono = prev.get("mono")
    if prev.get("boot_id") and prev.get("boot_id") == boot and isinstance(prev_mono, (int, float)) \
            and mono >= prev_mono:
        advanced = base + (mono - float(prev_mono))  # time that provably passed this boot
    # A synchronised sample more than SYNCED_JUMP_CONFIRM_S ahead of the
    # monotonic-proven time is implausible for one reading (a box off for over a
    # year, or a bad NTP/GPS source). It is TRUSTED for this evaluation but kept
    # only as `pending`, never latched into the persisted high-water mark. A LATER
    # synchronised sample that is not behind it corroborates it, and only then is
    # the pending time (never the newer reading) latched. One bad reading of 2099
    # therefore never latches: the next correct reading contradicts and drops it,
    # and nothing has to be deleted by hand.
    prev_pending = prev.get("pending") if isinstance(prev.get("pending"), dict) else None
    if prev_pending is not None and not isinstance(prev_pending.get("wall"), (int, float)):
        prev_pending = None
    pending: Optional[Dict[str, Any]] = None
    jump_held = False
    if synced:
        if prev_pending is not None and wall >= float(prev_pending["wall"]) - tol:
            confirmed = float(prev_pending["wall"])
            p_mono = prev_pending.get("mono")
            if (prev_pending.get("boot_id") == boot and isinstance(p_mono, (int, float))
                    and mono >= p_mono):
                confirmed += mono - float(p_mono)
            advanced = max(advanced, confirmed)
        if wall - advanced <= max(window, SYNCED_JUMP_CONFIRM_S):
            high_water = max(advanced, wall)
        else:
            high_water = advanced
            pending = {"wall": wall, "boot_id": boot, "mono": mono}
            jump_held = True
    else:
        high_water = advanced
        pending = prev_pending  # an unsynced run neither confirms nor drops a held jump
    trusted = max(float(floor), high_water, wall if jump_held else 0.0)
    detail = ""
    accepted_drift = False
    if not synced and 0 < wall - trusted <= window:
        trusted = wall  # small drift since the last trusted time: plausible, not persisted
        accepted_drift = True
    if wall < floor - tol:
        state = "behind-build"
        detail = (f"the wall clock ({day(wall)}) is before this image was built "
                  f"({day(floor)}); judging from the build time")
    elif wall < high_water - tol:
        state = "rolled-back"
        detail = (f"the wall clock ({day(wall)}) is behind the last trusted time "
                  f"({day(high_water)}); judging from the trusted time")
    elif not synced:
        state = "unsynced"
        if wall - trusted > window:
            detail = (f"the clock is not NTP-synchronised and reads {day(wall)}, "
                      f"{int((wall - trusted) // DAY)} day(s) past the last trusted time "
                      f"({day(trusted)}); the jump is ignored until the clock is synchronised")
        elif accepted_drift:
            detail = "the clock is not NTP-synchronised; a drift under the window is accepted"
        else:
            detail = "the clock is not NTP-synchronised"
    elif jump_held:
        state = "ok"
        detail = (f"the synchronised clock jumped to {day(wall)}, "
                  f"{int((wall - high_water) // DAY)} day(s) past the last trusted time "
                  f"({day(high_water)}); used now, remembered only once a later "
                  "synchronised reading confirms it")
    else:
        state = "ok"
    report = {"state": state, "trusted_now": int(trusted), "wall_now": int(wall),
              "synced": bool(synced), "floor": int(floor), "floor_source": floor_src,
              "high_water": int(high_water), "detail": detail}
    persist = {"schema": 1, "high_water": high_water, "boot_id": boot, "mono": mono,
               "updated_at": iso(wall), "synced": bool(synced)}
    if pending is not None:
        persist["pending"] = pending
    return report, persist


# ── the phase ────────────────────────────────────────────────────────────────

def compute(status: Optional[Dict[str, Any]], clock: Dict[str, Any], pol: Dict[str, Any],
            wall: float) -> Dict[str, Any]:
    grace_s = int(pol["grace_days"]) * DAY
    soon_s = int(pol["renew_soon_days"]) * DAY
    reminder_days = sorted(int(x) for x in pol["reminder_days"])
    t = float(clock["trusted_now"])
    state = status.get("state") if isinstance(status, dict) else None
    exp = _int_or_none(status.get("exp")) if isinstance(status, dict) else None
    detail = ""
    days: Optional[int] = None
    grace_until: Optional[int] = None
    if status is None:
        phase, detail = "unknown", "no license status yet (status.json absent)"
    elif state not in LICENSE_STATES:
        phase, detail = "unknown", f"license state {state!r} is not in the contract"
    elif state == "revoked":
        phase, detail = "revoked", "the license was revoked; there is no grace"
    elif state in ("invalid", "refused"):
        phase, detail = "invalid", f"the license was not accepted ({state})"
    elif state == "unlicensed":
        phase, detail = "unlicensed", "no license is installed"
    elif exp is None:
        if state == "valid":
            phase, detail = "perpetual", "the license carries no expiry"
        else:
            phase, detail = "unknown", f"license state {state} carries no expiry to judge"
    elif exp <= 0:
        phase, detail = "perpetual", "the license never expires (exp=0)"
    else:
        grace_until = exp + grace_s
        if state == "expired":
            # status.json is activation's verdict and is never re-derived
            # (license-envelope-and-status contract). On an air-gapped box that has
            # never synchronised, the trusted clock stalls across power cycles, so
            # the wall clock -- which activation judged from -- decides grace vs
            # lapsed. It is never persisted, so it cannot latch anything.
            t = max(t, float(wall), float(exp))
        days = int(math.floor((exp - t) / DAY))
        if t < exp:
            phase = "renew-soon" if exp - t <= soon_s else "active"
        elif t < grace_until:
            phase = "grace"
        else:
            phase = "lapsed"
        if state == "expired" and float(clock["trusted_now"]) < exp:
            detail = ("activation reports the license expired; judged from its expiry "
                      "although the trusted clock has not reached it")
        elif state == "valid" and t >= exp:
            detail = "the trusted clock is past the expiry although the wall clock is not"
        elif state == "offline":
            detail = "the license could not be re-checked; judged from its expiry"
    # reminders
    if phase in ("lapsed", "revoked", "invalid"):
        reminder = "lapsed"
    elif phase == "grace":
        reminder = "grace"
    elif phase in ("active", "renew-soon") and days is not None and days <= reminder_days[-1]:
        reminder = "urgent" if days <= 7 else ("warn" if days <= soon_s // DAY else "info")
    else:
        reminder = "none"
    due = bool(days is not None and (days in reminder_days or phase in ("grace", "lapsed")))
    # gates (True allowed, False denied, None could not judge)
    if phase in SERVING_PHASES:
        allowed: Dict[str, Optional[bool]] = {k: True for k in ("updates", "private-pulls",
                                                                "component-install", "packs")}
    elif phase == "unlicensed":
        allowed = {"updates": True, "private-pulls": False, "component-install": False,
                   "packs": False}
    elif phase == "unknown":
        allowed = {k: None for k in ("updates", "private-pulls", "component-install", "packs")}
    else:
        allowed = {k: False for k in ("updates", "private-pulls", "component-install", "packs")}
    return {
        "schema": 1, "phase": phase, "state": state, "exp": exp,
        "days_to_expiry": days, "grace_until": grace_until,
        "exp_iso": iso(exp) if exp else None, "grace_until_iso": iso(grace_until),
        "reminder": reminder, "reminder_due": due, "detail": detail,
        "clock": {"state": clock["state"], "trusted_now": clock["trusted_now"],
                  "wall_now": clock["wall_now"], "detail": clock.get("detail", "")},
        "gates": allowed,
        "keeps_serving": [r["id"] for r in pol["keeps_serving"]],
        "computed_at": iso(wall),
    }


def gate(lc: Dict[str, Any], name: str, pol: Dict[str, Any],
         built_at: Optional[int] = None) -> Tuple[int, str]:
    phase = lc["phase"]
    if name == "offline-bundle":
        if built_at is None or built_at <= 0:
            return 2, "offline-bundle needs --built-at EPOCH (the bundle's signed build time)"
        if phase == "unknown":
            return 2, lc.get("detail") or "phase unknown"
        if phase in ("revoked", "invalid"):
            return 1, f"license {phase}: offline bundles are refused"
        if phase == "perpetual":
            return 0, "license perpetual: no expiry applies"
        if phase == "unlicensed":
            # an offline bundle is the update path, so it follows the updates gate
            # (public updates stay open without a license); licensed content inside
            # it is refused by the component/pack gates at install time
            if lc["gates"].get("updates"):
                return 0, "no license installed: public updates allowed"
            return 1, "no license installed: offline bundles need one"
        limit = lc["grace_until"]
        if limit is not None and built_at <= limit and pol.get("perpetual_fallback", True):
            return 0, (f"bundle built {day(built_at)}, on or before the grace end "
                       f"{day(limit)}: installable forever (perpetual fallback)")
        if lc["gates"].get("updates"):
            return 0, f"license {phase}: updates allowed"
        return 1, (f"bundle built {day(built_at)}, after the grace end {day(limit)}; "
                   "renew to install it")
    verdict = lc["gates"].get(GATE_POLICY_ID[name])
    if verdict is None:
        return 2, lc.get("detail") or "phase unknown"
    if verdict:
        return 0, f"license {phase}: {name} allowed"
    if phase == "unlicensed":
        return 1, f"no license installed: {name} needs one (awnix license import <file>)"
    if phase == "lapsed":
        return 1, (f"license lapsed (grace ended {day(lc['grace_until'])}): {name} paused until "
                   "you renew; the appliance keeps running")
    return 1, f"license {phase}: {name} refused; the appliance keeps running"


# ── reminders ────────────────────────────────────────────────────────────────

RENEW_HINT = "Renew: awnix license import <file> (or the console License tab). See aither-license-lifecycle(7)."


def motd_text(lc: Dict[str, Any]) -> Optional[str]:
    phase, r = lc["phase"], lc["reminder"]
    if r == "none":
        return None
    exp, gu, days = lc["exp"], lc["grace_until"], lc["days_to_expiry"]
    if phase == "grace":
        left = max(0, int(math.ceil((gu - lc["clock"]["trusted_now"]) / DAY)))
        body = (f"Aither license expired on {day(exp)}. Everything keeps running. Updates, "
                f"private pulls and new licensed installs stop on {day(gu)} ({left} day(s)).")
    elif phase == "lapsed":
        body = (f"Aither license lapsed on {day(gu)}. Your appliance keeps running. Updates "
                "paused until you renew.")
    elif phase == "revoked":
        body = ("Aither license revoked. Services keep running; updates, private pulls and new "
                "licensed installs are stopped.")
    elif phase == "invalid":
        body = (f"Aither license not accepted ({lc['state']}). Services keep running; updates "
                "and private pulls are stopped.")
    else:
        body = f"Aither license expires on {day(exp)} ({days} day(s))."
    lines = [body, RENEW_HINT]
    if lc["clock"]["state"] != "ok":
        lines.append("Clock: " + (lc["clock"].get("detail") or lc["clock"]["state"]) + ".")
    return "\n".join(lines) + "\n"


def journal_prio(lc: Dict[str, Any]) -> int:
    return {"lapsed": 3 if lc["phase"] in ("revoked", "invalid") else 4, "grace": 4,
            "urgent": 4, "warn": 5}.get(lc["reminder"], 6)


def journal_line(lc: Dict[str, Any]) -> str:
    days = lc["days_to_expiry"]
    return (f"<{journal_prio(lc)}>{JOURNAL_TAG} phase={lc['phase']} "
            f"days={'-' if days is None else days} reminder={lc['reminder']} "
            f"clock={lc['clock']['state']} updates={_gate_word(lc['gates'].get('updates'))} "
            f"private-pulls={_gate_word(lc['gates'].get('private-pulls'))}")


def _gate_word(v: Optional[bool]) -> str:
    return "unknown" if v is None else ("allowed" if v else "paused")


# ── verbs ────────────────────────────────────────────────────────────────────

def lifecycle(ctx: Ctx) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    pol, _src = load_policy(ctx)
    status = read_json(ctx.status_path)
    if status is not None and not isinstance(status, dict):
        raise JudgeError(f"{ctx.status_path}: not a JSON object")
    clock, persist = judge_clock(ctx, pol)
    return compute(status, clock, pol, ctx.wall()), persist, pol


def human(lc: Dict[str, Any]) -> str:
    out = [f"phase:     {lc['phase']}  (license state: {lc['state'] or '-'})"]
    if lc["exp"]:
        out.append(f"expires:   {day(lc['exp'])}  ({lc['days_to_expiry']} day(s))")
        out.append(f"grace end: {day(lc['grace_until'])}")
    out.append(f"reminder:  {lc['reminder']}")
    out.append(f"clock:     {lc['clock']['state']}  trusted {iso(lc['clock']['trusted_now'])}"
               + (f"  -- {lc['clock']['detail']}" if lc['clock'].get('detail') else ""))
    out.append("gates:     " + ", ".join(f"{k}={_gate_word(v)}" for k, v in lc["gates"].items()))
    out.append("keeps running regardless: " + ", ".join(lc["keeps_serving"]))
    if lc["detail"]:
        out.append(f"detail:    {lc['detail']}")
    return "\n".join(out)


def status_exit(lc: Dict[str, Any]) -> int:
    if lc["phase"] == "unknown":
        return 2
    return 1 if lc["phase"] in ("lapsed", "revoked", "invalid") else 0


def cmd_status(ctx: Ctx, as_json: bool) -> int:
    lc, _p, _pol = lifecycle(ctx)
    print(json.dumps(lc, indent=1, sort_keys=True) if as_json else human(lc))
    return status_exit(lc)


def cmd_evaluate(ctx: Ctx, as_json: bool) -> int:
    lc, persist, _pol = lifecycle(ctx)
    try:
        atomic_write(ctx.clock_path, json.dumps(persist, indent=1, sort_keys=True) + "\n", 0o600)
        atomic_write(ctx.lifecycle_path, json.dumps(lc, indent=1, sort_keys=True) + "\n", 0o644)
        text = motd_text(lc)
        if text is None:
            try:
                os.unlink(ctx.motd_path)
            except FileNotFoundError:
                pass
        else:
            atomic_write(ctx.motd_path, text, 0o644)
    except OSError as exc:
        print(f"<3>{JOURNAL_TAG} evaluate could not write: {exc}", flush=True)
        return 1
    if as_json:
        print(json.dumps(lc, indent=1, sort_keys=True))
    else:
        print(journal_line(lc), flush=True)
    return 2 if lc["phase"] == "unknown" else 0


def cmd_gate(ctx: Ctx, name: str, built_at: Optional[int], as_json: bool) -> int:
    lc, _p, pol = lifecycle(ctx)
    rc, why = gate(lc, name, pol, built_at)
    word = {0: "allowed", 1: "denied", 2: "could-not-judge"}[rc]
    if as_json:
        print(json.dumps({"gate": name, "verdict": word, "exit": rc, "detail": why,
                          "phase": lc["phase"], "grace_until": lc["grace_until"],
                          "clock": lc["clock"]["state"]}, sort_keys=True))
    else:
        print(f"gate {name}: {word} -- {why}")
    return rc


def cmd_policy(ctx: Ctx, as_json: bool) -> int:
    pol, src = load_policy(ctx)
    if as_json:
        print(json.dumps(dict(pol, source=src), indent=1, sort_keys=True))
    else:
        print(f"policy: {src}\ngrace: {pol['grace_days']} day(s)\n"
              f"reminders at: {pol['reminder_days']} day(s) before expiry")
        print("keeps running: " + ", ".join(r["id"] for r in pol["keeps_serving"]))
        print("stops after grace: " + ", ".join(r["id"] for r in pol["stops_after_grace"]))
    return 0


# ── self-test (hermetic: a temp root, fixed clocks, no network) ──────────────

def self_test() -> int:
    fails: List[str] = []
    roots: List[str] = []
    t0 = 1_790_000_000  # 2026-09-21
    exp = t0 + 100 * DAY

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            fails.append(what)

    def box(state: str, exp_v: Any = exp, build: int = t0 - 10 * DAY) -> str:
        root = tempfile.mkdtemp(prefix="awnix-renewal-")
        os.makedirs(os.path.join(root, "var/lib/aither/license"))
        os.makedirs(os.path.join(root, "usr/lib/awnix"))
        with open(os.path.join(root, "var/lib/aither/license/status.json"), "w") as fh:
            json.dump({"schema": 1, "state": state, "exp": exp_v}, fh)
        with open(os.path.join(root, "usr/lib/awnix/release.env"), "w") as fh:
            fh.write(f"AWNIX_VARIANT=base\nAWNIX_BUILD_EPOCH={build}\n")
        return root

    def keep(root: str) -> str:
        roots.append(root)
        return root

    def run(root: str, now: float, *, ntp: str = "synced", boot: str = "b1",
            mono: Optional[float] = None) -> Dict[str, Any]:
        if mono is None:
            mono = 1000.0 + max(0.0, now - t0)  # a box that stays up between runs
        ctx = Ctx(root, now=now, mono=mono, boot_id=boot, ntp=ntp)
        lc, persist, _ = lifecycle(ctx)
        atomic_write(ctx.clock_path, json.dumps(persist))
        return lc

    try:
        check(not validate_policy(DEFAULT_POLICY), "builtin policy validates")
        r = keep(box("valid"))
        check(run(r, t0)["phase"] == "active", "100 days left -> active")
        check(run(r, exp - 20 * DAY)["phase"] == "renew-soon", "20 days left -> renew-soon")
        lc = run(r, exp - 5 * DAY)
        check(lc["reminder"] == "urgent" and lc["days_to_expiry"] == 5, "5 days left -> urgent")
        r = keep(box("expired"))
        lc = run(r, exp + 3 * DAY)
        check(lc["phase"] == "grace" and lc["gates"]["updates"] is True, "3 days past exp -> grace, updates on")
        lc = run(r, exp + 31 * DAY)
        check(lc["phase"] == "lapsed" and lc["gates"]["updates"] is False
              and lc["gates"]["private-pulls"] is False, "31 days past exp -> lapsed, updates paused")
        check("keeps running" in (motd_text(lc) or ""), "lapsed motd says it keeps running")
        pol = DEFAULT_POLICY
        check(gate(lc, "offline-bundle", pol, exp + 29 * DAY)[0] == 0,
              "bundle built before grace end installs after lapse (perpetual fallback)")
        check(gate(lc, "offline-bundle", pol, exp + 31 * DAY)[0] == 1, "bundle built after grace end refused")
        # rollback: clock wound back after the box saw a lapsed date
        lc = run(r, exp - 50 * DAY, ntp="unsynced", boot="b2")
        check(lc["phase"] == "lapsed" and lc["clock"]["state"] == "rolled-back",
              "clock rolled back does not un-lapse")
        # RTC reset to 1970 on a fresh box
        r = keep(box("valid"))
        lc = run(r, 0, ntp="unsynced")
        check(lc["clock"]["state"] == "behind-build" and lc["phase"] == "active",
              "RTC 1970 judged from the build epoch")
        # unsynced forward jump to 2099 must not lapse
        r = keep(box("valid"))
        run(r, t0, ntp="synced")
        lc = run(r, 4_070_908_800, ntp="unsynced", boot="b9")
        check(lc["phase"] == "active" and lc["clock"]["state"] == "unsynced",
              "unsynced jump to 2099 is ignored")
        # one bad SYNCED reading of 2099 must not latch: the next correct reading recovers
        r = keep(box("valid", t0 + 365 * DAY))
        run(r, t0, ntp="synced", boot="b1", mono=100.0)
        run(r, 4_070_908_800, ntp="synced", boot="b1", mono=200.0)
        with open(os.path.join(r, "var/lib/aither/license/clock.json")) as fh:
            held = json.load(fh)
        check(held["high_water"] < 4_000_000_000 and "pending" in held,
              "a single synced 2099 reading is held as pending, not latched")
        lc = run(r, t0 + DAY, ntp="synced", boot="b2", mono=100.0)
        check(lc["phase"] == "active" and lc["gates"]["updates"] is True
              and lc["clock"]["state"] == "ok",
              "the next correct synced reading drops the held jump (no rm clock.json)")
        # a genuine long power-off: the jump is confirmed by the next synced run
        r = keep(box("valid"))
        run(r, t0, ntp="synced", boot="b1", mono=100.0)
        run(r, exp + 40 * DAY, ntp="synced", boot="b2", mono=100.0)
        lc = run(r, exp + 40 * DAY + 3600, ntp="synced", boot="b2", mono=3700.0)
        check(lc["phase"] == "lapsed", "a confirmed synced jump past exp+grace lapses")
        # air-gapped box that never synced: activation says expired, wall clock correct
        r = keep(box("expired", t0 + 60 * DAY, build=t0))
        for b in ("b1", "b2", "b3"):
            lc = run(r, t0 + 120 * DAY, ntp="unsynced", boot=b, mono=3600.0)
        check(lc["phase"] == "lapsed" and lc["gates"]["updates"] is False,
              "never-synced box: an expired status.json is not re-derived as active")
        lc = run(keep(box("expired", t0 + 60 * DAY, build=t0)), t0 + 70 * DAY, ntp="unsynced")
        check(lc["phase"] == "grace", "never-synced box 10 days past exp -> grace")
        # monotonic time within one boot does advance an unsynced clock
        r = keep(box("valid"))
        run(r, t0, ntp="synced", mono=10.0)
        run(r, t0 + 60, ntp="synced", mono=70.0)  # the next synced run corroborates it
        lc = run(r, t0, ntp="unsynced", mono=10.0 + 131 * DAY)
        check(lc["phase"] == "lapsed", "monotonic uptime past exp+grace lapses an unsynced box")
        r = keep(box("valid", 0))
        check(run(r, t0 + 9999 * DAY)["phase"] == "perpetual", "exp=0 -> perpetual")
        r = keep(box("revoked"))
        lc = run(r, t0)
        check(lc["phase"] == "revoked" and gate(lc, "updates", pol)[0] == 1, "revoked -> no grace")
        r = keep(box("unlicensed", None))
        lc = run(r, t0)
        check(gate(lc, "updates", pol)[0] == 0 and gate(lc, "components", pol)[0] == 1,
              "unlicensed: public updates allowed, licensed installs refused")
        r = keep(tempfile.mkdtemp(prefix="awnix-renewal-"))
        lc = run(r, t0)
        check(lc["phase"] == "unknown" and gate(lc, "updates", pol)[0] == 2, "no status.json -> 2")
        for real in ("/", "//", "/./", "/../"):
            try:
                Ctx(real, now=1.0)
                check(False, f"--now refused on the real root ({real!r})")
            except JudgeError:
                check(True, f"--now refused on the real root ({real!r})")
    finally:
        for r in roots:
            shutil.rmtree(r, ignore_errors=True)
    print(f"self-test: {'PASS' if not fails else 'FAIL'} ({len(fails)} failure(s))")
    return 0 if not fails else 1


# ── main ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awnix renewal", description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--mono", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--boot-id", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--ntp", choices=("synced", "unsynced"), default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="verb")
    for v in ("status", "evaluate", "policy"):
        sp = sub.add_parser(v)
        sp.add_argument("--json", action="store_true")
    g = sub.add_parser("gate")
    g.add_argument("name", choices=GATE_NAMES)
    g.add_argument("--built-at", type=int, default=None)
    g.add_argument("--json", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_verbs:
        print("\n".join(VERBS))
        return 0
    if args.self_test:
        return self_test()
    if not args.verb:
        build_parser().print_help()
        return 2
    try:
        ctx = Ctx(args.root, now=args.now, mono=args.mono, boot_id=args.boot_id, ntp=args.ntp)
        if args.verb == "status":
            return cmd_status(ctx, args.json)
        if args.verb == "evaluate":
            return cmd_evaluate(ctx, args.json)
        if args.verb == "gate":
            return cmd_gate(ctx, args.name, args.built_at, args.json)
        return cmd_policy(ctx, args.json)
    except JudgeError as exc:
        print(f"awnix renewal: could not judge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
