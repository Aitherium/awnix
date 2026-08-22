#!/usr/bin/python3.11
"""awnix first-boot setup — pick your components, then optionally link this box.

Runs once, on tty1, the way any Linux asks its questions after install. Three steps and
you can decline all three:

    1. hostname
    2. components -- the aw* stack, core already baked, extras opt-in
    3. link this box to portal.aitherium.com (device flow), or stay standalone

WHY EVERY STEP IS DECLINABLE. awnix's stated identity is "No services. No agent. No
account." (ecosystem.yaml). A setup that requires an account to finish would contradict
the thing being installed. Skipping step 3 leaves a completely usable machine; the link
buys you a fleet, not a boot.

WHY THE CATALOGUE IS PROBED, NOT PRINTED FROM A LIST. Measured 2026-08-21: only 10 of the
26 aw* packages in the monorepo are actually on PyPI. A menu offering `awrun`, `awtunnel`
or `awiam` -- all of which sound exactly as real as the ones that exist -- hands the user
a `pip install` that 404s during their first five minutes with the OS. That is the ONB003
defect class, and a first-boot screen is the worst possible place for it. So the menu is
built from what answers, and anything that does not answer is shown as *unavailable* with
a reason rather than silently dropped -- absent is a status, not an omission.

    awnix-setup                # interactive
    awnix-setup --self-test    # offline; proves the decisions, installs nothing
    awnix-setup --status       # what was chosen last time
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("AWNIX_STATE_DIR", "/etc/awnix"))
STATE_FILE = STATE_DIR / "setup.json"

DEVICE_HOST = os.environ.get("AWNIX_LINK_HOST", "https://mcp.aitherium.com")
DEVICE_CODE_URL = DEVICE_HOST + "/auth/device/code"
DEVICE_TOKEN_URL = DEVICE_HOST + "/auth/device/token"
PYPI = "https://pypi.org/pypi/{}/json"
UA = "awnix-setup"

# name -> (group, one-line what-it-is). BAKED are already in the image; the rest are
# offered at setup. Groups exist so "core" can be one keystroke.
CATALOG: list[tuple[str, str, str]] = [
    ("awgit",     "core",  "op-log-aware git for trees several agents share"),
    ("awgraph",   "core",  "ask the code graph instead of grepping"),
    ("awrelay",   "core",  "cross-session agent messaging humans can read"),
    ("awm",       "core",  "scoped agent memory"),
    ("awshare",   "core",  "share an artifact with a verifiable identity"),
    ("awprism",   "extra", "structured views over a running system"),
    ("awreason",  "extra", "reasoning helpers"),
    ("awrecurse", "extra", "recursive task decomposition"),
    ("awrepl",    "extra", "a REPL wired to the aw* tools"),
    ("awsync",    "extra", "keep two trees in step"),
    # Source-installable: a public repo but no PyPI entry, because PyPI limits how many
    # projects an account may create and we ran out of slots -- not because these are
    # unfinished. Verified 2026-08-21: each has a public repo under github.com/Aitherium.
    ("awrun",     "extra", "run a job somewhere else and get the result back"),
    ("awrecover", "extra", "recover a broken tree or a lost change"),
    ("awseal",    "extra", "seal an artifact so tampering is detectable"),
    ("awfind",    "extra", "find things across trees"),
    ("awkno",     "extra", "a knowledge store you can query"),
    ("awmail",    "extra", "mail, for agents"),
    ("awnest",    "extra", "nest and compose agent workspaces"),
    ("awnboard",  "extra", "a board agents and humans share"),
    ("awbrowse",  "extra", "drive a browser"),
    ("awpredict", "extra", "prediction helpers"),
    ("awresearch", "extra", "research workflows"),
    ("awdk",      "agent", "the agent layer — an agent that can drive this box"),
]

GITHUB_ORG = os.environ.get("AWNIX_GITHUB_ORG", "Aitherium")
GITHUB_REPO = "https://github.com/{org}/{name}"
BAKED = {"awgit", "awgraph", "awrelay", "awm", "awshare"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def _get_json(url: str, payload: dict | None = None, timeout: float = 20.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"User-Agent": UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def pypi_available(name: str, timeout: float = 10.0) -> bool | None:
    """True published, False definitively absent, None could not ask.

    Three states, not two. Offline must never render as "this package does not exist" --
    that would quietly shrink the menu on a machine whose only problem is no network yet.
    """
    req = urllib.request.Request(PYPI.format(name), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


def github_available(name: str, timeout: float = 10.0) -> bool | None:
    """True the public repo exists, False definitively not, None could not ask.

    Same three states as the PyPI probe and for the same reason: on a box with no network
    yet, "could not ask" must never collapse into "does not exist".
    """
    url = GITHUB_REPO.format(org=GITHUB_ORG, name=name)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


def resolve_source(name: str, pypi=pypi_available, github=github_available):
    """Where this package can be installed from: 'pypi', 'git', False, or None.

    PyPI first -- a published wheel is faster, pinned and does not need git or a
    compiler on the box. Source is the FALLBACK, not the preference; it exists because
    PyPI project limits, not readiness, are what keeps 11 of these off the index.
    """
    got = pypi(name)
    if got is True:
        return "pypi"
    gh = github(name)
    if gh is True:
        return "git"
    # Absent from PyPI AND absent from GitHub is a real absence. Anything unknown on
    # either probe stays unknown -- never downgraded to "does not exist".
    if got is False and gh is False:
        return False
    return None


def install_target(name: str, source: str) -> str:
    """The exact thing pip is asked to install."""
    if source == "git":
        return "git+" + GITHUB_REPO.format(org=GITHUB_ORG, name=name) + ".git"
    return name


def resolve_catalog(source_probe=resolve_source) -> list[dict]:
    """Annotate the catalogue with what is installed and where the rest comes from.

    ONE probe, returning a source ('pypi' | 'git' | False | None) -- the same callable
    production uses, so a test injecting a fake exercises the identical branch.
    """
    out = []
    for name, group, blurb in CATALOG:
        entry = {"name": name, "group": group, "what": blurb,
                 "baked": name in BAKED, "available": None, "source": None}
        if entry["baked"]:
            entry["available"], entry["source"] = True, "baked"
        else:
            src = source_probe(name)
            if src in ("pypi", "git"):
                entry["available"], entry["source"] = True, src
            elif src is False:
                entry["available"] = False
            else:
                entry["available"] = None
        out.append(entry)
    return out


def selectable(catalog: list[dict]) -> list[dict]:
    """What the menu may offer: not already baked, and known to exist."""
    return [c for c in catalog if not c["baked"] and c["available"] is True]


def unavailable(catalog: list[dict]) -> list[dict]:
    return [c for c in catalog if c["available"] is False]


def unknown(catalog: list[dict]) -> list[dict]:
    return [c for c in catalog if c["available"] is None]


def parse_selection(raw: str, offered: list[dict]) -> list[str]:
    """Accept `1 3 5`, `1,3,5`, `all`, `none`/empty. Unknown tokens are IGNORED, never
    guessed at -- installing something the user did not pick is worse than skipping it."""
    s = (raw or "").strip().lower()
    if s in ("", "none", "n", "skip"):
        return []
    if s in ("all", "a", "*"):
        return [c["name"] for c in offered]
    picked: list[str] = []
    for tok in s.replace(",", " ").split():
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(offered):
                picked.append(offered[i]["name"])
        else:
            for c in offered:
                if c["name"] == tok:
                    picked.append(c["name"])
    seen: set[str] = set()
    return [p for p in picked if not (p in seen or seen.add(p))]


def pip_install(targets: list[str]) -> tuple[bool, str]:
    """`targets` are pip arguments, which for a source install is a git+https URL --
    NOT bare names. Passing the name for a package that is not on PyPI is the whole
    defect this fallback exists to avoid."""
    if not targets:
        return True, "nothing to install"
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", *targets]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode == 0, (p.stdout + p.stderr)[-2000:]


# ── linking ─────────────────────────────────────────────────────────────────────────────
def start_device_flow() -> dict:
    return _get_json(DEVICE_CODE_URL, {"client_id": "awnix-setup", "scope": "mcp"})


def poll_device_flow(device_code: str, interval: int, expires_in: int,
                     sleep=time.sleep, now=time.monotonic) -> tuple[bool, str]:
    """RFC 8628 polling. Returns (ok, token-or-reason).

    `authorization_pending` and `slow_down` are NORMAL -- they are the protocol saying
    "the human has not clicked yet". Treating any non-200 as failure is the classic way
    to make this flow look broken while it is working.
    """
    deadline = now() + max(30, expires_in)
    wait = max(1, interval)
    while now() < deadline:
        sleep(wait)
        try:
            body = _get_json(DEVICE_TOKEN_URL,
                             {"device_code": device_code, "client_id": "awnix-setup"})
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except Exception:
                body = {}
        except Exception as exc:
            return False, f"network error while polling: {exc}"
        err = body.get("error")
        if body.get("access_token"):
            return True, body["access_token"]
        if err == "slow_down":
            wait += 5
            continue
        if err in ("authorization_pending", None):
            continue
        return False, f"declined or expired ({err})"
    return False, "timed out waiting for approval"


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)
    try:
        STATE_FILE.chmod(0o600)   # it can hold a bearer
    except OSError as exc:
        # Never silent: if this fails the file may hold a bearer at default permissions,
        # and "the feature is off" is the wrong reading of a credential left readable.
        print(f"  WARNING: could not restrict {STATE_FILE} to 0600 ({exc}) -- "
              f"check its permissions by hand", flush=True)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── interactive ─────────────────────────────────────────────────────────────────────────
def run_interactive() -> int:
    say()
    say("=" * 68)
    say("  awnix — first boot")
    say("  An immutable Linux with the aw* tools already in it.")
    say("  No services, no agent, no account. All three questions are optional.")
    say("=" * 68)
    say()

    # 1 ── hostname
    current = socket.gethostname()
    ans = input(f"  Hostname [{current}]: ").strip()
    if ans and ans != current:
        r = subprocess.run(["hostnamectl", "set-hostname", ans],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        say(f"  hostname -> {ans}" if r.returncode == 0
            else f"  could not set hostname: {r.stderr.strip()}")
    say()

    # 2 ── components
    say("  Components")
    baked = [c for c in CATALOG if c[0] in BAKED]
    say(f"  Already installed (core): {', '.join(n for n, _, _ in baked)}")
    say("  Checking what else is available…")
    catalog = resolve_catalog()
    offered = selectable(catalog)
    absent = unavailable(catalog)
    cannot = unknown(catalog)
    say()
    if offered:
        for i, c in enumerate(offered, 1):
            tag = "" if c["source"] == "pypi" else "  [source]"
            say(f"    {i}. {c['name']:<11}{c['what']}{tag}")
        say()
        say("    Enter numbers (e.g. 1 3), 'all', or press Enter for none.")
        picked = parse_selection(input("  Install: "), offered)
    else:
        picked = []
        say("    Nothing extra is available to install right now.")
    if absent:
        say(f"  Not yet published, so not offered: {', '.join(c['name'] for c in absent)}")
    if cannot:
        say(f"  Could not check (no network?): {', '.join(c['name'] for c in cannot)}")
    say()

    installed: list[str] = []
    if picked:
        by_name = {c["name"]: c for c in catalog}
        targets = [install_target(n, by_name[n].get("source") or "pypi") for n in picked]
        say(f"  Installing: {', '.join(picked)} …")
        n_git = sum(1 for t in targets if t.startswith("git+"))
        if n_git:
            say(f"  ({n_git} of these build from source — PyPI project limits, not "
                f"unfinished packages)")
        ok, log = pip_install(targets)
        if ok:
            installed = picked
            say("  done.")
        else:
            say("  install FAILED — the box is still fine, nothing was changed:")
            say("  " + log.strip().splitlines()[-1] if log.strip() else "  (no output)")
    say()

    # 3 ── link
    say("  Link this machine?")
    say("  Connects it to your fleet at portal.aitherium.com so awdk/awsh can reach it.")
    say("  Skipping leaves a fully working standalone box — you can link later with")
    say("  `awnix-setup` at any time.")
    link = input("  Link now? [y/N]: ").strip().lower().startswith("y")
    linked = False
    token = None
    if link:
        try:
            d = start_device_flow()
            say()
            say(f"    Go to:  {d.get('verification_uri', 'https://portal.aitherium.com/link')}")
            say(f"    Code :  {d.get('user_code', '?')}")
            say()
            say("    Waiting for approval (Ctrl-C to skip)…")
            ok, res = poll_device_flow(d.get("device_code", ""),
                                       int(d.get("interval", 5)),
                                       int(d.get("expires_in", 900)))
            if ok:
                token, linked = res, True
                say("    linked.")
            else:
                say(f"    not linked: {res}")
        except KeyboardInterrupt:
            say("\n    skipped.")
        except Exception as exc:
            say(f"    could not start the link: {exc}")
    say()

    state = {
        "version": 1,
        "hostname": socket.gethostname(),
        "core": sorted(BAKED),
        "installed": installed,
        "linked": linked,
        "link_host": DEVICE_HOST if linked else None,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if token:
        state["bearer"] = token
    write_state(state)

    say("=" * 68)
    say("  Setup complete. Re-run any time with:  awnix-setup")
    say(f"  Choices recorded in {STATE_FILE}")
    say("=" * 68)
    say()
    return 0


# ── self-test ───────────────────────────────────────────────────────────────────────────
def self_test() -> int:
    fails = 0

    def chk(cond: bool, label: str) -> None:
        nonlocal fails
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        if not cond:
            fails += 1

    # A menu must never offer something that does not exist, and must not hide something
    # merely because the network was down.
    fake = {"awprism": "pypi", "awreason": False, "awrepl": None,
            "awrun": "git", "awrecurse": "pypi", "awsync": "pypi", "awdk": "pypi"}
    cat = resolve_catalog(source_probe=lambda n: fake.get(n, "pypi"))
    names = {c["name"] for c in selectable(cat)}
    chk("awprism" in names, "offers a published package")
    chk("awrun" in names, "offers a source-only package (PyPI limits, not unreadiness)")
    chk("awreason" not in names, "never offers one absent from BOTH pypi and github")
    chk("awrepl" not in names, "never offers one it could not check")
    by = {c["name"]: c for c in cat}
    chk(install_target("awprism", by["awprism"]["source"]) == "awprism",
        "a pypi package installs by bare name")
    chk(install_target("awrun", by["awrun"]["source"])
        == "git+https://github.com/Aitherium/awrun.git",
        "a source package installs from its git URL, never its bare name")
    chk(all(c["name"] not in names for c in cat if c["baked"]),
        "does not re-offer what is already baked in")
    chk({c["name"] for c in unavailable(cat)} == {"awreason"}, "names the absent one")
    chk({c["name"] for c in unknown(cat)} == {"awrepl"}, "reports unchecked separately")

    offered = selectable(cat)
    chk(parse_selection("", offered) == [], "empty means none")
    chk(parse_selection("none", offered) == [], "'none' means none")
    chk(parse_selection("all", offered) == [c["name"] for c in offered], "'all' takes all")
    chk(parse_selection("1", offered) == [offered[0]["name"]], "picks by number")
    chk(parse_selection("1,1 1", offered) == [offered[0]["name"]], "dedupes")
    chk(parse_selection("999 nonsense", offered) == [],
        "ignores unknown tokens rather than guessing")
    chk(parse_selection(offered[0]["name"], offered) == [offered[0]["name"]],
        "picks by name")

    # Polling: pending is not failure. This is the arm that decides whether the link
    # flow looks broken while it is working.
    seq = [
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": "tok-123"},
    ]

    def fake_get(url, payload=None, timeout=20.0):
        return seq.pop(0)

    global _get_json
    real = _get_json
    try:
        _get_json = fake_get
        ok, tok = poll_device_flow("d", 1, 60, sleep=lambda s: None,
                                   now=_counter())
        chk(ok and tok == "tok-123", "polls through pending + slow_down to a token")

        seq[:] = [{"error": "access_denied"}]
        ok2, why = poll_device_flow("d", 1, 60, sleep=lambda s: None, now=_counter())
        chk((not ok2) and "access_denied" in why, "a decline is reported, not retried forever")

        seq[:] = []
        ok3, why3 = poll_device_flow("d", 1, 1, sleep=lambda s: None,
                                     now=_expiring())
        chk((not ok3) and "timed out" in why3, "gives up at the deadline")
    finally:
        _get_json = real

    # resolve_source: order and the three states, driven directly.
    chk(resolve_source("x", pypi=lambda n: True, github=lambda n: False) == "pypi",
        "prefers pypi when both could serve")
    chk(resolve_source("x", pypi=lambda n: False, github=lambda n: True) == "git",
        "falls back to source when pypi lacks it")
    chk(resolve_source("x", pypi=lambda n: False, github=lambda n: False) is False,
        "absent from both is a real absence")
    chk(resolve_source("x", pypi=lambda n: None, github=lambda n: False) is None,
        "an unknown pypi answer stays unknown, never 'absent'")
    chk(resolve_source("x", pypi=lambda n: False, github=lambda n: None) is None,
        "an unknown github answer stays unknown too")

    print("SELF-TEST PASS" if not fails else "SELF-TEST FAILED")
    return 0 if not fails else 1


def _counter():
    t = {"v": 0.0}

    def now() -> float:
        t["v"] += 1
        return t["v"]
    return now


def _expiring():
    t = {"v": 0.0}

    def now() -> float:
        t["v"] += 100
        return t["v"]
    return now


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.status:
        st = read_state()
        if not st:
            print("awnix: setup has not run yet")
            return 1
        st.pop("bearer", None)   # never echo a credential
        print(json.dumps(st, indent=2))
        return 0
    try:
        return run_interactive()
    except KeyboardInterrupt:
        say("\n  setup cancelled — nothing was changed. Run `awnix-setup` to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
