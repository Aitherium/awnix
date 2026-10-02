#!/usr/bin/python3.11
"""awnix-ops-doctor -- clock, log and boot health for an unattended awnix/garg box.

Installed as /usr/libexec/awnix/awnix-ops-doctor. Stdlib only, Python 3.10 compatible.

    awnix-ops-doctor [--json] [--compact]      judge THIS booted system
    awnix-ops-doctor --fixture FACTS.json      judge a recorded facts file (tests, CI)
    awnix-ops-doctor --proof-marker            print 'AWNIX-OPS-PROOF {json}' (boot proof)
    awnix-ops-doctor --self-test               prove every check can go red
    awnix-ops-doctor --list-verbs

Exit: 0 ok (warnings allowed), 1 at least one check FAILED, 2 could not judge (not a
booted systemd Linux host, e.g. Windows or a container, or a probe tool missing).
It never returns 0 when it saw nothing.

Output {schema:1, verdict: ok|warn|fail|unknown, source: live|fixture, checks:[{id,
status: ok|warn|fail|unknown, detail}]}.

Checks
  CLK001 chronyd enabled + active            CLK002 NTPSynchronized (warn only)
  CLK003 clock earlier than the image build epoch or the license iat (fail)
  CLK004 |RTC - system| > 300 s              CLK005 license exp < 30 d while unsynced (warn)
  CLK006 chronyd holds an IP listen socket (breaks the zero-open-ports claim)
  CLK007 a pool/server/peer names a PUBLIC host (DNS + UDP 123 egress; breaks the air-gap
         claim). fail, or warn once the operator opted in with /etc/awnix/allow-public-ntp
  LOG001 journald SystemMaxUse cap configured    LOG002 journal usage above the cap
  LOG003 a gargbot log > 2x its maxsize, or logrotate.timer not active
  LOG004 /var free < 1 GiB
  BOOT001 multi-user.target not active, or a oneshot awnix-setup.service held it
  BOOT002 awnix-setup.service activating for > 120 s

It reads the license status.json written by `aitheros license` and never re-derives
license state from the envelope.
"""
from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = 1
BUILD_EPOCH_PATH = "/usr/lib/awnix/build-epoch"
LICENSE_STATUS_PATH = "/var/lib/aither/license/status.json"
RTC_PATH = "/sys/class/rtc/rtc0/since_epoch"
GARG_LOG_GLOB = "/var/log/gargbot/*.log"
GARG_MAXSIZE_BYTES = 50 * 1024 * 1024  # ops/garg-logrotate.conf `maxsize 50M`
JOURNALD_CONF_DIRS = (
    "/usr/lib/systemd/journald.conf.d",
    "/usr/local/lib/systemd/journald.conf.d",
    "/run/systemd/journald.conf.d",
    "/etc/systemd/journald.conf.d",
)
JOURNALD_MAIN = "/etc/systemd/journald.conf"
SETUP_UNIT = "awnix-setup.service"
CHRONY_MAIN = "/etc/chrony.conf"
CHRONY_DROPIN_GLOB = "/etc/chrony.d/*.conf"
ALLOW_PUBLIC_NTP = "/etc/awnix/allow-public-ntp"
LAN_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")

RTC_DRIFT_S = 300
EXP_SOON_S = 30 * 86400
VAR_FREE_MIN = 1024 ** 3
SETUP_HOLD_S = 120
PROOF_PREFIX = "AWNIX-OPS-PROOF"
MIB = 1024 ** 2

_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
          "T": 1024 ** 4, "P": 1024 ** 5, "E": 1024 ** 6}

Facts = Dict[str, Any]
Check = Dict[str, str]


# ── small parsers ────────────────────────────────────────────────────────────


def parse_size(text: Optional[str]) -> Optional[int]:
    """'256M' -> bytes (base 1024, as journald does). A percentage or junk -> None."""
    if text is None:
        return None
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([BKMGTPE]?)(?:i?B)?\s*", str(text), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def parse_journald_conf(texts: List[str]) -> Dict[str, str]:
    """Merge [Journal] keys from config texts in precedence order (last wins)."""
    merged: Dict[str, str] = {}
    for text in texts:
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != "Journal" or "=" not in line:
                continue
            key, _, value = line.partition("=")
            merged[key.strip()] = value.strip()
    return merged


def parse_disk_usage(text: str) -> Optional[int]:
    """journalctl --disk-usage: '... take up 48.0M in the file system.'"""
    m = re.search(r"take up\s+([0-9.]+)\s*([BKMGTPE]?)", text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def is_loopback(addr: str) -> bool:
    a = addr.strip().lower()
    return a.startswith(("127.", "[::1]", "::1", "[::ffff:127.", "localhost"))


CHRONY_PORTS = ("123", "323")


def parse_ss_chronyd(text: str) -> List[str]:
    """Local addresses of chrony sockets in `ss -H -tulnp` output.

    A socket counts when its process is chronyd OR its local port is 123/323. The port
    match matters because `-p` names the process only for root; a non-root caller would
    otherwise see no chronyd line and report a clean box that is not.
    """
    out: List[str] = []
    for line in (text or "").splitlines():
        cols = line.split()
        # Netid State Recv-Q Send-Q Local Peer [Process]
        if len(cols) < 5:
            continue
        local = cols[4]
        if "chronyd" in line or local.rsplit(":", 1)[-1] in CHRONY_PORTS:
            out.append(local)
    return out


_SOURCE_RE = re.compile(r"^\s*(pool|server|peer)\s+(\S+)")


def parse_chrony_sources(text: str) -> List[Dict[str, str]]:
    """Active pool/server/peer directives: [{directive, host}]. Comments are skipped."""
    out: List[Dict[str, str]] = []
    for raw in (text or "").splitlines():
        m = _SOURCE_RE.match(raw)
        if m:
            out.append({"directive": m.group(1), "host": m.group(2)})
    return out


def is_lan_host(host: str) -> bool:
    """True for a private/link-local/loopback IP or a LAN-only name; False = public egress."""
    h = host.strip().strip("[]").rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h or h.endswith(LAN_SUFFIXES)
    return ip.is_private or ip.is_link_local or ip.is_loopback


# ── live fact gathering (Linux + systemd only) ───────────────────────────────


def _run(argv: List[str], timeout: float = 10.0) -> Tuple[Optional[int], str]:
    if shutil.which(argv[0]) is None:
        return None, ""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return p.returncode, (p.stdout or "") + (p.stderr if p.returncode else "")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read(path)
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def _systemctl_show(unit: str, props: List[str]) -> Dict[str, str]:
    rc, out = _run(["systemctl", "show", unit, "-p", ",".join(props)])
    if rc is None:
        return {}
    res: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            res[k.strip()] = v.strip()
    return res


def _in_container() -> bool:
    if os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv"):
        return True
    if os.environ.get("container"):
        return True
    rc, _ = _run(["systemd-detect-virt", "--container", "--quiet"])
    return rc == 0


def _journald_texts() -> List[str]:
    """journald.conf then drop-ins, sorted by basename across dirs (later dir wins a clash)."""
    texts: List[str] = []
    main = _read(JOURNALD_MAIN)
    if main:
        texts.append(main)
    dropins: Dict[str, str] = {}
    for d in JOURNALD_CONF_DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*.conf"))):
            dropins[os.path.basename(p)] = p
    for name in sorted(dropins):
        t = _read(dropins[name])
        if t:
            texts.append(t)
    return texts


def _gather_boot() -> Dict[str, Any]:
    mu = _systemctl_show("multi-user.target", ["ActiveState", "After"])
    boot: Dict[str, Any] = {
        "multi_user_active": (mu.get("ActiveState") == "active") if mu else None,
        "multi_user_state": mu.get("ActiveState") if mu else None,
    }
    rc, out = _run(["systemd-analyze", "critical-chain", "multi-user.target", "--no-pager"])
    # Before boot finishes this errors ("Bootup is not yet finished"); the unit
    # timestamps below still judge it.
    boot["critical_chain"] = out if rc == 0 else None
    s = _systemctl_show(SETUP_UNIT, [
        "LoadState", "ActiveState", "ConditionResult", "Type",
        "InactiveExitTimestampMonotonic", "InactiveEnterTimestampMonotonic",
    ])
    setup: Optional[Dict[str, Any]] = None
    if s and s.get("LoadState") not in (None, "not-found"):
        now_us = time.monotonic() * 1e6
        start = int(s.get("InactiveExitTimestampMonotonic") or 0)
        end = int(s.get("InactiveEnterTimestampMonotonic") or 0)
        ran_s = (end - start) / 1e6 if start and end > start else None
        activating = s.get("ActiveState") == "activating" and start
        setup = {
            "load_state": s.get("LoadState"),
            "active_state": s.get("ActiveState"),
            "condition_result": s.get("ConditionResult") == "yes",
            "type": s.get("Type"),
            "ordered_before_multi_user": SETUP_UNIT in mu.get("After", "").split(),
            "ran_s": ran_s,
            "activating_s": (now_us - start) / 1e6 if activating else None,
        }
    boot["setup"] = setup
    return boot


def gather_live() -> Facts:
    facts: Facts = {"platform": sys.platform}
    if not sys.platform.startswith("linux"):
        return facts
    facts["in_container"] = _in_container()
    facts["systemd"] = bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")
    if facts["in_container"] or not facts["systemd"]:
        return facts
    facts["now"] = int(time.time())
    facts["build_epoch"] = _read_int(BUILD_EPOCH_PATH)
    lic_text = _read(LICENSE_STATUS_PATH)
    if lic_text:
        try:
            facts["license"] = json.loads(lic_text)
        except ValueError:
            facts["license"] = None

    chrony = _systemctl_show("chronyd.service", ["LoadState", "UnitFileState", "ActiveState"])
    facts["chronyd"] = {
        "load_state": chrony.get("LoadState"),
        "enabled": chrony.get("UnitFileState"),
        "active": chrony.get("ActiveState"),
    }
    rc, out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    facts["ntp_synchronized"] = None if rc != 0 else out.strip() == "yes"
    facts["rtc_epoch"] = _read_int(RTC_PATH)
    rc, out = _run(["ss", "-H", "-tulnp"])
    facts["chrony_listeners"] = None if rc != 0 else parse_ss_chronyd(out)
    main_conf = _read(CHRONY_MAIN)
    if main_conf is None:
        facts["chrony_sources"] = None
    else:
        sources: List[Dict[str, str]] = []
        for path in [CHRONY_MAIN] + sorted(glob.glob(CHRONY_DROPIN_GLOB)):
            for src in parse_chrony_sources(_read(path) or ""):
                src["file"] = path
                sources.append(src)
        facts["chrony_sources"] = sources
    facts["public_ntp_allowed"] = os.path.exists(ALLOW_PUBLIC_NTP)

    conf = parse_journald_conf(_journald_texts())
    facts["journald"] = {"system_max_use": conf.get("SystemMaxUse"),
                         "storage": conf.get("Storage")}
    rc, out = _run(["journalctl", "--disk-usage"])
    facts["journal_usage_bytes"] = parse_disk_usage(out) if rc == 0 else None

    logs: Dict[str, int] = {}
    for p in glob.glob(GARG_LOG_GLOB):
        try:
            # allocated bytes: a sparse file counts as what it really holds on disk
            logs[p] = os.stat(p).st_blocks * 512
        except OSError:
            continue
    facts["gargbot_logs"] = logs if os.path.isdir(os.path.dirname(GARG_LOG_GLOB)) else None
    lr = _systemctl_show("logrotate.timer", ["LoadState", "ActiveState"])
    facts["logrotate_timer"] = {"load_state": lr.get("LoadState"), "active": lr.get("ActiveState")}
    try:
        st = os.statvfs("/var")
        facts["var_free_bytes"] = st.f_bavail * st.f_frsize
    except OSError:
        facts["var_free_bytes"] = None
    facts["boot"] = _gather_boot()
    return facts


# ── pure evaluation ──────────────────────────────────────────────────────────


def _c(cid: str, status: str, detail: str) -> Check:
    return {"id": cid, "status": status, "detail": detail}


def cannot_judge(facts: Facts) -> Optional[str]:
    if not str(facts.get("platform", "linux")).startswith("linux"):
        return "not a Linux host (%s)" % facts.get("platform")
    if facts.get("in_container"):
        return "running in a container: clock, journald and boot belong to the host"
    if facts.get("systemd") is False:
        return "not booted with systemd"
    return None


def _clock_checks(facts: Facts, now: int) -> List[Check]:
    checks: List[Check] = []
    synced = facts.get("ntp_synchronized")

    ch = facts.get("chronyd") or {}
    if ch.get("load_state") in (None, "not-found"):
        checks.append(_c("CLK001", "fail", "chronyd.service is not installed"))
    elif ch.get("enabled") != "enabled" or ch.get("active") != "active":
        checks.append(_c("CLK001", "fail", "chronyd enabled=%s active=%s"
                         % (ch.get("enabled"), ch.get("active"))))
    else:
        checks.append(_c("CLK001", "ok", "chronyd enabled and active"))

    if synced is None:
        checks.append(_c("CLK002", "unknown", "timedatectl could not report NTPSynchronized"))
    elif synced:
        checks.append(_c("CLK002", "ok", "NTPSynchronized=yes"))
    else:
        checks.append(_c("CLK002", "warn", "NTPSynchronized=no (air gap with no LAN server? "
                                           "add /etc/chrony.d/10-awnix-lan.conf)"))

    lic = facts.get("license") or {}
    floors: List[Tuple[str, int]] = []
    if isinstance(facts.get("build_epoch"), int):
        floors.append(("build-epoch", int(facts["build_epoch"])))
    iat = lic.get("iat")
    if isinstance(iat, (int, float)) and iat > 0:
        floors.append(("license iat", int(iat)))
    if not floors:
        checks.append(_c("CLK003", "warn", "no clock floor: %s absent and license has no iat"
                         % BUILD_EPOCH_PATH))
    else:
        name, floor = max(floors, key=lambda f: f[1])
        if now < floor:
            checks.append(_c("CLK003", "fail", "clock %d is %d s before the %s %d; license "
                             "expiry cannot be judged" % (now, floor - now, name, floor)))
        else:
            checks.append(_c("CLK003", "ok", "clock is after the %s" % name))

    rtc = facts.get("rtc_epoch")
    if not isinstance(rtc, int):
        checks.append(_c("CLK004", "warn", "no RTC reading at %s" % RTC_PATH))
    else:
        drift = abs(rtc - now)
        if drift <= RTC_DRIFT_S:
            checks.append(_c("CLK004", "ok", "RTC within %d s of system time" % drift))
        elif synced:
            checks.append(_c("CLK004", "warn", "RTC off by %d s; NTP synced, rtcsync will "
                                               "correct it" % drift))
        else:
            checks.append(_c("CLK004", "fail", "RTC and system clock disagree by %d s with "
                                               "no NTP sync" % drift))

    exp = lic.get("exp")
    if isinstance(exp, (int, float)) and exp > 0 and (exp - now) < EXP_SOON_S \
            and synced is not True:
        checks.append(_c("CLK005", "warn", "license exp in %d d while the clock is unsynced"
                         % ((exp - now) // 86400)))
    else:
        checks.append(_c("CLK005", "ok", "no near expiry on an unsynced clock"))

    lst = facts.get("chrony_listeners")
    if lst is None:
        checks.append(_c("CLK006", "unknown", "ss could not list sockets"))
    else:
        public = [a for a in lst if not is_loopback(a)]
        if public:
            checks.append(_c("CLK006", "fail", "chronyd listens on %s (want cmdport 0, port 0)"
                             % ", ".join(public)))
        elif lst:
            checks.append(_c("CLK006", "warn", "chronyd listens on loopback %s" % ", ".join(lst)))
        else:
            checks.append(_c("CLK006", "ok", "chronyd holds no listen socket"))

    srcs = facts.get("chrony_sources")
    if srcs is None:
        checks.append(_c("CLK007", "unknown", "%s unreadable: time-source egress unknown"
                         % CHRONY_MAIN))
    else:
        public = ["%s %s (%s)" % (x.get("directive"), x.get("host"), x.get("file", "?"))
                  for x in srcs if not is_lan_host(str(x.get("host", "")))]
        if public and facts.get("public_ntp_allowed"):
            checks.append(_c("CLK007", "warn", "public time source by operator opt-in (%s): "
                             "not air-gap clean: %s" % (ALLOW_PUBLIC_NTP, "; ".join(public))))
        elif public:
            checks.append(_c("CLK007", "fail", "public time source = DNS + UDP 123 egress: %s. "
                             "Comment it out, or touch %s to accept the egress"
                             % ("; ".join(public), ALLOW_PUBLIC_NTP)))
        else:
            checks.append(_c("CLK007", "ok", "no public time source (%d LAN source(s))"
                             % len(srcs)))
    return checks


def _log_checks(facts: Facts) -> List[Check]:
    checks: List[Check] = []
    jd = facts.get("journald") or {}
    cap = parse_size(jd.get("system_max_use"))
    if cap is None:
        checks.append(_c("LOG001", "fail", "journald SystemMaxUse not set (default grows to "
                                           "10% of /var, up to 4 GiB)"))
    else:
        checks.append(_c("LOG001", "ok", "journald SystemMaxUse=%s" % jd.get("system_max_use")))
    usage = facts.get("journal_usage_bytes")
    if not isinstance(usage, int):
        checks.append(_c("LOG002", "unknown", "journalctl --disk-usage unreadable"))
    elif cap is not None and usage > cap * 1.1:
        checks.append(_c("LOG002", "fail", "journal uses %d MiB over a %d MiB cap"
                         % (usage // MIB, cap // MIB)))
    elif cap is None and usage > 1024 ** 3:
        checks.append(_c("LOG002", "fail", "journal uses %d MiB with no cap" % (usage // MIB)))
    else:
        checks.append(_c("LOG002", "ok", "journal uses %d MiB" % (usage // MIB)))

    lr = facts.get("logrotate_timer") or {}
    logs = facts.get("gargbot_logs")
    problems: List[str] = []
    if lr.get("load_state") in (None, "not-found"):
        problems.append("logrotate.timer not installed")
    elif lr.get("active") != "active":
        problems.append("logrotate.timer %s" % lr.get("active"))
    if isinstance(logs, dict):
        for path, size in sorted(logs.items()):
            if isinstance(size, int) and size > 2 * GARG_MAXSIZE_BYTES:
                problems.append("%s is %d MiB (> 2x maxsize)" % (path, size // MIB))
    if problems:
        checks.append(_c("LOG003", "fail", "; ".join(problems)))
    else:
        checks.append(_c("LOG003", "ok", "logrotate.timer active; gargbot logs bounded"))

    free = facts.get("var_free_bytes")
    if not isinstance(free, int):
        checks.append(_c("LOG004", "unknown", "statvfs(/var) failed"))
    elif free < VAR_FREE_MIN:
        checks.append(_c("LOG004", "fail", "/var has %d MiB free (< 1 GiB)" % (free // MIB)))
    else:
        checks.append(_c("LOG004", "ok", "/var has %d MiB free" % (free // MIB)))
    return checks


def _boot_checks(facts: Facts) -> List[Check]:
    checks: List[Check] = []
    boot = facts.get("boot") or {}
    setup = boot.get("setup")
    mu = boot.get("multi_user_active")
    chain = boot.get("critical_chain")
    # Only a oneshot holds the target it is ordered before; a Type=simple setup that
    # sat at the tty for an hour started in milliseconds as far as systemd is concerned.
    # The same gate applies to the critical chain: a Type=simple unit can be the last
    # dependency activated on a first boot and so appear there without holding anything.
    oneshot = (not isinstance(setup, dict)
               or str(setup.get("type") or "oneshot").lower() == "oneshot")
    held = (isinstance(setup, dict) and setup.get("condition_result")
            and str(setup.get("type") or "").lower() == "oneshot"
            and setup.get("ordered_before_multi_user")
            and isinstance(setup.get("ran_s"), (int, float)) and setup["ran_s"] > SETUP_HOLD_S)
    if mu is None:
        checks.append(_c("BOOT001", "unknown", "multi-user.target state unreadable"))
    elif not mu and boot.get("multi_user_state") == "activating" and not held:
        # greenboot can run while the target is still starting; that is a boot in
        # progress, not a hold. A real hold still fails through BOOT002 / `held`.
        checks.append(_c("BOOT001", "warn", "multi-user.target still activating"))
    elif not mu:
        checks.append(_c("BOOT001", "fail", "multi-user.target is not active"))
    elif isinstance(chain, str) and SETUP_UNIT in chain and oneshot:
        checks.append(_c("BOOT001", "fail", "%s is in the critical chain of multi-user.target"
                         % SETUP_UNIT))
    elif held:
        checks.append(_c("BOOT001", "fail", "%s ran %d s before multi-user.target (it waited "
                                            "on tty input)" % (SETUP_UNIT, setup["ran_s"])))
    else:
        checks.append(_c("BOOT001", "ok", "multi-user.target active; %s did not hold it"
                         % SETUP_UNIT))
    act = setup.get("activating_s") if isinstance(setup, dict) else None
    if isinstance(act, (int, float)) and act > SETUP_HOLD_S:
        checks.append(_c("BOOT002", "fail", "%s activating for %d s" % (SETUP_UNIT, act)))
    else:
        checks.append(_c("BOOT002", "ok", "%s not stuck activating" % SETUP_UNIT))
    return checks


def evaluate(facts: Facts) -> List[Check]:
    why = cannot_judge(facts)
    if why:
        return [_c("ENV001", "unknown", why)]
    now = int(facts.get("now") or time.time())
    return _clock_checks(facts, now) + _log_checks(facts) + _boot_checks(facts)


def verdict_of(checks: List[Check]) -> Tuple[str, int]:
    st = [c["status"] for c in checks]
    if "fail" in st:
        return "fail", 1
    if "unknown" in st or not st:
        return "unknown", 2
    if "warn" in st:
        return "warn", 0
    return "ok", 0


def report(facts: Facts, source: str) -> Tuple[Dict[str, Any], int]:
    checks = evaluate(facts)
    verdict, code = verdict_of(checks)
    return {"schema": SCHEMA, "verdict": verdict, "source": source, "checks": checks}, code


# ── self-test ────────────────────────────────────────────────────────────────


def healthy_facts() -> Facts:
    now = 1_790_000_000
    return {
        "platform": "linux", "in_container": False, "systemd": True, "now": now,
        "build_epoch": now - 86400,
        "license": {"state": "valid", "exp": 0, "iat": now - 3600},
        "chronyd": {"load_state": "loaded", "enabled": "enabled", "active": "active"},
        "ntp_synchronized": True, "rtc_epoch": now + 2, "chrony_listeners": [],
        "chrony_sources": [{"directive": "server", "host": "10.0.0.1",
                            "file": "/etc/chrony.d/10-awnix-lan.conf"}],
        "public_ntp_allowed": False,
        "journald": {"system_max_use": "256M", "storage": "persistent"},
        "journal_usage_bytes": 40 * MIB,
        "gargbot_logs": {"/var/log/gargbot/backend.log": 1024},
        "logrotate_timer": {"load_state": "loaded", "active": "active"},
        "var_free_bytes": 20 * 1024 ** 3,
        "boot": {
            "multi_user_active": True,
            "critical_chain": "multi-user.target @4s\n",
            "setup": {"load_state": "loaded", "active_state": "inactive",
                      "condition_result": False, "type": "oneshot",
                      "ordered_before_multi_user": True, "ran_s": None, "activating_s": None},
        },
    }


def mutate(path: str, value: Any, base: Optional[Facts] = None) -> Facts:
    """A deep copy of `base` (default: healthy) with one dotted key replaced."""
    f = json.loads(json.dumps(base if base is not None else healthy_facts()))
    node = f
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return f


def _setup(**kw: Any) -> Dict[str, Any]:
    s: Dict[str, Any] = {"load_state": "loaded", "active_state": "inactive",
                         "condition_result": True, "type": "oneshot",
                         "ordered_before_multi_user": True, "ran_s": None, "activating_s": None}
    s.update(kw)
    return s


def self_test_cases() -> List[Tuple[str, Facts, str, str, int]]:
    """(name, facts, check id, expected status, expected exit)."""
    now = healthy_facts()["now"]
    unsynced = mutate("ntp_synchronized", False)
    chain = "multi-user.target @9min\n  awnix-setup.service +9min\n"
    return [
        ("healthy", healthy_facts(), "CLK001", "ok", 0),
        ("chrony-missing", mutate("chronyd.load_state", "not-found"), "CLK001", "fail", 1),
        ("unsynced", unsynced, "CLK002", "warn", 0),
        ("before-build", mutate("now", now - 7 * 86400), "CLK003", "fail", 1),
        ("before-iat", mutate("license.iat", now + 3600), "CLK003", "fail", 1),
        ("rtc-drift", mutate("rtc_epoch", now + 3600, unsynced), "CLK004", "fail", 1),
        ("rtc-drift-synced", mutate("rtc_epoch", now + 3600), "CLK004", "warn", 0),
        ("exp-soon", mutate("license.exp", now + 5 * 86400, unsynced), "CLK005", "warn", 0),
        ("chrony-port", mutate("chrony_listeners", ["0.0.0.0:323"]), "CLK006", "fail", 1),
        ("stock-pool", mutate("chrony_sources", [{"directive": "pool",
                                                  "host": "2.centos.pool.ntp.org"}]),
         "CLK007", "fail", 1),
        ("public-ip", mutate("chrony_sources", [{"directive": "server", "host": "8.8.8.8"}]),
         "CLK007", "fail", 1),
        ("public-opt-in", mutate("public_ntp_allowed", True, mutate(
            "chrony_sources", [{"directive": "pool", "host": "pool.ntp.org"}])),
         "CLK007", "warn", 0),
        ("no-source", mutate("chrony_sources", []), "CLK007", "ok", 0),
        ("chrony-conf-unread", mutate("chrony_sources", None), "CLK007", "unknown", 2),
        ("no-cap", mutate("journald.system_max_use", None), "LOG001", "fail", 1),
        ("over-cap", mutate("journal_usage_bytes", 900 * MIB), "LOG002", "fail", 1),
        ("garg-log-huge", mutate("gargbot_logs", {"/var/log/gargbot/llama.log": 400 * MIB}),
         "LOG003", "fail", 1),
        ("no-timer", mutate("logrotate_timer.load_state", "not-found"), "LOG003", "fail", 1),
        ("var-full", mutate("var_free_bytes", 100 * MIB), "LOG004", "fail", 1),
        ("mu-inactive", mutate("boot.multi_user_active", False), "BOOT001", "fail", 1),
        ("setup-in-chain", mutate("boot.critical_chain", chain), "BOOT001", "fail", 1),
        ("setup-held", mutate("boot.setup", _setup(ran_s=600.0)), "BOOT001", "fail", 1),
        ("setup-quick", mutate("boot.setup", _setup(ran_s=3.0)), "BOOT001", "ok", 0),
        ("setup-simple-long", mutate("boot.setup", _setup(type="simple", ran_s=600.0)),
         "BOOT001", "ok", 0),
        ("setup-simple-chain", mutate("boot.critical_chain", chain,
                                      mutate("boot.setup", _setup(type="simple"))),
         "BOOT001", "ok", 0),
        ("mu-activating", mutate("boot.multi_user_state", "activating",
                                 mutate("boot.multi_user_active", False)),
         "BOOT001", "warn", 0),
        ("setup-stuck", mutate("boot.setup", _setup(active_state="activating",
                                                    ordered_before_multi_user=False,
                                                    activating_s=900.0)),
         "BOOT002", "fail", 1),
        ("container", {"platform": "linux", "in_container": True}, "ENV001", "unknown", 2),
        ("windows", {"platform": "win32"}, "ENV001", "unknown", 2),
    ]


def self_test() -> int:
    bad = 0
    for name, facts, cid, want_status, want_exit in self_test_cases():
        rep, code = report(facts, "self-test")
        got = {c["id"]: c["status"] for c in rep["checks"]}
        ok = got.get(cid) == want_status and code == want_exit
        print("%-18s %-4s want %s=%s exit=%d, got %s exit=%d" % (
            name, "PASS" if ok else "FAIL", cid, want_status, want_exit, got.get(cid), code))
        bad += 0 if ok else 1
    sizes = (("256M", 256 * MIB), ("1G", 1024 ** 3), ("10%", None), ("32MiB", 32 * MIB))
    for text, want in sizes:
        if parse_size(text) != want:
            print("parse_size(%r) FAIL got %r" % (text, parse_size(text)))
            bad += 1
    usage = "Archived and active journals take up 48.0M in the file system."
    if parse_disk_usage(usage) != 48 * MIB:
        print("parse_disk_usage FAIL")
        bad += 1
    ss = 'udp UNCONN 0 0 127.0.0.1:323 0.0.0.0:* users:(("chronyd",pid=7,fd=5))\n'
    if parse_ss_chronyd(ss) != ["127.0.0.1:323"]:
        print("parse_ss_chronyd FAIL")
        bad += 1
    # non-root: no process column, still caught by port; an unrelated socket is ignored
    ss2 = ("udp UNCONN 0 0 0.0.0.0:323 0.0.0.0:*\n"
           "tcp LISTEN 0 128 127.0.0.1:9443 0.0.0.0:*\n")
    if parse_ss_chronyd(ss2) != ["0.0.0.0:323"]:
        print("parse_ss_chronyd non-root FAIL got %r" % parse_ss_chronyd(ss2))
        bad += 1
    conf = parse_journald_conf(["[Journal]\nSystemMaxUse=4G\n", "[Journal]\nSystemMaxUse=256M\n"])
    stock = "# pool 1.example\npool 2.centos.pool.ntp.org iburst\n  server 10.1.2.3 iburst\n"
    if [x["host"] for x in parse_chrony_sources(stock)] != ["2.centos.pool.ntp.org",
                                                              "10.1.2.3"]:
        print("parse_chrony_sources FAIL got %r" % parse_chrony_sources(stock))
        bad += 1
    lan = {"10.0.0.1": True, "192.168.1.1": True, "fe80::1": True, "ntp": True,
           "ntp.lan": True, "time.corp.internal": True, "pool.ntp.org": False,
           "8.8.8.8": False, "2001:4860::1": False}
    for host, want in lan.items():
        if is_lan_host(host) != want:
            print("is_lan_host(%r) FAIL want %r" % (host, want))
            bad += 1
    if conf.get("SystemMaxUse") != "256M":
        print("parse_journald_conf precedence FAIL")
        bad += 1
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "facts.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(healthy_facts(), fh)
        with open(p, encoding="utf-8") as fh:
            rep, code = report(json.load(fh), "fixture")
        if code != 0 or rep["verdict"] != "ok":
            print("fixture round trip FAIL verdict=%s exit=%d" % (rep["verdict"], code))
            bad += 1
    print("self-test: %s (%d failure(s))" % ("PASS" if bad == 0 else "FAIL", bad))
    return 0 if bad == 0 else 1


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix-ops-doctor",
                                 description="clock, log and boot health for awnix/garg")
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    ap.add_argument("--compact", action="store_true", help="single-line JSON")
    ap.add_argument("--fixture", metavar="FACTS", help="judge a recorded facts JSON file")
    ap.add_argument("--proof-marker", action="store_true",
                    help="print 'AWNIX-OPS-PROOF {json}' for the serial boot proof")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    a = ap.parse_args(argv)

    if a.list_verbs:
        # A flags-only CLI: it has no verbs, so the list is empty (the dispatcher's
        # `awnix ops-doctor` verb is the whole tool). Flags are in --help, not here, so
        # docs AWD003 never binds man pages to option names.
        return 0
    if a.self_test:
        return self_test()
    if a.fixture:
        try:
            with open(a.fixture, encoding="utf-8") as fh:
                facts = json.load(fh)
        except (OSError, ValueError) as exc:
            print("awnix-ops-doctor: cannot read fixture %s: %s" % (a.fixture, exc),
                  file=sys.stderr)
            return 2
        if not isinstance(facts, dict):
            print("awnix-ops-doctor: fixture is not a JSON object", file=sys.stderr)
            return 2
        rep, code = report(facts, "fixture")
    else:
        rep, code = report(gather_live(), "live")

    if a.proof_marker:
        line = json.dumps(rep, separators=(",", ":"), sort_keys=True)
        print("%s %s" % (PROOF_PREFIX, line), flush=True)
        return code
    if a.compact:
        print(json.dumps(rep, separators=(",", ":"), sort_keys=True))
        return code
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return code
    for c in rep["checks"]:
        print("%-8s %-7s %s" % (c["id"], c["status"].upper(), c["detail"]))
    print("verdict: %s (exit %d)" % (rep["verdict"], code))
    return code


if __name__ == "__main__":
    sys.exit(main())
