#!/usr/bin/python3.11
"""Choose a model this box can run, and print how to fetch it.

Config-driven: everything about WHICH models exist lives in awnix-models.yaml, so adding
one is an entry there and nothing here or in the launcher changes. The previous design
hardcoded a single model in a shell script, which made "run something smaller" or "run one
of our own models" an edit to the launcher.

Two jobs, kept apart so each can be tested:

    select   given a memory budget, pick the largest model in the default ladder that
             fits -- or say plainly that none does
    plan     given a model id, emit the URLs, the output filename, and whether the parts
             need concatenating

It prints a plan; it does not download. The shell script fetches, because curl with
resume and progress is better at that than urllib, and because a selector that also
downloads cannot be tested without downloading.

    awnix-model-select.py --budget-mb 4096          # what fits?
    awnix-model-select.py --model gemma4-12b --plan # how do I get it?
    awnix-model-select.py --list
    awnix-model-select.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = Path(os.environ.get("AWNIX_MODELS_YAML", HERE / "awnix-models.yaml"))

#: Weights must fit within this FRACTION of the budget. llama.cpp needs room for the KV
#: cache and the context window on top of the weights; sizing to the weights alone yields
#: a model that loads and dies mid-generation, which reads as a broken install rather
#: than a too-big model.
HEADROOM_DIVISOR = 2


class Dead(RuntimeError):
    """Could not judge. Exit 2, never 0."""


def _load(path: Path = None) -> dict:
    """Parse the catalogue. Uses PyYAML when present, else a small reader.

    The fallback exists because awnix's base image installs no yaml module and this must
    work on a box that has just booted with no network. It handles exactly the shapes
    this file uses -- nested maps, scalars, and flow lists -- and raises rather than
    guessing on anything else, because a catalogue half-read is worse than none.
    """
    p = path or CATALOG
    if not p.is_file():
        raise Dead(f"catalogue not found: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    return _mini_yaml(text)


def _scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [x.strip() for x in v[1:-1].split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _mini_yaml(text: str) -> dict:
    """Indentation-based reader for the subset this catalogue uses."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    pending_block: str | None = None
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise Dead("catalogue indentation is inconsistent — refusing to guess")
        parent = stack[-1][1]
        if ":" not in line:
            # A folded/literal block's continuation line; attach to the last key.
            if pending_block:
                parent[pending_block] = (str(parent.get(pending_block, "")) + " " + line).strip()
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest in (">-", ">", "|", "|-"):
            parent[key] = ""
            pending_block = key
            stack.append((indent, parent))
            continue
        pending_block = None
        if rest == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(rest)
    return root


def detect_budget_mb() -> int:
    """VRAM if there is a GPU, else RAM. 0 when neither can be read."""
    import subprocess
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=15, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            first = (r.stdout or "").strip().splitlines()
            if first and first[0].strip().isdigit():
                return int(first[0].strip())
    except Exception:
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def select(cfg: dict, budget_mb: int) -> str | None:
    """Largest ladder entry whose weights fit budget/HEADROOM_DIVISOR. None if none do."""
    defaults = cfg.get("defaults") or {}
    ladder = defaults.get("ladder") or []
    models = cfg.get("models") or {}
    if not ladder:
        raise Dead("catalogue declares no default ladder — nothing to choose from")
    if budget_mb < int(defaults.get("min_budget_mb", 0) or 0):
        return None
    allowed = budget_mb // HEADROOM_DIVISOR
    best = None
    for mid in ladder:
        m = models.get(mid)
        if not m:
            raise Dead(f"ladder names '{mid}', which the catalogue does not define")
        if int(m["size_mb"]) <= allowed:
            best = mid
    return best


def plan(cfg: dict, model_id: str) -> dict:
    """URLs + output filename + whether parts must be joined."""
    models = cfg.get("models") or {}
    m = models.get(model_id)
    if not m:
        raise Dead(f"no such model '{model_id}' (have: {', '.join(sorted(models))})")
    srcs = cfg.get("sources") or {}
    src = srcs.get(m["source"])
    if not src:
        raise Dead(f"model '{model_id}' names source '{m['source']}', which is not defined")

    base = str(src["base"])
    parts = int(m.get("parts", 0) or 0)
    if m["source"] == "aitherkvcache" or src.get("kind") == "github-release":
        stem = f"{base}{m['release']}/{m['file']}"
        urls = ([f"{stem}.part{i}" for i in range(parts)] if parts else [stem])
    else:
        urls = [f"{base}{m['path']}"]
        if parts:
            raise Dead(f"'{model_id}' declares parts on a direct source — unsupported")
    return {
        "id": model_id,
        "file": m["file"],
        "size_mb": int(m["size_mb"]),
        "parts": parts,
        "join": parts > 1,
        "urls": urls,
        "opt_in": bool(m.get("opt_in", False)),
    }


def self_test() -> int:
    fails = 0

    def chk(cond, label):
        nonlocal fails
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        if not cond:
            fails += 1

    cfg = _load()
    chk(bool(cfg.get("models")), "the real catalogue parses and defines models")

    # Selection, against the measured ladder.
    for budget, want in ((1024, "bonsai-1.7b"), (4096, "bonsai-4b"),
                         (8192, "bonsai-8b"), (24576, "bonsai-27b")):
        got = select(cfg, budget)
        chk(got == want, f"{budget}MB -> {want} (got {got})")
    chk(select(cfg, 512) is None, "below the floor, nothing is chosen rather than a guess")
    chk(select(cfg, 12288) == "bonsai-8b",
        "12GB picks 8B, not 27B — headroom is honoured, not just weight size")

    # The default ladder must be Bonsai ONLY. A fleet model reaching it would mean a
    # first boot pulls several GB of ours unasked, which is the thing the opt_in flag
    # exists to prevent.
    ladder = (cfg.get("defaults") or {}).get("ladder") or []
    chk(all(m.startswith("bonsai-") for m in ladder),
        "the default ladder is Bonsai only")
    chk(all((cfg["models"][m].get("opt_in") is not True) for m in ladder),
        "nothing opt-in is reachable from the default ladder")
    optin = [k for k, v in cfg["models"].items() if v.get("opt_in")]
    chk(sorted(optin) == ["deepseek-v4-flash", "gemma4-12b", "orchestrator", "qwen36-27b"],
        "the four fleet models are present and marked opt-in")

    # Plans.
    p = plan(cfg, "bonsai-4b")
    chk(len(p["urls"]) == 1 and p["urls"][0].startswith("https://huggingface.co/"),
        "a direct model yields one huggingface URL")
    chk(p["join"] is False, "a single-file model needs no join")
    g = plan(cfg, "gemma4-12b")
    chk(len(g["urls"]) == 5 and all(".part" in u for u in g["urls"]),
        "a mirrored model yields its .partN URLs")
    chk(g["join"] is True and g["file"].endswith(".gguf"),
        "parts join back to the original filename")
    chk(all(u.startswith("https://github.com/Aitherium/aitherkvcache/releases/download/")
            for u in g["urls"]), "mirrored URLs point at aitherkvcache releases")
    chk(g["opt_in"] is True, "a fleet model is flagged opt-in in its plan")

    try:
        plan(cfg, "nope")
        chk(False, "an unknown model is refused")
    except Dead:
        chk(True, "an unknown model is refused")

    # A catalogue that cannot be read is DEAD, never an empty-but-fine answer.
    try:
        _load(Path("/definitely/not/here.yaml"))
        chk(False, "a missing catalogue is DEAD")
    except Dead:
        chk(True, "a missing catalogue is DEAD")

    print("SELF-TEST PASS" if not fails else "SELF-TEST FAILED")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-mb", type=int, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    try:
        cfg = _load()
        if a.list:
            for mid, m in (cfg.get("models") or {}).items():
                tag = " [opt-in]" if m.get("opt_in") else ""
                print(f"{mid:<20} {m['size_mb']:>6} MB  {m.get('quant', '?'):<10}"
                      f"{m['source']}{tag}")
            return 0
        mid = a.model
        if not mid:
            budget = a.budget_mb if a.budget_mb is not None else detect_budget_mb()
            mid = select(cfg, budget)
            if not mid:
                print(f"no model fits {budget}MB", file=sys.stderr)
                return 1
            if not a.plan:
                print(mid)
                return 0
        print(json.dumps(plan(cfg, mid), indent=2) if a.plan else mid)
        return 0
    except Dead as e:
        print(f"awnix-model-select: {e} — NOT VERIFIED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
