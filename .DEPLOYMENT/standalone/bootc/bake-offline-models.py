#!/usr/bin/python3.11
"""Bake catalogue models into /opt/bonsai/models for awnix-ai-offline, at image build.

Names, URLs and sha256 come from the same catalogue the first-boot selector reads, so the
baked files are exactly the ones serve-awnix-bonsai.sh looks for. Every file is staged as
.part and moved into place only after its sha256 matches the catalogue (or, with no hash
recorded, its size is within 5% of size_mb). Exit 1 on any mismatch -- a truncated or
wrong-format model must fail the build, never ship.

    bake-offline-models.py bonsai-1.7b bonsai-4b
    bake-offline-models.py --self-test
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SELECT = os.environ.get("AWNIX_SELECT", "/usr/local/sbin/awnix-model-select.py")
CATALOG = os.environ.get("AWNIX_MODELS_YAML", "/usr/local/sbin/awnix-models.yaml")
OUT = Path(os.environ.get("AWNIX_BAKE_DIR", "/opt/bonsai/models"))


def catalogue_sha(model_id: str) -> str:
    """The sha256 recorded for model_id, read without a yaml dependency."""
    cur = None
    for raw in Path(CATALOG).read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            cur = line.strip()[:-1]
        elif cur == model_id and line.strip().startswith("sha256:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def plan(model_id: str) -> dict:
    out = subprocess.run([sys.executable, SELECT, "--model", model_id, "--plan"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def fetch(urls: list[str], dest: Path) -> None:
    with open(dest, "wb") as fh:
        for url in urls:
            with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - catalogue URLs
                while chunk := r.read(1 << 20):
                    fh.write(chunk)


def bake(model_id: str, fetcher=fetch) -> Path:
    p = plan(model_id)
    final = OUT / p["file"]
    part = final.with_name(final.name + ".part")
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"baking {model_id}: {p['file']} (~{p['size_mb']} MB)", flush=True)
    fetcher(p["urls"], part)
    want = catalogue_sha(model_id)
    if want:
        got = hashlib.sha256(part.read_bytes()).hexdigest()
        if got != want:
            part.unlink(missing_ok=True)
            raise SystemExit(f"{p['file']}: sha256 {got[:12]} != catalogue {want[:12]} -- not baked")
    else:
        mb = part.stat().st_size / 1048576
        if mb < p["size_mb"] * 0.95:
            part.unlink(missing_ok=True)
            raise SystemExit(f"{p['file']}: {mb:.0f} MB, expected ~{p['size_mb']} -- not baked")
    part.replace(final)
    return final


def self_test() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        body = b"gguf-bytes"
        (d / "models.yaml").write_text(
            "models:\n  tiny:\n    file: \"t.gguf\"\n    size_mb: 0\n"
            f"    sha256: \"{hashlib.sha256(body).hexdigest()}\"\n", encoding="utf-8")
        sel = d / "select.py"
        sel.write_text("import json,sys;print(json.dumps({'file':'t.gguf','size_mb':0,"
                       "'urls':['u']}))\n", encoding="utf-8")
        global SELECT, CATALOG, OUT
        SELECT, CATALOG, OUT = str(sel), str(d / "models.yaml"), d / "out"
        ok = bake("tiny", fetcher=lambda urls, dest: dest.write_bytes(body))
        if not (ok.exists() and ok.read_bytes() == body):
            fails.append("a matching sha256 is baked")
        try:
            bake("tiny", fetcher=lambda urls, dest: dest.write_bytes(b"wrong"))
            fails.append("a sha256 mismatch fails the bake")
        except SystemExit:
            if (OUT / "t.gguf.part").exists():
                fails.append("a mismatched .part is removed")
    for f in fails:
        print(f"  FAIL {f}")
    print(f"bake-offline-models self-test: {'PASS' if not fails else 'FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        raise SystemExit(self_test())
    if not sys.argv[1:]:
        raise SystemExit(__doc__)
    for m in sys.argv[1:]:
        print(f"  ok: {bake(m)}")
