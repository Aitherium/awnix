"""Step 2 capture on the node: tcpdump start/stop with a pidfile, then analysis.

`egress start` runs `tcpdump -i any -n -U -w /var/lib/proof/egress-node.pcap
-G <seconds> -W 1`, which stops by itself after one rotation period (default
30 minutes, the proof plan's window). `egress stop` ends it early with SIGINT so
tcpdump flushes a complete file. The node capture is supporting evidence only:
the tap capture on a separate host is authoritative because it does not trust
the node. Loopback traffic (the local model server on 127.0.0.1) is skipped on
the NODE capture only when the link layer says loopback (tcpdump -i any writes
SLL/SLL2 with ARPHRD_LOOPBACK) and reported as `loopback_skipped`; 127/8 or ::1
addresses on a wire (Ethernet, a tap) are martians and count.
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import verify as pv
from .steps import StepRun, proof_dir

DEFAULT_DURATION_S = 1800
PIDFILE = "egress.pid"


def _pidfile() -> Path:
    return proof_dir() / PIDFILE


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pidfile() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_pidfile().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def capture_running() -> bool:
    info = _read_pidfile()
    return bool(info and _alive(int(info.get("pid", 0))))


def start(args: Any) -> int:
    run = StepRun("egress-start", getattr(args, "argv", None))
    if capture_running():
        run.check("capture-started", False, "no capture already running", _read_pidfile())
        return run.finish()
    if not run.runner.which("tcpdump"):
        run.check("tool:tcpdump", None, "installed", "absent")
        return run.finish()
    out = Path(getattr(args, "out", None) or proof_dir() / "egress-node.pcap")
    duration = int(getattr(args, "duration", None) or DEFAULT_DURATION_S)
    iface = getattr(args, "iface", None) or "any"
    argv = ["tcpdump", "-i", iface, "-n", "-U", "-w", str(out), "-G", str(duration), "-W", "1"]
    pid = run.runner.spawn(argv, proof_dir() / "egress-tcpdump.log")
    info = {"pid": pid, "out": str(out), "started": time.time(), "duration_s": duration, "argv": argv}
    _pidfile().write_text(json.dumps(info), encoding="utf-8")
    run.extra.update(info)
    ok = True if run.runner.fixture_mode else _alive(pid)
    run.check("capture-started", ok, f"tcpdump running for {duration}s", f"pid={pid}")
    return run.finish()


def stop(args: Any) -> int:
    run = StepRun("egress-stop", getattr(args, "argv", None))
    info = _read_pidfile()
    if not info:
        run.check("capture-stopped", None, "a capture started by `egress start`", "no pidfile")
        return run.finish()
    pid = int(info.get("pid", 0))
    if _alive(pid):
        os.kill(pid, signal.SIGINT)
        run.log.line(f"$ kill -INT {pid}")
        deadline = time.time() + 15
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.2)
    stopped = not _alive(pid)
    run.check("capture-stopped", stopped, "tcpdump exited", f"pid={pid} alive={not stopped}")
    if stopped:
        _pidfile().unlink()
    cap = Path(info.get("out", ""))
    run.extra["capture"] = str(cap)
    run.extra["wall_s"] = round(time.time() - float(info.get("started", time.time())), 1)
    if cap.is_file():
        try:
            r = pv.analyze_capture(cap)
            run.extra["egress"] = r
            run.check("capture-readable", True, "a complete pcap", f"frames={r['frames']} disallowed={r['disallowed']}")
        except pv.CaptureError as exc:
            run.check("capture-readable", None, "a complete pcap", str(exc))
    else:
        run.check("capture-readable", None, "capture file present", f"missing: {cap}")
    return run.finish()


def analyze(path: str) -> Dict[str, Any]:
    return pv.analyze_capture(Path(path))
