"""Run a command and record it verbatim in the step log.

Every command a step relies on is written as `$ argv`, its output, then `rc=N`,
so a witness can read exactly what was asked and what came back.

Test seam: AWNIX_PROOF_FIXTURES=<dir> replaces real execution with canned
results from <dir>/commands.json:

    {"which": ["ip", "ss", ...],
     "commands": [{"argv": ["ss", "-H", "-ltun"], "rc": 0, "stdout": "..."},
                  {"argv": [...], "rc": 0, "stdout_file": "ss-pass.txt"},
                  {"argv_prefix": ["stage-stub"], "rc": 0}]}

An argv with no fixture behaves like a missing binary (rc=127). In fixture mode
every call is appended to <proof dir>/runner-calls.txt so a test can prove a
command was NEVER invoked.

Fixture mode is REFUSED unless AWNIX_PROOF_TEST_MODE=1 is also set, and every
step run under it stamps fixture_mode=true into its step JSON, its awdit record
and (step6) the export seal meta, so the offline verifier reports it and fails
the export unless told --allow-fixtures. Canned output can never pass for a
real run.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

MISSING_RC = 127
TEST_MODE_ENV = "AWNIX_PROOF_TEST_MODE"


class FixtureRefusedError(Exception):
    """AWNIX_PROOF_FIXTURES set without AWNIX_PROOF_TEST_MODE=1."""


def fixture_dir_from_env() -> Optional[Path]:
    """The fixture dir, or None; raises FixtureRefusedError when test mode is not declared."""
    fx = os.environ.get("AWNIX_PROOF_FIXTURES")
    if not fx:
        return None
    if os.environ.get(TEST_MODE_ENV) != "1":
        raise FixtureRefusedError(
            f"AWNIX_PROOF_FIXTURES is set but {TEST_MODE_ENV}=1 is not: refusing to replace real "
            "commands with canned output on what may be a real node"
        )
    return Path(fx)


@dataclass
class Result:
    argv: List[str]
    rc: int
    stdout: str
    stderr: str
    missing: bool = False


class Runner:
    def __init__(self, log: Any, proof_dir: Path):
        self.log = log
        self.proof_dir = Path(proof_dir)
        self.fixture_dir: Optional[Path] = fixture_dir_from_env()
        self._fixtures: Dict[str, Any] = {}
        if self.fixture_dir is not None:
            try:
                self._fixtures = json.loads((self.fixture_dir / "commands.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._fixtures = {}

    @property
    def fixture_mode(self) -> bool:
        return self.fixture_dir is not None

    def which(self, name: str) -> bool:
        if self.fixture_mode:
            return name in self._fixtures.get("which", [])
        return shutil.which(name) is not None

    def _record_call(self, argv: Sequence[str]) -> None:
        self.proof_dir.mkdir(parents=True, exist_ok=True)
        with (self.proof_dir / "runner-calls.txt").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"argv": list(argv)}) + "\n")

    def _fixture(self, argv: Sequence[str]) -> Result:
        self._record_call(argv)
        for entry in self._fixtures.get("commands", []):
            prefix = entry.get("argv_prefix")
            exact = "argv" in entry and list(entry["argv"]) == list(argv)
            if exact or (prefix and list(argv[: len(prefix)]) == list(prefix)):
                out = entry.get("stdout", "")
                if "stdout_file" in entry and self.fixture_dir is not None:
                    out = (self.fixture_dir / entry["stdout_file"]).read_text(encoding="utf-8")
                return Result(list(argv), int(entry.get("rc", 0)), out, entry.get("stderr", ""))
        return Result(list(argv), MISSING_RC, "", f"{argv[0]}: command not found (no fixture)", missing=True)

    def run(self, argv: Sequence[str], timeout: int = 300, quiet: bool = False) -> Result:
        argv = [str(a) for a in argv]
        if self.fixture_mode:
            res = self._fixture(argv)
        else:
            try:
                cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
                res = Result(argv, cp.returncode, cp.stdout, cp.stderr)
            except FileNotFoundError:
                res = Result(argv, MISSING_RC, "", f"{argv[0]}: command not found", missing=True)
            except subprocess.TimeoutExpired as exc:
                res = Result(argv, 124, str(exc.stdout or ""), f"timed out after {timeout}s")
        self.log.command(res, quiet=quiet)
        return res

    def spawn(self, argv: Sequence[str], out_path: Path) -> int:
        """Start a detached process (tcpdump). Returns its pid, or 0 in fixture mode."""
        argv = [str(a) for a in argv]
        self.log.line("$ " + shlex.join(argv) + "  &")
        if self.fixture_mode:
            self._record_call(argv)
            self.log.line("rc=0 (fixture: not spawned)")
            return 0
        with open(out_path, "ab") as fh:
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        self.log.line(f"pid={proc.pid}")
        return proc.pid
