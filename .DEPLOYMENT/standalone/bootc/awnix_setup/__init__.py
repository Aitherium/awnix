"""awnix first-boot setup: shared by the tty (awnix-setup --tty) and the web console.

The web half is NOT here. awnix-console (:9443) mounts ``awnix_setup.api``, whose
handlers are pure functions with no HTTP in them, so the tty and the browser run the
exact same code and write the exact same /etc/awnix/setup.json.
"""
from __future__ import annotations

__version__ = "2.0.0"

#: /etc/awnix/setup.json schema. Readers accept 1 (the bearer sat inline there).
STATE_SCHEMA = 2

#: steps.d file schema.
STEPS_SCHEMA = 1

#: awnix-seed.json schema.
SEED_SCHEMA = 1
