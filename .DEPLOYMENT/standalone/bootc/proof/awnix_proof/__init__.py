"""Node-side awnix proof harness (air-gap proof plan Steps 0-6).

The pure logic (awdit chain, awseal seal, Ed25519, pcap analysis) lives in the
single-file verifier `awnix_proof_verify.py` beside this package, so the node
and the verifier laptop run the SAME code over the same bytes.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import awnix_proof_verify as verify  # noqa: E402

PROOF_DIR_DEFAULT = "/var/lib/proof"
STEPS = ("step0", "step1", "step2", "step3", "step4", "step5", "step6")

__all__ = ["verify", "PROOF_DIR_DEFAULT", "STEPS"]
